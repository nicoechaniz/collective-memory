#!/usr/bin/env python3
"""Fixture integral del catálogo, proyección, API y sesiones de UI V2."""
import json
import os
import sqlite3
import sys
import tempfile


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f)


def make_fixture(root, data, ui):
    os.makedirs(os.path.join(root, "mapa", "proyectos"), exist_ok=True)
    os.makedirs(data, exist_ok=True)
    os.makedirs(ui, exist_ok=True)
    con = sqlite3.connect(os.path.join(data, "index.db"))
    con.executescript("""
    CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT);
    CREATE TABLE docs(doc_id TEXT PRIMARY KEY,kind TEXT,project TEXT,title TEXT,body TEXT,
      content_hash TEXT,source_path TEXT,ext TEXT,size_bytes INTEGER,mtime INTEGER,
      extractor TEXT,included_by TEXT,duplicate_of TEXT);
    CREATE TABLE chunks(id INTEGER PRIMARY KEY,doc_id TEXT,kind TEXT,project TEXT,ord INT,
      heading TEXT,title TEXT,body TEXT,content_hash TEXT);
    CREATE TABLE chunks_vec_rowids(rowid INTEGER PRIMARY KEY,id,chunk_id INTEGER,chunk_offset INTEGER);
    """)
    con.execute("INSERT INTO meta VALUES('index_generation','fixture-1')")
    docs = [
        ("mapa/proyectos/alpha.md", "map", "alpha-src", "Alpha", "---\nid: alpha\ntype: research\nproject: Alpha\n---\n# Alpha — investigación\n\nExplora señales.", None),
        ("mapa/proyectos/beta.md", "map", "Beta", "Beta", "---\nid: beta\ntype: project\nproject: Beta\n---\n# Beta — producto\n\nConstruye sensores.", None),
        ("Alpha/notas/a.md", "fs_doc", "Alpha", "Ventanas armónicas", "Contenido alpha", None),
        ("Beta/docs/b.md", "synthesis", "Beta", "Síntesis de sensores", "Contenido beta", None),
        ("Beta/docs/c.md", "fs_doc", "Beta", "Documento duplicado", "Contenido repetido", "Beta/docs/b.md"),
    ]
    for i, (doc_id, kind, project, title, body, duplicate) in enumerate(docs, 1):
        con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, kind, project, title, body, f"h{i}", doc_id, ".md", len(body), 100 + i,
                     "text", "fixture", duplicate))
        con.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)",
                    (i, doc_id, kind, project, 0, "", title, body, f"h{i}"))
        if i in (1, 3, 4):
            con.execute("INSERT INTO chunks_vec_rowids VALUES(?,?,?,?)", (i, None, 1, i - 1))
    con.commit(); con.close()

    write_json(os.path.join(ui, "manifest.json"), {
        "generation": "ui-fixture", "index_generation": "fixture-1",
        "projects": [{"project": "Alpha", "graph_file": "alpha.json"},
                     {"project": "Beta", "graph_file": "beta.json"}],
    })
    write_json(os.path.join(ui, "stats.json"), {"counts": {"global_nodes": 2, "global_edges": 1}})
    write_json(os.path.join(ui, "communities.json"), {
        "index_generation": "fixture-1", "communities": {
            "Alpha/notas/a.md": 7, "Beta/docs/b.md": 7, "mapa/proyectos/alpha.md": 8,
        }})
    edge = {"id": "e1", "source": "doc:Alpha/notas/a.md", "target": "doc:Beta/docs/b.md",
            "type": "semantic_neighbor", "weight": 1.2, "directed": False,
            "source_project": "Alpha", "target_project": "Beta"}
    write_json(os.path.join(ui, "edges_cross.json"), {"edges": [edge]})
    graph = {"nodes": [
        {"id": "doc:Alpha/notas/a.md", "label": "A", "kind": "fs_doc", "project": "Alpha", "doc_id": "Alpha/notas/a.md", "x": 1, "y": 2},
        {"id": "doc:Beta/docs/b.md", "label": "B", "kind": "synthesis", "project": "Beta", "doc_id": "Beta/docs/b.md", "x": 3, "y": 4},
    ], "edges": [edge]}
    write_json(os.path.join(ui, "graph_global.json"), graph)
    write_json(os.path.join(ui, "alpha.json"), graph)
    write_json(os.path.join(ui, "beta.json"), graph)


def main():
    with tempfile.TemporaryDirectory(prefix="mapa-ui-v2-") as tmp:
        root, data = os.path.join(tmp, "root"), os.path.join(tmp, "data")
        ui = os.path.join(data, "ui")
        make_fixture(root, data, ui)
        os.environ["MAPA_ROOT"] = root
        os.environ["MAPA_DATA"] = data
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "mapa"))
        import ui_v2_store
        from ui_v2_api import V2API
        from ui_v2_sessions import SessionStore

        meta = ui_v2_store.build(ui_root=ui)
        assert meta["total_docs"] == 5 and meta["vectorized_docs"] == 3
        assert meta["curated_projects"] == 2 and meta["semantic_fresh"] is True

        api = V2API(ui)
        code, bootstrap = api.dispatch("/ui/v2/bootstrap", {})
        assert code == 200 and bootstrap["data"]["system"]["curated_projects"] == 2
        alpha = next(p for p in bootstrap["data"]["projects"] if p["project_id"] == "alpha")
        assert alpha["doc_count"] == 2 and alpha["vector_count"] == 2
        code, overview = api.dispatch("/ui/v2/overview", {})
        assert code == 200 and len(overview["data"]["edges"]) == 1
        code, relation = api.dispatch("/ui/v2/relation", {"source": ["alpha"], "target": ["beta"]})
        assert code == 200 and relation["meta"]["total"] == 1
        code, project = api.dispatch("/ui/v2/project", {"id": ["beta"], "limit": ["1"]})
        assert code == 200 and project["meta"]["truncated"] is True
        code, tree = api.dispatch("/ui/v2/tree", {"prefix": ["Alpha/notas"], "limit": ["10"]})
        assert code == 200 and tree["meta"]["total"] == 1
        code, tree = api.dispatch("/ui/v2/tree", {"limit": ["10"]})
        assert code == 200 and tree["data"][0]["abstraction"] == "curated"
        code, tree = api.dispatch("/ui/v2/tree", {
            "q": ["síntesis"], "kind": ["synthesis"], "abstraction": ["synthesis"],
            "project": ["beta"], "limit": ["10"], "offset": ["0"],
        })
        assert code == 200 and tree["meta"]["total"] == 1
        assert tree["data"][0]["doc_id"] == "Beta/docs/b.md"
        code, tree = api.dispatch("/ui/v2/tree", {"q": ["%_literal"], "limit": ["10"]})
        assert code == 200 and tree["meta"]["total"] == 0

        store = SessionStore("fixture_cookie", os.path.join(tmp, "sessions.db"))
        raw, csrf, _ = store.create("fixture-user")
        header = store.cookie_value(raw)
        session = store.authenticate(header, lambda user: user == "fixture-user")
        assert session and store.verify_csrf(session, csrf)
        rotated = store.rotate_csrf(session["session_hash"])
        session = store.authenticate(header, lambda user: user == "fixture-user")
        assert store.verify_csrf(session, rotated) and not store.verify_csrf(session, csrf)
        store.delete(header)
        assert store.authenticate(header, lambda user: True) is None
    print("ui_v2: fixture integral OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
