#!/usr/bin/env python3
"""Capa de síntesis destilada del mapa de memoria colectiva (F8).

Clusteriza los docs de prosa por sus vectores (doc_vectors.db, Louvain) y destila
cada cluster con un LLM local (ollama) a un nodo markdown con título + resumen +
Fuentes trazables. Los nodos van a .mapa/overlays/sintesis/ y el bibliotecario
los publica en el snapshot (mapa/sintesis/). Invariantes:
  - NINGÚN nodo sin source_paths >= MIN_SOURCES (trazabilidad anti-alucinación).
  - Escritura bajo doble lock (synth.lock para el run; lock del librarian para el swap).
  - --limit escribe a un PREVIEW fuera del overlay publicado (gate humano sin riesgo).
"""
import argparse
import fcntl
import glob
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import (ROOT, DMAPA, DB, CODE_HOME)  # noqa: E402
UI_LINK = os.path.join(DMAPA, "ui")
OVERLAYS = os.path.join(DMAPA, "overlays")
SINTESIS = os.path.join(OVERLAYS, "sintesis")
PREVIEW = os.path.join(OVERLAYS, "sintesis.preview")
SYNTH_LOCK = os.path.join(DMAPA, "synth.lock")
LIB_LOCK = os.path.join(DMAPA, "lock")

OLLAMA_URL = os.environ.get("MAPA_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
DEFAULT_MODEL = "qwen3:8b"
PROMPT_VERSION = "1"
MIN_SOURCES = 3
K_QUERY = 16        # criterio EXACTO de ui_builder: query k=16, saltear self, conservar 8
KEEP_NEIGHBORS = 8
LOUVAIN_SEED = 42
EVIDENCE_SNIPPET = 300
EVIDENCE_CAP = 8000
CLUSTER_GATE = 150  # dry-run gate: más que esto => subir min_size
# Docs volátiles generados por máquina: cambian en cada publish → churn de fingerprints sin valor.
EXCLUDE_DOC_IDS = {"mapa/status.md", "mapa/log.md", ".mapa/corpus_audit.md"}

sys.path.insert(0, CODE_HOME)
import tier1  # noqa: E402  (gpu_lock, sin cargar modelo)


def sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def slugify(value):
    base = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base[:60] or "sintesis"


def open_vec(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def strip_frontmatter(body):
    return re.sub(r"^---\n.*?\n---\n?", "", body or "", count=1, flags=re.S)


def parse_frontmatter_meta(text):
    """fingerprint/model/prompt_version/source_paths del frontmatter de un nodo síntesis."""
    m = re.match(r"^---\n(.*?)\n---", text or "", re.S)
    if not m:
        return {}
    fm = m.group(1)
    out = {}
    for key in ("cluster_fingerprint", "generated_by", "prompt_version", "id"):
        km = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
        if km:
            out[key] = km.group(1).strip().strip("\"'")
    sm = re.search(r"^source_paths:\s*(\[.*?\])\s*$", fm, re.M | re.S)
    if sm:
        try:
            out["source_paths"] = json.loads(sm.group(1))
        except Exception:
            out["source_paths"] = [x.strip().strip("\"'") for x in sm.group(1)[1:-1].split(",") if x.strip()]
    return out


# ---------- Universo + clustering ----------

def coherence_gate():
    ui_manifest = read_json(os.path.join(UI_LINK, "manifest.json"))
    if not ui_manifest:
        sys.exit("[synthesize] falta ui/manifest.json — corré: ui_builder.py build --mode full")
    dv = os.path.join(UI_LINK, "doc_vectors.db")
    if not os.path.isfile(dv):
        sys.exit("[synthesize] falta ui/doc_vectors.db (solo lo genera el modo full) — corré: ui_builder.py build --mode full")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    idx_gen = dict(con.execute("SELECT k,v FROM meta").fetchall()).get("index_generation")
    con.close()
    ui_gen = str(ui_manifest.get("index_generation"))
    if str(idx_gen) != ui_gen:
        sys.exit(f"[synthesize] UI stale (ui index_generation={ui_gen} vs index.db={idx_gen}) — corré ui_builder full primero")
    return dv


def load_universe(dv_path):
    """Docs de prosa con vector (kind != synthesis) + metadata desde index.db."""
    coni = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    meta = {r[0]: {"kind": r[1], "title": r[2], "body": r[3], "content_hash": r[4], "project": r[5]}
            for r in coni.execute("SELECT doc_id, kind, title, body, content_hash, project FROM docs WHERE body IS NOT NULL")}
    coni.close()
    conv = open_vec(dv_path)
    members = []
    for rowid, doc_id, chash in conv.execute("SELECT rowid, doc_id, content_hash FROM doc_vec_map"):
        d = meta.get(doc_id)
        if not d or d["kind"] == "synthesis" or doc_id in EXCLUDE_DOC_IDS:  # anti síntesis-de-síntesis + anti-churn
            continue
        members.append({"rowid": rowid, "doc_id": doc_id, "content_hash": d["content_hash"], **{k: d[k] for k in ("kind", "title", "body", "project")}})
    return conv, members


def knn_communities(conv, members, min_size):
    import networkx as nx
    by_rowid = {m["rowid"]: m for m in members}
    g = nx.Graph()
    for m in members:
        g.add_node(m["doc_id"])
    for m in members:
        row = conv.execute("SELECT embedding FROM doc_vec WHERE rowid=?", (m["rowid"],)).fetchone()
        if not row:
            continue
        hits = conv.execute("SELECT rowid, distance FROM doc_vec WHERE embedding MATCH ? AND k=?",
                            (row[0], K_QUERY)).fetchall()
        kept = 0
        for other_rowid, dist in hits:
            if other_rowid == m["rowid"]:
                continue
            other = by_rowid.get(other_rowid)
            if not other:
                continue
            w = 1.0 / (1.0 + float(dist))
            if g.has_edge(m["doc_id"], other["doc_id"]):
                if g[m["doc_id"]][other["doc_id"]]["weight"] < w:
                    g[m["doc_id"]][other["doc_id"]]["weight"] = w
            else:
                g.add_edge(m["doc_id"], other["doc_id"], weight=w)
            kept += 1
            if kept >= KEEP_NEIGHBORS:
                break
    comms = nx.community.louvain_communities(g, weight="weight", seed=LOUVAIN_SEED)
    sized = [sorted(c) for c in comms if len(c) >= min_size]
    sized.sort(key=lambda c: (-len(c), c[0]))
    return sized


# ---------- Destilación ----------

def evidence_pack(cluster, meta_by_id):
    lines = []
    total = 0
    for doc_id in cluster:
        d = meta_by_id[doc_id]
        snippet = re.sub(r"\s+", " ", strip_frontmatter(d["body"]))[:EVIDENCE_SNIPPET]
        entry = f"- [{d['project']}] {d['title']} ({doc_id}): {snippet}"
        if total + len(entry) > EVIDENCE_CAP:
            lines.append(f"- … (+{len(cluster) - len(lines)} documentos más del mismo grupo)")
            break
        lines.append(entry)
        total += len(entry)
    return "\n".join(lines)


def call_llm(model, evidence, n_docs):
    system = ("Sos un bibliotecario que destila grupos de documentos relacionados en síntesis breves y fieles. "
              "Respondés SOLO con JSON válido, en castellano rioplatense neutro, sin inventar nada que no esté en la evidencia.")
    user = (f"Estos {n_docs} documentos de un mismo grupo temático:\n\n{evidence}\n\n"
            'Devolvé JSON: {"titulo": "<título descriptivo del tema común, 4-10 palabras>", '
            '"resumen": "<2 a 4 frases: qué une a estos documentos, qué contienen, para qué sirven>", '
            '"temas": ["<tema1>", "<tema2>", "<tema3>"]}')
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                content = json.loads(r.read())["message"]["content"]
            out = json.loads(content)
            if out.get("titulo") and out.get("resumen"):
                return {"titulo": str(out["titulo"]).strip(), "resumen": str(out["resumen"]).strip(),
                        "temas": [str(t).strip() for t in (out.get("temas") or [])][:5]}
        except Exception as e:
            if attempt == 2:
                print(f"  [llm] fallo definitivo: {e}", file=sys.stderr)
    return None


def render_node(fp, model, cluster, meta_by_id, gen):
    if len(cluster) < MIN_SOURCES:
        return None, None  # guardrail: no existe síntesis sin evidencia suficiente
    slug = f"{slugify(gen['titulo'])}-{fp[:6]}"
    temas = " · ".join(gen["temas"]) if gen["temas"] else ""
    fuentes = "\n".join(
        f'- <a data-doc="{html.escape(doc_id, quote=True)}">{html.escape(meta_by_id[doc_id]["title"])}</a>'
        for doc_id in cluster
    )
    body = f"""---
id: sintesis.{slug}
type: synthesis
cluster_fingerprint: {fp}
generated_by: {model}
prompt_version: {PROMPT_VERSION}
generated_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
source_paths: {json.dumps(cluster, ensure_ascii=False)}
---
# {gen['titulo']}

{gen['resumen']}

{f'**Temas:** {temas}' if temas else ''}

## Fuentes

{fuentes}
"""
    return slug, body


def existing_nodes(dirpath):
    out = {}
    for f in glob.glob(os.path.join(dirpath, "*.md")):
        meta = parse_frontmatter_meta(open(f, encoding="utf-8", errors="replace").read())
        if meta.get("cluster_fingerprint"):
            out[meta["cluster_fingerprint"]] = {"path": f, **meta}
    return out


# ---------- Run ----------

def run(args):
    # Lock de run completo (no-bloqueante): dos síntesis concurrentes = el 2do swap pisaría al 1ro.
    lockf = open(SYNTH_LOCK, "a")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("[synthesize] ya hay una síntesis corriendo (synth.lock) — abortando")

    # Limpieza de tmps huérfanos de corridas muertas.
    for old in glob.glob(os.path.join(OVERLAYS, ".sintesis.*.tmp")):
        shutil.rmtree(old, ignore_errors=True)

    dv = coherence_gate()
    conv, members = load_universe(dv)
    meta_by_id = {m["doc_id"]: m for m in members}
    print(f"[synthesize] universo: {len(members)} docs de prosa con vector")
    clusters = knn_communities(conv, members, args.min_size)
    conv.close()
    print(f"[synthesize] comunidades ≥{args.min_size}: {len(clusters)} (tamaños: {[len(c) for c in clusters[:12]]}{'…' if len(clusters) > 12 else ''})")
    if len(clusters) > CLUSTER_GATE:
        sys.exit(f"[synthesize] GATE: {len(clusters)} clusters > {CLUSTER_GATE} — subí --min-size antes de gastar LLM")
    if args.dry_run:
        for i, c in enumerate(clusters[:20]):
            print(f"  #{i:02d} n={len(c):3d}  ej: {', '.join(c[:4])}")
        return

    preview = bool(args.limit)
    if preview:
        target = PREVIEW
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target)
        todo = clusters[: args.limit]
        print(f"[synthesize] PREVIEW de {len(todo)} clusters → {target} (NO se publica)")
    else:
        run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f".{time.time_ns() % 1_000_000_000}"
        target = os.path.join(OVERLAYS, f".sintesis.{run_id}.tmp")
        os.makedirs(target)
        todo = clusters

    prev = existing_nodes(SINTESIS) if os.path.isdir(SINTESIS) else {}
    generated = skipped = failed = 0
    with tier1.gpu_lock():
        for i, cluster in enumerate(todo):
            fp = sha("".join(sorted(meta_by_id[d]["content_hash"] for d in cluster)))[:16]
            old = prev.get(fp)
            if (not args.force and old and old.get("generated_by") == args.model
                    and old.get("prompt_version") == PROMPT_VERSION):
                shutil.copy2(old["path"], os.path.join(target, os.path.basename(old["path"])))
                skipped += 1
                continue
            gen = call_llm(args.model, evidence_pack(cluster, meta_by_id), len(cluster))
            if not gen:
                if old:  # fallo LLM: conservar la síntesis anterior antes que perderla
                    shutil.copy2(old["path"], os.path.join(target, os.path.basename(old["path"])))
                    skipped += 1
                failed += 1
                continue
            slug, body = render_node(fp, args.model, cluster, meta_by_id, gen)
            if not slug:
                failed += 1
                continue
            with open(os.path.join(target, f"{slug}.md"), "w", encoding="utf-8") as f:
                f.write(body)
            generated += 1
            print(f"  [{i+1}/{len(todo)}] n={len(cluster):3d} → {gen['titulo'][:70]}")

    if preview:
        print(f"[synthesize] PREVIEW listo: {generated} generados, {failed} fallidos → inspeccionar {target}")
        return

    # Swap atómico BAJO EL LOCK DEL LIBRARIAN: ningún publish puede copiar un set a medias.
    liblock = open(LIB_LOCK, "a")
    fcntl.flock(liblock, fcntl.LOCK_EX)
    try:
        old_dir = SINTESIS + f".old.{time.time_ns()}"
        if os.path.isdir(SINTESIS):
            os.rename(SINTESIS, old_dir)
        os.rename(target, SINTESIS)
        shutil.rmtree(old_dir, ignore_errors=True)
    finally:
        fcntl.flock(liblock, fcntl.LOCK_UN)
        liblock.close()
    print(f"[synthesize] OK — generados={generated} skipped={skipped} fallidos={failed} → {SINTESIS}")
    print("[synthesize] siguiente paso: MAPA_UI_MODE=full bash scripts/librarian.sh")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--min-size", type=int, default=5)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd != "run":
        ap.print_help()
        return 1
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
