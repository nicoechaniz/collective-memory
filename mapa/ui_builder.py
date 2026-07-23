#!/usr/bin/env python3
"""Build read-only visual projections for the memory atlas."""
import argparse
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import (ROOT, DMAPA, DB, CODE_HOME)  # noqa: E402
MANIFEST = os.path.join(DMAPA, "manifest.json")
MAPA = os.path.join(ROOT, "mapa")
AUDIT_JSON = os.path.join(DMAPA, "corpus_audit.json")
UI_LINK = os.path.join(DMAPA, "ui")
UI_STATUS = os.path.join(DMAPA, "ui_status.json")
WEB_DIR = os.path.join(DMAPA, "web")
LAYOUT_SCRIPT = os.path.join(WEB_DIR, "layout.mjs")

SCHEMA_VERSION = 1
UI_BUILDER_VERSION = "2"
KEEP_GENERATIONS = 3
GLOBAL_NODE_CAP = 1500
GLOBAL_EDGE_CAP = 5000
PROJECT_NODE_CAP = 2500
PROJECT_EDGE_CAP = 8000
NEIGHBOR_NODE_CAP = 300
NEIGHBOR_EDGE_CAP = 800
CROSS_EDGE_CAP = 20000
MACRO_EDGE_CAP = 2000

KIND_PRIORITY = {
    "map": 0,
    "discovery": 1,
    "synthesis": 1,
    "biblioteca": 1,
    "source": 2,
    "fs_doc": 3,
    "fs_pdf": 4,
    "fs_notebook": 5,
    "fs_dataset": 6,
    "fs_media": 7,
    "fs_code": 8,
    "fs_config": 9,
}
CURATED_KINDS = {"map", "biblioteca", "source"}
DEFAULT_GRAPH_KINDS = {"map", "discovery", "synthesis", "biblioteca", "source", "fs_doc", "fs_pdf", "fs_notebook", "fs_dataset", "fs_media"}
# Aristas de la capa discovery (F9): hallazgo -> fuentes/contra/targets, desde el manifest.
DISCOVERY_EDGE_KEYS = (("source_paths", "source_of"), ("counter_paths", "challenges"),
                       ("bridge_paths", "bridges"), ("target_paths", "flags_freshness"))
NO_SEMANTIC_KINDS = {"fs_config", "fs_code", "fs_media"}  # solo topológicos: sin vecinos semánticos


def now():
    return int(time.time())


def sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def short_hash(s, n=10):
    return sha(s)[:n]


def json_dump(path, obj, gzip_copy=False):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    if gzip_copy:
        with open(path, "rb") as src, gzip.open(path + ".gz", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def ensure_rel(path):
    if not path:
        return ""
    path = path.replace("\\", "/")
    if path.startswith(ROOT + "/"):
        path = path[len(ROOT) + 1:]
    return path.lstrip("/")


def node_id_for_doc(doc_id):
    return "doc:" + doc_id


def node_id_for_project(project):
    return "project:" + project


def node_id_for_folder(project, path_rel):
    return "folder:" + project + "/" + path_rel.strip("/")


def slugify(value, used):
    base = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower() or "project"
    slug = base
    if slug in used and used[slug] != value:
        slug = f"{base}-{short_hash(value, 6)}"
    used[slug] = value
    return slug


def parse_frontmatter(body):
    m = re.match(r"^---\n(.*?)\n---", body or "", re.S)
    if not m:
        return {}
    data = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            # JSON primero (doc_ids reales contienen comas); fallback split legacy.
            try:
                parsed = json.loads(v)
                data[k] = [str(x) for x in parsed] if isinstance(parsed, list) else []
            except Exception:
                data[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        else:
            data[k] = v.strip("\"'")
    return data


def title_from_body(body, fallback):
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def edge_id(source, target, etype):
    return f"{etype}:{short_hash(source + '|' + target, 16)}"


def base_meta(meta):
    return {
        "schema_version": SCHEMA_VERSION,
        "ui_builder_version": UI_BUILDER_VERSION,
        "generation": meta["generation"],
        "indexed_snapshot": meta.get("indexed_snapshot"),
        "index_generation": meta.get("index_generation"),
        "built_at": meta["built_at"],
        "truncated": False,
    }


def connect_db(load_vec=False):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    if load_vec:
        try:
            con.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(con)
            con.enable_load_extension(False)
        except Exception:
            con.close()
            raise
    return con


def load_index():
    con = connect_db()
    meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
    docs = {}
    rows = con.execute(
        "SELECT doc_id,kind,project,title,body,content_hash,ext,size_bytes,mtime,included_by,duplicate_of "
        "FROM docs ORDER BY doc_id"
    ).fetchall()
    for r in rows:
        doc_id = r["doc_id"]
        docs[doc_id] = {
            "doc_id": doc_id,
            "id": node_id_for_doc(doc_id),
            "kind": r["kind"] or "unknown",
            "project": r["project"] or "root",
            "title": r["title"] or doc_id.rsplit("/", 1)[-1],
            "body": r["body"] or "",
            "content_hash": r["content_hash"] or "",
            "path_rel": ensure_rel(doc_id),
            "size": int(r["size_bytes"] or 0),
            "mtime": int(r["mtime"] or 0),
            "included_by": r["included_by"] or "",
            "duplicate_of": r["duplicate_of"],
            "curated": (r["kind"] in CURATED_KINDS),
        }
    con.close()
    return meta, docs


def make_doc_node(doc, degree=0, x=0.0, y=0.0):
    return {
        "id": doc["id"],
        "label": doc["title"][:120],
        "kind": doc["kind"],
        "project": doc["project"],
        "title": doc["title"],
        "doc_id": doc["doc_id"],
        "path_rel": doc["path_rel"],
        "size": doc["size"],
        "degree": degree,
        "curated": bool(doc["curated"]),
        "duplicate_of": doc["duplicate_of"],
        "x": x,
        "y": y,
    }


def make_project_node(project, count=0):
    return {
        "id": node_id_for_project(project),
        "label": project,
        "kind": "project",
        "project": project,
        "title": project,
        "doc_id": None,
        "path_rel": project,
        "size": 0,
        "degree": count,
        "curated": False,
        "duplicate_of": None,
        "x": 0.0,
        "y": 0.0,
    }


def add_edge(edges, source, target, etype, weight=1.0, directed=True):
    if source == target:
        return
    eid = edge_id(source, target, etype)
    if eid not in edges:
        edges[eid] = {
            "id": eid,
            "source": source,
            "target": target,
            "type": etype,
            "weight": float(weight),
            "directed": bool(directed),
        }


def alias_candidates(doc):
    body = doc["body"]
    fm = parse_frontmatter(body)
    aliases = set()
    for k in ("id",):
        if fm.get(k):
            aliases.add(str(fm[k]).strip())
    val = fm.get("aliases")
    if isinstance(val, list):
        aliases.update([x for x in val if x])
    path_no_ext = re.sub(r"\.[^.]+$", "", doc["doc_id"])
    aliases.add(path_no_ext)
    aliases.add(path_no_ext.replace("/", "."))
    aliases.add(os.path.basename(path_no_ext))
    h1 = title_from_body(body, "")
    if h1:
        aliases.add(h1)
    return {a.strip() for a in aliases if a and a.strip()}


def build_alias_index(docs):
    exact = defaultdict(list)
    lower = defaultdict(list)
    for doc in docs.values():
        if doc["duplicate_of"]:
            continue
        for a in alias_candidates(doc):
            exact[a].append(doc["doc_id"])
            lower[a.lower()].append(doc["doc_id"])
    return exact, lower


def resolve_wikilink(raw, exact, lower):
    target = raw.split("#", 1)[0].split("|", 1)[0].strip()
    if not target:
        return None, "unresolved"
    for key in (target, target.replace("/", ".")):
        hits = exact.get(key, [])
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "ambiguous"
    hits = lower.get(target.lower(), [])
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "unresolved"


def explicit_edges(docs, selected_ids, stats):
    edges = {}
    exact, lower = build_alias_index(docs)
    selected_doc_ids = {nid[4:] for nid in selected_ids if nid.startswith("doc:")}
    wikire = re.compile(r"\[\[([^\]]+)\]\]")
    for doc_id in sorted(selected_doc_ids):
        doc = docs.get(doc_id)
        if not doc:
            continue
        for raw in wikire.findall(doc["body"]):
            target_doc_id, err = resolve_wikilink(raw, exact, lower)
            if err == "unresolved":
                stats["wikilink_unresolved"].append({"from": doc_id, "target": raw})
                continue
            if err == "ambiguous":
                stats["wikilink_ambiguous"].append({"from": doc_id, "target": raw})
                continue
            if target_doc_id and target_doc_id in selected_doc_ids:
                add_edge(edges, node_id_for_doc(doc_id), node_id_for_doc(target_doc_id), "wikilink", 2.0, True)
    manifest = read_json(MANIFEST, {"nodes": {}})
    by_doc_id = {d["doc_id"]: d for d in docs.values()}
    for _nid, node in sorted(manifest.get("nodes", {}).items()):
        logical = ensure_rel(node.get("logical_path", ""))
        if logical.startswith("mapa/") and logical in by_doc_id:
            source_node = node_id_for_doc(logical)
        else:
            continue
        if source_node not in selected_ids:
            continue
        for key, etype in DISCOVERY_EDGE_KEYS:
            for sp in node.get(key, []) or []:
                target_doc_id = ensure_rel(sp)
                if target_doc_id in by_doc_id and node_id_for_doc(target_doc_id) in selected_ids:
                    add_edge(edges, source_node, node_id_for_doc(target_doc_id), etype, 1.5, True)
                elif key == "source_paths":
                    stats["missing_source_paths"].append({"from": logical, "source_path": target_doc_id})
                else:
                    stats["missing_source_paths"].append({"from": logical, "source_path": target_doc_id, "role": key})
    return edges


def project_groups(docs):
    groups = defaultdict(list)
    for doc in docs.values():
        if doc["duplicate_of"]:
            continue
        groups[doc["project"]].append(doc)
    for project in groups:
        groups[project].sort(key=lambda d: (KIND_PRIORITY.get(d["kind"], 50), d["doc_id"]))
    return dict(sorted(groups.items()))


def select_global_docs(groups):
    selected = []
    seen = set()
    for project, items in groups.items():
        picked = 0
        for d in items:
            if d["kind"] not in DEFAULT_GRAPH_KINDS:
                continue
            selected.append(d)
            seen.add(d["doc_id"])
            picked += 1
            if picked >= 20:
                break
    rest = []
    for items in groups.values():
        for d in items:
            if d["doc_id"] in seen:
                continue
            if d["kind"] not in DEFAULT_GRAPH_KINDS:
                continue
            rest.append(d)
    rest.sort(key=lambda d: (KIND_PRIORITY.get(d["kind"], 50), -int(d["curated"]), d["project"], d["doc_id"]))
    for d in rest:
        if len(selected) >= GLOBAL_NODE_CAP - len(groups):
            break
        selected.append(d)
    # Los agregados de media son pocos (~100) y mapean el territorio: entran siempre al global.
    for items in groups.values():
        for d in items:
            if d["kind"] == "fs_media" and d["doc_id"] not in seen:
                selected.append(d)
                seen.add(d["doc_id"])
    return selected


def graph_counts(nodes, edges):
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "by_kind": dict(Counter(n["kind"] for n in nodes).most_common()),
        "by_project": dict(Counter(n["project"] for n in nodes if n.get("project")).most_common()),
        "by_edge_type": dict(Counter(e["type"] for e in edges).most_common()),
    }


def add_membership(nodes, edges, docs):
    projects = Counter(d["project"] for d in docs)
    for project, count in sorted(projects.items()):
        pid = node_id_for_project(project)
        nodes[pid] = make_project_node(project, count)
    for d in docs:
        did = node_id_for_doc(d["doc_id"])
        nodes[did] = make_doc_node(d)
        add_edge(edges, node_id_for_project(d["project"]), did, "contains", 0.25, True)


def cap_edges(edges, cap, project_scope=False):
    if project_scope:
        # membresía > similitud: en grafos por-proyecto los contains sobreviven antes que semantic
        priority = {"wikilink": 0, "source_of": 1, "contains": 2, "semantic_neighbor": 3, "duplicate_of": 4, "belongs_to": 5}
    else:
        priority = {"wikilink": 0, "source_of": 1, "semantic_neighbor": 2, "duplicate_of": 3, "contains": 4, "belongs_to": 5}
    ordered = sorted(edges.values(), key=lambda e: (priority.get(e["type"], 50), -e["weight"], e["id"]))
    return ordered[:cap], len(ordered) > cap


def fallback_layout(nodes):
    ordered = sorted(nodes.values(), key=lambda n: n["id"])
    n = max(1, len(ordered))
    for i, node in enumerate(ordered):
        h = int(short_hash(node["id"], 8), 16)
        r = 10 + (h % 1000) / 40.0
        a = (2 * math.pi * i) / n
        node["x"] = round(math.cos(a) * r, 6)
        node["y"] = round(math.sin(a) * r, 6)


def run_layout(stage, graph_rel):
    graph_path = os.path.join(stage, graph_rel)
    if not os.path.isfile(LAYOUT_SCRIPT):
        return False
    try:
        cp = subprocess.run(["node", LAYOUT_SCRIPT, graph_path, graph_path], cwd=WEB_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False)
        return cp.returncode == 0
    except Exception:
        return False


def write_graph(stage, rel, meta, nodes, edges, cap, stats, keep_all_nodes=False, project_scope=False):
    edge_list, edges_truncated = cap_edges(edges, cap, project_scope=project_scope)
    if keep_all_nodes:
        node_list = [nodes[nid] for nid in sorted(nodes)]
    else:
        used = {e["source"] for e in edge_list} | {e["target"] for e in edge_list}
        for nid in list(nodes):
            if nodes[nid]["kind"] in ("project", "fs_media"):  # media: pocos agregados, mapean territorio
                used.add(nid)
        node_list = [nodes[nid] for nid in sorted(nodes) if nid in used]
    if not all(("x" in n and "y" in n) for n in node_list):
        fallback_layout({n["id"]: n for n in node_list})
    obj = {
        **base_meta(meta),
        "truncated": edges_truncated or len(nodes) > len(node_list),
        "nodes": node_list,
        "edges": edge_list,
        "counts": graph_counts(node_list, edge_list),
    }
    path = os.path.join(stage, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json_dump(path, obj, gzip_copy=True)
    if run_layout(stage, rel):
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        json_dump(path, obj, gzip_copy=True)
    stats["artifact_sizes"][rel] = {"bytes": os.path.getsize(path), "gzip_bytes": os.path.getsize(path + ".gz")}
    return obj


def build_tree(stage, meta, docs, audit, stats):
    nodes = {}
    roots = []
    for doc in docs.values():
        project = doc["project"]
        pid = node_id_for_project(project)
        if pid not in nodes:
            nodes[pid] = {"id": pid, "label": project, "path_rel": project, "kind": "project",
                          "project": project, "parent": None, "doc_id": None, "child_count": 0, "doc_count": 0}
            roots.append(pid)
        parts = doc["doc_id"].split("/")[:-1]
        parent = pid
        accum = []
        for part in parts:
            accum.append(part)
            fid = node_id_for_folder(project, "/".join(accum))
            if fid not in nodes:
                nodes[fid] = {"id": fid, "label": part, "path_rel": "/".join(accum), "kind": "folder",
                              "project": project, "parent": parent, "doc_id": None, "child_count": 0, "doc_count": 0}
                nodes[parent]["child_count"] += 1
            parent = fid
        did = node_id_for_doc(doc["doc_id"])
        nodes[did] = {"id": did, "label": doc["title"], "path_rel": doc["path_rel"], "kind": doc["kind"],
                      "project": project, "parent": parent, "doc_id": doc["doc_id"], "child_count": 0, "doc_count": 1}
        nodes[parent]["child_count"] += 1
        cur = parent
        while cur:
            nodes[cur]["doc_count"] += 1
            cur = nodes[cur]["parent"]
    excluded_summary = {}
    stale = True
    if audit and audit.get("policy_hash") == meta.get("policy_hash"):
        excluded_summary = audit.get("excluded_by_reason", {})
        stale = False
    obj = {
        **base_meta(meta),
        "nodes": [nodes[k] for k in sorted(nodes)],
        "roots": sorted(roots),
        "excluded_summary": excluded_summary,
        "excluded_summary_stale": stale,
    }
    json_dump(os.path.join(stage, "tree.json"), obj, gzip_copy=True)
    stats["artifact_sizes"]["tree.json"] = {
        "bytes": os.path.getsize(os.path.join(stage, "tree.json")),
        "gzip_bytes": os.path.getsize(os.path.join(stage, "tree.json.gz")),
    }


# F21: partición Leiden persistente sobre el grafo knn doc-level.
COMMUNITY_SEED = 42
COMMUNITY_K = 16
COMMUNITY_KEEP = 8


def _empty_semantic_snapshot():
    # Contrato F21: dict tipado, directed_edges es dict edge_id→edge (no lista).
    return {"semantic_ok": False, "doc_ids": [], "directed_edges": {}}


def maybe_build_semantic(stage, meta, docs, stats):
    if meta.get("mode") != "hybrid":
        stats["warnings"].append("semantic_neighbors_unavailable:index_not_hybrid")
        return _empty_semantic_snapshot()
    try:
        con = connect_db(load_vec=True)
        con.execute("SELECT 1 FROM chunks_vec LIMIT 1").fetchone()
    except Exception as e:
        stats["warnings"].append("semantic_neighbors_unavailable:" + str(e)[:120])
        return _empty_semantic_snapshot()
    out_path = os.path.join(stage, "doc_vectors.db")
    try:
        import numpy as np
        import sqlite_vec
        if os.path.exists(out_path):
            os.remove(out_path)
        out = sqlite3.connect(out_path)
        out.enable_load_extension(True)
        sqlite_vec.load(out)
        out.enable_load_extension(False)
        out.execute("CREATE TABLE doc_vec_map(rowid INTEGER PRIMARY KEY, doc_id TEXT UNIQUE, content_hash TEXT)")
        out.execute("CREATE VIRTUAL TABLE doc_vec USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[1024])")
        eligible = {d["doc_id"]: d for d in docs.values() if not d["duplicate_of"] and d["kind"] not in NO_SEMANTIC_KINDS}
        rows = con.execute(
            "SELECT c.doc_id, c.content_hash, v.embedding "
            "FROM chunks c JOIN chunks_vec v ON c.id=v.chunk_id "
            "ORDER BY c.doc_id"
        )
        current = None
        acc = None
        count = 0
        rowid = 0
        inserted = []

        def flush():
            nonlocal rowid, current, acc, count
            if current is None or acc is None or count == 0 or current not in eligible:
                current = None; acc = None; count = 0
                return
            vec = acc / max(1, count)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            rowid += 1
            b = np.asarray(vec, dtype="float32").tobytes()
            out.execute("INSERT INTO doc_vec_map(rowid,doc_id,content_hash) VALUES(?,?,?)",
                        (rowid, current, eligible[current]["content_hash"]))
            out.execute("INSERT INTO doc_vec(rowid,embedding) VALUES(?,?)", (rowid, b))
            inserted.append((rowid, current, b))
            current = None; acc = None; count = 0

        for r in rows:
            doc_id = r["doc_id"]
            if doc_id not in eligible:
                continue
            if current is not None and doc_id != current:
                flush()
            if current is None:
                current = doc_id
                acc = None
                count = 0
            arr = np.frombuffer(r["embedding"], dtype="float32")
            if arr.size != 1024:
                continue
            acc = arr.astype("float64") if acc is None else acc + arr
            count += 1
        flush()
        out.commit()
        rowid_to_doc = {rid: did for rid, did, _b in inserted}
        edges = {}
        for rowid, doc_id, blob in inserted:
            hits = out.execute("SELECT rowid, distance FROM doc_vec WHERE embedding MATCH ? AND k=?",
                               (blob, COMMUNITY_K)).fetchall()
            # Desempate determinista: ordenar por (distance, doc_id) antes del corte keep=8.
            resolved = []
            for other_rowid, distance in hits:
                if other_rowid == rowid:
                    continue
                other = rowid_to_doc.get(other_rowid)
                if other is None or other == doc_id:
                    continue
                resolved.append((float(distance), other))
            resolved.sort(key=lambda t: (t[0], t[1]))
            for distance, other in resolved[:COMMUNITY_KEEP]:
                add_edge(edges, node_id_for_doc(doc_id), node_id_for_doc(other),
                         "semantic_neighbor", 1.0 / (1.0 + distance), True)
        out.close()
        con.close()
        os.chmod(out_path, 0o644)
        doc_ids = [did for _rid, did, _b in inserted]
        stats["semantic_neighbors"] = {"docs": len(inserted), "edges": len(edges)}
        return {"semantic_ok": True, "doc_ids": doc_ids, "directed_edges": edges}
    except Exception as e:
        stats["warnings"].append("semantic_neighbors_unavailable:" + str(e)[:160])
        try:
            con.close()
        except Exception:
            pass
        try:
            out.close()   # NameError si nunca se creó: lo traga el except
        except Exception:
            pass
        # Borrar el doc_vectors.db parcial: un DB a medio construir jamás debe
        # confundirse con un universo legítimamente vacío (guard de communities).
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return _empty_semantic_snapshot()


def _canonical_json(obj):
    # Serialización canónica estable para el partition_hash (misma en builder y loader del minero).
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def community_partition_hash(core):
    """sha256 de los 6 campos núcleo de la partición (EXCLUYE ui_generation, timestampeada)."""
    return hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()


def compute_partition(n_vertices, idx_edges, weights, seed):
    """Partición de comunidades: Leiden (igraph+leidenalg) con fallback Louvain (networkx,
    ya instalado) si las libs no están. Devuelve (membership, degrees, algorithm)."""
    try:
        import igraph as ig
        import leidenalg
        g = ig.Graph(n=n_vertices, edges=idx_edges)
        part = leidenalg.find_partition(g, leidenalg.ModularityVertexPartition,
                                        weights=weights, seed=seed)
        return list(part.membership), list(g.degree()), "leiden"
    except ImportError:
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(range(n_vertices))
        for (a, b), w in zip(idx_edges, weights):
            g.add_edge(a, b, weight=w)
        comms = nx.community.louvain_communities(g, weight="weight", seed=seed)
        membership = [0] * n_vertices
        for ci, nodes in enumerate(comms):
            for v in nodes:
                membership[v] = ci
        return membership, [g.degree(v) for v in range(n_vertices)], "louvain"


def maybe_build_communities(stage, snapshot, ui_generation, index_generation, stats):
    """F21: detección de comunidades (Leiden) sobre el grafo knn doc-level colapsado.
    Emite communities.json en el dir de generación (viaja en el rename atómico) SOLO si la
    construcción semántica fue OK y el doc_vectors.db coincide con la membresía. Fail-open:
    ante cualquier problema no emite nada y el Atlas cae al Louvain client-side."""
    if not snapshot.get("semantic_ok"):
        stats["warnings"].append("communities_skipped:semantic_not_ok")
        return
    doc_ids = list(snapshot.get("doc_ids") or [])
    directed = snapshot.get("directed_edges") or {}
    dv_path = os.path.join(stage, "doc_vectors.db")
    if not os.path.exists(dv_path):
        stats["warnings"].append("communities_skipped:no_doc_vectors_db")
        return
    try:
        dv = sqlite3.connect(f"file:{dv_path}?mode=ro", uri=True)
        db_ids = [r[0] for r in dv.execute("SELECT doc_id FROM doc_vec_map").fetchall()]
        dv.close()
    except Exception as e:
        stats["warnings"].append("communities_skipped:doc_vec_map_read:" + str(e)[:80])
        return
    if set(db_ids) != set(doc_ids):
        stats["warnings"].append("communities_skipped:membership_mismatch")
        return
    if not doc_ids:
        stats["warnings"].append("communities_skipped:empty_universe")
        return
    try:
        # 1. Normalizar espacio de IDs (doc:<id> → <id>) + colapsar a grafo no dirigido.
        id_set = set(doc_ids)
        undirected = {}  # (a,b) con a<b → peso (max de las dos direcciones)
        for e in directed.values():
            s = e["source"]; t = e["target"]
            s = s[4:] if s.startswith("doc:") else s
            t = t[4:] if t.startswith("doc:") else t
            if s not in id_set or t not in id_set or s == t:
                continue
            a, b = (s, t) if s < t else (t, s)
            w = float(e["weight"])
            if (a, b) not in undirected or w > undirected[(a, b)]:
                undirected[(a, b)] = w
        # 2. Grafo con vértices ordenados por doc_id y aristas ordenadas → partición.
        ordered = sorted(doc_ids)
        idx = {d: i for i, d in enumerate(ordered)}
        edge_pairs = sorted(undirected.keys())
        idx_edges = [(idx[a], idx[b]) for (a, b) in edge_pairs]
        weights = [undirected[p] for p in edge_pairs]
        membership, deg, algorithm = compute_partition(len(ordered), idx_edges, weights, COMMUNITY_SEED)
        # 3. Singletons/aislados → -1 ("sin barrio").
        raw = {d: (membership[i] if deg[i] > 0 else -1) for i, d in enumerate(ordered)}
        # 4. Renumeración canónica: comunidades ordenadas por su menor doc_id.
        first_doc = {}
        for d in ordered:  # ascendente
            c = raw[d]
            if c != -1 and c not in first_doc:
                first_doc[c] = d
        renum = {c: i for i, c in enumerate(sorted(first_doc, key=lambda c: first_doc[c]))}
        communities = {d: (renum[raw[d]] if raw[d] != -1 else -1) for d in ordered}
        sizes = {}
        for c in communities.values():
            sizes[c] = sizes.get(c, 0) + 1
        # 5. pair_density: aristas colapsadas entre comunidades distintas (ambas != -1).
        pair_density = {}
        for (a, b) in edge_pairs:
            ca, cb = communities[a], communities[b]
            if ca == -1 or cb == -1 or ca == cb:
                continue
            k = f"{min(ca, cb)}|{max(ca, cb)}"
            pair_density[k] = pair_density.get(k, 0) + 1
        # 6. Artefacto (partition_hash sobre los 6 campos núcleo, sin ui_generation).
        core = {
            "algorithm": algorithm,
            "params": {"seed": COMMUNITY_SEED, "k": COMMUNITY_K, "keep": COMMUNITY_KEEP, "weight_agg": "max"},
            "universe": {"excluded_kinds": sorted(NO_SEMANTIC_KINDS), "n_docs": len(ordered)},
            "communities": {d: communities[d] for d in ordered},
            "sizes": {str(c): sizes[c] for c in sorted(sizes)},
            "pair_density": pair_density,
        }
        artifact = {
            "index_generation": index_generation,
            "ui_generation": ui_generation,
            "partition_hash": community_partition_hash(core),
            "n_communities": sum(1 for c in sizes if c != -1),
            **core,
        }
        json_dump(os.path.join(stage, "communities.json"), artifact, gzip_copy=True)
        stats["communities"] = {
            "n_communities": artifact["n_communities"],
            "n_docs": len(ordered),
            "n_edges_collapsed": len(edge_pairs),
            "partition_hash": artifact["partition_hash"],
            "algorithm": algorithm,
        }
    except Exception as e:
        stats["warnings"].append("communities_skipped:error:" + str(e)[:160])
        return


def cross_project_edges(docs, semantic_edges, stats):
    """Pass independiente sobre TODOS los docs no-duplicados: aristas doc-a-doc entre
    proyectos distintos (wikilink/source_of/semantic). No derivable de los grafos capados."""
    local = {"wikilink_unresolved": [], "wikilink_ambiguous": [], "missing_source_paths": []}
    universe = {d["id"] for d in docs.values() if not d["duplicate_of"]}
    edges = explicit_edges(docs, universe, local)
    edges.update(semantic_edges)
    by_id = {d["id"]: d for d in docs.values()}
    cross = {}
    for eid, e in edges.items():
        s = by_id.get(e["source"])
        t = by_id.get(e["target"])
        if not s or not t or s["project"] == t["project"]:
            continue
        ce = dict(e)
        ce["source_project"] = s["project"]
        ce["target_project"] = t["project"]
        cross[eid] = ce
    stats["cross_edges"] = {
        "total": len(cross),
        "by_type": dict(Counter(e["type"] for e in cross.values()).most_common()),
        "wikilink_unresolved_full_pass": len(local["wikilink_unresolved"]),
    }
    return cross


def build_cross(stage, meta, cross_edges, stats):
    edge_list, truncated = cap_edges(cross_edges, CROSS_EDGE_CAP)
    pair_counter = Counter(tuple(sorted((e["source_project"], e["target_project"]))) for e in edge_list)
    obj = {
        **base_meta(meta),
        "truncated": truncated,
        "edges": edge_list,
        "counts": {
            "edges": len(edge_list),
            "by_edge_type": dict(Counter(e["type"] for e in edge_list).most_common()),
            "by_project_pair_top": {f"{a}<->{b}": c for (a, b), c in pair_counter.most_common(30)},
        },
    }
    json_dump(os.path.join(stage, "edges_cross.json"), obj, gzip_copy=True)
    stats["artifact_sizes"]["edges_cross.json"] = {
        "bytes": os.path.getsize(os.path.join(stage, "edges_cross.json")),
        "gzip_bytes": os.path.getsize(os.path.join(stage, "edges_cross.json.gz")),
    }


def build_macro(stage, meta, groups, cross_edges, stats):
    """Un nodo por proyecto; aristas inter_project agregadas desde las cross-edges del universo completo."""
    nodes = {}
    for project, items in groups.items():
        pid = node_id_for_project(project)
        nodes[pid] = make_project_node(project, len(items))
    agg = {}
    for e in cross_edges.values():
        key = tuple(sorted((e["source_project"], e["target_project"])))
        rec = agg.setdefault(key, {"weight": 0.0, "counts_by_type": Counter()})
        rec["weight"] += e["weight"]
        rec["counts_by_type"][e["type"]] += 1
    edges = {}
    for (a, b), rec in sorted(agg.items()):
        sid, tid = node_id_for_project(a), node_id_for_project(b)
        if sid not in nodes or tid not in nodes:
            continue
        eid = edge_id(sid, tid, "inter_project")
        edges[eid] = {
            "id": eid,
            "source": sid,
            "target": tid,
            "type": "inter_project",
            "weight": round(rec["weight"], 4),
            "directed": False,
            "counts_by_type": dict(rec["counts_by_type"]),
        }
    return write_graph(stage, "graph_macro.json", meta, nodes, edges, MACRO_EDGE_CAP, stats, keep_all_nodes=True)


def build_neighbors(stage, meta, all_nodes, all_edges, docs, selected_doc_ids, stats):
    adjacency = defaultdict(list)
    for e in all_edges.values():
        adjacency[e["source"]].append(e)
        adjacency[e["target"]].append(e)
    nroot = os.path.join(stage, "neighbors")
    os.makedirs(nroot, exist_ok=True)
    mapping = {}
    for doc_id in sorted(selected_doc_ids):
        center = node_id_for_doc(doc_id)
        if center not in all_nodes:
            continue
        seen_nodes = {center}
        q = deque([center])
        edges = {}
        while q and len(seen_nodes) < NEIGHBOR_NODE_CAP:
            nid = q.popleft()
            for e in adjacency.get(nid, []):
                edges[e["id"]] = e
                other = e["target"] if e["source"] == nid else e["source"]
                if other not in seen_nodes and other in all_nodes:
                    seen_nodes.add(other)
                    q.append(other)
                if len(edges) >= NEIGHBOR_EDGE_CAP:
                    break
        nodes = [all_nodes[nid] for nid in sorted(seen_nodes)]
        edge_list = list(edges.values())[:NEIGHBOR_EDGE_CAP]
        obj = {**base_meta(meta), "nodes": nodes, "edges": edge_list,
               "counts": graph_counts(nodes, edge_list),
               "truncated": len(edges) > len(edge_list) or len(seen_nodes) >= NEIGHBOR_NODE_CAP}
        fname = short_hash(doc_id, 16) + ".json"
        mapping[doc_id] = "neighbors/" + fname
        json_dump(os.path.join(nroot, fname), obj, gzip_copy=True)
    json_dump(os.path.join(stage, "neighbors_map.json"), {**base_meta(meta), "map": mapping}, gzip_copy=True)
    stats["artifact_sizes"]["neighbors_map.json"] = {
        "bytes": os.path.getsize(os.path.join(stage, "neighbors_map.json")),
        "gzip_bytes": os.path.getsize(os.path.join(stage, "neighbors_map.json.gz")),
    }


def build(mode):
    index_meta, docs = load_index()
    generation = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "." + short_hash(index_meta.get("index_generation", "0"), 8)
    meta = {
        "generation": generation,
        "built_at": now(),
        **index_meta,
    }
    stage = os.path.join(DMAPA, f"ui.{generation}.tmp")
    final = os.path.join(DMAPA, f"ui.{generation}")
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)
    stats = {
        **base_meta(meta),
        "counts": {},
        "missing_source_paths": [],
        "wikilink_unresolved": [],
        "wikilink_ambiguous": [],
        "warnings": [],
        "artifact_sizes": {},
    }
    audit = read_json(AUDIT_JSON, {})
    groups = project_groups(docs)
    used_slugs = {}
    projects = []
    for project, items in groups.items():
        slug = slugify(project, used_slugs)
        projects.append({"project": project, "slug": slug, "graph_file": f"graph_project/{slug}.json", "count": len(items)})
    slug_by_project = {p["project"]: p["slug"] for p in projects}
    semantic_snapshot = maybe_build_semantic(stage, meta, docs, stats) if mode == "full" else _empty_semantic_snapshot()
    semantic_edges = semantic_snapshot["directed_edges"]
    if mode == "full":
        maybe_build_communities(stage, semantic_snapshot, meta["generation"],
                                meta.get("index_generation"), stats)

    cross = cross_project_edges(docs, semantic_edges, stats)
    build_cross(stage, meta, cross, stats)
    build_macro(stage, meta, groups, cross, stats)

    # Síntesis/hallazgos → proyectos de sus fuentes: pertenecen a los grafos de los
    # proyectos que resumen/conectan (viven bajo project "root" y si no quedarían solo en global).
    synth_by_project = defaultdict(set)
    for nid, node in (read_json(MANIFEST, {"nodes": {}}) or {}).get("nodes", {}).items():
        if not nid.startswith(("sintesis.", "hallazgo.")):
            continue
        logical = ensure_rel(node.get("logical_path", ""))
        if logical not in docs:
            continue
        for sp in node.get("source_paths", []) or []:
            src = docs.get(ensure_rel(sp))
            if src:
                synth_by_project[src["project"]].add(logical)

    selected_global = select_global_docs(groups)
    global_nodes = {}
    global_edges = {}
    add_membership(global_nodes, global_edges, selected_global)
    global_edges.update(explicit_edges(docs, set(global_nodes), stats))
    for eid, edge in semantic_edges.items():
        if edge["source"] in global_nodes and edge["target"] in global_nodes:
            global_edges[eid] = edge
    global_graph = write_graph(stage, "graph_global.json", meta, global_nodes, global_edges, GLOBAL_EDGE_CAP, stats)

    all_nodes = dict(global_nodes)
    all_edges = dict(global_edges)
    selected_neighbor_docs = {d["doc_id"] for d in selected_global}
    os.makedirs(os.path.join(stage, "graph_project"), exist_ok=True)
    for project, items in groups.items():
        selected = items[:PROJECT_NODE_CAP - 1]
        nodes = {}
        edges = {}
        add_membership(nodes, edges, selected)
        for sid in sorted(synth_by_project.get(project, [])):
            d = docs.get(sid)
            if d and d["id"] not in nodes:
                nodes[d["id"]] = make_doc_node(d)  # conectividad vía source_of (explicit_edges)
        edges.update(explicit_edges(docs, set(nodes), stats))
        for eid, edge in semantic_edges.items():
            if edge["source"] in nodes and edge["target"] in nodes:
                edges[eid] = edge
        rel = f"graph_project/{slug_by_project[project]}.json"
        write_graph(stage, rel, meta, nodes, edges, PROJECT_EDGE_CAP, stats, keep_all_nodes=True, project_scope=True)
        all_nodes.update(nodes)
        all_edges.update(edges)
        selected_neighbor_docs.update(d["doc_id"] for d in selected)

    all_edges.update(semantic_edges)
    # Cross-edges también en vecinos: las síntesis viven en project root y sus fuentes
    # en cualquier proyecto — sin esto el drill-down "vecinos de una síntesis" quedaría vacío.
    all_edges.update(cross)

    # Capa discovery (F9): subgrafo de hallazgos + su evidencia (fuentes/contra/targets).
    # Se emite SIEMPRE (aunque vacío) para que /ui/graph?view=discovery no falle, y se
    # mergea en all_nodes/all_edges ANTES de build_neighbors (que saltea centros ausentes).
    disc_nodes, disc_edges, disc_doc_ids = {}, {}, set()
    for nid, node in (read_json(MANIFEST, {"nodes": {}}) or {}).get("nodes", {}).items():
        if not nid.startswith("hallazgo."):
            continue
        logical = ensure_rel(node.get("logical_path", ""))
        d = docs.get(logical)
        if not d:
            continue
        disc_doc_ids.add(logical)
        disc_nodes[d["id"]] = make_doc_node(d)
        for key, _etype in DISCOVERY_EDGE_KEYS:
            for sp in node.get(key, []) or []:
                t = docs.get(ensure_rel(sp))
                if t:
                    disc_doc_ids.add(t["doc_id"])
                    disc_nodes[t["id"]] = make_doc_node(t)
    if disc_nodes:
        disc_edges.update(explicit_edges(docs, set(disc_nodes), stats))
        for eid, edge in semantic_edges.items():
            if edge["source"] in disc_nodes and edge["target"] in disc_nodes:
                disc_edges[eid] = edge
    write_graph(stage, "graph_discovery.json", meta, disc_nodes, disc_edges, PROJECT_EDGE_CAP, stats, keep_all_nodes=True)
    all_nodes.update(disc_nodes)
    all_edges.update(disc_edges)
    selected_neighbor_docs.update(disc_doc_ids)

    build_tree(stage, meta, docs, audit, stats)
    build_neighbors(stage, meta, all_nodes, all_edges, docs, selected_neighbor_docs, stats)

    stats["counts"] = {
        "docs": len(docs),
        "projects": len(groups),
        "global_nodes": len(global_graph["nodes"]),
        "global_edges": len(global_graph["edges"]),
        "by_kind": dict(Counter(d["kind"] for d in docs.values()).most_common()),
        "by_project": dict(Counter(d["project"] for d in docs.values()).most_common()),
        "duplicates": sum(1 for d in docs.values() if d["duplicate_of"]),
        "excluded": audit.get("n_excluded"),
    }
    if stats["artifact_sizes"].get("graph_global.json", {}).get("gzip_bytes", 0) > 5 * 1024 * 1024:
        stats["warnings"].append("graph_global_gzip_gt_5mb")
    manifest_files = {
        "global": "graph_global.json",
        "discovery": "graph_discovery.json",
        "macro": "graph_macro.json",
        "edges_cross": "edges_cross.json",
        "tree": "tree.json",
        "stats": "stats.json",
        "neighbors_map": "neighbors_map.json",
    }
    if stats.get("communities"):
        manifest_files["communities"] = "communities.json"
    manifest = {
        **base_meta(meta),
        "projects": projects,
        "files": manifest_files,
    }
    json_dump(os.path.join(stage, "stats.json"), stats, gzip_copy=True)
    json_dump(os.path.join(stage, "manifest.json"), manifest, gzip_copy=True)

    for root, dirs, files in os.walk(stage):
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)
    if os.path.exists(final):
        shutil.rmtree(final)
    os.rename(stage, final)
    tmp_link = os.path.join(DMAPA, f".uilink.{generation}")
    try:
        os.unlink(tmp_link)
    except FileNotFoundError:
        pass
    os.symlink(final, tmp_link)
    os.replace(tmp_link, UI_LINK)
    prune_old_generations(final)
    write_status({"status": "ok", "generation": generation, "index_generation": meta.get("index_generation"),
                  "built_at": meta["built_at"], "last_ok_generation": generation, "error": None})
    print(f"[ui_builder] OK generation={generation} docs={len(docs)} projects={len(groups)} ui={UI_LINK}")


def prune_old_generations(current):
    gens = []
    for name in os.listdir(DMAPA):
        path = os.path.join(DMAPA, name)
        if name.startswith("ui.") and os.path.isdir(path) and not name.endswith(".tmp"):
            gens.append(path)
    gens.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for old in gens[KEEP_GENERATIONS:]:
        if os.path.realpath(old) != os.path.realpath(current):
            shutil.rmtree(old, ignore_errors=True)


def write_status(obj):
    prev = read_json(UI_STATUS, {})
    if obj.get("status") == "error" and prev.get("last_ok_generation"):
        obj["last_ok_generation"] = prev.get("last_ok_generation")
    with open(UI_STATUS, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(UI_STATUS, 0o644)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("--mode", choices=["fast", "full"], default="fast")
    args = ap.parse_args()
    if args.cmd != "build":
        ap.print_help()
        return 1
    try:
        build(args.mode)
        return 0
    except Exception as e:
        write_status({"status": "error", "error": str(e), "failed_at": now()})
        print(f"[ui_builder] ERROR {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
