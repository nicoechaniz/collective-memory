#!/usr/bin/env python3
"""Operadores de discovery (F11 deterministas + F12 LLM local) — motor de discover.py run.

Pipeline por candidato: GENERAR → NOVELTY (links editoriales recomputados) →
SCREEN LLM (obligatorio, mapeo de roles, grounding sintáctico) → FALSAR
(ablation contrafactual, negative search, diversidad, stale) → queda en
discovery.db para review humana. Nada de esto publica: publicar es promote+librarian.

Reglas duras:
  - Universo de evidencia primaria: kind NOT IN (synthesis, discovery) — anti echo-chamber.
  - "Sin enlace" = sin enlace EDITORIAL (wikilink/source_of del pase completo), nunca
    ausencia en los grafos truncados de la UI.
  - Hub penalty: docs con degree semántico en el top percentil no son endpoints de puentes.
  - LLM local-only (ollama); GPU bajo tier1.gpu_lock(). Sin ollama: candidatos unscreened.
"""
import difflib
import fcntl
import glob
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

import discover
from discover import (DMAPA, DB, UI_LINK, PROMPT_VERSION, MIN_SOURCES,
                      PRIMARY_KINDS_EXCLUDED, sha, now_iso, read_json, strip_frontmatter,
                      open_index_ro, build_explicit_links, editorially_linked, novelty_gate,
                      novelty_gate_cross, save_candidate, jget)

from mapa_config import CODE_HOME  # noqa: E402
sys.path.insert(0, CODE_HOME)
import tier1  # noqa: E402

OPERATOR_VERSION = "2"  # v2: filtro de pares triviales (copias/plantillas) + emisión por peso desc
K_QUERY = 16
SNIPPET = 300
HUB_PERCENTILE = 0.95         # degree semántico sobre este percentil => hub, no puentea
BRIDGE_MIN_W = 0.42           # peso 1/(1+dist) mínimo para considerar cercanía fuerte
FRONTIER_MIN_SEMANTIC = 6     # aristas semánticas cross mínimas para frontera
FRONTIER_MAX_EDITORIAL = 2    # ... con a lo sumo estas editoriales
OUTLIER_MIN_FOREIGN = 6       # de KEEP_NEIGHBORS=8 vecinos, cuántos de un mismo proyecto ajeno
KEEP_NEIGHBORS = 8
VOLATILE_DOC_IDS = {"mapa/status.md", "mapa/log.md", ".mapa/corpus_audit.md"}

DETERMINISTIC = ("latent_bridge", "cluster_frontier", "outlier")
LLM_OPERATORS = ("tension", "analogy", "freshness_negative")


# ---------- Infra compartida ----------

def open_vec(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def coherence_gate():
    ui_manifest = read_json(f"{UI_LINK}/manifest.json")
    if not ui_manifest:
        sys.exit("[discover] falta ui/manifest.json — corré: MAPA_UI_MODE=full bash librarian.sh")
    dv = f"{UI_LINK}/doc_vectors.db"
    import os
    if not os.path.isfile(dv):
        sys.exit("[discover] falta ui/doc_vectors.db (solo lo genera el modo full) — corré: MAPA_UI_MODE=full bash librarian.sh")
    coni = open_index_ro()
    idx_gen = dict(coni.execute("SELECT k,v FROM meta").fetchall()).get("index_generation")
    coni.close()
    if str(idx_gen) != str(ui_manifest.get("index_generation")):
        sys.exit(f"[discover] UI stale (ui={ui_manifest.get('index_generation')} vs index.db={idx_gen}) — corré ui_builder full primero")
    return dv, str(idx_gen), str(ui_manifest.get("generation"))


def load_universe(dv_path, projects=None):
    """Docs de prosa con vector, SIN synthesis/discovery (anti echo-chamber)."""
    coni = open_index_ro()
    meta = {r[0]: {"kind": r[1], "title": r[2], "body": r[3], "content_hash": r[4],
                   "project": r[5], "mtime": r[6]}
            for r in coni.execute("SELECT doc_id, kind, title, body, content_hash, project, mtime "
                                  "FROM docs WHERE body IS NOT NULL")}
    coni.close()
    conv = open_vec(dv_path)
    members = []
    for rowid, doc_id, chash in conv.execute("SELECT rowid, doc_id, content_hash FROM doc_vec_map"):
        d = meta.get(doc_id)
        if not d or d["kind"] in PRIMARY_KINDS_EXCLUDED or doc_id in VOLATILE_DOC_IDS:
            continue
        if projects and d["project"] not in projects:
            continue
        members.append({"rowid": rowid, "doc_id": doc_id, **d})
    return conv, members, meta


def knn_all(conv, members):
    """Vecinos semánticos por doc (criterio ui_builder: k=16, keep 8) + degree in/out."""
    by_rowid = {m["rowid"]: m for m in members}
    neigh = {}
    indeg = {m["doc_id"]: 0 for m in members}
    for m in members:
        row = conv.execute("SELECT embedding FROM doc_vec WHERE rowid=?", (m["rowid"],)).fetchone()
        if not row:
            continue
        hits = conv.execute("SELECT rowid, distance FROM doc_vec WHERE embedding MATCH ? AND k=?",
                            (row[0], K_QUERY)).fetchall()
        kept = []
        for other_rowid, dist in hits:
            if other_rowid == m["rowid"]:
                continue
            other = by_rowid.get(other_rowid)
            if not other:
                continue
            w = 1.0 / (1.0 + float(dist))
            kept.append((other["doc_id"], w))
            indeg[other["doc_id"]] = indeg.get(other["doc_id"], 0) + 1
            if len(kept) >= KEEP_NEIGHBORS:
                break
        neigh[m["doc_id"]] = kept
    return neigh, indeg


MIN_PROJECT_DOCS = 5   # proyectos con menos docs de prosa son pseudo-proyectos (archivos sueltos del root, .mapa)
BRIDGES_PER_PAIR = 2   # cap de puentes por par de proyectos (diversidad > volumen)


def pseudo_projects(members):
    """Pseudo-proyectos: no son proyectos reales sino archivos sueltos del root o infra
    (.mapa, AGENTS.md, CLAUDE.md...) — no pueden ser endpoints de puentes/fronteras/outliers."""
    from collections import Counter
    counts = Counter(m["project"] for m in members)
    return {p for p, c in counts.items() if c < MIN_PROJECT_DOCS} | {".mapa", "root", "inbox"}


def same_project(pa, pb):
    # El índice tiene quirks de case (Proyecto vs proyecto): tratarlos como el mismo.
    return (pa or "").lower() == (pb or "").lower()


# Filtro de pares triviales (calibrado 2026-07-12 sobre el corpus real): los hallazgos
# falsos del juzgado a ciegas eran copias (repos vendoreados, traducciones, exports) y
# hermanos de plantilla (config/eval de corridas, MANIFEST generados). En el corpus:
# traducciones ~0.93, mismo paper en 2 formatos 0.93-0.99, plantillas 0.90+; el p90 de
# los pares legítimos es 0.75 — hay margen limpio para cortar sin perder conexiones.
NEARDUP_W = 0.90    # peso 1/(1+dist) sobre el cual un par es el mismo contenido
TYPEDUP_W = 0.80    # mismo nombre de archivo (normalizado) + w alto = mismo artefacto típico
TENSION_MAX_W = 0.85  # techo para tensiones: la franja 0.85-0.90 es casi toda espejos/versiones

# F21: prior de inesperadez (cross-community). Un par que cruza barrios semánticos distintos
# es más prometedor que uno intra-barrio al mismo peso; dos barrios ya muy conectados entre sí
# sorprenden menos. Solo REORDENA qué pares se evalúan primero (no filtra) → si algo sale mal,
# el daño posible es orden subóptimo, idéntico al status quo.
UNEXPECTED_SAT = 20    # aristas colapsadas cross-community sobre las cuales la sorpresa satura a 0
UNEXPECTED_ALPHA = 0.5  # cuánto pesa la inesperadez en la prioridad de orden


def _digit_norm(s):
    return re.sub(r"\d+", "#", (s or "").lower())


_EXT_RE = re.compile(r"\.(md|txt|pdf|html?|rst|json|ya?ml|tex|csv)$")
_LANG_RE = re.compile(r"[_\-](en|es|pt|fr|de|it)$")


def _base_norm(name):
    """Basename canónico: digitos→#, extensiones apiladas fuera (x.md.pdf→x), sufijo
    de idioma fuera (informe_EN→informe) — para comparar variantes del mismo archivo."""
    s = _digit_norm(name)
    while True:
        s2 = _EXT_RE.sub("", s)
        if s2 == s:
            break
        s = s2
    return _LANG_RE.sub("", s)


_PROJSET_CACHE = {}


def _known_projects(meta_by_id):
    k = id(meta_by_id)
    v = _PROJSET_CACHE.get(k)
    if v is None:
        v = {(d.get("project") or "").lower() for d in meta_by_id.values()} - {""}
        _PROJSET_CACHE[k] = v
    return v


def _vendored(doc_id, own_project, projects):
    """True si el doc vive dentro de una copia vendorizada de OTRO proyecto.

    Ejemplo: `proyecto-a/tools/proyecto-b/...` contiene el segmento
    'proyecto-b', asi que sus docs no son de 'proyecto-a'."""
    own = (own_project or "").lower()
    return any(s.lower() in projects and s.lower() != own for s in doc_id.split("/")[1:-1])


def trivial_pair(meta_by_id, a, b, w=None):
    """Razón de trivialidad de un par de docs, o None si es un par sustantivo.
    Un par trivial no es evidencia de puente/frontera/tensión/outlier: es el mismo
    contenido en otro lugar, u otro engendro del mismo molde."""
    da, db = meta_by_id.get(a) or {}, meta_by_id.get(b) or {}
    if da.get("content_hash") and da.get("content_hash") == db.get("content_hash"):
        return "copia_exacta"
    # Vendoreo: cualquiera de los dos docs vive dentro de una copia de otro proyecto
    # conocido — sus cercanías hablan del upstream, no de nuestros proyectos.
    projects = _known_projects(meta_by_id)
    if _vendored(a, da.get("project"), projects) or _vendored(b, db.get("project"), projects):
        return "subtree_vendoreado"
    if w is not None and w >= NEARDUP_W:
        return "near_dup_semantico"
    sa, sb = a.split("/"), b.split("/")
    if [_digit_norm(x) for x in sa[-2:]] == [_digit_norm(x) for x in sb[-2:]]:
        return "misma_ruta_relativa"  # plantilla del mismo generador (eval_epochN, MANIFEST)
    if w is not None and w >= TYPEDUP_W:
        na, nb = _base_norm(sa[-1]), _base_norm(sb[-1])
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        # Igualdad/contención (bitacora ⊂ 16_bitacora, final_results ⊂ final_results_eN)
        # o casi-igualdad (minicpmo2.6 vs minicpmv2.6): variantes del mismo archivo.
        if na == nb or (len(short) >= 8 and (long_.startswith(short) or long_.endswith(short))):
            return "mismo_archivo_tipico"
        if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85:
            return "casi_mismo_nombre"
    return None


KNN_CACHE_DIR = f"{DMAPA}/discovery/knn_cache"
KNN_CACHE_KEEP = 4


def knn_cached(conv, members, ui_gen, projects):
    """Cache del grafo KNN por (ui_generation, scope de proyectos) — el costo dominante
    de una campaña (miles de queries vec0) se paga una vez por generación/scope.

    Anti-colisión: sha256 COMPLETO del scope en el nombre + verificación embebida
    {ui_generation, projects_sorted} al cargar (mismatch → recompute, jamás error mudo).
    Concurrencia (CLI del dueño + worker del playground comparten dir): flock por clave
    alrededor de compute+write, escritura tmp + os.replace; lock ocupado → recompute
    en memoria sin cachear."""
    scope = sorted(set(projects or []))
    key = sha(json.dumps(scope, ensure_ascii=False))
    path = f"{KNN_CACHE_DIR}/{ui_gen}.{key}.json.gz"

    def try_load():
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("ui_generation") == str(ui_gen) and data.get("projects_sorted") == scope:
                return ({k: [(d, float(w)) for d, w in v] for k, v in data["neigh"].items()},
                        {k: int(v) for k, v in data["indeg"].items()})
        except Exception:
            pass
        return None

    cached = try_load()
    if cached:
        print(f"[discover] KNN desde cache ({path.rsplit('/', 1)[-1]})")
        return cached
    os.makedirs(KNN_CACHE_DIR, exist_ok=True)
    lf = open(path + ".lock", "a")
    got = False
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = True
    except OSError:
        pass
    if got:
        cached = try_load()  # otro proceso pudo escribirlo mientras esperábamos el flock
        if cached:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
            return cached
    neigh, indeg = knn_all(conv, members)
    if got:
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"ui_generation": str(ui_gen), "projects_sorted": scope,
                       "neigh": {k: [[d, w] for d, w in v] for k, v in neigh.items()},
                       "indeg": indeg}, f, ensure_ascii=False)
        os.replace(tmp, path)
        olds = sorted(glob.glob(f"{KNN_CACHE_DIR}/*.json.gz"), key=os.path.getmtime, reverse=True)
        for old in olds[KNN_CACHE_KEEP:]:
            try:
                os.unlink(old)
            except OSError:
                pass
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
    return neigh, indeg


def hub_set(indeg):
    """Docs-hub: degree semántico entrante sobre el percentil HUB_PERCENTILE."""
    vals = sorted(indeg.values())
    if not vals:
        return set()
    cut = vals[min(len(vals) - 1, int(len(vals) * HUB_PERCENTILE))]
    return {d for d, v in indeg.items() if v > max(cut, KEEP_NEIGHBORS)}


def snippet_of(meta_by_id, doc_id):
    d = meta_by_id.get(doc_id) or {}
    return re.sub(r"\s+", " ", strip_frontmatter(d.get("body") or ""))[:SNIPPET]


def src_row(meta_by_id, doc_id, role, score=None, rank=None):
    d = meta_by_id.get(doc_id) or {}
    return {"role": role, "doc_id": doc_id, "chunk_id": None, "project": d.get("project"),
            "kind": d.get("kind"), "title": d.get("title") or doc_id,
            "snippet": snippet_of(meta_by_id, doc_id), "score": score, "rank": rank,
            "content_hash": d.get("content_hash")}


# ---------- F21: partición de barrios (prior de inesperadez) + fingerprints existentes ----------

def _canonical_json_comm(obj):
    # Igual que ui_builder._canonical_json — misma serialización para recomputar partition_hash.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Communities:
    """Partición Leiden servida (F21). `unexpectedness(a, b)` da el prior cross-community:
    0.0 mismo barrio; None si algún doc está fuera de la partición / sin barrio (neutro)."""
    def __init__(self, comm, pair_density, algorithm):
        self.comm = comm                    # doc_id -> cid (>=0)
        self.pair_density = pair_density    # "ca|cb" (ca<cb) -> aristas colapsadas
        self.algorithm = algorithm

    def unexpectedness(self, a, b):
        ca = self.comm.get(a)
        cb = self.comm.get(b)
        if ca is None or cb is None or ca < 0 or cb < 0:
            return None
        if ca == cb:
            return 0.0
        cross = self.pair_density.get(f"{min(ca, cb)}|{max(ca, cb)}", 0)
        return 1.0 - min(1.0, cross / UNEXPECTED_SAT)


def _strict_int(v):
    # bool es subclase de int en Python: True pasaría int(v) — acá se rechaza.
    return isinstance(v, int) and not isinstance(v, bool)


def load_communities(ui_link, ui_gen, idx_gen):
    """Carga la partición servida para el prior. Validación ESTRICTA (tipos exactos, sin
    coerción): cualquier desvío → (None, motivo) y el prior queda inactivo (fail-open).
    NUNCA aborta la campaña — toda la validación va envuelta en try."""
    try:
        art = read_json(f"{ui_link}/communities.json")
        if not isinstance(art, dict):
            return None, "ausente_o_ilegible"
        if str(art.get("ui_generation")) != str(ui_gen) or str(art.get("index_generation")) != str(idx_gen):
            return None, "generacion_incoherente"
        comm_raw = art.get("communities")
        sizes_raw = art.get("sizes")
        pd_raw = art.get("pair_density")
        universe = art.get("universe")
        params = art.get("params")
        algorithm = art.get("algorithm")
        if not isinstance(comm_raw, dict) or not isinstance(sizes_raw, dict) \
                or not isinstance(pd_raw, dict) or not isinstance(universe, dict) \
                or not isinstance(params, dict):
            return None, "esquema_invalido"
        if algorithm not in ("leiden", "louvain"):
            return None, "algorithm_invalido"
        # communities: str -> int estricto, valores >= -1
        if not all(isinstance(k, str) and _strict_int(v) and v >= -1 for k, v in comm_raw.items()):
            return None, "communities_tipos"
        comm = dict(comm_raw)
        # membresía exacta contra doc_vec_map
        try:
            dv = open_vec(f"{ui_link}/doc_vectors.db")
            db_ids = {r[0] for r in dv.execute("SELECT doc_id FROM doc_vec_map")}
            dv.close()
        except Exception:
            return None, "doc_vec_map_ilegible"
        if set(comm.keys()) != db_ids:
            return None, "membresia_mismatch"
        from collections import Counter
        got = Counter(comm.values())
        # sizes: claves str de int, valores int estricto; igualdad exacta con lo contado
        decl = {}
        for k, v in sizes_raw.items():
            if not isinstance(k, str) or not _strict_int(v):
                return None, "sizes_tipos"
            try:
                decl[int(k)] = v
            except ValueError:
                return None, "sizes_tipos"
        if dict(got) != decl:
            return None, "sizes_inconsistentes"
        if not _strict_int(universe.get("n_docs")) or universe["n_docs"] != len(comm):
            return None, "n_docs_inconsistente"
        valid = set(got.keys())
        # pair_density: claves canónicas "a|b" con a<b, ambos cids válidos >=0, valores int >=0.
        # No se re-canonicaliza: una clave no canónica o duplicada-tras-canon se rechaza.
        pd = {}
        for k, v in pd_raw.items():
            if not isinstance(k, str) or not _strict_int(v) or v < 0:
                return None, "pair_density_invalida"
            parts = k.split("|")
            if len(parts) != 2:
                return None, "pair_density_invalida"
            try:
                ca, cb = int(parts[0]), int(parts[1])
            except ValueError:
                return None, "pair_density_invalida"
            if not (0 <= ca < cb) or ca not in valid or cb not in valid or k != f"{ca}|{cb}":
                return None, "pair_density_invalida"
            pd[k] = v
        # Verificación de integridad: recomputar partition_hash sobre los 6 campos núcleo (raw).
        core = {k: art.get(k) for k in ("algorithm", "params", "universe", "communities", "sizes", "pair_density")}
        if sha(_canonical_json_comm(core)) != art.get("partition_hash"):
            return None, "partition_hash_mismatch"
        return Communities(comm, pd, algorithm), "ok"
    except Exception as e:
        return None, "validacion_error:" + type(e).__name__


def read_existing_fingerprints(db_path):
    """Fingerprints ya en el ledger, leídos en conexión READ-ONLY (no crea/migra la DB en
    --dry-run ni rompe el aislamiento por --db). DB ausente → conjunto vacío."""
    if not os.path.isfile(db_path):
        return set()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return {r[0] for r in con.execute("SELECT fingerprint FROM candidates")}
        finally:
            con.close()
    except Exception:
        return set()


def _already_known(cand, pairs, existing_fps):
    """True si el candidato ya está condenado ANTES del corte por limit: novelty agotada
    (supports ya editorialmente conectados) o fingerprint ya en el ledger. Reproduce EXACTO
    los dos rechazos que hoy ocurren recién en persistencia."""
    supports = sorted({s["doc_id"] for s in cand["sources"] if s["role"] == "support"})
    if novelty_gate(pairs, supports) <= 0.0:
        return True
    fp = discover.fingerprint(cand["discovery_type"], OPERATOR_VERSION, supports,
                              cand.get("stable_params", ""))
    return fp in existing_fps


# ---------- Operadores deterministas ----------

def op_latent_bridge(neigh, hubs, pairs, meta_by_id, limit, pseudo=frozenset(),
                     communities=None, existing_fps=frozenset()):
    """Pares cross-project semánticamente cercanos SIN enlace editorial, expandidos a >=3 fuentes.
    Descarta pares triviales (copias/plantillas) y emite por peso desc: con `limit` chico,
    el orden de diccionario elegía puentes arbitrarios en vez de los más fuertes."""
    from collections import Counter
    cand = []
    seen_pairs = set()
    for a, kept in neigh.items():
        if a in hubs:
            continue
        pa = meta_by_id[a]["project"]
        if pa in pseudo:
            continue
        for b, w in kept:
            if w < BRIDGE_MIN_W or b in hubs:
                continue
            pb = meta_by_id[b]["project"]
            if pb in pseudo or same_project(pa, pb) or frozenset((a, b)) in seen_pairs:
                continue
            seen_pairs.add(frozenset((a, b)))
            if trivial_pair(meta_by_id, a, b, w):
                continue
            if editorially_linked(pairs, a, b):
                continue  # novelty-check: ya estaba escrito → recuperación, no hallazgo
            cand.append((w, a, b, pa, pb))
    # Prior de inesperadez: prioridad = w*(1+ALPHA*u). Con u=0 (prior inactivo o intra-barrio)
    # la prioridad == w y el desempate (w,a,b,pa,pb) reproduce byte-a-byte el orden actual.
    def _pri(t):
        w, a, b, pa, pb = t
        u = communities.unexpectedness(a, b) if communities else None
        return (w * (1.0 + UNEXPECTED_ALPHA * u) if u else w, w, a, b, pa, pb)
    out = []
    per_pair = Counter()
    for w, a, b, pa, pb in sorted(cand, key=_pri, reverse=True):
        pkey = tuple(sorted((pa.lower(), pb.lower())))
        if per_pair[pkey] >= BRIDGES_PER_PAIR:
            continue
        # tercera fuente: vecino de a o b (no hub, no linkeado con ambos, no copia de ellos)
        third = None
        for c, wc in (neigh.get(a, []) + neigh.get(b, [])):
            if c in (a, b) or c in hubs or (wc and wc >= NEARDUP_W):
                continue
            if trivial_pair(meta_by_id, a, c) or trivial_pair(meta_by_id, b, c):
                continue  # una copia de a/b no es tercera fuente, es la misma fuente de nuevo
            if editorially_linked(pairs, a, c) and editorially_linked(pairs, b, c):
                continue
            third = (c, wc)
            break
        if not third:
            continue
        u = communities.unexpectedness(a, b) if communities else None
        srcs = [src_row(meta_by_id, a, "support", w, 1),
                src_row(meta_by_id, b, "support", w, 2),
                src_row(meta_by_id, third[0], "support", third[1], 3)]
        cand_dict = {"discovery_type": "latent_bridge", "sources": srcs,
                     "scores": {"novelty": round(1.0 - 0.0, 3), "support": round(w, 3), "risk": 0.3,
                                "unexpectedness": (round(u, 3) if u is not None else None)},
                     "seed_title": f"Puente latente: {pa} ↔ {pb}",
                     "stable_params": ""}
        # Prefiltrado anti mina-seca-falsa: saltar los ya condenados ANTES de consumir el limit.
        if _already_known(cand_dict, pairs, existing_fps):
            continue
        per_pair[pkey] += 1
        out.append(cand_dict)
        if len(out) >= limit:
            break
    return out


def op_cluster_frontier(conv, members, neigh, pairs, meta_by_id, limit, pseudo=frozenset()):
    """Pares de proyectos con muchas cercanías semánticas y pocos enlaces editoriales."""
    from collections import Counter
    sem = Counter()
    examples = {}
    for a, kept in neigh.items():
        pa = meta_by_id[a]["project"]
        if pa in pseudo:
            continue
        for b, w in kept:
            pb = meta_by_id[b]["project"]
            if pb in pseudo or same_project(pa, pb):
                continue
            if trivial_pair(meta_by_id, a, b, w):
                continue  # un repo vendoreado en dos proyectos infla n_sem sin frontera real
            key = tuple(sorted((pa, pb)))
            sem[key] += 1
            examples.setdefault(key, []).append((w, a, b))
    edi = Counter()
    for pr in pairs:
        pr = sorted(pr)
        if len(pr) != 2:
            continue
        a, b = pr
        pa = (meta_by_id.get(a) or {}).get("project")
        pb = (meta_by_id.get(b) or {}).get("project")
        if pa and pb and pa != pb:
            edi[tuple(sorted((pa, pb)))] += 1
    out = []
    for key, n_sem in sem.most_common():
        if n_sem < FRONTIER_MIN_SEMANTIC or edi.get(key, 0) > FRONTIER_MAX_EDITORIAL:
            continue
        best = sorted(examples[key], reverse=True)[:4]
        docs = []
        for w, a, b in best:
            for d in (a, b):
                if d not in docs:
                    docs.append(d)
        srcs = [src_row(meta_by_id, d, "support", None, i + 1) for i, d in enumerate(docs[:5])]
        out.append({"discovery_type": "cluster_frontier", "sources": srcs,
                    "scores": {"novelty": 0.5, "support": round(n_sem / 20.0, 3), "risk": 0.3},
                    "seed_title": f"Frontera subexplorada: {key[0]} ↔ {key[1]} ({n_sem} cercanías, {edi.get(key, 0)} links)",
                    "stable_params": f"{key[0]}|{key[1]}"})
        if len(out) >= limit:
            break
    return out


def op_outlier(neigh, meta_by_id, limit, pseudo=frozenset()):
    """Docs cuyo vecindario semántico está dominado por OTRO proyecto (rareza estructural)."""
    from collections import Counter
    out = []
    for a, kept in neigh.items():
        if len(kept) < KEEP_NEIGHBORS - 2:
            continue
        pa = meta_by_id[a]["project"]
        if pa in pseudo:
            continue
        foreign = Counter(meta_by_id[b]["project"] for b, w in kept
                          if not same_project(meta_by_id[b]["project"], pa)
                          and meta_by_id[b]["project"] not in pseudo
                          and not trivial_pair(meta_by_id, a, b, w))
        if not foreign:
            continue
        proj, n = foreign.most_common(1)[0]
        if n < OUTLIER_MIN_FOREIGN:
            continue
        others = [b for b, w in kept if meta_by_id[b]["project"] == proj
                  and not trivial_pair(meta_by_id, a, b, w)][:2]
        srcs = [src_row(meta_by_id, a, "support", None, 1)] + [
            src_row(meta_by_id, b, "support", None, i + 2) for i, b in enumerate(others)]
        out.append({"discovery_type": "outlier", "sources": srcs,
                    "scores": {"novelty": 0.5, "support": round(n / KEEP_NEIGHBORS, 3), "risk": 0.4},
                    "seed_title": f"Outlier: {a} vive en {pa} pero su barrio es {proj} ({n}/{len(kept)})",
                    "stable_params": ""})
        if len(out) >= limit:
            break
    return out


# ---------- Capa LLM local (F12) ----------

# F14: el transporte LLM vive en llm_provider (ollama local default, API por config).
import llm_provider


def call_llm(prov, system, user):
    return llm_provider.chat_json(system, user, provider=prov)


def evidence_pack(sources):
    """Pack numerado: los modelos chicos no reproducen doc_ids largos textualmente —
    citan por índice [n] y nosotros mapeamos de vuelta (grounding programático)."""
    return "\n".join(f"[{i + 1}] ({s.get('project')}/{s.get('kind')}) {s.get('title')} — doc: {s['doc_id']}\n"
                     f"    {s.get('snippet') or ''}"
                     for i, s in enumerate(sources))


SCREEN_SYSTEM = (
    "Sos un auditor epistémico de una memoria colectiva de proyectos técnicos. Evaluás si un grupo de "
    "documentos sostiene una conexión REAL (misma forma relacional: problema→restricción→solución→costo), "
    "no una coincidencia de vocabulario, plantilla o estilo. Respondés SOLO JSON válido, en castellano "
    "rioplatense neutro. Solo podés citar doc_ids que estén en la evidencia provista."
)

# Pregunta específica por tipo (v3): el screen genérico validaba "hay conexión" y dejaba
# pasar tensiones que eran complementariedad y fronteras que eran plantillas compartidas.
# Cada tipo tiene SU pregunta, y valid=true responde ESA pregunta, no la genérica.
TYPE_QUESTION = {
    "latent_bridge": (
        "Pregunta específica de un PUENTE: ¿lo que sabe o resuelve un lado le sirve CONCRETAMENTE al otro "
        "proyecto (mismo problema, misma técnica, restricción compartida)? valid=true SOLO si podés nombrar "
        "qué aprendería un proyecto del otro."),
    "cluster_frontier": (
        "Pregunta específica de una FRONTERA: ¿hay un tema sustantivo compartido entre los DOS PROYECTOS que "
        "amerite conectarlos editorialmente? valid=true SOLO si lo compartido es contenido, no estructura "
        "(dos proyectos que usan el mismo formato de informe o las mismas herramientas no son una frontera)."),
    "outlier": (
        "Pregunta específica de un OUTLIER: ¿este documento contiene conocimiento que temáticamente pertenece "
        "al OTRO proyecto — está fuera de lugar de forma interesante? valid=true SOLO si el cruce habilita algo "
        "(citar, reusar, mover); una copia, un backup o un artefacto generado no es un outlier."),
    "tension": (
        "Pregunta específica de una TENSIÓN: ¿los documentos AFIRMAN cosas incompatibles entre sí? Hablar del "
        "mismo tema, complementarse o ser versiones del mismo texto NO es tensión: tiene que haber desacuerdo real."),
    "analogy": (
        "Pregunta específica de una ANALOGÍA: ¿los dos dominios comparten la MISMA forma relacional "
        "(problema→restricción→solución→costo) más allá del vocabulario? Palabras compartidas no son analogía."),
    "freshness_negative": (
        "Pregunta específica de FRESHNESS: ¿la evidencia más nueva ACTUALIZA, CONTRADICE o DEBILITA algo que el "
        "documento publicado afirma? Ser del mismo tema y más nuevo no alcanza: tiene que afectar lo afirmado."),
}

ANTI_DUP = (
    "OJO: si los documentos son esencialmente el MISMO contenido (copia, backup, traducción, export a otro "
    "formato, versiones del mismo archivo, artefactos generados por la misma plantilla), NO hay conexión que "
    'descubrir: valid=false con reason que empiece con "duplicación/plantilla".')


def screen_candidate(model, discovery_type, seed_title, sources):
    """Screen transversal obligatorio: valida la pregunta específica del tipo + emite
    claim/why/roles. Grounding sintáctico: cited ⊆ evidence pack (validación programática)."""
    ev = evidence_pack(sources)
    extra = ""
    if discovery_type == "analogy":
        extra = (' Además "roles" DEBE incluir "role_mapping" (problema/restricción/solución/costo de cada lado) '
                 'y "breaking_point" (dónde se rompe la analogía). Sin ambos, valid=false.')
    elif discovery_type == "tension":
        extra = (' Además "roles" DEBE incluir "side_a", "side_b" (qué afirma cada lado, citando los números de la evidencia), '
                 '"mediators" (posibles mediadores: ¿escala? ¿definición? ¿contexto?) e "intensity" (leve|media|fuerte). '
                 "El desacuerdo casi nunca es binario: si un mediador lo disuelve, decilo.")
    user = (f"Tipo de candidato: {discovery_type}. Semilla: {seed_title}\n\n"
            f"{TYPE_QUESTION.get(discovery_type, '')}\n{ANTI_DUP}\n\nEvidencia numerada:\n{ev}\n\n"
            'Devolvé JSON: {"valid": true|false, "reason": "<por qué es o no una conexión del tipo pedido>", '
            '"title": "<título 4-12 palabras>", "claim": "<1-2 frases verificables>", '
            '"why_interesting": "<qué habilita saber esto>", "cited": [<números de la evidencia que sostienen el claim>], '
            '"roles": {"role_mapping": "...", "breaking_point": "...", "next_step": "..."}}' + extra)
    out = call_llm(model, SCREEN_SYSTEM, user)
    if not out:
        return None
    try:
        cited_idx = {int(x) for x in (out.get("cited") or [])}
    except (TypeError, ValueError):
        cited_idx = set()
    valid_idx = set(range(1, len(sources) + 1))
    if not cited_idx or not cited_idx.issubset(valid_idx):
        return {"valid": False, "reason": "grounding_failed: citó índices fuera del pack o ninguno"}
    # Validación programática estricta: sin campos obligatorios, un override de falsación
    # podría publicar un hallazgo con "(sin claim)" o una tensión sin lados.
    # `valid` como booleano ESTRICTO: '"false"' (string) es truthy y colaría screen_passed.
    if out.get("valid") is not True:
        return {"valid": False, "reason": str(out.get("reason") or "screen_rechazado")}
    if not (str(out.get("claim") or "").strip() and str(out.get("title") or "").strip()
            and str(out.get("why_interesting") or "").strip()):
        return {"valid": False, "reason": "screen_incompleto: falta claim/title/why_interesting"}
    roles = out.get("roles") or {}
    if discovery_type == "analogy":
        if not (roles.get("role_mapping") and roles.get("breaking_point")):
            return {"valid": False, "reason": "analogy_sin_scaffolding: falta role_mapping o breaking_point"}
    elif discovery_type == "tension":
        if not (roles.get("side_a") and roles.get("side_b") and roles.get("intensity")):
            return {"valid": False, "reason": "tension_incompleta: falta side_a/side_b/intensity"}
    out["valid"] = True
    return out


def falsify(model, con, cand_id, meta_by_id):
    """Falsación: ablation contrafactual + negative search + diversidad + stale.
    Escribe falsification_json y flags; passed = ablation_ok ∧ ¬stale ∧ diversidad."""
    cand = discover.get_candidate(con, cand_id)
    sources = discover.get_sources(con, cand_id)
    supports = sorted([s for s in sources if s["role"] == "support"],
                      key=lambda s: -(s.get("score") or 0))
    fals = {"passed": False}
    flags = jget(cand, "flags_json", {})

    # stale_check: hashes vigentes
    coni = open_index_ro()
    cur = {r[0]: r[1] for r in coni.execute(
        f"SELECT doc_id, content_hash FROM docs WHERE doc_id IN ({','.join('?' * len(supports))})",
        [s["doc_id"] for s in supports])}
    stale = [s["doc_id"] for s in supports if cur.get(s["doc_id"]) != s.get("content_hash")]
    fals["stale_check"] = {"stale_sources": stale}
    if stale:
        flags["stale"] = True

    # diversity_check
    projs = {s.get("project") for s in supports}
    kinds = {s.get("kind") for s in supports}
    fals["diversity_check"] = {"projects": sorted(p or "" for p in projs), "kinds": sorted(k or "" for k in kinds)}
    diverse = len(projs) >= 2 or len(kinds) >= 2
    if not diverse:
        flags["low_diversity"] = True

    # source_ablation CONTRAFACTUAL: re-screen sin la fuente más fuerte; si el claim cae → fragile.
    # (grounding por lectura de citas no alcanza: hasta 57% de citas correctas no son fieles)
    ablation_ok = False
    if len(supports) >= 2 and model:
        rest = supports[1:]
        user = (f"Claim: {cand['claim']}\n\nEvidencia REDUCIDA (se quitó la fuente principal):\n"
                f"{evidence_pack(rest)}\n\n"
                'Con SOLO esta evidencia, ¿el claim sigue sostenido? JSON: {"still_supported": true|false, "reason": "..."}')
        out = call_llm(model, SCREEN_SYSTEM, user)
        # `is True` estricto: un modelo que devuelva "false" (string) haría bool()==True
        # y colaría una ablación fallida (mismo criterio que screen con `valid`).
        ablation_ok = bool(out) and out.get("still_supported") is True
        fals["source_ablation"] = {"removed": supports[0]["doc_id"], "survives": ablation_ok,
                                   "reason": (out or {}).get("reason")}
    if not ablation_ok:
        flags["fragile"] = True

    # negative_search: vecinos semánticos del claim NO citados → ¿alguno contradice/debilita?
    counters = []
    if model and cand.get("claim"):
        try:
            vec = tier1.embed([cand["claim"]], lock_timeout=30)[0]  # embed toma su propio gpu_lock
            conv = open_vec(f"{UI_LINK}/doc_vectors.db")
            import numpy as np
            hits = conv.execute("SELECT rowid, distance FROM doc_vec WHERE embedding MATCH ? AND k=12",
                                (np.asarray(vec, dtype="float32").tobytes(),)).fetchall()
            cited = {s["doc_id"] for s in sources}
            near = []
            for rowid, dist in hits:
                r = conv.execute("SELECT doc_id FROM doc_vec_map WHERE rowid=?", (rowid,)).fetchone()
                if r and r[0] not in cited and (meta_by_id.get(r[0]) or {}).get("kind") not in PRIMARY_KINDS_EXCLUDED:
                    near.append(r[0])
                if len(near) >= 3:
                    break
            conv.close()
            if near:
                packs = "\n".join(f"- [{d}] {(meta_by_id.get(d) or {}).get('title')}: {snippet_of(meta_by_id, d)}"
                                  for d in near)
                user = (f"Claim: {cand['claim']}\n\nDocumentos cercanos NO citados:\n{packs}\n\n"
                        '¿Alguno CONTRADICE o debilita el claim? JSON: '
                        '{"counter_doc_ids": ["<doc_id>", ...], "contradicted": true|false, "reason": "..."}')
                out = call_llm(model, SCREEN_SYSTEM, user)
                if out:
                    counters = [d for d in (out.get("counter_doc_ids") or []) if d in near]
                    fals["negative_search"] = {"checked": near, "counters": counters,
                                               "contradicted": bool(out.get("contradicted")),
                                               "reason": out.get("reason")}
                    if out.get("contradicted"):
                        flags["contradicted"] = True
        except Exception as e:
            fals["negative_search"] = {"error": str(e)}
    for i, d in enumerate(counters):
        row = src_row(meta_by_id, d, "counter", None, i + 1)
        con.execute("INSERT INTO candidate_sources(candidate_id, role, doc_id, chunk_id, project, kind, "
                    "title, snippet, score, rank, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (cand_id, row["role"], row["doc_id"], row["chunk_id"], row["project"], row["kind"],
                     row["title"], row["snippet"], row["score"], row["rank"], row["content_hash"]))

    fals["passed"] = bool(ablation_ok and not stale and diverse and not flags.get("contradicted"))
    con.execute("UPDATE candidates SET falsification_json=?, flags_json=? WHERE id=?",
                (json.dumps(fals, ensure_ascii=False), json.dumps(flags, ensure_ascii=False), cand_id))
    con.commit()
    coni.close()
    return fals["passed"]


# --- Operadores LLM (generadores) ---

def op_tension(neigh, pairs, meta_by_id, limit, communities=None, existing_fps=frozenset()):
    """Candidatos de tensión: pares muy cercanos (mismo tema) — el screen decide si hay
    tension card real. Genera pares intra o cross project con soporte textual de ambos lados.
    Dos versiones/copias del mismo doc no pueden estar en desacuerdo: se filtran.
    Solo docs discursivos: un json de métricas no AFIRMA nada, no puede tensionar."""
    cand = []
    seen = set()
    for a, kept in neigh.items():
        if meta_by_id[a]["kind"] == "fs_dataset":
            continue
        for b, w in kept[:3]:
            if meta_by_id[b]["kind"] == "fs_dataset":
                continue
            # Banda: piso = mismo tema; techo = la franja alta son espejos/versiones,
            # y dos copias del mismo texto no pueden estar en desacuerdo.
            if not (BRIDGE_MIN_W <= w < TENSION_MAX_W) or frozenset((a, b)) in seen:
                continue
            seen.add(frozenset((a, b)))
            if trivial_pair(meta_by_id, a, b, w):
                continue
            cand.append((w, a, b))
    # Prior de inesperadez (ver op_latent_bridge): con u=0 el orden reproduce el actual (w,a,b).
    def _pri(t):
        w, a, b = t
        u = communities.unexpectedness(a, b) if communities else None
        return (w * (1.0 + UNEXPECTED_ALPHA * u) if u else w, w, a, b)
    out = []
    for w, a, b in sorted(cand, key=_pri, reverse=True):
        third = next((c for c, wc in neigh.get(a, [])
                      if c != b and not trivial_pair(meta_by_id, a, c, wc)), None)
        if not third:
            continue
        u = communities.unexpectedness(a, b) if communities else None
        srcs = [src_row(meta_by_id, a, "support", w, 1),
                src_row(meta_by_id, b, "support", w, 2),
                src_row(meta_by_id, third, "support", None, 3)]
        cand_dict = {"discovery_type": "tension", "sources": srcs,
                     "scores": {"novelty": 0.4, "support": round(w, 3), "risk": 0.5,
                                "unexpectedness": (round(u, 3) if u is not None else None)},
                     "seed_title": f"¿Tensión entre {a} y {b}?", "stable_params": ""}
        # Prefiltrado anti mina-seca-falsa: saltar los ya condenados ANTES de consumir el limit.
        if _already_known(cand_dict, pairs, existing_fps):
            continue
        out.append(cand_dict)
        if len(out) >= limit:
            break
    return out


def op_analogy(meta_by_id, limit):
    """Analogías estructurales entre dominios, orientadas por síntesis F8 (que NO son evidencia:
    los supports son los docs primarios de cada síntesis)."""
    coni = open_index_ro()
    synths = coni.execute("SELECT doc_id, title, body, project FROM docs WHERE kind='synthesis'").fetchall()
    coni.close()
    out = []
    for i, sa in enumerate(synths):
        for sb in synths[i + 1:]:
            src_a = tier1.frontmatter_source_paths(sa[2])[:2]
            src_b = tier1.frontmatter_source_paths(sb[2])[:2]
            docs = [d for d in src_a + src_b if (meta_by_id.get(d) or {}).get("kind") not in (None, *PRIMARY_KINDS_EXCLUDED)]
            if len(set(docs)) < MIN_SOURCES:
                continue
            srcs = [src_row(meta_by_id, d, "support", None, j + 1) for j, d in enumerate(dict.fromkeys(docs))]
            out.append({"discovery_type": "analogy", "sources": srcs,
                        "scores": {"novelty": 0.6, "support": 0.4, "risk": 0.5},
                        "seed_title": f"¿Analogía estructural entre «{sa[1]}» y «{sb[1]}»?",
                        "stable_params": f"{sa[0]}|{sb[0]}"})
            if len(out) >= limit:
                return out
    return out


def op_freshness(conv, meta_by_id, limit):
    """Docs más nuevos que actualizan/debilitan claims de nodos publicados (map/síntesis)."""
    coni = open_index_ro()
    published = coni.execute(
        "SELECT doc_id, title, body, mtime, content_hash, project, kind FROM docs "
        "WHERE kind IN ('map','synthesis','discovery')").fetchall()
    coni.close()
    out = []
    vecmap = {r[1]: r[0] for r in conv.execute("SELECT rowid, doc_id FROM doc_vec_map")}
    for doc_id, title, body, mtime, chash, project, kind in published:
        rowid = vecmap.get(doc_id)
        if not rowid:
            continue
        row = conv.execute("SELECT embedding FROM doc_vec WHERE rowid=?", (rowid,)).fetchone()
        if not row:
            continue
        hits = conv.execute("SELECT rowid, distance FROM doc_vec WHERE embedding MATCH ? AND k=12",
                            (row[0], )).fetchall()
        newer = []
        for orid, dist in hits:
            r = conv.execute("SELECT doc_id FROM doc_vec_map WHERE rowid=?", (orid,)).fetchone()
            if not r or r[0] == doc_id:
                continue
            d = meta_by_id.get(r[0])
            if d and d["kind"] not in PRIMARY_KINDS_EXCLUDED and (d.get("mtime") or 0) > (mtime or 0) + 86400:
                newer.append((r[0], 1.0 / (1.0 + float(dist))))
            if len(newer) >= 3:
                break
        if len(newer) < MIN_SOURCES:
            continue
        srcs = [src_row(meta_by_id, d, "support", w, i + 1) for i, (d, w) in enumerate(newer)]
        srcs.append({"role": "target", "doc_id": doc_id, "chunk_id": None, "project": project,
                     "kind": kind, "title": title, "snippet": "", "score": None, "rank": 1,
                     "content_hash": chash})
        out.append({"discovery_type": "freshness_negative", "sources": srcs,
                    "scores": {"novelty": 0.5, "support": 0.5, "risk": 0.4},
                    "seed_title": f"¿Evidencia más nueva afecta a «{title}»?",
                    "stable_params": doc_id})
        if len(out) >= limit:
            break
    return out


# ---------- Campaña ----------

def run_campaign(args):
    db_path = getattr(args, "db", None) or discover.DISC_DB
    owner = getattr(args, "owner", None)
    # Lock DERIVADO del ledger: una campaña sandbox no bloquea la writer-side ni viceversa.
    lockf = open(db_path + ".lock", "a")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"[discover] ya hay una campaña corriendo sobre {db_path} — abortando")

    import os
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    ops = DETERMINISTIC + LLM_OPERATORS if args.operators == "all" else tuple(
        o.strip() for o in args.operators.split(",") if o.strip())
    bad = [o for o in ops if o not in DETERMINISTIC + LLM_OPERATORS]
    if bad:
        sys.exit(f"[discover] operadores inválidos: {bad}")

    dv, idx_gen, ui_gen = coherence_gate()
    prov = llm_provider.get_provider(getattr(args, "provider", "default") or "default",
                                     getattr(args, "model", None) or None)
    generated_by = llm_provider.provider_label(prov)
    no_llm = os.environ.get("MAPA_DISCOVERY_NO_LLM", "0") == "1"
    llm = False if no_llm else llm_provider.provider_up(prov)
    if not llm:
        skipped_llm = [o for o in ops if o in LLM_OPERATORS]
        ops = tuple(o for o in ops if o in DETERMINISTIC)
        reason = "desactivado por política" if no_llm else f"provider {generated_by} no responde"
        print(f"[discover] LLM {reason}: screen deshabilitado, operadores LLM "
              f"salteados {skipped_llm} — los candidatos quedarán unscreened (no promovibles)", file=sys.stderr)

    conv, members, meta_by_id = load_universe(dv, set(args.project) or None)
    print(f"[discover] universo: {len(members)} docs de prosa con vector (sin synthesis/discovery)")
    coni = open_index_ro()
    pairs, _adj = build_explicit_links(coni)
    coni.close()
    print(f"[discover] links editoriales recomputados: {len(pairs)} pares (pase completo, no grafos truncados)")
    neigh, indeg = knn_cached(conv, members, ui_gen, args.project)
    hubs = hub_set(indeg)
    pseudo = pseudo_projects(members)
    print(f"[discover] hubs semánticos penalizados: {len(hubs)} | pseudo-proyectos excluidos: {sorted(pseudo)}")

    # F21: prior de inesperadez (barrios) + fingerprints existentes para el prefiltrado anti
    # mina-seca-falsa. El prior es fail-open (ranking, no gate); el prefiltrado se aplica siempre.
    communities, comm_reason = load_communities(UI_LINK, ui_gen, idx_gen)
    existing_fps = read_existing_fingerprints(db_path)
    if communities:
        note_prior = f"prior de inesperadez: activo ({communities.algorithm}, {len(communities.comm)} docs)"
        if args.project:
            note_prior += " — partición GLOBAL (campaña scoped por --project)"
    else:
        note_prior = f"prior de inesperadez: inactivo ({comm_reason})"
    print(f"[discover] {note_prior} | fingerprints en ledger: {len(existing_fps)}")

    generated = []
    per_op = max(1, args.limit // max(1, len(ops)))
    for op in ops:
        if op == "latent_bridge":
            cands = op_latent_bridge(neigh, hubs, pairs, meta_by_id, per_op, pseudo,
                                     communities=communities, existing_fps=existing_fps)
        elif op == "cluster_frontier":
            cands = op_cluster_frontier(conv, members, neigh, pairs, meta_by_id, per_op, pseudo)
        elif op == "outlier":
            cands = op_outlier(neigh, meta_by_id, per_op, pseudo)
        elif op == "tension":
            cands = op_tension(neigh, pairs, meta_by_id, per_op,
                               communities=communities, existing_fps=existing_fps)
        elif op == "analogy":
            cands = op_analogy(meta_by_id, per_op)
        elif op == "freshness_negative":
            cands = op_freshness(conv, meta_by_id, per_op)
        else:
            cands = []
        print(f"[discover] {op}: {len(cands)} candidatos generados")
        generated.extend((op, c) for c in cands)

    if args.dry_run:
        for op, c in generated[:30]:
            print(f"  [{op}] {c['seed_title'][:100]}")
        conv.close()
        return 0

    con = discover.open_disc(db_path, create=True)
    con.execute("INSERT INTO campaigns(name, created_at, status, operators_json, params_json, "
                "index_generation, ui_generation, owner) VALUES(?,?,?,?,?,?,?,?)",
                (args.campaign, now_iso(), "running", json.dumps(list(ops)),
                 json.dumps({"limit": args.limit, "projects": args.project, "provider": generated_by}),
                 idx_gen, ui_gen, owner))
    camp = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()

    new_ids, dup, known = [], 0, 0
    for op, c in generated:
        supports = [s["doc_id"] for s in c["sources"] if s["role"] == "support"]
        # Novelty central (todos los operadores): si TODOS los pares relevantes ya están
        # editorialmente linkeados, la relación está escrita → recuperación, no hallazgo.
        # freshness_negative es role-aware: la relación es support↔target — se evalúan SOLO
        # esos pares cruzados (evidencia "nueva" que ya es fuente del target no descubre nada).
        targets = [s["doc_id"] for s in c["sources"] if s["role"] == "target"]
        if c["discovery_type"] == "freshness_negative" and targets:
            nov = novelty_gate_cross(pairs, supports, targets)
        else:
            nov = novelty_gate(pairs, supports)
        if nov <= 0.0:
            known += 1
            continue
        c["scores"]["novelty"] = round(nov, 3)
        flags = {"unscreened": True}
        hub_srcs = sorted(set(supports) & hubs)
        if hub_srcs:
            flags["hub_sources"] = hub_srcs  # persistido: el gate de promoción lo mira
        cid, created = save_candidate(
            con, camp, c["discovery_type"], op, OPERATOR_VERSION, c["sources"],
            title=c["seed_title"], scores=c["scores"], stable_params=c.get("stable_params", ""),
            flags=flags, gens={"index": idx_gen, "ui": ui_gen}, generated_by=generated_by, owner=owner)
        if created:
            new_ids.append(cid)
        else:
            dup += 1
    print(f"[discover] guardados: {len(new_ids)} nuevos, {dup} ya existentes (fingerprint), "
          f"{known} descartados por novelty (relación ya explícita)")

    # OJO: sin gpu_lock exterior — tier1.gpu_lock() es flock NO reentrante y
    # tier1.embed()/get_model() lo toman internamente (deadlock si lo tenemos nosotros).
    # Ollama gestiona su propia VRAM; el único uso de GPU nuestro (negative_search)
    # pasa por embed(lock_timeout=...) que arbitra solo.
    screened = passed = 0
    if llm and new_ids:
        for i, cid in enumerate(new_ids):
            cand = discover.get_candidate(con, cid)
            sources = discover.get_sources(con, cid)
            out = screen_candidate(prov, cand["discovery_type"], cand["title"],
                                   [s for s in sources if s["role"] == "support"])
            flags = jget(cand, "flags_json", {})
            flags.pop("unscreened", None)
            if out and out.get("valid") is True:
                flags["screen_passed"] = True
                roles = jget(cand, "roles_json", {})
                roles.update(out.get("roles") or {})
                con.execute("UPDATE candidates SET title=?, claim=?, why_interesting=?, roles_json=?, "
                            "flags_json=? WHERE id=?",
                            (out.get("title") or cand["title"], out.get("claim"),
                             out.get("why_interesting"), json.dumps(roles, ensure_ascii=False),
                             json.dumps(flags, ensure_ascii=False), cid))
                con.commit()
                screened += 1
                if falsify(prov, con, cid, meta_by_id):
                    passed += 1
            else:
                flags["screen_passed"] = False
                flags["screen_reason"] = (out or {}).get("reason") or "llm_error"
                con.execute("UPDATE candidates SET flags_json=? WHERE id=?",
                            (json.dumps(flags, ensure_ascii=False), cid))
                con.commit()
            print(f"  [{i+1}/{len(new_ids)}] {cid} screen={'OK' if out and out.get('valid') is True else 'NO'}")

    con.execute("UPDATE campaigns SET status='finished', finished_at=?, notes=? WHERE id=?",
                (now_iso(), note_prior, camp))
    con.commit()
    conv.close()
    # Política VRAM (F14): si negative_search cargó bge-m3, descargarlo al terminar el job.
    try:
        tier1.unload_model_if_idle(force=True)
    except Exception:
        pass
    print(f"[discover] campaña #{camp} «{args.campaign}»: {len(new_ids)} candidatos, "
          f"{screened} screen OK, {passed} falsación OK — revisá con: discover.py list --status candidate")
    return 0
