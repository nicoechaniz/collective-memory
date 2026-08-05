#!/usr/bin/env python3
"""Build the normalized, read-only projection consumed by UI V2.

The canonical corpus remains index.db. This database contains navigation and
visualization metadata only, so it can be rebuilt without losing knowledge.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from mapa_config import DB, DMAPA, MAPA  # noqa: E402
except ImportError:  # runtime historico del host, anterior a mapa_config.py
    _ROOT_RAW = os.environ.get("MAPA_ROOT")
    if not _ROOT_RAW:
        raise RuntimeError("MAPA_ROOT is required when mapa_config is unavailable")
    _ROOT = os.path.abspath(_ROOT_RAW)
    DMAPA = os.path.abspath(os.environ.get("MAPA_DATA", os.path.join(_ROOT, ".mapa")))
    DB = os.path.join(DMAPA, "index.db")
    MAPA = os.path.join(_ROOT, "mapa")

SCHEMA_VERSION = 1
BUILDER_VERSION = "1"
CATALOG_PATH = os.path.join(DMAPA, "ui-v2-private", "catalog.json")
STOPWORDS = {
    "para", "como", "este", "esta", "estos", "estas", "desde", "sobre",
    "entre", "hacia", "donde", "cuando", "todo", "toda", "with", "from",
    "that", "this", "into", "using", "the", "and", "los", "las", "del",
    "una", "uno", "por", "con", "sin", "que", "sus", "document", "archivo",
}
PALETTE = (
    "#e76f51", "#2a9d8f", "#e9c46a", "#457b9d", "#f4a261", "#6d8f71",
    "#bc6c5c", "#8e9aaf", "#d4a373", "#5f7a8a", "#b56576", "#7f8f4e",
)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _slug(value):
    raw = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "project"


def _summary(body):
    lines = []
    in_frontmatter = False
    for line in (body or "").splitlines():
        if line.strip() == "---" and not lines:
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or line.startswith("#") or line.startswith(">"):
            continue
        clean = re.sub(r"[*_`]", "", line).strip()
        if clean:
            lines.append(clean)
        if sum(map(len, lines)) > 360:
            break
    return " ".join(lines)[:420]


def _frontmatter(body):
    match = re.match(r"^---\n(.*?)\n---", body or "", re.S)
    if not match:
        return {}
    data = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = [x.strip().strip("\"'") for x in value[1:-1].split(",") if x.strip()]
            data[key.strip()] = parsed if isinstance(parsed, list) else []
        else:
            data[key.strip()] = value.strip("\"'")
    return data


def _heading(body, fallback):
    for line in (body or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _canonical_alias(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _project_catalog(con, override):
    rows = con.execute(
        "SELECT doc_id,title,body,project FROM docs "
        "WHERE kind='map' AND doc_id GLOB 'mapa/proyectos/*.md' ORDER BY doc_id"
    ).fetchall()
    hidden = {_canonical_alias(x) for x in override.get("hidden_projects", [])}
    projects = []
    for row in rows:
        if row["doc_id"].count("/") != 2:
            continue
        fm = _frontmatter(row["body"])
        pid = str(fm.get("id") or os.path.splitext(os.path.basename(row["doc_id"]))[0])
        if _canonical_alias(pid) in hidden:
            continue
        custom = override.get("projects", {}).get(pid, {})
        aliases = [pid, fm.get("project"), row["project"], os.path.splitext(os.path.basename(row["doc_id"]))[0]]
        aliases.extend(custom.get("aliases", []))
        aliases = sorted({str(x) for x in aliases if x})
        projects.append({
            "project_id": pid,
            "title": custom.get("title") or _heading(row["body"], row["title"] or pid),
            "category": custom.get("category") or str(fm.get("type") or "other"),
            "description": custom.get("description") or _summary(row["body"]),
            "status": custom.get("status") or "active",
            "map_doc_id": row["doc_id"],
            "aliases": aliases,
        })
    return projects


def _positions(projects, category_order):
    by_category = defaultdict(list)
    for project in projects:
        by_category[project["category"]].append(project)
    categories = [c for c in category_order if c in by_category]
    categories.extend(sorted(set(by_category) - set(categories)))
    for ci, category in enumerate(categories):
        group = sorted(by_category[category], key=lambda p: p["title"].casefold())
        anchor_angle = 2 * math.pi * ci / max(1, len(categories)) - math.pi / 2
        anchor_x, anchor_y = 780 * math.cos(anchor_angle), 480 * math.sin(anchor_angle)
        for i, project in enumerate(group):
            local = 2 * math.pi * i / max(1, len(group))
            radius = 70 + 22 * math.sqrt(len(group))
            project["x"] = round(anchor_x + radius * math.cos(local), 3)
            project["y"] = round(anchor_y + radius * math.sin(local), 3)


def _label_communities(con, assignments):
    tokens = defaultdict(Counter)
    projects = defaultdict(Counter)
    batch = []
    for doc_id, cid in assignments.items():
        batch.append((doc_id, int(cid)))
        if len(batch) >= 800:
            _community_batch(con, batch, tokens, projects)
            batch.clear()
    if batch:
        _community_batch(con, batch, tokens, projects)
    return tokens, projects


def _community_batch(con, batch, tokens, projects):
    ids = [x[0] for x in batch]
    cid_by_doc = {x[0]: x[1] for x in batch}
    placeholders = ",".join("?" for _ in ids)
    for row in con.execute(f"SELECT doc_id,title,project FROM docs WHERE doc_id IN ({placeholders})", ids):
        cid = cid_by_doc[row["doc_id"]]
        projects[cid][row["project"] or "root"] += 1
        words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9_-]{2,}", row["title"] or "")
        tokens[cid].update(w.casefold() for w in words if w.casefold() not in STOPWORDS)


def _schema(out):
    out.executescript("""
    PRAGMA journal_mode=OFF;
    PRAGMA synchronous=OFF;
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    CREATE TABLE projects (
      project_id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
      description TEXT NOT NULL, status TEXT NOT NULL, map_doc_id TEXT NOT NULL,
      color TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
      doc_count INTEGER NOT NULL, unique_count INTEGER NOT NULL,
      vector_count INTEGER NOT NULL, sort_order INTEGER NOT NULL
    );
    CREATE TABLE project_aliases (alias TEXT PRIMARY KEY, project_id TEXT NOT NULL);
    CREATE TABLE docs (
      doc_id TEXT PRIMARY KEY, project_id TEXT, raw_project TEXT NOT NULL,
      title TEXT NOT NULL, kind TEXT NOT NULL, abstraction TEXT NOT NULL,
      path_rel TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime INTEGER NOT NULL,
      vectorized INTEGER NOT NULL, duplicate_of TEXT
    );
    CREATE INDEX docs_project_idx ON docs(project_id,kind,title);
    CREATE INDEX docs_path_idx ON docs(path_rel);
    CREATE TABLE communities (
      community_id INTEGER PRIMARY KEY, title TEXT NOT NULL, size INTEGER NOT NULL,
      project_mix TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL
    );
    CREATE TABLE community_docs (
      community_id INTEGER NOT NULL, doc_id TEXT NOT NULL,
      PRIMARY KEY (community_id,doc_id)
    ) WITHOUT ROWID;
    CREATE INDEX community_docs_doc_idx ON community_docs(doc_id);
    CREATE TABLE project_edges (
      source_project TEXT NOT NULL, target_project TEXT NOT NULL,
      total_weight REAL NOT NULL, evidence_count INTEGER NOT NULL,
      counts_by_type TEXT NOT NULL,
      PRIMARY KEY (source_project,target_project)
    ) WITHOUT ROWID;
    CREATE TABLE relation_evidence (
      source_project TEXT NOT NULL, target_project TEXT NOT NULL,
      source_doc TEXT NOT NULL, target_doc TEXT NOT NULL,
      edge_type TEXT NOT NULL, weight REAL NOT NULL
    );
    CREATE INDEX relation_pair_idx ON relation_evidence(source_project,target_project,weight DESC);
    CREATE TABLE graph_nodes (
      graph_name TEXT NOT NULL, node_id TEXT NOT NULL, label TEXT NOT NULL,
      kind TEXT NOT NULL, project_id TEXT, doc_id TEXT, x REAL NOT NULL, y REAL NOT NULL,
      PRIMARY KEY (graph_name,node_id)
    ) WITHOUT ROWID;
    CREATE TABLE graph_edges (
      graph_name TEXT NOT NULL, edge_id TEXT NOT NULL, source TEXT NOT NULL,
      target TEXT NOT NULL, edge_type TEXT NOT NULL, weight REAL NOT NULL,
      directed INTEGER NOT NULL, PRIMARY KEY (graph_name,edge_id)
    ) WITHOUT ROWID;
    """)


def _abstraction(kind):
    if kind == "synthesis":
        return "synthesis"
    if kind in {"map", "discovery"}:
        return "curated"
    return "raw"


def _import_graph(out, name, obj, alias_map):
    nodes = obj.get("nodes", []) if isinstance(obj, dict) else []
    edges = obj.get("edges", []) if isinstance(obj, dict) else []
    valid = set()
    for node in nodes:
        raw_project = node.get("project")
        project_id = alias_map.get(_canonical_alias(raw_project))
        if raw_project and not project_id and name != "global":
            continue
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        valid.add(node_id)
        out.execute(
            "INSERT OR IGNORE INTO graph_nodes VALUES (?,?,?,?,?,?,?,?)",
            (name, node_id, str(node.get("label") or node_id)[:240], str(node.get("kind") or "unknown"),
             project_id, node.get("doc_id"), float(node.get("x") or 0), float(node.get("y") or 0)),
        )
    for edge in edges:
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if source not in valid or target not in valid:
            continue
        out.execute(
            "INSERT OR IGNORE INTO graph_edges VALUES (?,?,?,?,?,?,?)",
            (name, str(edge.get("id") or hashlib.sha256(f"{name}|{source}|{target}".encode()).hexdigest()[:20]),
             source, target, str(edge.get("type") or "relation"), float(edge.get("weight") or 0),
             1 if edge.get("directed") else 0),
        )


def build(ui_root=None, output_path=None):
    ui_root = os.path.realpath(ui_root or os.path.join(DMAPA, "ui"))
    output_path = output_path or os.path.join(ui_root, "ui_v2.db")
    tmp = output_path + f".tmp.{os.getpid()}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass

    override = _read_json(CATALOG_PATH, {})
    source = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    index_meta = dict(source.execute("SELECT k,v FROM meta"))
    manifest = _read_json(os.path.join(ui_root, "manifest.json"), {})
    stats = _read_json(os.path.join(ui_root, "stats.json"), {})
    communities_obj = _read_json(os.path.join(ui_root, "communities.json"), {})
    semantic_root = ui_root
    semantic_source = "current_generation" if communities_obj.get("communities") else "unavailable"
    if not communities_obj.get("communities"):
        previous_root = os.path.realpath(os.path.join(DMAPA, "ui"))
        previous_manifest = _read_json(os.path.join(previous_root, "manifest.json"), {})
        previous_communities = _read_json(os.path.join(previous_root, "communities.json"), {})
        if (previous_root != ui_root
                and previous_manifest.get("index_generation") == index_meta.get("index_generation")
                and previous_communities.get("index_generation") == index_meta.get("index_generation")
                and previous_communities.get("communities")):
            communities_obj = previous_communities
            semantic_root = previous_root
            semantic_source = "reused_same_index_generation"
    projects = _project_catalog(source, override)
    category_order = override.get("category_order", [])
    _positions(projects, category_order)

    alias_map = {}
    for project in projects:
        for alias in project["aliases"]:
            alias_map.setdefault(_canonical_alias(alias), project["project_id"])

    # vec0 guarda los rowids publicados en una tabla sombra consultable sin
    # cargar sqlite-vec. Asi el builder visual no depende de CUDA ni del modelo.
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    vectorized = (
        {
            row[0] for row in source.execute(
                "SELECT DISTINCT c.doc_id FROM chunks c "
                "JOIN chunks_vec_rowids v ON v.rowid=c.id"
            )
        }
        if "chunks_vec_rowids" in tables
        else set()
    )
    project_counts = defaultdict(lambda: [0, 0, 0])
    docs_rows = []
    for row in source.execute(
        "SELECT doc_id,project,title,kind,size_bytes,mtime,duplicate_of FROM docs ORDER BY doc_id"
    ):
        pid = alias_map.get(_canonical_alias(row["project"]))
        vals = project_counts[pid]
        vals[0] += 1
        if not row["duplicate_of"]:
            vals[1] += 1
        if row["doc_id"] in vectorized:
            vals[2] += 1
        docs_rows.append((row["doc_id"], pid, row["project"] or "root", row["title"] or row["doc_id"],
                          row["kind"] or "unknown", _abstraction(row["kind"]), row["doc_id"],
                          int(row["size_bytes"] or 0), int(row["mtime"] or 0),
                          1 if row["doc_id"] in vectorized else 0, row["duplicate_of"]))

    out = sqlite3.connect(tmp)
    _schema(out)
    out.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?)", docs_rows)
    for order, project in enumerate(projects):
        counts = project_counts[project["project_id"]]
        color = PALETTE[order % len(PALETTE)]
        out.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            project["project_id"], project["title"], project["category"], project["description"],
            project["status"], project["map_doc_id"], color, project["x"], project["y"],
            counts[0], counts[1], counts[2], order,
        ))
        for alias in project["aliases"]:
            out.execute("INSERT OR IGNORE INTO project_aliases VALUES (?,?)",
                        (_canonical_alias(alias), project["project_id"]))

    assignments = communities_obj.get("communities", {}) if isinstance(communities_obj, dict) else {}
    if assignments:
        tokens, mixes = _label_communities(source, assignments)
        sizes = Counter(int(cid) for cid in assignments.values())
        n = max(1, len(sizes))
        for i, (cid, size) in enumerate(sorted(sizes.items(), key=lambda x: (-x[1], x[0]))):
            title = " · ".join(word for word, _ in tokens[cid].most_common(3)) or f"Comunidad {cid}"
            angle = 2 * math.pi * i / n - math.pi / 2
            radius = 260 + 34 * math.sqrt(i)
            out.execute("INSERT INTO communities VALUES (?,?,?,?,?,?)", (
                cid, title, size, _json(mixes[cid].most_common(12)),
                round(radius * math.cos(angle), 3), round(radius * math.sin(angle), 3),
            ))
        out.executemany("INSERT OR IGNORE INTO community_docs VALUES (?,?)",
                        ((int(cid), doc_id) for doc_id, cid in assignments.items()))

    pair_types = defaultdict(Counter)
    pair_weight = Counter()
    pair_evidence = Counter()
    cross = _read_json(os.path.join(semantic_root, "edges_cross.json"), {})
    for edge in cross.get("edges", []) if isinstance(cross, dict) else []:
        source_pid = alias_map.get(_canonical_alias(edge.get("source_project")))
        target_pid = alias_map.get(_canonical_alias(edge.get("target_project")))
        if not source_pid or not target_pid or source_pid == target_pid:
            continue
        pair = tuple(sorted((source_pid, target_pid)))
        etype = str(edge.get("type") or "relation")
        weight = float(edge.get("weight") or 0)
        pair_types[pair][etype] += 1
        pair_weight[pair] += weight
        pair_evidence[pair] += 1
        out.execute("INSERT INTO relation_evidence VALUES (?,?,?,?,?,?)", (
            pair[0], pair[1], str(edge.get("source") or "").removeprefix("doc:"),
            str(edge.get("target") or "").removeprefix("doc:"), etype, weight,
        ))
    for pair in sorted(pair_evidence):
        out.execute("INSERT INTO project_edges VALUES (?,?,?,?,?)", (
            pair[0], pair[1], float(pair_weight[pair]), pair_evidence[pair], _json(pair_types[pair]),
        ))

    _import_graph(out, "global", _read_json(os.path.join(semantic_root, "graph_global.json"), {}), alias_map)
    by_scope = {p.get("project"): p.get("graph_file") for p in manifest.get("projects", [])}
    for raw_scope, graph_file in by_scope.items():
        pid = alias_map.get(_canonical_alias(raw_scope))
        if pid and graph_file:
            _import_graph(out, f"project:{pid}", _read_json(os.path.join(semantic_root, graph_file), {}), alias_map)

    total_docs = int(source.execute("SELECT count(*) FROM docs").fetchone()[0])
    unique_docs = int(source.execute("SELECT count(*) FROM docs WHERE duplicate_of IS NULL").fetchone()[0])
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "built_at": int(time.time()),
        "generation": manifest.get("generation"),
        "index_generation": index_meta.get("index_generation"),
        "ui_index_generation": manifest.get("index_generation"),
        "semantic_generation": communities_obj.get("index_generation"),
        "semantic_available": bool(assignments),
        "semantic_fresh": bool(assignments and communities_obj.get("index_generation") == index_meta.get("index_generation")),
        "semantic_source": semantic_source,
        "total_docs": total_docs,
        "unique_docs": unique_docs,
        "vectorized_docs": len(vectorized),
        "curated_projects": len(projects),
        "raw_scopes": int(source.execute("SELECT count(DISTINCT project) FROM docs").fetchone()[0]),
        "legacy_global_nodes": stats.get("counts", {}).get("global_nodes", 0),
        "legacy_global_edges": stats.get("counts", {}).get("global_edges", 0),
    }
    out.executemany("INSERT INTO meta VALUES (?,?)", ((key, _json(value)) for key, value in metadata.items()))
    out.execute("PRAGMA optimize")
    out.commit()
    integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
    out.close()
    source.close()
    if integrity != "ok":
        raise RuntimeError(f"ui_v2.db integrity_check: {integrity}")
    os.chmod(tmp, 0o644)
    os.replace(tmp, output_path)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-root")
    parser.add_argument("--output")
    args = parser.parse_args()
    print(json.dumps(build(args.ui_root, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
