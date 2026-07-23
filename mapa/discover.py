#!/usr/bin/env python3
"""Capa de discovery trazable de la memoria colectiva (F9-F12).

Genera CANDIDATOS privados de hallazgos (puentes latentes, fronteras, outliers,
tensiones, analogías, evidencia fresca) sobre el índice + vectores existentes,
los pasa por screen LLM local + falsación, y solo tras revisión humana los
promueve a markdown en .mapa/overlays/hallazgos/ (el bibliotecario los publica
en mapa/hallazgos/ por snapshot atómico). Invariantes:
  - Candidatos privados: viven en .mapa/discovery.db, JAMÁS se sirven.
  - Ningún hallazgo publicado sin >= MIN_SOURCES fuentes support primarias
    (kind no synthesis/discovery) trazables y vigentes (content_hash).
  - screen_passed es condición dura de promoción (NO overrideable).
  - Escritura atómica bajo LIB_LOCK; solo librarian.sh publica en mapa/.
  - LLM local-only (ollama); GPU bajo tier1.gpu_lock().
"""
import argparse
import fcntl
import glob
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import (ROOT, DMAPA, DB, CODE_HOME, OWNER_REVIEWER, is_reserved_reviewer)  # noqa: E402
UI_LINK = os.path.join(DMAPA, "ui")
OVERLAYS = os.path.join(DMAPA, "overlays")
HALLAZGOS = os.path.join(OVERLAYS, "hallazgos")
MANIFEST = os.path.join(DMAPA, "manifest.json")
DISC_DB = os.path.join(DMAPA, "discovery.db")
PG_DB = os.path.join(DMAPA, "discovery", "playground.db")  # sandbox de la mesa chica (F15)
DISC_LOCK = os.path.join(DMAPA, "discovery.lock")
LIB_LOCK = os.path.join(DMAPA, "lock")

OLLAMA_URL = os.environ.get("MAPA_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
DEFAULT_MODEL = "qwen3:8b"
PROMPT_VERSION = "3"  # v3: pregunta específica por tipo + instrucción anti-duplicación (v2: cita por índice)

sys.path.insert(0, CODE_HOME)
import tier1  # noqa: E402  (gpu_lock, MIN_SOURCES; sin cargar modelo)

MIN_SOURCES = tier1.MIN_SOURCES  # umbral unificado writer/indexer/promoción
PRIMARY_KINDS_EXCLUDED = ("synthesis", "discovery")  # anti-echo-chamber: nunca evidencia primaria
DISCOVERY_TYPES = ("latent_bridge", "cluster_frontier", "outlier", "tension", "analogy",
                   "freshness_negative", "gap")  # gap: pregunta que nadie formuló (modo agente F17)
STATUSES = ("candidate", "interesting", "actionable", "confirmed", "discarded")
SOURCE_ROLES = ("support", "counter", "bridge", "target", "context")
JACCARD_DUP = 0.70


def sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(value):
    base = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base[:60] or "hallazgo"


def open_index_ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def strip_frontmatter(body):
    return re.sub(r"^---\n.*?\n---\n?", "", body or "", count=1, flags=re.S)


# ---------- discovery.db (ledger privado) ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  operators_json TEXT,
  params_json TEXT,
  index_generation TEXT,
  ui_generation TEXT,
  notes TEXT,
  owner TEXT
);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY,
  fingerprint TEXT UNIQUE NOT NULL,
  campaign_id INTEGER REFERENCES campaigns(id),
  discovery_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate',
  title TEXT,
  claim TEXT,
  why_interesting TEXT,
  operator TEXT NOT NULL,
  operator_version TEXT NOT NULL,
  novelty_score REAL,
  support_score REAL,
  risk_score REAL,
  projects_json TEXT,
  roles_json TEXT,
  falsification_json TEXT,
  flags_json TEXT,
  created_at TEXT NOT NULL,
  reviewed_at TEXT,
  promoted_doc_id TEXT,
  duplicate_of TEXT,
  index_generation TEXT,
  ui_generation TEXT,
  owner TEXT,
  generated_by TEXT
);
CREATE TABLE IF NOT EXISTS candidate_sources(
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  role TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  chunk_id INTEGER,
  project TEXT,
  kind TEXT,
  title TEXT,
  snippet TEXT,
  score REAL,
  rank INTEGER,
  content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_cand ON candidate_sources(candidate_id);
CREATE TABLE IF NOT EXISTS candidate_reviews(
  id INTEGER PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  reviewer TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);
"""


# Migraciones best-effort para DBs anteriores al F14/F15 (columna ya existente = no-op).
MIGRATIONS = (
    "ALTER TABLE campaigns ADD COLUMN owner TEXT",
    "ALTER TABLE candidates ADD COLUMN owner TEXT",
    "ALTER TABLE candidates ADD COLUMN generated_by TEXT",
    "ALTER TABLE candidates ADD COLUMN unexpectedness_score REAL",  # F21: prior de inesperadez (cross-community)
)


def open_disc(path=None, create=False):
    """Ledger inyectable (F15): default = discovery.db del dueño; el playground pasa
    su sandbox. WAL + busy_timeout: el playground tiene threads HTTP + worker
    concurrentes sobre la misma DB."""
    path = path or DISC_DB
    if not create and not os.path.exists(path):
        sys.exit(f"[discover] no existe {path} — corré: discover.py init-db")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            con.execute(m)
        except sqlite3.OperationalError:
            pass
    return con


def cas_update_status(con, cand_id, from_status, to_status, extra_sql="", extra_params=(),
                      review=None):
    """Transición compare-and-swap: falla (False) si otro thread ya movió el estado.

    Si `review` = (reviewer, note), la fila de candidate_reviews se inserta en la MISMA
    transacción que el UPDATE — la transición y su traza son atómicas (o ambas o ninguna)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        cur = con.execute(
            f"UPDATE candidates SET status=?, reviewed_at=?{(', ' + extra_sql) if extra_sql else ''} "
            "WHERE id=? AND status=?",
            (to_status, now_iso(), *extra_params, cand_id, from_status))
        if cur.rowcount != 1:
            con.rollback()
            return False
        if review is not None:
            reviewer, note = review
            con.execute("INSERT INTO candidate_reviews(candidate_id, reviewer, from_status, "
                        "to_status, note, created_at) VALUES(?,?,?,?,?,?)",
                        (cand_id, reviewer, from_status, to_status, note, now_iso()))
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise


def get_candidate(con, cid):
    row = con.execute("SELECT * FROM candidates WHERE id=? OR fingerprint=?", (cid, cid)).fetchone()
    if not row:
        sys.exit(f"[discover] candidato no encontrado: {cid}")
    return dict(row)


def get_sources(con, cid):
    return [dict(r) for r in con.execute(
        "SELECT * FROM candidate_sources WHERE candidate_id=? ORDER BY role, rank", (cid,))]


def jget(row, key, default=None):
    try:
        return json.loads(row.get(key) or "null") or default
    except Exception:
        return default


# ---------- Registro de links explícitos (novelty-check) ----------
# Se recomputa desde index.db + manifest (pase completo); los grafos publicados
# están truncados por selección y NO sirven para afirmar ausencia de vínculo.

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
# Para el REGISTRO de links (no para el universo de evidencia) sí entran synthesis/discovery:
# sus wikilinks/source_paths son decisiones editoriales que cuentan como "ya escrito".
LINK_SCAN_KINDS = ("map", "synthesis", "discovery", "biblioteca", "source", "fs_doc", "fs_pdf", "fs_notebook", "fs_dataset")
FM_ID_RE = re.compile(r"^id:\s*(\S+)", re.M)
FM_ALIASES_RE = re.compile(r"^aliases:\s*(\[.*?\])\s*$", re.M)


def build_explicit_links(coni):
    """Set de pares editoriales {frozenset(a,b)} + adyacencia por doc.

    Fuentes: (a) source_paths/counter/bridge/target del manifest;
             (b) wikilinks en bodies de docs de prosa, resueltos por alias
                 exacto/lower (doc_id, path sin ext, basename, título).
    Ambiguo => se trata como LINKEADO (conservador: mejor perder un candidato
    que emitir un 'puente' que ya estaba escrito)."""
    pairs = set()
    adj = {}

    def link(a, b):
        if not a or not b or a == b:
            return
        pairs.add(frozenset((a, b)))
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    def ensure_rel(p):
        p = (p or "").strip()
        return p[len(ROOT) + 1:] if p.startswith(ROOT + "/") else p

    man = read_json(MANIFEST, {"nodes": {}})
    for nid, node in (man.get("nodes") or {}).items():
        src = ensure_rel(node.get("logical_path"))
        for key in ("source_paths", "counter_paths", "bridge_paths", "target_paths"):
            for sp in node.get(key) or []:
                link(src, ensure_rel(sp))

    # Alias maps con paridad ui_builder (alias_candidates/resolve_wikilink): doc_id,
    # path sin ext (y su forma con puntos), basename, título, frontmatter id y aliases.
    docs = coni.execute(
        f"SELECT doc_id, kind, title, body FROM docs WHERE kind IN ({','.join('?' * len(LINK_SCAN_KINDS))}) "
        "AND duplicate_of IS NULL", LINK_SCAN_KINDS).fetchall()
    exact, lower = {}, {}

    def add_alias(alias, doc_id):
        alias = (alias or "").strip().strip("\"'")
        if not alias:
            return
        exact.setdefault(alias, set()).add(doc_id)
        lower.setdefault(alias.lower(), set()).add(doc_id)

    for doc_id, _kind, title, body in docs:
        add_alias(doc_id, doc_id)
        noext = os.path.splitext(doc_id)[0]
        add_alias(noext, doc_id)
        add_alias(noext.replace("/", "."), doc_id)
        add_alias(os.path.basename(noext), doc_id)
        if title:
            add_alias(title, doc_id)
        fm = (body or "").split("\n---", 1)[0]
        m = FM_ID_RE.search(fm)
        if m:
            add_alias(m.group(1), doc_id)
        m = FM_ALIASES_RE.search(fm)
        if m:
            try:
                for a in json.loads(m.group(1)):
                    add_alias(str(a), doc_id)
            except Exception:
                for a in m.group(1)[1:-1].split(","):
                    add_alias(a, doc_id)

    def resolve(raw):
        raw = raw.strip()
        dotted = raw.replace("/", ".")  # paridad UI: wikilinks con / resuelven contra alias con puntos
        for table, key in ((exact, raw), (exact, dotted), (lower, raw.lower()), (lower, dotted.lower())):
            hit = table.get(key)
            if hit:
                return set(hit)  # 1 => resuelto; >1 => ambiguo (linkea a todos, conservador)
        return set()

    for doc_id, _kind, _title, body in docs:
        for m in WIKILINK_RE.finditer(strip_frontmatter(body)):
            for target in resolve(m.group(1)):
                link(doc_id, target)

    return pairs, adj


def novelty_gate_cross(pairs, side_a_ids, side_b_ids):
    """Novelty entre dos LADOS (p.ej. supports↔target en freshness): fracción de pares
    cruzados sin link editorial. 0.0 = toda la evidencia 'nueva' ya es fuente del target."""
    a, b = sorted(set(side_a_ids)), sorted(set(side_b_ids))
    total = linked = 0
    for x in a:
        for y in b:
            if x == y:
                continue
            total += 1
            if frozenset((x, y)) in pairs:
                linked += 1
    return 1.0 - (linked / total) if total else 1.0


def novelty_gate(pairs, support_doc_ids):
    """Novelty central: fracción de pares de supports SIN link editorial.
    0.0 = la relación ya está toda escrita (recuperación, no hallazgo) → descartar."""
    ids = sorted(set(support_doc_ids))
    if len(ids) < 2:
        return 1.0
    total = linked = 0
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            total += 1
            if frozenset((a, b)) in pairs:
                linked += 1
    return 1.0 - (linked / total) if total else 1.0


def editorially_linked(pairs, a, b):
    return frozenset((a, b)) in pairs


# ---------- Fingerprint + dedup ----------

def fingerprint(discovery_type, operator_version, primary_doc_ids, stable_params=""):
    return sha(f"{discovery_type}|{operator_version}|" + "|".join(sorted(primary_doc_ids)) + f"|{stable_params}")[:16]


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a | b) else 0.0


def find_duplicate(con, cand_supports, exclude_id=None):
    """duplicate_of por Jaccard >= JACCARD_DUP contra candidatos existentes."""
    rows = con.execute(
        "SELECT c.id, group_concat(s.doc_id, '\n') AS docs FROM candidates c "
        "JOIN candidate_sources s ON s.candidate_id=c.id AND s.role='support' "
        "WHERE c.status != 'discarded' GROUP BY c.id").fetchall()
    for r in rows:
        if r["id"] == exclude_id:
            continue
        if jaccard(cand_supports, (r["docs"] or "").split("\n")) >= JACCARD_DUP:
            return r["id"]
    return None


# ---------- Persistencia de candidatos ----------

def save_candidate(con, campaign_id, discovery_type, operator, operator_version,
                   sources, title=None, claim=None, why=None, scores=None,
                   roles=None, flags=None, stable_params="", gens=None,
                   generated_by=None, owner=None):
    """Inserta si el fingerprint es nuevo; devuelve (id, created:bool)."""
    supports = sorted({s["doc_id"] for s in sources if s["role"] == "support"})
    fp = fingerprint(discovery_type, operator_version, supports, stable_params)
    cid = f"cand.{fp}"
    if con.execute("SELECT 1 FROM candidates WHERE fingerprint=?", (fp,)).fetchone():
        return cid, False
    dup = find_duplicate(con, supports)
    scores = scores or {}
    con.execute(
        "INSERT INTO candidates(id, fingerprint, campaign_id, discovery_type, status, title, claim, "
        "why_interesting, operator, operator_version, novelty_score, support_score, risk_score, "
        "unexpectedness_score, projects_json, roles_json, falsification_json, flags_json, created_at, "
        "duplicate_of, index_generation, ui_generation, owner, generated_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, fp, campaign_id, discovery_type, "candidate", title, claim, why, operator, operator_version,
         scores.get("novelty"), scores.get("support"), scores.get("risk"), scores.get("unexpectedness"),
         json.dumps(sorted({s.get("project") or "" for s in sources if s["role"] == "support"}), ensure_ascii=False),
         json.dumps(roles or {}, ensure_ascii=False), json.dumps({}, ensure_ascii=False),
         json.dumps(flags or {}, ensure_ascii=False), now_iso(), dup,
         (gens or {}).get("index"), (gens or {}).get("ui"), owner, generated_by))
    con.executemany(
        "INSERT INTO candidate_sources(candidate_id, role, doc_id, chunk_id, project, kind, title, "
        "snippet, score, rank, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(cid, s["role"], s["doc_id"], s.get("chunk_id"), s.get("project"), s.get("kind"),
          s.get("title"), s.get("snippet"), s.get("score"), s.get("rank"), s.get("content_hash"))
         for s in sources])
    con.commit()
    return cid, True


# ---------- Promoción ----------

def check_promotion_gates(con, cand, sources, coni):
    """Devuelve lista de violaciones (vacía = promovible)."""
    errs = []
    if cand["status"] != "confirmed":
        errs.append(f"status={cand['status']} (se requiere confirmed con historial de review)")
    flags = jget(cand, "flags_json", {})
    if not flags.get("screen_passed"):
        errs.append("screen_passed != true (condición dura, NO overrideable)")
    supports = [s for s in sources if s["role"] == "support"]
    primary = [s for s in supports if (s.get("kind") or "") not in PRIMARY_KINDS_EXCLUDED]
    if len({s["doc_id"] for s in primary}) < MIN_SOURCES:
        errs.append(f"fuentes support primarias únicas: {len({s['doc_id'] for s in primary})} < {MIN_SOURCES}")
    cur = {r[0]: (r[1], r[2]) for r in coni.execute(
        f"SELECT doc_id, content_hash, kind FROM docs WHERE doc_id IN ({','.join('?' * len(supports))})",
        [s["doc_id"] for s in supports]).fetchall()} if supports else {}
    for s in supports:
        got = cur.get(s["doc_id"])
        if not got:
            errs.append(f"fuente inexistente en index.db: {s['doc_id']}")
        elif got[0] != s.get("content_hash"):
            errs.append(f"fuente stale (content_hash cambió): {s['doc_id']}")
        elif got[1] in PRIMARY_KINDS_EXCLUDED:
            errs.append(f"fuente no primaria (kind={got[1]}): {s['doc_id']}")
    fals = jget(cand, "falsification_json", {})
    if not fals.get("passed") and not flags.get("falsification_override"):
        errs.append("falsación no pasada y sin override humano registrado")
    projs = {s.get("project") for s in primary}
    kinds = {s.get("kind") for s in primary}
    if len(projs) < 2 and len(kinds) < 2:
        errs.append(f"diversidad insuficiente (1 proyecto y 1 kind: {projs} / {kinds})")
    hub_srcs = set(flags.get("hub_sources") or [])
    non_hub = [s for s in primary if s["doc_id"] not in hub_srcs]
    if primary and not non_hub:
        errs.append("soporte depende exclusivamente de docs-hub (hub_sources cubre todas las primarias)")
    if cand.get("duplicate_of"):
        errs.append(f"duplicate_of pendiente: {cand['duplicate_of']} (descartá o revisá el duplicado primero)")
    return errs


def render_hallazgo(cand, sources, review_rows):
    supports = [s for s in sources if s["role"] == "support"]
    counters = [s for s in sources if s["role"] == "counter"]
    bridges = [s for s in sources if s["role"] == "bridge"]
    targets = [s for s in sources if s["role"] == "target"]
    roles = jget(cand, "roles_json", {})
    slug = f"{slugify(cand['title'] or cand['claim'] or cand['id'])}-{cand['fingerprint'][:6]}"

    def paths(rows):
        return json.dumps(sorted({s["doc_id"] for s in rows}), ensure_ascii=False)

    def anchors(rows):
        return "\n".join(
            f'- <a data-doc="{html.escape(s["doc_id"], quote=True)}">{html.escape(s.get("title") or s["doc_id"])}</a>'
            f'{" — " + html.escape(s["snippet"][:160]) if s.get("snippet") else ""}'
            for s in rows) or "- (ninguna)"

    hashes = json.dumps({s["doc_id"]: s.get("content_hash") for s in supports}, ensure_ascii=False, sort_keys=True)
    reviews = "\n".join(
        f"- {r['created_at']} · {r['reviewer']}: {r['from_status']} → {r['to_status']}"
        f"{' — ' + r['note'] if r['note'] else ''}"
        for r in review_rows)
    mediadores = roles.get("breaking_point") or roles.get("mediators") or ""
    fm = f"""---
id: hallazgo.{cand['discovery_type']}.{slug}
type: discovery
discovery_type: {cand['discovery_type']}
candidate_id: {cand['id']}
campaign_id: {cand['campaign_id']}
operator: {cand['operator']}
operator_version: {cand['operator_version']}
generated_by: {cand.get('generated_by') or 'ollama-cli'}
prompt_version: {PROMPT_VERSION}
created_at: {cand['created_at']}
validated_at: {now_iso()}
index_generation: "{cand['index_generation'] or ''}"
source_paths: {paths(supports)}
counter_paths: {paths(counters)}
bridge_paths: {paths(bridges)}
target_paths: {paths(targets)}
source_hashes: {hashes}
---
# {cand['title'] or cand['claim']}

## Claim

{cand['claim'] or '(sin claim)'}

## Por qué importa

{cand['why_interesting'] or '(sin justificación registrada)'}

## Evidencia primaria

{anchors(supports)}

## Contraevidencia y límites

{anchors(counters)}

{f'## Mediadores / punto de ruptura' + chr(10) + chr(10) + mediadores + chr(10) if mediadores else ''}
## Ruta de lectura

{anchors(supports[:3])}

## Próximo paso sugerido

{roles.get('next_step') or '(a definir en revisión)'}

## Registro de validación

{reviews or '- (sin reviews registradas)'}
"""
    return slug, fm


def promote(args):
    con = open_disc()
    cand = get_candidate(con, args.candidate_id)
    sources = get_sources(con, cand["id"])
    coni = open_index_ro()
    errs = check_promotion_gates(con, cand, sources, coni)
    if errs:
        print(f"[discover] PROMOCIÓN RECHAZADA para {cand['id']}:", file=sys.stderr)
        for e in errs:
            print(f"  ✗ {e}", file=sys.stderr)
        coni.close()
        sys.exit(2)
    reviews = con.execute(
        "SELECT reviewer, from_status, to_status, note, created_at FROM candidate_reviews "
        "WHERE candidate_id=? ORDER BY created_at", (cand["id"],)).fetchall()
    slug, body = render_hallazgo(cand, sources, [dict(r) for r in reviews])
    coni.close()

    os.makedirs(HALLAZGOS, exist_ok=True)
    final = os.path.join(HALLAZGOS, f"{slug}.md")
    tmp = os.path.join(HALLAZGOS, f".{slug}.md.tmp")
    # Escritura atómica bajo LIB_LOCK: el allowlist del librarian instala todo *.md
    # regular del overlay — jamás puede ver un archivo parcial.
    liblock = open(LIB_LOCK, "a")
    fcntl.flock(liblock, fcntl.LOCK_EX)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        reread = open(tmp, encoding="utf-8").read()
        fmsrc = re.search(r"^source_paths:\s*(\[.*?\])\s*$", reread, re.M)
        if not (reread.startswith("---") and fmsrc and len(json.loads(fmsrc.group(1))) >= MIN_SOURCES):
            os.unlink(tmp)
            sys.exit("[discover] validación de relectura falló — no se publica")
        os.replace(tmp, final)
    finally:
        fcntl.flock(liblock, fcntl.LOCK_UN)
        liblock.close()

    doc_id = f"mapa/hallazgos/{slug}.md"
    con.execute("UPDATE candidates SET promoted_doc_id=? WHERE id=?", (doc_id, cand["id"]))
    con.commit()
    print(f"[discover] PROMOVIDO → {final}")
    print(f"[discover] doc_id publicable: {doc_id}")
    print("[discover] siguiente paso: MAPA_UI_MODE=full bash scripts/librarian.sh")


# ---------- Review ----------

def review(args):
    con = open_disc(getattr(args, "db", None))
    cand = get_candidate(con, args.candidate_id)
    if args.status not in STATUSES[1:]:
        sys.exit(f"[discover] status inválido: {args.status} (válidos: {', '.join(STATUSES[1:])})")
    # Guard de reviewer: confirmed/actionable y los overrides son del dueño;
    # cualquier otro reviewer queda limitado a interesting/discarded. En código, no en prompt.
    if args.reviewer != OWNER_REVIEWER:
        if args.status not in ("interesting", "discarded"):
            sys.exit(f"[discover] reviewer '{args.reviewer}' no puede mover a {args.status} "
                     "(solo interesting|discarded)")
        if args.override_falsification or args.generated_work or args.clear_duplicate:
            sys.exit(f"[discover] reviewer '{args.reviewer}' no puede usar overrides")
    flags = jget(cand, "flags_json", {})
    if args.override_falsification:
        flags["falsification_override"] = True
    if args.generated_work:
        flags["generated_work"] = True  # insumo del KPI actionability
    ok = cas_update_status(con, cand["id"], cand["status"], args.status,
                           extra_sql="flags_json=?", extra_params=(json.dumps(flags, ensure_ascii=False),),
                           review=(args.reviewer, args.note))
    if not ok:
        sys.exit(f"[discover] conflicto: {cand['id']} ya no está en status={cand['status']} — releé con show")
    if args.clear_duplicate:
        con.execute("UPDATE candidates SET duplicate_of=NULL WHERE id=?", (cand["id"],))
        con.commit()
    print(f"[discover] {cand['id']}: {cand['status']} → {args.status}")


# ---------- Listado / show / metrics ----------

def list_cmd(args):
    con = open_disc(getattr(args, "db", None))
    if getattr(args, "json", False):
        q = ("SELECT id, discovery_type, status, operator, title, claim, novelty_score, "
             "duplicate_of, owner, flags_json, created_at FROM candidates WHERE 1=1")
    else:
        q = ("SELECT id, discovery_type, status, operator, "
             "substr(coalesce(title, claim, ''),1,70) AS t, created_at FROM candidates WHERE 1=1")
    params = []
    if args.status:
        q += " AND status=?"
        params.append(args.status)
    if args.type:
        q += " AND discovery_type=?"
        params.append(args.type)
    q += " ORDER BY created_at DESC"
    rows = con.execute(q, params).fetchall()
    if getattr(args, "json", False):
        for r in rows:
            flags = jget(dict(r), "flags_json", {})  # jget necesita .get(); Row no lo tiene
            print(json.dumps({
                "id": r["id"], "type": r["discovery_type"], "status": r["status"],
                "operator": r["operator"], "title": r["title"] or (r["claim"] or "")[:70],
                "novelty": r["novelty_score"], "duplicate_of": r["duplicate_of"],
                "owner": r["owner"],
                "screen_passed": bool(flags.get("screen_passed")),
                "created_at": r["created_at"],
            }, ensure_ascii=False))
        return
    for r in rows:
        print(f"{r['id']}  [{r['status']:>11}] {r['discovery_type']:<17} {r['t']}")
    print(f"-- {len(rows)} candidatos")


def show(args):
    con = open_disc(getattr(args, "db", None))
    cand = get_candidate(con, args.candidate_id)
    out = {k: cand[k] for k in cand if not k.endswith("_json")}
    for k in ("projects_json", "roles_json", "falsification_json", "flags_json"):
        out[k[:-5]] = jget(cand, k)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if args.with_sources:
        for s in get_sources(con, cand["id"]):
            print(f"  [{s['role']:>8}] {s['kind'] or '?':<12} {s['doc_id']}"
                  + (f"  «{(s['snippet'] or '')[:80]}»" if s.get("snippet") else ""))
    reviews = con.execute("SELECT * FROM candidate_reviews WHERE candidate_id=? ORDER BY created_at",
                          (cand["id"],)).fetchall()
    for r in reviews:
        print(f"  review {r['created_at']} {r['reviewer']}: {r['from_status']}→{r['to_status']} {r['note'] or ''}")


def inbox(args):
    """Propuestas de la mesa chica (status=proposed en el sandbox del playground)."""
    con = open_disc(PG_DB)
    rows = con.execute(
        "SELECT id, owner, discovery_type, substr(coalesce(title, claim, ''),1,70) AS t, created_at "
        "FROM candidates WHERE status='proposed' ORDER BY created_at").fetchall()
    for r in rows:
        print(f"{r['id']}  [{r['owner'] or '?':>12}] {r['discovery_type']:<17} {r['t']}")
    print(f"-- {len(rows)} propuestas (importar con: discover.py import <id>)")


def import_cmd(args):
    """Copia una propuesta del sandbox al ledger del dueño, con integridad referencial.

    - campaign_id destino = campaña local 'importaciones-playground' (busca-o-crea);
      JAMÁS el id de campaña del sandbox (referencia inexistente acá).
    - Copia candidato (mismo id/fingerprint → dedup entre ledgers gratis), fuentes y
      reviews; agrega review de trazabilidad. En el sandbox queda status='imported'.
    """
    src = open_disc(PG_DB)
    cand = get_candidate(src, args.candidate_id)
    # Reclamar la propuesta ANTES de copiar (proposed → importing): si el dueño la mueve
    # concurrentemente, el CAS falla acá y no copiamos nada a medias.
    if not cas_update_status(src, cand["id"], "proposed", "importing"):
        sys.exit(f"[discover] {cand['id']} no está proposed o alguien lo movió (status={cand['status']})")
    try:
        _do_import(src, cand)
    except Exception:
        cas_update_status(src, cand["id"], "importing", "proposed")  # no dejar la propuesta atascada
        raise


def _sanitize_reviewer(name, origin):
    """Impide que una identidad privilegiada llegue desde el sandbox web.

    El ledger distingue al dueño, al director y a la marca de procedencia solo
    por este texto. Se REETIQUETA en vez de rechazar: rechazar el import entero
    permitiria fabricar un candidato imposible de importar (denegacion de
    servicio trivial sobre el canal de propuestas).
    """
    if is_reserved_reviewer(name):
        return f"{origin}:{name}"
    return name


def _do_import(src, cand):
    dst = open_disc()
    row = dst.execute("SELECT id FROM campaigns WHERE name='importaciones-playground'").fetchone()
    if row:
        camp = row["id"]
    else:
        dst.execute("INSERT INTO campaigns(name, created_at, status, notes) VALUES(?,?,?,?)",
                    ("importaciones-playground", now_iso(), "open",
                     "campaña contenedora de candidatos importados del sandbox del playground"))
        camp = dst.execute("SELECT last_insert_rowid()").fetchone()[0]
    existing = dst.execute("SELECT id, status FROM candidates WHERE fingerprint=?",
                           (cand["fingerprint"],)).fetchone()
    owner = cand["owner"] or "?"
    if existing:
        dst.execute("INSERT INTO candidate_reviews(candidate_id, reviewer, from_status, to_status, note, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (existing["id"], "import", existing["status"], existing["status"],
                     f"re-propuesto por {owner} desde el playground", now_iso()))
        dst.commit()
        print(f"[discover] ya existía como {existing['id']} (status={existing['status']}) — anotado el re-propuesto")
    else:
        cols = [d[1] for d in src.execute("PRAGMA table_info(candidates)")]
        vals = [cand.get(c) for c in cols]
        vals[cols.index("campaign_id")] = camp
        vals[cols.index("status")] = "candidate"  # el dueño lo revisa desde cero, con historial
        dst.execute(f"INSERT INTO candidates({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        dst.executemany(
            "INSERT INTO candidate_sources(candidate_id, role, doc_id, chunk_id, project, kind, title, "
            "snippet, score, rank, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(cand["id"], s["role"], s["doc_id"], s.get("chunk_id"), s.get("project"), s.get("kind"),
              s.get("title"), s.get("snippet"), s.get("score"), s.get("rank"), s.get("content_hash"))
             for s in get_sources(src, cand["id"])])
        dst.executemany(
            "INSERT INTO candidate_reviews(candidate_id, reviewer, from_status, to_status, note, created_at) "
            "VALUES(?,?,?,?,?,?)",
            # `reviewer` viene del sandbox que escribe el servicio web: este es
            # el cruce de confianza web -> ledger del dueño, y se reetiqueta acá.
            [(cand["id"], _sanitize_reviewer(r["reviewer"], "playground"),
              r["from_status"], r["to_status"], r["note"], r["created_at"])
             for r in src.execute("SELECT * FROM candidate_reviews WHERE candidate_id=? ORDER BY created_at",
                                  (cand["id"],))])
        dst.execute("INSERT INTO candidate_reviews(candidate_id, reviewer, from_status, to_status, note, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (cand["id"], "import", "proposed", "candidate",
                     f"importado desde playground, propuesto por {owner}", now_iso()))
        dst.commit()
        print(f"[discover] importado {cand['id']} (de {owner}) → campaña #{camp}")
    if not cas_update_status(src, cand["id"], "importing", "imported"):
        print("[discover] aviso: no pude marcar imported en el sandbox (¿carrera?)", file=sys.stderr)


def metrics(args):
    con = open_disc()
    m = {"by_type_status": {}, "discard_reasons": [], "published": 0}
    for r in con.execute("SELECT discovery_type, status, count(*) c FROM candidates GROUP BY 1,2"):
        m["by_type_status"].setdefault(r["discovery_type"], {})[r["status"]] = r["c"]
    total = con.execute("SELECT count(*) FROM candidates").fetchone()[0]
    confirmed = con.execute("SELECT count(*) FROM candidates WHERE status='confirmed'").fetchone()[0]
    m["total"] = total
    m["conversion_confirmed"] = round(confirmed / total, 3) if total else None
    m["published"] = con.execute("SELECT count(*) FROM candidates WHERE promoted_doc_id IS NOT NULL").fetchone()[0]
    m["duplicate_rate"] = round(
        con.execute("SELECT count(*) FROM candidates WHERE duplicate_of IS NOT NULL").fetchone()[0] / total, 3) if total else None
    gw = con.execute("SELECT count(*) FROM candidates WHERE flags_json LIKE '%\"generated_work\": true%'").fetchone()[0]
    m["actionability_rate"] = round(gw / confirmed, 3) if confirmed else None  # KPI rector
    fragile = con.execute("SELECT count(*) FROM candidates WHERE flags_json LIKE '%\"fragile\": true%'").fetchone()[0]
    m["ablation_survival_rate"] = round(1 - fragile / total, 3) if total else None
    stale = con.execute("SELECT count(*) FROM candidates WHERE flags_json LIKE '%\"stale\": true%'").fetchone()[0]
    m["stale_source_rate"] = round(stale / total, 3) if total else None
    for r in con.execute("SELECT note, count(*) c FROM candidate_reviews WHERE to_status='discarded' GROUP BY note ORDER BY c DESC LIMIT 10"):
        m["discard_reasons"].append({"note": r["note"], "count": r["c"]})
    # Concentración de fuentes (monocultivo)
    top = con.execute("SELECT doc_id, count(*) c FROM candidate_sources WHERE role='support' "
                      "GROUP BY doc_id ORDER BY c DESC LIMIT 10").fetchall()
    m["top_support_docs"] = [{"doc_id": r["doc_id"], "count": r["c"]} for r in top]
    print(json.dumps(m, indent=2, ensure_ascii=False))


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init-db")
    r = sub.add_parser("run")
    # --campaign y --operators son opcionales: en modo agente (--task) el adaptador
    # genera el nombre de campaña y no hay operadores (unión exclusiva, F17).
    r.add_argument("--campaign", default=None)
    r.add_argument("--operators", default=None, help="csv: latent_bridge,cluster_frontier,outlier,tension,analogy,freshness_negative | all")
    r.add_argument("--task", default=None, help="tarea en lenguaje natural → modo agente de descubrimiento (F17)")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--project", action="append", default=[])
    r.add_argument("--model", default=None, help="override del modelo del provider")
    r.add_argument("--provider", default="default", help="provider de llm_providers.json")
    r.add_argument("--db", default=None, help="ledger alternativo (sandbox del playground)")
    r.add_argument("--owner", default=None, help="dueño de los candidatos (playground)")
    r.add_argument("--dry-run", action="store_true")
    lc = sub.add_parser("list")
    lc.add_argument("--status")
    lc.add_argument("--type")
    lc.add_argument("--db", default=None, help="ledger alternativo (tests/sandbox)")
    lc.add_argument("--json", action="store_true", help="una línea JSON por candidato")
    sh = sub.add_parser("show")
    sh.add_argument("candidate_id")
    sh.add_argument("--with-sources", action="store_true")
    sh.add_argument("--db", default=None, help="ledger alternativo (tests/sandbox)")
    rv = sub.add_parser("review")
    rv.add_argument("candidate_id")
    rv.add_argument("--status", required=True)
    rv.add_argument("--note", default="")
    rv.add_argument("--reviewer", default=OWNER_REVIEWER)
    rv.add_argument("--override-falsification", action="store_true")
    rv.add_argument("--generated-work", action="store_true")
    rv.add_argument("--clear-duplicate", action="store_true")
    rv.add_argument("--db", default=None, help="ledger alternativo (tests/sandbox)")
    pr = sub.add_parser("promote")
    pr.add_argument("candidate_id")
    sub.add_parser("metrics")
    sub.add_parser("inbox")
    im = sub.add_parser("import")
    im.add_argument("candidate_id")
    args = ap.parse_args()

    if args.cmd == "init-db":
        open_disc(create=True).close()
        print(f"[discover] schema OK → {DISC_DB}")
        return 0
    if args.cmd == "run":
        # Dispatch por rama (unión exclusiva task/operadores):
        if args.task:
            if args.operators:
                sys.exit("[discover] --task y --operators son mutuamente excluyentes")
            from discover_agent import run_agent_task  # F17
            # --campaign es opcional en modo task (F19): si viene, nombra la campaña
            # (el director lo usa para el conteo de presupuesto por ledger).
            return run_agent_task(args.task, args.provider, args.db or DISC_DB, args.owner,
                                  campaign_name=args.campaign)
        if not args.operators:
            sys.exit("[discover] falta --operators (o --task para modo agente)")
        if not args.campaign:
            sys.exit("[discover] modo operadores requiere --campaign")
        from discover_operators import run_campaign  # F11/F12
        return run_campaign(args)
    if args.cmd == "list":
        list_cmd(args)
    elif args.cmd == "show":
        show(args)
    elif args.cmd == "review":
        review(args)
    elif args.cmd == "promote":
        promote(args)
    elif args.cmd == "metrics":
        metrics(args)
    elif args.cmd == "inbox":
        inbox(args)
    elif args.cmd == "import":
        import_cmd(args)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
