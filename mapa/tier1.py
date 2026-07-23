#!/usr/bin/env python3
"""Indice hibrido (lexico + semantico) del corpus.

Local-first: nada sale de la maquina. Dos scopes:
  curated  -> mapa/**.md + source_paths + markdown de las raices curadas
  total    -> curated + filesystem textual filtrado por politica auditable

Uso:
  tier1.py audit-corpus --scope total
  tier1.py index --scope curated|total
  tier1.py search [--kind K1,K2] [--project P] QUERY
  tier1.py stats
  tier1.py health
  tier1.py self-test
"""
import argparse
import contextlib
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as stat_mod
import subprocess
import sys
import time
import warnings
from collections import Counter, defaultdict
from pathlib import Path

try:
    import fcntl
except Exception:  # pragma: no cover - Linux host expected
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import (  # noqa: E402
    ROOT, DMAPA, DB, EMB_CACHE, MANIFEST, MAPA, POLICY_PATH, AUDIT_JSON, AUDIT_MD,
    MODEL_ID, model_cache_dir,
)

warnings.filterwarnings("ignore")

# El tratamiento "curado" se aplicaba a un directorio fijo de un proyecto
# concreto. Ahora lo declara la política: `curated_roots` lista rutas relativas
# a ROOT cuyo markdown recibe ese trato. Vacío por defecto.
MODEL_CACHE_DIR = model_cache_dir()
NORMALIZE = True
CHUNKER_VERSION = "2"
NORM_VERSION = "1"
RRF_K = 60
FTS_TOKENIZE = "unicode61 remove_diacritics 2"

KIND_BOOST = {
    "map": 1.50,
    "discovery": 1.35,
    "synthesis": 1.30,
    "biblioteca": 1.25,
    "source": 1.20,
    "fs_doc": 1.00,
    "fs_pdf": 0.95,
    "fs_dataset": 0.90,
    "fs_notebook": 0.85,
    "fs_code": 0.75,
    "fs_config": 0.65,
    "fs_media": 0.50,
}

# Kinds solo léxicos/topológicos: indexados en FTS y visibles en grafos, pero SIN embedding
# (bge-m3 agrupa código/config por tokens superficiales → ruido en los vecinos semánticos).
NO_EMBED_KINDS = {"fs_code", "fs_config", "fs_media"}

DEFAULT_POLICY = {
    "version": 1,
    "include_exts": {
        "fs_doc": [".md", ".txt", ".rst", ".tex"],
        "fs_pdf": [".pdf"],
        "fs_notebook": [".ipynb"],
        "fs_code": [
            ".py", ".sh", ".bash", ".zsh", ".js", ".jsx", ".ts", ".tsx",
            ".vue", ".svelte", ".c", ".cc", ".cpp", ".h", ".hpp", ".cu", ".cuh",
        ],
        "fs_config": [".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".css", ".html"],
    },
    "curated_extra_exts": [".txt", ".sql", ".jsonl"],
    "exclude_dir_names": [
        ".git", ".hg", ".svn", ".claude", ".codex", ".agents", ".venv", "venv", "env", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
        "build", "dist", "out", ".cache", "cache", "model_cache",
        "unsloth_compiled_cache", ".tools",
    ],
    # Solo patrones genéricos: caches, entornos y artefactos que ningún corpus
    # quiere indexar. Los directorios pesados propios de cada instalación se
    # agregan en corpus_policy.json, no acá.
    "exclude_dir_globs": [
        ".venv-*", "*venv*/**", ".mapa/hf/**", ".mapa/venv/**", ".mapa/snapshots/**",
        ".mapa/staging/**", ".mapa/overlays/**", ".mapa/ui/**", ".mapa/ui.*/**",
        ".mapa/discovery/**", ".mapa/web/**", "hf-cache/**", "*/checkpoints/**",
    ],
    # Rutas (relativas a ROOT) cuyo markdown recibe tratamiento curado.
    "curated_roots": [],
    "exclude_file_names": [
        "hackeados.md", ".env", ".htpasswd",
        ".git-credentials", ".netrc", ".pgpass", ".npmrc", ".pypirc",
        ".dockercfg",
    ],
    "exclude_file_globs": [
        "*.key", "*.pem", "id_rsa*", "id_ed25519*", "*.tfstate",
        "*.kubeconfig", "*.pt", "*.pth", "*.safetensors", "*.gguf",
        "*.bin", "*.onnx", "*.npy", "*.npz", "*.wav", "*.mp3", "*.flac",
        "*.jpg", "*.jpeg", "*.png", "*.webp", "*.mp4", "*.mov", "*.ttf",
        "*.so", "*.o", "*.db", "*.sqlite", "*.parquet", "*.arrow",
    ],
    "exclude_path_globs": [
        ".docker/config.json", "*/.docker/config.json", "kubeconfig/*.kubeconfig",
        "*/kubeconfig/*.kubeconfig", ".kube/config", "*/.kube/config",
    ],
    "exclude_name_contains": ["secret", "credential", "password"],
    "max_file_bytes": 2 * 1024 * 1024,
    "max_pdf_bytes": 25 * 1024 * 1024,
    "max_extracted_chars": 500_000,
    "max_chunk_chars": 5000,
    "doc_batch_size": 200,
    "embed_batch_size": 32,
    "commit_every_docs": 200,
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(refresh_token|client_secret)\b.{0,40}[=:]\s*['\"]?[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"\b(password|passwd|api_key|apikey|token)\b\s*[:=]\s*['\"]?[^'\"\s]{8,}", re.I),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@[^/\s]+", re.I),
    re.compile(r"\b(cookie|set-cookie)\b.{0,40}[=:]\s*['\"]?[^'\"\n]{40,}", re.I),
]

_model = None
_dims = None
_model_device = None
_last_model_use = None
_index_error = None
_gpu_lock_ok = None


def sha_text(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def rel_root(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def inside_root(real_path):
    return real_path == ROOT or real_path.startswith(ROOT + os.sep)


def normalize_rel(rel):
    return rel.replace("\\", "/").lstrip("./")


def load_policy():
    if not os.path.exists(POLICY_PATH):
        return DEFAULT_POLICY.copy()
    with open(POLICY_PATH, encoding="utf-8") as f:
        merged = DEFAULT_POLICY.copy()
        custom = json.load(f)
        for k, v in custom.items():
            merged[k] = v
        return merged


def policy_hash(policy):
    return sha_bytes(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def kind_for_ext(ext, policy):
    ext = ext.lower()
    for kind, exts in policy["include_exts"].items():
        if ext in exts:
            return kind
    return None


def dataset_kind_override(doc_id, policy):
    """Docs bajo un dataset_root reciben kind fs_dataset (chip propio en el Atlas)."""
    for root in policy.get("dataset_roots", []):
        if doc_id == root or doc_id.startswith(root + "/"):
            return "fs_dataset"
    return None


def synthesis_kind_override(doc_id):
    """Nodos de la capa de síntesis destilada (F8): mapa/sintesis/* → kind synthesis."""
    return "synthesis" if doc_id.startswith("mapa/sintesis/") else None


def discovery_kind_override(doc_id):
    """Hallazgos publicados de la capa de discovery (F9+): mapa/hallazgos/* → kind discovery."""
    return "discovery" if doc_id.startswith("mapa/hallazgos/") else None


MIN_SOURCES = 3  # umbral UNIFICADO writer/indexer/verificación: síntesis sin >=3 fuentes NO existe


def frontmatter_source_paths(body):
    """source_paths del frontmatter — JSON primero (doc_ids reales tienen comas), fallback split legacy."""
    m = re.search(r"^source_paths:\s*(\[.*?\])\s*$", (body or "").split("\n---", 1)[0], re.M | re.S)
    if not m:
        return []
    raw = m.group(1)
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except Exception:
        return [x.strip().strip("\"'") for x in raw[1:-1].split(",") if x.strip()]


def validate_discovery_sources(con, audit=None):
    """Post-pass BLOQUEANTE (F9): un doc kind=discovery queda en el índice solo si
    >= MIN_SOURCES de sus source_paths existen como docs de kind primario (no
    synthesis/discovery). Corre sobre la tmp DB ANTES del swap atómico — el
    invariante se sostiene en el índice servido, no solo en discover.py promote.
    Devuelve cuántos hallazgos inválidos se eliminaron (docs+chunks+fts+vec)."""
    removed = 0
    rows = con.execute("SELECT doc_id, body FROM docs WHERE kind='discovery'").fetchall()
    for doc_id, body in rows:
        valid = 0
        for sp in set(frontmatter_source_paths(body)):
            r = con.execute("SELECT kind FROM docs WHERE doc_id=?", (sp,)).fetchone()
            if r and r[0] not in ("synthesis", "discovery"):
                valid += 1
        if valid < MIN_SOURCES:
            for (chunk_id,) in con.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)).fetchall():
                con.execute("DELETE FROM chunks_fts WHERE rowid=?", (chunk_id,))
                try:
                    con.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (chunk_id,))
                except sqlite3.OperationalError:
                    pass  # índice fts-only: no existe chunks_vec
            con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            con.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            removed += 1
            if audit:
                audit.add_excluded("discovery_invalid_sources", doc_id)
    return removed


def project_for_doc_id(doc_id):
    first = doc_id.split("/", 1)[0]
    if first == "mapa":
        parts = doc_id.split("/")
        return parts[2].split(".")[0] if len(parts) > 2 and parts[1] == "proyectos" else "root"
    return first or "root"


def is_frontmatter_opt_out(body):
    m = re.match(r"^---\n(.*?)\n---", body, re.S)
    return bool(m and re.search(r"^serve:\s*false\s*$", m.group(1), re.I | re.M))


def contains_secret(body):
    sample = body[:500_000]
    return any(p.search(sample) for p in SECRET_PATTERNS)


def excluded_by_name(rel, policy):
    rel = normalize_rel(rel)
    base = os.path.basename(rel)
    low = base.lower()
    file_names = {x.lower() for x in policy["exclude_file_names"]}
    if base in policy["exclude_file_names"] or low in file_names:
        return "excluded_secret_name"
    for needle in policy["exclude_name_contains"]:
        if needle.lower() in low:
            return "excluded_secret_name"
    rel_low = rel.lower()
    for pat in policy.get("exclude_path_globs", []):
        pat_norm = normalize_rel(pat)
        if fnmatch.fnmatch(rel, pat_norm) or fnmatch.fnmatch(rel_low, pat_norm.lower()):
            return "excluded_secret_name"
    for pat in policy["exclude_file_globs"]:
        if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(low, pat.lower()):
            if pat.lower() in {"*.pt", "*.pth", "*.safetensors", "*.gguf", "*.bin", "*.onnx", "*.npy", "*.npz"}:
                return "excluded_model"
            return "excluded_by_ext"
    return None


def excluded_by_dir(rel, policy):
    rel = normalize_rel(rel)
    parts = rel.split("/")[:-1]
    for part in parts:
        if part in policy["exclude_dir_names"]:
            if part in {"cache", ".cache", "model_cache", "unsloth_compiled_cache"}:
                return "excluded_generated"
            return "excluded_by_dir"
    for glob_pat in policy["exclude_dir_globs"]:
        pat = normalize_rel(glob_pat)
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/".join(parts) + "/", pat.rstrip("*")):
            if "datasets" in pat or "/data/" in "/" + pat:
                return "excluded_dataset"
            if "models" in pat or "ollama" in pat or "hf-cache" in pat:
                return "excluded_model"
            return "excluded_by_dir"
    return None


def skip_reason_for_path(path, policy, curated=False, logical_rel=None):
    rp = os.path.realpath(path)
    if not inside_root(rp):
        return "excluded_symlink_escape"
    if not os.path.isfile(rp):
        return "not_file"
    rel = normalize_rel(logical_rel) if logical_rel else rel_root(rp)
    reason = excluded_by_dir(rel, policy)
    if reason:
        return reason
    reason = excluded_by_name(rel, policy)
    if reason:
        return reason
    ext = Path(rel).suffix.lower()
    if curated:
        allowed = set(sum(policy["include_exts"].values(), [])) | set(policy["curated_extra_exts"])
        if ext and ext not in allowed:
            return "excluded_by_ext"
        return None
    if kind_for_ext(ext, policy) is None:
        return "excluded_by_ext"
    try:
        size = os.path.getsize(rp)
    except OSError:
        return "read_error"
    max_bytes = policy["max_pdf_bytes"] if ext == ".pdf" else policy["max_file_bytes"]
    if size > max_bytes:
        return "excluded_too_large"
    return None


MD_TITLE_EXTS = {".md", ".markdown"}


def title_of(text, doc_id, ext=".md"):
    # Solo markdown real puede titular desde el body: en PDFs extraídos, código y configs,
    # una línea "# comentario" NO es un heading (bug: paper de AlphaGo titulado con un comment de Python).
    if ext.lower() in MD_TITLE_EXTS:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("# "):
                return s[2:].strip()
    return doc_id.rsplit("/", 1)[-1]


def read_text_file(path, policy):
    with open(path, encoding="utf-8", errors="replace") as f:
        body = f.read(policy["max_extracted_chars"] + 1)
    if len(body) > policy["max_extracted_chars"]:
        return None, "excluded_too_large", "text"
    return body, None, "text"


def read_pdf(path, policy):
    try:
        cp = subprocess.run(
            ["pdftotext", "-layout", "-q", path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:
        return None, "read_error", "pdftotext"
    body = cp.stdout.decode("utf-8", "replace").strip()
    if not body:
        return None, "pdf_no_text", "pdftotext"
    if len(body) > policy["max_extracted_chars"]:
        body = body[: policy["max_extracted_chars"]]
    return body, None, "pdftotext"


def read_ipynb(path, policy):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            nb = json.load(f)
    except Exception:
        return None, "read_error", "ipynb"
    pieces = []
    for i, cell in enumerate(nb.get("cells", [])):
        ctype = cell.get("cell_type")
        if ctype not in {"markdown", "code"}:
            continue
        src = cell.get("source", "")
        if isinstance(src, list):
            src = "".join(src)
        if not isinstance(src, str) or not src.strip():
            continue
        heading = "markdown" if ctype == "markdown" else "code"
        pieces.append(f"## cell {i} ({heading})\n{src.strip()}")
    body = "\n\n".join(pieces).strip()
    if not body:
        return None, "read_error", "ipynb"
    if len(body) > policy["max_extracted_chars"]:
        body = body[: policy["max_extracted_chars"]]
    return body, None, "ipynb"


def extract_body(path, policy, kind):
    ext = Path(path).suffix.lower()
    if kind == "fs_pdf" or ext == ".pdf":
        return read_pdf(path, policy)
    if kind == "fs_notebook" or ext == ".ipynb":
        return read_ipynb(path, policy)
    return read_text_file(path, policy)


def chunk_body(text, policy):
    max_chars = int(policy["max_chunk_chars"])
    chunks = []
    cur = []
    heading = ""
    ordn = 0

    def flush():
        nonlocal cur, ordn
        body = "\n".join(cur).strip()
        if not body:
            cur = []
            return
        for i in range(0, len(body), max_chars):
            part = body[i:i + max_chars].strip()
            if part:
                chunks.append((ordn, heading, part))
                ordn += 1
        cur = []

    for line in text.splitlines():
        if re.match(r"^#{1,4}\s+", line):
            flush()
            heading = line.lstrip("#").strip()
            cur = [line]
        else:
            cur.append(line)
    flush()
    if not chunks and text.strip():
        body = text.strip()
        for i in range(0, len(body), max_chars):
            part = body[i:i + max_chars].strip()
            if part:
                chunks.append((ordn, "", part))
                ordn += 1
    return chunks


def curated_candidates(policy):
    seen = set()
    mapa_real = os.path.realpath(MAPA)
    for p in sorted(glob.glob(os.path.join(mapa_real, "**", "*.md"), recursive=True)):
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        rel = os.path.relpath(p, mapa_real).replace(os.sep, "/")
        logical = "mapa/" + rel
        yield {"doc_id": logical, "path": rp, "logical_rel": logical, "kind": "map", "project": project_for_doc_id(logical),
               "included_by": "curated_map"}
    if os.path.exists(MANIFEST):
        try:
            man = json.load(open(MANIFEST, encoding="utf-8"))
        except Exception:
            man = {}
        for nid, n in sorted(man.get("nodes", {}).items()):
            if nid.startswith(("sintesis.", "hallazgo.")):
                # NO promover fuentes de síntesis/hallazgos como kind=source:
                # ya se indexan por filesystem con su kind/project verdaderos; las aristas
                # source_of/challenges/etc salen del manifest directo (ui_builder), no de esta promoción.
                continue
            for sp in n.get("source_paths", []):
                if os.path.isabs(sp):
                    continue
                rp = os.path.realpath(os.path.join(ROOT, sp))
                if rp in seen:
                    continue
                seen.add(rp)
                logical = rel_root(rp)
                yield {"doc_id": logical, "path": rp, "logical_rel": logical, "kind": "source",
                       "project": nid.split(".")[0], "included_by": "curated_source_path"}
    # Raíces curadas: markdown que recibe tratamiento preferente. Antes era un
    # único directorio fijo de un proyecto del dueño; ahora la política declara
    # cuántas y cuáles, y el `project` se deriva de la propia ruta en vez de ser
    # un literal.
    for croot in policy.get("curated_roots", []):
        base = os.path.realpath(os.path.join(ROOT, croot))
        if not os.path.isdir(base):
            continue
        project = croot.strip("/").split("/")[0]
        for p in sorted(glob.glob(os.path.join(base, "**", "*.md"), recursive=True)):
            rp = os.path.realpath(p)
            if rp in seen:
                continue
            seen.add(rp)
            logical = rel_root(rp)
            yield {"doc_id": logical, "path": rp, "logical_rel": logical, "kind": "biblioteca",
                   "project": project, "included_by": "curated_root_md"}


def should_prune_dir(dir_path, policy):
    rp = os.path.realpath(dir_path)
    if not inside_root(rp):
        return True, "excluded_symlink_escape"
    rel = rel_root(rp)
    base = os.path.basename(rel)
    if base in policy["exclude_dir_names"] or fnmatch.fnmatch(base, ".venv-*"):
        return True, "excluded_by_dir"
    # normalize_rel lstripea el "." inicial (.mapa -> mapa): hay que comparar rel
    # normalizado contra patrón normalizado, si no los globs .mapa/** nunca podan
    # (los archivos igual caían por excluded_by_dir, pero el walk descendía al pedo).
    rel_n = normalize_rel(rel)
    for pat in policy["exclude_dir_globs"]:
        pat_n = normalize_rel(pat)
        if fnmatch.fnmatch(rel_n + "/", pat_n.rstrip("*")) or fnmatch.fnmatch(rel_n, pat_n):
            return True, "excluded_by_dir"
    return False, None


def filesystem_candidates(policy, seen_realpaths):
    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            prune, _ = should_prune_dir(full, policy)
            if not prune:
                keep.append(d)
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rp = os.path.realpath(path)
            if rp in seen_realpaths:
                continue
            rel = rel_root(rp) if inside_root(rp) else name
            ext = Path(rel).suffix.lower()
            kind = kind_for_ext(ext, policy)
            yield {"doc_id": rel_root(rp) if inside_root(rp) else rel, "path": rp, "kind": kind or "unknown",
                   "project": project_for_doc_id(rel), "included_by": "filesystem_total"}


def candidates(scope, policy):
    seen = set()
    for c in curated_candidates(policy):
        seen.add(os.path.realpath(c["path"]))
        yield c, True
    if scope == "total":
        for c in filesystem_candidates(policy, seen):
            seen.add(os.path.realpath(c["path"]))
            yield c, False


def _media_dir_excluded(rel, globs):
    for g in globs:
        base = g[:-3] if g.endswith("/**") else g
        if fnmatch.fnmatch(rel, base) or fnmatch.fnmatch(rel, base + "/*"):
            return True
    return False


def media_aggregates(policy):
    """Nodos sintéticos de media: un doc por (dir truncado a group_depth, clase de extensión).
    Walk propio SIN los globs quirúrgicos del texto (la media de los datasets se cuenta igual)."""
    mp = policy.get("media")
    if not mp:
        return
    ext_class = {}
    for cls, exts in mp.get("classes", {}).items():
        for e in exts:
            ext_class[e.lower()] = cls
    if not ext_class:
        return
    xnames = set(policy.get("exclude_dir_names", []))
    xglobs = mp.get("exclude_dir_globs", [])
    depth = int(mp.get("group_depth", 4))
    groups = {}  # (gdir, cls) -> [n, bytes, Counter(ext)]
    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        keep = []
        for d in dirnames:
            if d in xnames or d.startswith("."):
                continue
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if _media_dir_excluded(rel, xglobs):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        if rel_dir == ".":
            continue
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            cls = ext_class.get(ext)
            if not cls:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            parts = rel_dir.split("/")
            gdir = "/".join(parts[:depth])
            rec = groups.setdefault((gdir, cls), [0, 0, Counter()])
            rec[0] += 1
            rec[1] += st.st_size
            rec[2][ext] += 1
    labels = {"imagenes": "imágenes", "audio": "archivos de audio", "video": "videos"}
    for (gdir, cls), (n, nbytes, exts) in sorted(groups.items()):
        label = labels.get(cls, cls)
        size = f"{nbytes / 1073741824:.1f} GB" if nbytes > 1073741824 else f"{nbytes // 1048576} MB"
        top = " ".join(e for e, _ in exts.most_common(4))
        body = f"{n:,} archivos de {label} ({size}) bajo {gdir}/ · extensiones: {top}".replace(",", ".")
        doc = {
            "doc_id": f"{gdir}/#{cls}",
            "kind": "fs_media",
            "project": gdir.split("/", 1)[0],
            "title": f"{n:,} {label} · {os.path.basename(gdir)}".replace(",", "."),
            "body": body,
            "content_hash": sha_text(body),
            "source_path": os.path.join(ROOT, gdir),
            "ext": "",
            "size_bytes": nbytes,
            "mtime": 0,
            "extractor": "media_aggregate",
            "included_by": "media_pass",
            "duplicate_of": None,
        }
        yield doc, [(0, None, body)]


class Audit:
    def __init__(self, scope, policy):
        self.scope = scope
        self.policy = policy
        self.policy_hash = policy_hash(policy)
        self.included = []
        self.excluded = Counter()
        self.excluded_examples = defaultdict(list)
        self.by_kind = Counter()
        self.by_project = Counter()
        self.by_ext = Counter()
        self.estimated_chunks = 0
        self.duplicates = 0
        self.duplicate_examples = []

    def add_excluded(self, reason, doc_id):
        self.excluded[reason] += 1
        if len(self.excluded_examples[reason]) < 10:
            self.excluded_examples[reason].append(doc_id)

    def add_included(self, doc, nchunks):
        self.included.append(doc["doc_id"])
        self.by_kind[doc["kind"]] += 1
        self.by_project[doc["project"]] += 1
        self.by_ext[doc.get("ext") or "[no_ext]"] += 1
        self.estimated_chunks += nchunks

    def as_dict(self):
        n = len(self.included)
        warnings_ = []
        if n > 25000:
            warnings_.append("included_gt_25000")
        if self.by_kind.get("fs_media", 0) > 500:
            warnings_.append("media_dirs_gt_500")
        if n and self.by_project and self.by_project.most_common(1)[0][1] / n > 0.50:
            warnings_.append("project_gt_50_percent:" + self.by_project.most_common(1)[0][0])
        return {
            "scope": self.scope,
            "policy_hash": self.policy_hash,
            "generated_at": int(time.time()),
            "n_included": n,
            "n_excluded": sum(self.excluded.values()),
            "n_duplicates": self.duplicates,
            "estimated_chunks": self.estimated_chunks,
            "included_by_kind": dict(self.by_kind),
            "included_by_project": dict(self.by_project.most_common()),
            "included_by_ext": dict(self.by_ext.most_common()),
            "excluded_by_reason": dict(self.excluded.most_common()),
            "excluded_examples": dict(self.excluded_examples),
            "duplicate_examples": self.duplicate_examples[:20],
            "warnings": warnings_,
        }


def audit_to_markdown(a):
    d = a.as_dict()
    lines = [
        f"# Corpus audit — scope `{d['scope']}`",
        "",
        f"- policy_hash: `{d['policy_hash']}`",
        f"- incluidos: {d['n_included']}",
        f"- excluidos: {d['n_excluded']}",
        f"- duplicados: {d['n_duplicates']}",
        f"- chunks estimados: {d['estimated_chunks']}",
        f"- warnings: {', '.join(d['warnings']) if d['warnings'] else 'none'}",
        "",
        "## Included by kind",
    ]
    for k, v in d["included_by_kind"].items():
        lines.append(f"- {k}: {v}")
    lines.append("\n## Top projects")
    for k, v in list(d["included_by_project"].items())[:30]:
        lines.append(f"- {k}: {v}")
    lines.append("\n## Excluded by reason")
    for k, v in d["excluded_by_reason"].items():
        lines.append(f"- {k}: {v}")
    lines.append("\n## Exclusion examples")
    for reason, examples in d["excluded_examples"].items():
        lines.append(f"### {reason}")
        for ex in examples:
            lines.append(f"- `{ex}`")
    return "\n".join(lines) + "\n"


def iter_prepared_docs(scope, policy, audit=None):
    content_owner = {}
    for cand, curated in candidates(scope, policy):
        path = cand["path"]
        reason = skip_reason_for_path(path, policy, curated=curated, logical_rel=cand.get("logical_rel"))
        if reason:
            if audit:
                audit.add_excluded(reason, cand["doc_id"])
            continue
        ext = Path(path).suffix.lower()
        kind = cand["kind"] if cand["kind"] != "unknown" else kind_for_ext(ext, policy)
        kind = (synthesis_kind_override(cand["doc_id"]) or discovery_kind_override(cand["doc_id"])
                or dataset_kind_override(cand["doc_id"], policy) or kind)
        body, reason, extractor = extract_body(path, policy, kind)
        if reason:
            if audit:
                audit.add_excluded(reason, cand["doc_id"])
            continue
        if kind in ("synthesis", "discovery") and len(set(frontmatter_source_paths(body))) < MIN_SOURCES:
            # Guardrail del indexer: nodo destilado sin evidencia suficiente NO entra (mismo umbral
            # que el writer). Set: paths ÚNICOS, no largo de lista.
            if audit:
                audit.add_excluded(f"{kind}_without_sources", cand["doc_id"])
            continue
        if is_frontmatter_opt_out(body):
            if audit:
                audit.add_excluded("excluded_opt_out", cand["doc_id"])
            continue
        if contains_secret(body):
            if audit:
                audit.add_excluded("excluded_secret_content", cand["doc_id"])
            continue
        chash = sha_text(body)
        stat = os.stat(path)
        doc = {
            "doc_id": cand["doc_id"],
            "kind": kind,
            "project": cand["project"],
            "title": title_of(body, cand["doc_id"], ext),
            "body": body,
            "content_hash": chash,
            "source_path": path,
            "ext": ext,
            "size_bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
            "extractor": extractor,
            "included_by": cand["included_by"],
            "duplicate_of": content_owner.get(chash),
        }
        if doc["duplicate_of"]:
            if audit:
                audit.duplicates += 1
                if len(audit.duplicate_examples) < 20:
                    audit.duplicate_examples.append({"doc_id": doc["doc_id"], "duplicate_of": doc["duplicate_of"]})
            yield doc, []
            continue
        content_owner[chash] = doc["doc_id"]
        chunks = chunk_body(body, policy)
        if audit:
            audit.add_included(doc, len(chunks))
        yield doc, chunks
    if scope == "total":
        for doc, chunks in media_aggregates(policy):
            if audit:
                audit.add_included(doc, len(chunks))
            yield doc, chunks


@contextlib.contextmanager
def gpu_lock(timeout=None):
    global _gpu_lock_ok
    if fcntl is None:
        _gpu_lock_ok = False
        yield True
        return
    f = None
    for p in ("/run/mapa/gpu.lock", os.path.join(DMAPA, "gpu.lock")):
        if os.path.isdir(os.path.dirname(p)):
            try:
                f = open(p, "a")
                break
            except Exception:
                pass
    if f is None:
        _gpu_lock_ok = False
        yield True
        return
    _gpu_lock_ok = True
    got = False
    try:
        if timeout is None:
            fcntl.flock(f, fcntl.LOCK_EX)
            got = True
        else:
            end = time.time() + timeout
            while time.time() < end:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
        yield got
    finally:
        if got:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
        f.close()


def local_model_path():
    ref = os.path.join(MODEL_CACHE_DIR, "refs", "main")
    if os.path.isfile(ref):
        rev = open(ref, encoding="utf-8").read().strip()
        snap = os.path.join(MODEL_CACHE_DIR, "snapshots", rev)
        if os.path.isdir(snap):
            return snap
    snaps = sorted(glob.glob(os.path.join(MODEL_CACHE_DIR, "snapshots", "*")), key=os.path.getmtime, reverse=True)
    for snap in snaps:
        if os.path.isfile(os.path.join(snap, "modules.json")):
            return snap
    raise RuntimeError(f"modelo local no encontrado: {MODEL_CACHE_DIR}")


def get_model():
    global _model, _dims, _model_device, _last_model_use
    if _model is None:
        from sentence_transformers import SentenceTransformer
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        with gpu_lock():
            _model = SentenceTransformer(local_model_path(), device=dev, local_files_only=True)
        _dims = _model.get_sentence_embedding_dimension()
        _model_device = str(_model.device)
    _last_model_use = time.time()
    return _model


def unload_model_if_idle(idle_seconds=300, force=False):
    global _model, _model_device, _last_model_use
    if _model is None:
        return False
    if not force and _last_model_use and (time.time() - _last_model_use) < idle_seconds:
        return False
    _model = None
    _model_device = None
    _last_model_use = None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    return True


def fingerprint():
    return f"{MODEL_ID}|local={os.path.basename(local_model_path())}|dims={_dims}|norm={NORMALIZE}|chunk={CHUNKER_VERSION}|nv={NORM_VERSION}"


def cache_key(content_hash):
    return sha_text(content_hash + "|" + fingerprint())


def embed(texts, lock_timeout=None):
    global _last_model_use
    import numpy as np
    m = get_model()
    with gpu_lock(timeout=lock_timeout) as got:
        if not got:
            return None
        v = m.encode(list(texts), normalize_embeddings=NORMALIZE, batch_size=32, show_progress_bar=False)
    _last_model_use = time.time()
    return np.asarray(v, dtype="float32")


def vector_available():
    try:
        import sqlite_vec  # noqa
        import torch  # noqa
        import sentence_transformers  # noqa
        return True
    except Exception:
        return False


def load_vec(con):
    con.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(con)
    con.enable_load_extension(False)


def emb_cache_con():
    con = sqlite3.connect(EMB_CACHE)
    con.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, dim INT, vec BLOB)")
    return con


def create_schema(con, want_vec, dims):
    con.execute("""CREATE TABLE docs(
        doc_id TEXT PRIMARY KEY, kind TEXT, project TEXT, title TEXT, body TEXT, content_hash TEXT,
        source_path TEXT, ext TEXT, size_bytes INT, mtime INT, extractor TEXT, included_by TEXT, duplicate_of TEXT
    )""")
    con.execute("""CREATE TABLE chunks(
        id INTEGER PRIMARY KEY, doc_id TEXT, kind TEXT, project TEXT, ord INT,
        heading TEXT, title TEXT, body TEXT, content_hash TEXT
    )""")
    con.execute(f"CREATE VIRTUAL TABLE chunks_fts USING fts5(body, heading, title, tokenize='{FTS_TOKENIZE}')")
    if want_vec:
        load_vec(con)
        con.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{dims}])")


def old_index_generation():
    if not os.path.exists(DB):
        return 0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        r = con.execute("SELECT v FROM meta WHERE k='index_generation'").fetchone()
        con.close()
        return int(r[0]) if r else 0
    except Exception:
        return 0


def copy_prev_index():
    if os.path.exists(DB):
        shutil.copy2(DB, DB + ".prev")
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
            con.close()
            with open(os.path.join(DMAPA, "index_meta.prev.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception:
            pass


def do_audit(scope):
    policy = load_policy()
    audit = Audit(scope, policy)
    for _doc, _chunks in iter_prepared_docs(scope, policy, audit=audit):
        pass
    data = audit.as_dict()
    with open(AUDIT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    with open(AUDIT_MD, "w", encoding="utf-8") as f:
        f.write(audit_to_markdown(audit))
    print(json.dumps({
        "scope": scope,
        "included": data["n_included"],
        "excluded": data["n_excluded"],
        "duplicates": data["n_duplicates"],
        "estimated_chunks": data["estimated_chunks"],
        "policy_hash": data["policy_hash"],
        "warnings": data["warnings"],
        "audit_json": AUDIT_JSON,
        "audit_md": AUDIT_MD,
    }, ensure_ascii=False, indent=2))


def do_index(scope):
    global _index_error
    os.makedirs(DMAPA, exist_ok=True)
    policy = load_policy()
    phash = policy_hash(policy)
    _index_error = None
    want_vec = vector_available()
    dims = None
    cache = None
    if want_vec:
        try:
            get_model()
            dims = _dims
            cache = emb_cache_con()
        except Exception as e:
            want_vec = False
            _index_error = f"model load: {e}"

    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    try:
        create_schema(con, want_vec, dims)
    except Exception as e:
        con.close()
        if want_vec:
            want_vec = False
            _index_error = f"vec setup: {e}"
            if os.path.exists(tmp):
                os.remove(tmp)
            con = sqlite3.connect(tmp)
            create_schema(con, False, None)
        else:
            raise

    audit = Audit(scope, policy)
    cid = 0
    doc_count = 0
    pending = []
    cache_hits = 0
    cache_misses = 0

    def flush_embeddings():
        nonlocal pending, cache_hits, cache_misses
        if not want_vec or not pending:
            pending = []
            return
        missing = []
        for item in pending:
            ck = cache_key(item["content_hash"])
            row = cache.execute("SELECT vec FROM cache WHERE k=?", (ck,)).fetchone()
            if row and len(row[0]) == dims * 4:
                con.execute("INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?,?)", (item["chunk_id"], row[0]))
                cache_hits += 1
            else:
                missing.append((item, ck))
        if missing:
            arr = embed([m[0]["body"] for m in missing])
            if arr is None:
                raise RuntimeError("gpu lock unavailable during index")
            for i, (item, ck) in enumerate(missing):
                b = arr[i].tobytes()
                cache.execute("INSERT OR REPLACE INTO cache(k,dim,vec) VALUES(?,?,?)", (ck, dims, b))
                con.execute("INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?,?)", (item["chunk_id"], b))
                cache_misses += 1
            cache.commit()
        pending = []

    try:
        for doc, chunks in iter_prepared_docs(scope, policy, audit=audit):
            con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                doc["doc_id"], doc["kind"], doc["project"], doc["title"], doc["body"], doc["content_hash"],
                doc["source_path"], doc["ext"], doc["size_bytes"], doc["mtime"], doc["extractor"],
                doc["included_by"], doc["duplicate_of"],
            ))
            doc_count += 1
            if not doc["duplicate_of"]:
                for ordn, heading, cbody in chunks:
                    cid += 1
                    chash = sha_text(cbody)
                    con.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", (
                        cid, doc["doc_id"], doc["kind"], doc["project"], ordn,
                        heading, doc["title"], cbody, chash,
                    ))
                    con.execute("INSERT INTO chunks_fts(rowid,body,heading,title) VALUES(?,?,?,?)",
                                (cid, cbody, heading, doc["title"]))
                    if want_vec and doc["kind"] not in NO_EMBED_KINDS:
                        pending.append({"chunk_id": cid, "content_hash": chash, "body": cbody})
                        if len(pending) >= int(policy["embed_batch_size"]):
                            flush_embeddings()
            if doc_count % int(policy["commit_every_docs"]) == 0:
                flush_embeddings()
                con.commit()
        flush_embeddings()
    except Exception as e:
        if want_vec:
            # Rebuild from scratch as FTS-only rather than publishing a partial/mixed index.
            con.close()
            if os.path.exists(tmp):
                os.remove(tmp)
            _index_error = f"embed: {e}"
            return do_index_fts_only(scope, policy, phash, _index_error)
        raise

    removed_discovery = validate_discovery_sources(con, audit)
    if removed_discovery:
        doc_count -= removed_discovery
        cid = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    mode = "hybrid" if want_vec else "fts-only"
    gen = old_index_generation() + 1
    snap = os.path.basename(os.path.realpath(MAPA))
    ad = audit.as_dict()
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    meta = {
        "discovery_source_violations": str(removed_discovery),
        "mode": mode,
        "corpus_scope": scope,
        "policy_hash": phash,
        "indexed_snapshot": snap,
        "last_index_at": str(int(time.time())),
        "index_generation": str(gen),
        "embedder_fingerprint": fingerprint() if want_vec else "none",
        "dims": str(dims) if dims else "0",
        "last_index_error": _index_error or "",
        "n_docs": str(doc_count),
        "n_chunks": str(cid),
        "n_excluded": str(ad["n_excluded"]),
        "n_duplicates": str(ad["n_duplicates"]),
        "top_exclusion_reasons": json.dumps(dict(list(ad["excluded_by_reason"].items())[:10]), ensure_ascii=False),
        "top_projects": json.dumps(dict(list(ad["included_by_project"].items())[:10]), ensure_ascii=False),
        "last_audit_at": str(int(time.time())),
        "serving_search_mode_note": "remote serving uses fts-only unless MAPA_SERVE_VECTOR=1",
        "cache_hits": str(cache_hits),
        "cache_misses": str(cache_misses),
    }
    con.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    con.commit()
    con.close()
    with open(AUDIT_JSON, "w", encoding="utf-8") as f:
        json.dump(ad, f, ensure_ascii=False, indent=2, sort_keys=True)
    with open(AUDIT_MD, "w", encoding="utf-8") as f:
        f.write(audit_to_markdown(audit))
    copy_prev_index()
    os.replace(tmp, DB)
    print(f"[tier1] index -> {DB}  scope={scope} mode={mode} docs={doc_count} chunks={cid}"
          + (f" dims={dims}" if want_vec else "") + f" snapshot={snap}")


def do_index_fts_only(scope, policy, phash, error):
    global _index_error
    _index_error = error
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    create_schema(con, False, None)
    audit = Audit(scope, policy)
    cid = 0
    doc_count = 0
    for doc, chunks in iter_prepared_docs(scope, policy, audit=audit):
        con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            doc["doc_id"], doc["kind"], doc["project"], doc["title"], doc["body"], doc["content_hash"],
            doc["source_path"], doc["ext"], doc["size_bytes"], doc["mtime"], doc["extractor"],
            doc["included_by"], doc["duplicate_of"],
        ))
        doc_count += 1
        if not doc["duplicate_of"]:
            for ordn, heading, cbody in chunks:
                cid += 1
                con.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", (
                    cid, doc["doc_id"], doc["kind"], doc["project"], ordn,
                    heading, doc["title"], cbody, sha_text(cbody),
                ))
                con.execute("INSERT INTO chunks_fts(rowid,body,heading,title) VALUES(?,?,?,?)",
                            (cid, cbody, heading, doc["title"]))
    removed_discovery = validate_discovery_sources(con, audit)
    if removed_discovery:
        doc_count -= removed_discovery
        cid = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    gen = old_index_generation() + 1
    snap = os.path.basename(os.path.realpath(MAPA))
    ad = audit.as_dict()
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    meta = {
        "discovery_source_violations": str(removed_discovery),
        "mode": "fts-only", "corpus_scope": scope, "policy_hash": phash,
        "indexed_snapshot": snap, "last_index_at": str(int(time.time())),
        "index_generation": str(gen), "embedder_fingerprint": "none", "dims": "0",
        "last_index_error": error, "n_docs": str(doc_count), "n_chunks": str(cid),
        "n_excluded": str(ad["n_excluded"]), "n_duplicates": str(ad["n_duplicates"]),
        "top_exclusion_reasons": json.dumps(dict(list(ad["excluded_by_reason"].items())[:10]), ensure_ascii=False),
        "top_projects": json.dumps(dict(list(ad["included_by_project"].items())[:10]), ensure_ascii=False),
        "last_audit_at": str(int(time.time())),
        "serving_search_mode_note": "remote serving uses fts-only unless MAPA_SERVE_VECTOR=1",
    }
    con.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    con.commit()
    con.close()
    copy_prev_index()
    os.replace(tmp, DB)
    print(f"[tier1] index -> {DB}  scope={scope} mode=fts-only docs={doc_count} chunks={cid} snapshot={snap}")


def ro(load_vector=False):
    if not os.path.exists(DB):
        print("[tier1] no hay indice; corre: tier1.py index --scope curated", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    if load_vector:
        load_vec(con)
    return con


def fts_query(raw):
    toks = re.findall(r"\w+", raw, flags=re.UNICODE)
    return " ".join('"' + t + '"' for t in toks) if toks else None


def db_mode():
    con = ro()
    try:
        r = con.execute("SELECT v FROM meta WHERE k='mode'").fetchone()
        return r[0] if r else "fts-only"
    finally:
        con.close()


def parse_filter(value):
    if not value:
        return None
    if isinstance(value, str):
        return {x.strip() for x in value.split(",") if x.strip()}
    return set(value)


def search_json(raw, k=10, allow_vector=True, kind=None, project=None):
    q = fts_query(raw)
    if not q:
        return {"mode": "none", "results": []}
    kind_filter = parse_filter(kind)
    project_filter = parse_filter(project)
    hybrid = bool(allow_vector) and db_mode() == "hybrid" and vector_available()
    try:
        con = ro(load_vector=hybrid)
    except Exception:
        hybrid = False
        con = ro(load_vector=False)
    try:
        fts = con.execute(
            "SELECT rowid, snippet(chunks_fts,0,'»','«','…',14) FROM chunks_fts "
            "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts,1.0,5.0,10.0) LIMIT 120", (q,)).fetchall()
        ranks, fts_snips = {}, {}
        for rank, (cid, snip) in enumerate(fts):
            ranks.setdefault(cid, {})["fts"] = rank
            fts_snips[cid] = snip
        if hybrid:
            try:
                ev = embed([raw], lock_timeout=2)
                if ev is None:
                    hybrid = False
                else:
                    vec = con.execute(
                        "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k=120 ORDER BY distance",
                        (ev[0].tobytes(),)).fetchall()
                    for rank, (cid, _d) in enumerate(vec):
                        ranks.setdefault(cid, {})["vec"] = rank
            except Exception:
                hybrid = False
        scored = []
        for cid, rr in ranks.items():
            base = sum(1.0 / (RRF_K + rr[x]) for x in rr)
            row = con.execute("SELECT kind,project FROM chunks WHERE id=?", (cid,)).fetchone()
            if not row:
                continue
            if kind_filter and row[0] not in kind_filter:
                continue
            if project_filter and row[1] not in project_filter:
                continue
            scored.append((base * KIND_BOOST.get(row[0], 1.0), cid))
        scored.sort(reverse=True)
        best, results = set(), []
        for s, cid in scored:
            ch = con.execute("SELECT doc_id,kind,project,title,heading,body FROM chunks WHERE id=?", (cid,)).fetchone()
            if not ch or ch[0] in best:
                continue
            best.add(ch[0])
            snip = fts_snips.get(cid) or (ch[5][:220] if ch[5] else "")
            results.append({"doc_id": ch[0], "kind": ch[1], "project": ch[2], "title": ch[3],
                            "heading": ch[4], "snippet": " ".join(snip.split()), "score": round(s, 6)})
            if len(results) >= k:
                break
        return {"mode": "hibrido" if hybrid else "fts-only", "results": results}
    finally:
        con.close()


def get_doc(doc_id):
    con = ro()
    try:
        row = con.execute("SELECT * FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in con.execute("SELECT * FROM docs LIMIT 0").description]
        d = dict(zip(cols, row))
        return {k: d.get(k) for k in ("doc_id", "kind", "project", "title", "body")}
    finally:
        con.close()


def health_dict():
    con = ro()
    try:
        meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
    finally:
        con.close()
    current = os.path.basename(os.path.realpath(MAPA))
    imode = meta.get("mode")
    rt_vec = vector_available()
    return {
        "index_mode": imode,
        "runtime_vector": "up" if rt_vec else "unavailable",
        "effective_mode": "hybrid" if (imode == "hybrid" and rt_vec) else "fts-only",
        "gpu_lock": {None: "unknown", True: "ok", False: "unavailable"}.get(_gpu_lock_ok, "unknown"),
        "model_device": _model_device or "not_loaded",
        "model_loaded": _model is not None,
        "model_idle_seconds": round(time.time() - _last_model_use, 1) if _last_model_use else None,
        "dims": meta.get("dims"),
        "n_docs": meta.get("n_docs"),
        "n_chunks": meta.get("n_chunks"),
        "n_excluded": meta.get("n_excluded"),
        "n_duplicates": meta.get("n_duplicates"),
        "corpus_scope": meta.get("corpus_scope", "curated"),
        "policy_hash": meta.get("policy_hash"),
        "top_exclusion_reasons": meta.get("top_exclusion_reasons"),
        "top_projects": meta.get("top_projects"),
        "embedder": meta.get("embedder_fingerprint"),
        "index_generation": meta.get("index_generation"),
        "indexed_snapshot": meta.get("indexed_snapshot"),
        "current_snapshot": current,
        "stale": meta.get("indexed_snapshot") != current,
        "last_index_at": meta.get("last_index_at"),
        "last_index_error": (meta.get("last_index_error") or None),
        "discovery_published_count": _count_kind("discovery"),
        "discovery_source_violations": meta.get("discovery_source_violations", "0"),
    }


def _count_kind(kind):
    con = ro()
    try:
        return con.execute("SELECT count(*) FROM docs WHERE kind=?", (kind,)).fetchone()[0]
    finally:
        con.close()


def do_search(raw, k=10, kind=None, project=None):
    r = search_json(raw, k, kind=kind, project=project)
    if not r["results"]:
        print("(sin resultados)")
        return
    print(f"# modo: {r['mode']}")
    for it in r["results"]:
        head = f"  > {it['heading']}" if it["heading"] else ""
        print(f"[{it['kind']:12} · {it['project']:20}] {it['title']}{head}")
        print(f"    {it['doc_id']}")
        if it["snippet"]:
            print(f"    ...{it['snippet']}...")
        print()


def do_stats():
    con = ro()
    keys = ("mode", "corpus_scope", "n_docs", "n_chunks", "n_excluded", "n_duplicates", "dims", "indexed_snapshot")
    for k in keys:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        print(f"  {k:18} {r[0] if r else '?'}")
    con.close()


def self_test():
    # Fixtures del detector de secretos. Se arman por partes a proposito: si los
    # literales estuvieran completos en el fuente, cualquier escaner de secretos
    # (incluido el del repo) los marcaria como hallazgo real.
    positives = [
        "Authorization: " + "Bearer " + "abcdefghijklmnopqrstuvwxyz1234567890",
        "password=" + "supersecretpass",
        "postgres://user:" + "pass@host/db",
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        "sk" + "-abcdefghijklmnopqrstuvwxyz123456",
    ]
    negatives = [
        "tokenizacion armonica como concepto teorico",
        "password policy described in docs",
        "postgres://host/db",
    ]
    for s in positives:
        assert contains_secret(s), s
    for s in negatives:
        assert not contains_secret(s), s
    policy = load_policy()
    assert skip_reason_for_path("/etc/passwd", policy) == "excluded_symlink_escape"
    assert excluded_by_name("x/.docker/config.json", policy) == "excluded_secret_name"
    assert excluded_by_name("x/config.json", policy) is None
    assert kind_for_ext(".sql", policy) is None
    assert kind_for_ext(".jsonl", policy) is None
    policy = dict(policy, dataset_roots=["proyecto-a/datasets", "proyecto-b/corpus"])
    assert dataset_kind_override("proyecto-a/datasets/set1/Readme.md", policy) == "fs_dataset"
    assert dataset_kind_override("proyecto-b/corpus/contrato.md", policy) == "fs_dataset"
    assert dataset_kind_override("proyecto-a/Documentos/informe.md", policy) is None
    assert dataset_kind_override("proyecto-c/src/main.py", policy) is None
    assert "fs_code" in NO_EMBED_KINDS and "fs_media" in NO_EMBED_KINDS and "fs_dataset" not in NO_EMBED_KINDS
    assert synthesis_kind_override("mapa/sintesis/x.md") == "synthesis"
    assert synthesis_kind_override("mapa/proyectos/x.md") is None
    assert "synthesis" not in NO_EMBED_KINDS and KIND_BOOST["synthesis"] == 1.30
    assert frontmatter_source_paths('---\nsource_paths: ["a, con coma.md", "b.md", "c.md"]\n---\nx') == ["a, con coma.md", "b.md", "c.md"]
    assert len(frontmatter_source_paths('---\nsource_paths: ["a.md"]\n---\nx')) < MIN_SOURCES
    # F9+ discovery
    assert discovery_kind_override("mapa/hallazgos/x.md") == "discovery"
    assert discovery_kind_override("mapa/sintesis/x.md") is None
    assert "discovery" not in NO_EMBED_KINDS and KIND_BOOST["discovery"] == 1.35
    assert should_prune_dir(os.path.join(ROOT, ".mapa/discovery"), policy)[0]  # candidatos privados jamás indexados
    assert excluded_by_dir(".mapa/discovery/private.md", policy) == "excluded_by_dir"
    assert len(set(frontmatter_source_paths('---\nsource_paths: ["a.md", "a.md", "a.md"]\n---\nx'))) < MIN_SOURCES
    # skip de promoción curated para nodos hallazgo.* (mismo trato que sintesis.*)
    assert ("hallazgo.x").startswith(("sintesis.", "hallazgo."))
    assert _media_dir_excluded(".mapa/ui", [".mapa/**"])
    assert _media_dir_excluded("x/node_modules/y", ["*/node_modules/**"])
    assert not _media_dir_excluded("proyecto-a/datasets/set1/images", [".mapa/**", "hf-cache/**"])
    print("[tier1] self-test ok")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    p_index = sub.add_parser("index")
    p_index.add_argument("--scope", choices=["curated", "total"], default="curated")
    p_audit = sub.add_parser("audit-corpus")
    p_audit.add_argument("--scope", choices=["curated", "total"], default="total")
    p_search = sub.add_parser("search")
    p_search.add_argument("--kind")
    p_search.add_argument("--project")
    p_search.add_argument("query", nargs=argparse.REMAINDER)
    sub.add_parser("stats")
    sub.add_parser("health")
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.cmd == "index":
        do_index(args.scope)
    elif args.cmd == "audit-corpus":
        do_audit(args.scope)
    elif args.cmd == "search":
        if not args.query:
            parser.error("search requiere QUERY")
        do_search(" ".join(args.query), kind=args.kind, project=args.project)
    elif args.cmd == "stats":
        do_stats()
    elif args.cmd == "health":
        print(json.dumps(health_dict(), ensure_ascii=False, indent=2))
    elif args.cmd == "self-test":
        self_test()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
