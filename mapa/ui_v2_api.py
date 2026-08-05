#!/usr/bin/env python3
"""Read-only query surface for the normalized UI V2 projection."""
import json
import os
import sqlite3
from collections import defaultdict

try:
    from mapa_config import DMAPA
except ImportError:  # runtime historico del host, anterior a mapa_config.py
    _ROOT_RAW = os.environ.get("MAPA_ROOT")
    if not _ROOT_RAW:
        raise RuntimeError("MAPA_ROOT is required when mapa_config is unavailable")
    _ROOT = os.path.abspath(_ROOT_RAW)
    DMAPA = os.path.abspath(os.environ.get("MAPA_DATA", os.path.join(_ROOT, ".mapa")))
import tier1
from exchange import ExchangeError, assert_publication_stable

DB_NAME = "ui_v2.db"
MAX_LIMIT = 500


def _one(qs, key, default=""):
    value = qs.get(key, [default])
    return (value[0] if value else default) or default


def _integer(qs, key, default, low=0, high=MAX_LIMIT):
    try:
        value = int(_one(qs, key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _decode(value, default=None):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _like(value):
    """Escape a literal value for SQLite LIKE ... ESCAPE '!'."""
    return str(value).replace("!", "!!").replace("%", "!%").replace("_", "!_")


class V2API:
    def __init__(self, ui_link=None, allow_vector=False):
        self.ui_link = ui_link or os.path.join(DMAPA, "ui")
        self.allow_vector = bool(allow_vector)

    def _path(self):
        root = os.path.realpath(self.ui_link)
        dm = os.path.realpath(DMAPA)
        if root != dm and not root.startswith(dm + os.sep):
            raise RuntimeError("invalid UI projection path")
        return os.path.join(root, DB_NAME)

    def _connect(self):
        assert_publication_stable(DMAPA)
        path = self._path()
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con

    @staticmethod
    def _meta(con):
        return {row["k"]: _decode(row["v"], row["v"]) for row in con.execute("SELECT k,v FROM meta")}

    @staticmethod
    def _envelope(meta, data, *, total=None, shown=None, truncated=False, policy="deterministic"):
        count = shown if shown is not None else (len(data) if isinstance(data, list) else None)
        return {
            "meta": {
                "generation": meta.get("generation"),
                "index_generation": meta.get("index_generation"),
                "semantic_generation": meta.get("semantic_generation"),
                "semantic_available": meta.get("semantic_available", False),
                "semantic_fresh": meta.get("semantic_fresh", False),
                "total": total,
                "shown": count,
                "truncated": bool(truncated),
                "selection_policy": policy,
            },
            "data": data,
        }

    def dispatch(self, path, qs):
        try:
            con = self._connect()
        except FileNotFoundError:
            return 503, {"error": "ui v2 projection missing", "hint": "rebuild ui_v2.db"}
        except ExchangeError:
            return 503, {"error": "publication generation is not yet stable"}
        try:
            meta = self._meta(con)
            route = path.removeprefix("/ui/v2").rstrip("/") or "/bootstrap"
            handlers = {
                "/bootstrap": self._bootstrap,
                "/overview": self._overview,
                "/project": self._project,
                "/communities": self._communities,
                "/community": self._community,
                "/relation": self._relation,
                "/tree": self._tree,
                "/document-context": self._document_context,
                "/graph": self._graph,
            }
            if route == "/search":
                return self._search(meta, qs)
            handler = handlers.get(route)
            if not handler:
                return 404, {"error": "unknown ui v2 endpoint"}
            return handler(con, meta, qs)
        finally:
            con.close()

    def _bootstrap(self, con, meta, qs):
        projects = [dict(row) for row in con.execute("SELECT * FROM projects ORDER BY sort_order,title")]
        by_kind = {row[0]: row[1] for row in con.execute("SELECT kind,count(*) FROM docs GROUP BY kind")}
        by_level = {row[0]: row[1] for row in con.execute("SELECT abstraction,count(*) FROM docs GROUP BY abstraction")}
        payload = {
            "system": {
                "name": "Memoria Colectiva",
                "read_only": True,
                "total_docs": meta.get("total_docs", 0),
                "unique_docs": meta.get("unique_docs", 0),
                "vectorized_docs": meta.get("vectorized_docs", 0),
                "curated_projects": meta.get("curated_projects", 0),
                "raw_scopes": meta.get("raw_scopes", 0),
            },
            "projects": projects,
            "counts_by_kind": by_kind,
            "counts_by_abstraction": by_level,
            "views": ["overview", "project", "communities", "search", "tree"],
        }
        return 200, self._envelope(meta, payload, total=len(projects), shown=len(projects), policy="curated_catalog")

    def _selected_projects(self, con, qs):
        requested = [x.strip() for x in _one(qs, "projects").split(",") if x.strip()]
        if not requested:
            return [row[0] for row in con.execute("SELECT project_id FROM projects ORDER BY sort_order")]
        valid = {row[0] for row in con.execute("SELECT project_id FROM projects")}
        return [x for x in requested if x in valid]

    def _overview(self, con, meta, qs):
        selected = self._selected_projects(con, qs)
        if not selected:
            return 200, self._envelope(meta, {"nodes": [], "edges": []}, total=0, shown=0,
                                       policy="curated_selected_projects")
        marks = ",".join("?" for _ in selected)
        nodes = [dict(row) for row in con.execute(
            f"SELECT * FROM projects WHERE project_id IN ({marks}) ORDER BY sort_order", selected)]
        edges = [dict(row) for row in con.execute(
            f"SELECT * FROM project_edges WHERE source_project IN ({marks}) "
            f"AND target_project IN ({marks}) ORDER BY total_weight DESC", selected + selected)]
        for edge in edges:
            edge["counts_by_type"] = _decode(edge["counts_by_type"], {})
        return 200, self._envelope(meta, {"nodes": nodes, "edges": edges}, total=len(nodes),
                                   shown=len(nodes), policy="curated_selected_projects")

    def _project(self, con, meta, qs):
        pid = _one(qs, "id")[:160]
        row = con.execute("SELECT * FROM projects WHERE project_id=?", (pid,)).fetchone()
        if not row:
            return 404, {"error": "project not found"}
        limit, offset = _integer(qs, "limit", 80, 1), _integer(qs, "offset", 0, 0, 1_000_000)
        kind = _one(qs, "kind")[:64]
        where, args = "project_id=?", [pid]
        if kind:
            where += " AND kind=?"
            args.append(kind)
        total = con.execute(f"SELECT count(*) FROM docs WHERE {where}", args).fetchone()[0]
        docs = [dict(x) for x in con.execute(
            f"SELECT * FROM docs WHERE {where} ORDER BY abstraction DESC,mtime DESC,title LIMIT ? OFFSET ?",
            args + [limit, offset])]
        community_rows = con.execute(
            "SELECT c.community_id,c.title,count(*) n FROM communities c "
            "JOIN community_docs cd USING(community_id) JOIN docs d USING(doc_id) "
            "WHERE d.project_id=? GROUP BY c.community_id ORDER BY n DESC LIMIT 20", (pid,)).fetchall()
        payload = {"project": dict(row), "documents": docs,
                   "communities": [dict(x) for x in community_rows]}
        return 200, self._envelope(meta, payload, total=total, shown=len(docs),
                                   truncated=offset + len(docs) < total, policy="project_docs_recent_curated_first")

    def _communities(self, con, meta, qs):
        pid = _one(qs, "project")[:160]
        limit = _integer(qs, "limit", 100, 1, 500)
        if pid:
            rows = con.execute(
                "SELECT c.*,count(*) project_size FROM communities c JOIN community_docs cd USING(community_id) "
                "JOIN docs d USING(doc_id) WHERE d.project_id=? GROUP BY c.community_id "
                "ORDER BY project_size DESC LIMIT ?", (pid, limit)).fetchall()
        else:
            rows = con.execute("SELECT *,size project_size FROM communities ORDER BY size DESC LIMIT ?", (limit,)).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["project_mix"] = _decode(item["project_mix"], [])
            data.append(item)
        total = con.execute("SELECT count(*) FROM communities").fetchone()[0]
        return 200, self._envelope(meta, data, total=total, shown=len(data), truncated=len(data) < total,
                                   policy="semantic_communities_by_size")

    def _community(self, con, meta, qs):
        cid = _integer(qs, "id", -1, -1, 1_000_000)
        row = con.execute("SELECT * FROM communities WHERE community_id=?", (cid,)).fetchone()
        if not row:
            return 404, {"error": "community not found"}
        limit, offset = _integer(qs, "limit", 100, 1), _integer(qs, "offset", 0, 0, 1_000_000)
        docs = [dict(x) for x in con.execute(
            "SELECT d.* FROM community_docs cd JOIN docs d USING(doc_id) WHERE cd.community_id=? "
            "ORDER BY d.abstraction DESC,d.mtime DESC,d.title LIMIT ? OFFSET ?", (cid, limit, offset))]
        community = dict(row)
        community["project_mix"] = _decode(community["project_mix"], [])
        return 200, self._envelope(meta, {"community": community, "documents": docs}, total=row["size"],
                                   shown=len(docs), truncated=offset + len(docs) < row["size"],
                                   policy="community_docs_curated_first")

    def _relation(self, con, meta, qs):
        source, target = sorted((_one(qs, "source")[:160], _one(qs, "target")[:160]))
        if not source or not target or source == target:
            return 400, {"error": "source and target are required"}
        edge = con.execute("SELECT * FROM project_edges WHERE source_project=? AND target_project=?",
                           (source, target)).fetchone()
        if not edge:
            return 404, {"error": "relation not found"}
        limit = _integer(qs, "limit", 100, 1, 300)
        evidence = [dict(row) for row in con.execute(
            "SELECT * FROM relation_evidence WHERE source_project=? AND target_project=? "
            "ORDER BY weight DESC,edge_type LIMIT ?", (source, target, limit))]
        item = dict(edge)
        item["counts_by_type"] = _decode(item["counts_by_type"], {})
        return 200, self._envelope(meta, {"relation": item, "evidence": evidence},
                                   total=edge["evidence_count"], shown=len(evidence),
                                   truncated=len(evidence) < edge["evidence_count"], policy="strongest_relation_evidence")

    def _tree(self, con, meta, qs):
        pid, prefix = _one(qs, "project")[:160], _one(qs, "prefix")[:512].strip("/")
        kind = _one(qs, "kind")[:64]
        abstraction = _one(qs, "abstraction")[:32]
        query = _one(qs, "q")[:160].strip()
        limit, offset = _integer(qs, "limit", 200, 1), _integer(qs, "offset", 0, 0, 1_000_000)
        where, args = "1=1", []
        if pid:
            where += " AND project_id=?"
            args.append(pid)
        if prefix:
            where += " AND path_rel LIKE ? ESCAPE '!'"
            args.append(_like(prefix) + "/%")
        if kind:
            where += " AND kind=?"
            args.append(kind)
        if abstraction:
            where += " AND abstraction=?"
            args.append(abstraction)
        if query:
            where += " AND (title LIKE ? ESCAPE '!' OR path_rel LIKE ? ESCAPE '!')"
            needle = "%" + _like(query) + "%"
            args.extend((needle, needle))
        total = con.execute(f"SELECT count(*) FROM docs WHERE {where}", args).fetchone()[0]
        rows = [dict(row) for row in con.execute(
            f"SELECT * FROM docs WHERE {where} ORDER BY "
            "CASE abstraction WHEN 'curated' THEN 0 WHEN 'synthesis' THEN 1 ELSE 2 END,path_rel "
            "LIMIT ? OFFSET ?", args + [limit, offset])]
        return 200, self._envelope(meta, rows, total=total, shown=len(rows),
                                   truncated=offset + len(rows) < total,
                                   policy="filtered_curated_first_filesystem_slice")

    def _document_context(self, con, meta, qs):
        doc_id = _one(qs, "id")[:1024]
        doc = con.execute("SELECT * FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        if not doc:
            return 404, {"error": "document not found"}
        communities = [dict(row) for row in con.execute(
            "SELECT c.* FROM communities c JOIN community_docs cd USING(community_id) WHERE cd.doc_id=?",
            (doc_id,))]
        for item in communities:
            item["project_mix"] = _decode(item["project_mix"], [])
        node_id = "doc:" + doc_id
        neighbors = []
        for row in con.execute(
            "SELECT e.*,CASE WHEN e.source=? THEN e.target ELSE e.source END neighbor_id "
            "FROM graph_edges e WHERE e.graph_name='global' AND (e.source=? OR e.target=?) "
            "ORDER BY e.weight DESC LIMIT 80", (node_id, node_id, node_id)):
            neighbors.append(dict(row))
        return 200, self._envelope(meta, {"document": dict(doc), "communities": communities,
                                          "neighbors": neighbors}, total=len(neighbors), shown=len(neighbors),
                                   policy="strongest_global_neighbors")

    def _graph(self, con, meta, qs):
        view = _one(qs, "view", "overview")
        if view == "overview":
            return self._overview(con, meta, qs)
        graph_name = "global" if view == "global" else f"project:{_one(qs, 'project')[:160]}"
        node_limit = _integer(qs, "nodes", 1600, 1, 3000)
        edge_limit = _integer(qs, "edges", 5000, 1, 10000)
        total_nodes = con.execute("SELECT count(*) FROM graph_nodes WHERE graph_name=?", (graph_name,)).fetchone()[0]
        nodes = [dict(row) for row in con.execute(
            "SELECT * FROM graph_nodes WHERE graph_name=? ORDER BY kind,node_id LIMIT ?", (graph_name, node_limit))]
        ids = {row["node_id"] for row in nodes}
        edges = []
        if ids:
            for row in con.execute("SELECT * FROM graph_edges WHERE graph_name=? ORDER BY weight DESC LIMIT ?",
                                   (graph_name, edge_limit * 3)):
                if row["source"] in ids and row["target"] in ids:
                    edges.append(dict(row))
                    if len(edges) >= edge_limit:
                        break
        return 200, self._envelope(meta, {"nodes": nodes, "edges": edges}, total=total_nodes,
                                   shown=len(nodes), truncated=len(nodes) < total_nodes,
                                   policy="legacy_projection_normalized")

    def _search(self, meta, qs):
        query = _one(qs, "q").strip()
        if not query:
            return 400, {"error": "missing q"}
        if len(query) > 512:
            return 413, {"error": "q too long", "max": 512}
        limit, offset = _integer(qs, "limit", 20, 1, 100), _integer(qs, "offset", 0, 0, 400)
        result = tier1.search_json(query, min(500, limit + offset), allow_vector=self.allow_vector,
                                   kind=_one(qs, "kind") or None, project=_one(qs, "project") or None)
        rows = result.get("results", [])[offset:offset + limit]
        payload = {"mode": result.get("mode"), "query": query, "results": rows}
        return 200, self._envelope(meta, payload, total=None, shown=len(rows),
                                   truncated=len(result.get("results", [])) > offset + len(rows),
                                   policy="rrf_or_fts_rank")
