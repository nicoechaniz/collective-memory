#!/usr/bin/env python3
"""Serving read-only del indice.

Corre como usuario de servicio (no-root). SOLO LECTURA: sin endpoints de
escritura; /doc se sirve desde la DB (no toca el FS).
Endpoints GET: /health · /search?q=&k=&kind=&project= · /doc?id= · /atlas/ · /ui/* · /wiki/

El bind lo decide `safe_bind()`: loopback por defecto, comodines y direcciones
publicas rechazadas siempre, y una interfaz de red exige opt-in explicito. La
garantia fuerte de no-exposicion la da el filtro de red de la unidad systemd
(ver docs/seguridad.md); esta es la primera capa, no la unica.
"""
import os, sys, json, threading, time, mimetypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import DMAPA, safe_bind  # noqa: E402
import tier1  # noqa: E402
from ui_v2_api import V2API  # noqa: E402

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, parse_qs, unquote  # noqa: E402

BIND = safe_bind()
PORT = int(os.environ.get("MAPA_PORT", "8899"))
K_MAX = 50
Q_MAX = 512
MODEL_IDLE_SECONDS = int(os.environ.get("MAPA_MODEL_IDLE_SECONDS", "300"))
PRELOAD_MODEL = os.environ.get("MAPA_PRELOAD_MODEL", "0") == "1"
UNLOAD_AFTER_SEARCH = os.environ.get("MAPA_UNLOAD_AFTER_SEARCH", "0") == "1"
SERVE_VECTOR = os.environ.get("MAPA_SERVE_VECTOR", "0") == "1"
_sem = threading.Semaphore(4)                        # límite de concurrencia
WEB_DIST = os.path.join(DMAPA, "web", "dist")
WEB_V2_DIST = os.path.join(DMAPA, "web", "dist-v2-atlas")
WEB_V3_DIST = os.path.join(DMAPA, "web", "dist-v3-atlas")
QUARTZ_PUBLIC = os.path.join(DMAPA, "quartz", "public")
UI_LINK = os.path.join(DMAPA, "ui")
UI_STATUS = os.path.join(DMAPA, "ui_status.json")
V2_API = V2API(UI_LINK, allow_vector=SERVE_VECTOR)

def _send(h, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.end_headers()
    h.wfile.write(body)


# worker-src blob: es imprescindible — graphology-layout-forceatlas2/worker crea el worker desde un Blob URL
CSP = ("default-src 'self'; script-src 'self'; worker-src 'self' blob:; "
       "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
       "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'self'; form-action 'self'")


def _send_html(h, code, title, message):
    body = f"<!doctype html><meta charset='utf-8'><title>{title}</title><body style='font-family:serif;background:#111713;color:#f4ead6;padding:3rem'><h1>{title}</h1><p>{message}</p></body>".encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Content-Security-Policy", CSP)
    h.end_headers()
    h.wfile.write(body)


def _inside(path, root):
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def _safe_join(root, rel):
    rel = unquote(rel).lstrip("/")
    candidate = os.path.realpath(os.path.join(root, rel))
    if not _inside(candidate, root):
        return None
    return candidate


def _send_file(h, path, mime=None):
    if not path or not os.path.isfile(path):
        return _send(h, 404, {"error": "not found"})
    source = path
    use_gzip = False
    if "gzip" in (h.headers.get("Accept-Encoding") or "") and os.path.isfile(path + ".gz"):
        source = path + ".gz"
        use_gzip = True
    st = os.stat(source)
    etag = f'"{int(st.st_mtime)}-{st.st_size}{"-gz" if use_gzip else ""}"'
    if h.headers.get("If-None-Match") == etag:
        h.send_response(304)
        h.send_header("ETag", etag)
        h.end_headers()
        return
    ctype = mime or mimetypes.guess_type(path)[0] or "application/octet-stream"
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(st.st_size))
    h.send_header("ETag", etag)
    h.send_header("Cache-Control", "no-cache")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    if ctype.startswith("text/html"):
        h.send_header("Content-Security-Policy", CSP)
    if use_gzip:
        h.send_header("Content-Encoding", "gzip")
        h.send_header("Vary", "Accept-Encoding")
    h.end_headers()
    with open(source, "rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            h.wfile.write(chunk)


def _static(h, root, prefix, spa=False, index="index.html"):
    if not os.path.isdir(root):
        return _send_html(h, 503, "Projection missing", f"{prefix} is not built yet.")
    path = urlparse(h.path).path
    rel = path[len(prefix):].lstrip("/")
    if not rel:
        rel = index
    target = _safe_join(root, rel)
    if target and os.path.isdir(target):
        target = os.path.join(target, "index.html")
    if target and os.path.isfile(target):
        return _send_file(h, target)
    if spa and not rel.startswith("assets/"):
        return _send_file(h, os.path.join(root, index), "text/html; charset=utf-8")
    return _send(h, 404, {"error": "not found"})


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ui_root():
    if not os.path.islink(UI_LINK) and not os.path.isdir(UI_LINK):
        return None
    root = os.path.realpath(UI_LINK)
    if not _inside(root, DMAPA):
        return None
    return root


def _send_ui_file(h, rel):
    root = _ui_root()
    if not root:
        return _send(h, 503, {"error": "ui projection missing", "hint": "ui_builder.py build"})
    path = _safe_join(root, rel)
    return _send_file(h, path, "application/json; charset=utf-8")


def _ui_status():
    status = _read_json(UI_STATUS) or {}
    root = _ui_root()
    manifest = _read_json(os.path.join(root, "manifest.json")) if root else None
    return status, manifest


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15   # socket timeout por request (mitiga slowloris)

    def log_message(self, *a):
        pass  # el journal de systemd ya registra lo necesario

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/atlas-v3" or u.path.startswith("/atlas-v3/"):
                return _static(self, WEB_V3_DIST, "/atlas-v3/", spa=True, index="v3-atlas.html")
            if u.path == "/atlas-v2" or u.path.startswith("/atlas-v2/"):
                return _static(self, WEB_V2_DIST, "/atlas-v2/", spa=True, index="v2-atlas.html")
            if u.path == "/atlas" or u.path.startswith("/atlas/"):
                return _static(self, WEB_DIST, "/atlas/", spa=True)
            if u.path == "/wiki" or u.path.startswith("/wiki/"):
                if not os.path.isdir(QUARTZ_PUBLIC):
                    return _send_html(self, 503, "Quartz mirror not built", "Quartz mirror not built.")
                return _static(self, QUARTZ_PUBLIC, "/wiki/", spa=False)
            if u.path.startswith("/ui/v2"):
                code, obj = V2_API.dispatch(u.path, qs)
                return _send(self, code, obj)
            if u.path.startswith("/ui/"):
                return self._handle_ui(u, qs)
            if u.path in ("/", "/help"):
                return _send(self, 200, {
                    "service": "indice de memoria colectiva — read-only",
                    "read_only": True,
                    "endpoints": {
                        "GET /search?q=&k=&kind=&project=": "busqueda read-only (q<=512 chars, k<=50; kind/project opcionales)",
                        "GET /doc?id=<doc_id>": "markdown completo de un doc (doc_id viene de /search)",
                        "GET /atlas/": "atlas visual read-only",
                        "GET /atlas-v2/": "beta del observatorio visual V2",
                        "GET /atlas-v3/": "observatorio visual V3",
                        "GET /ui/graph?view=macro|global|project|neighbors|cross|discovery": "proyecciones visuales JSON (macro=proyectos, cross=aristas entre proyectos, discovery=hallazgos)",
                        "GET /ui/communities": "particion de barrios semanticos (Leiden) de la generacion vigente, si fue construida",
                        "GET /discovery": "hallazgos publicados (lista; ?id=<doc_id> devuelve uno)",
                        "GET /wiki/": "mirror Quartz del mapa curado, si fue construido",
                        "GET /health": "estado del indice"},
                    "guia_completa": "GET /doc?id=mapa/GUIA_CONSULTA.md"})
            if u.path == "/health":
                h = tier1.health_dict()
                h["serving_vector_enabled"] = SERVE_VECTOR
                h["serving_search_mode"] = "hybrid" if SERVE_VECTOR else "fts-only"
                status, manifest = _ui_status()
                h["ui_generation"] = (manifest or {}).get("generation")
                h["ui_index_generation"] = (manifest or {}).get("index_generation")
                h["ui_built_at"] = (manifest or {}).get("built_at")
                h["ui_error"] = status.get("error") if status.get("status") == "error" else None
                h["ui_status"] = status.get("status")
                h["ui_stale"] = bool((manifest or {}).get("index_generation") != h.get("index_generation"))
                h["quartz_built_at"] = int(os.path.getmtime(QUARTZ_PUBLIC)) if os.path.isdir(QUARTZ_PUBLIC) else None
                return _send(self, 200, h)
            if u.path == "/search":
                q = (qs.get("q", [""])[0] or "").strip()
                if not q:
                    return _send(self, 400, {"error": "missing q"})
                if len(q) > Q_MAX:
                    return _send(self, 413, {"error": "q too long", "max": Q_MAX})
                try:
                    k = int(qs.get("k", ["10"])[0])
                except Exception:
                    k = 10
                k = max(1, min(k, K_MAX))
                kind = (qs.get("kind", [None])[0] or None)
                project = (qs.get("project", [None])[0] or None)
                if not _sem.acquire(timeout=5):
                    return _send(self, 429, {"error": "busy"})
                try:
                    result = tier1.search_json(q, k, allow_vector=SERVE_VECTOR, kind=kind, project=project)
                    if UNLOAD_AFTER_SEARCH:
                        tier1.unload_model_if_idle(force=True)
                    return _send(self, 200, result)
                finally:
                    _sem.release()
            if u.path == "/doc":
                if not _sem.acquire(timeout=5):
                    return _send(self, 429, {"error": "busy"})
                try:
                    d = tier1.get_doc(qs.get("id", [""])[0] or "")
                    return _send(self, 200, d) if d else _send(self, 404, {"error": "not found"})
                finally:
                    _sem.release()
            if u.path == "/discovery":
                # Read-only: SOLO hallazgos publicados en index.db (kind=discovery).
                # Nunca candidatos privados, campañas ni reviews (discovery.db no se toca).
                doc_id = (qs.get("id", [""])[0] or "").strip()
                if len(doc_id) > Q_MAX:
                    return _send(self, 413, {"error": "id too long", "max": Q_MAX})
                if not _sem.acquire(timeout=5):
                    return _send(self, 429, {"error": "busy"})
                try:
                    if doc_id:
                        d = tier1.get_doc(doc_id)
                        if d and d.get("kind") == "discovery":
                            return _send(self, 200, d)
                        return _send(self, 404, {"error": "not found"})
                    con = tier1.ro()
                    try:
                        rows = con.execute(
                            "SELECT doc_id, title, mtime FROM docs WHERE kind='discovery' ORDER BY mtime DESC"
                        ).fetchall()
                    finally:
                        con.close()
                    return _send(self, 200, {"count": len(rows), "hallazgos": [
                        {"doc_id": r[0], "title": r[1], "mtime": r[2]} for r in rows]})
                finally:
                    _sem.release()
            return _send(self, 404, {"error": "unknown endpoint"})
        except Exception:
            return _send(self, 500, {"error": "internal"})

    def _handle_ui(self, u, qs):
        if u.path == "/ui/manifest":
            return _send_ui_file(self, "manifest.json")
        if u.path == "/ui/stats":
            return _send_ui_file(self, "stats.json")
        if u.path == "/ui/tree":
            return _send_ui_file(self, "tree.json")
        if u.path == "/ui/communities":
            # F21: partición Leiden servida. Ausente (build fast / skipped) → 404 y el Atlas
            # cae al Louvain client-side. Read-only, sin parámetros.
            return _send_ui_file(self, "communities.json")
        if u.path != "/ui/graph":
            return _send(self, 404, {"error": "unknown ui endpoint"})
        view = (qs.get("view", [""])[0] or "").strip()
        if view not in {"global", "project", "neighbors", "macro", "cross", "discovery"}:
            return _send(self, 400, {"error": "invalid view"})
        if view == "global":
            return _send_ui_file(self, "graph_global.json")
        if view == "discovery":
            return _send_ui_file(self, "graph_discovery.json")
        if view == "macro":
            return _send_ui_file(self, "graph_macro.json")
        if view == "cross":
            return _send_ui_file(self, "edges_cross.json")
        root = _ui_root()
        if not root:
            return _send(self, 503, {"error": "ui projection missing", "hint": "ui_builder.py build"})
        manifest = _read_json(os.path.join(root, "manifest.json")) or {}
        if view == "project":
            project = (qs.get("project", [""])[0] or "").strip()
            if not project:
                return _send(self, 400, {"error": "missing project"})
            if len(project) > 128:
                return _send(self, 413, {"error": "project too long"})
            for p in manifest.get("projects", []):
                if p.get("project") == project:
                    return _send_ui_file(self, p.get("graph_file", ""))
            return _send(self, 404, {"error": "project not found"})
        doc_id = (qs.get("id", [""])[0] or "").strip()
        if not doc_id:
            return _send(self, 400, {"error": "missing id"})
        if len(doc_id) > 512:
            return _send(self, 413, {"error": "id too long"})
        mapping_doc = _read_json(os.path.join(root, "neighbors_map.json")) or {}
        mapping = mapping_doc.get("map", mapping_doc)
        rel = mapping.get(doc_id)
        if not rel:
            return _send(self, 404, {"error": "neighbors not found"})
        return _send_ui_file(self, rel)

    def _ro(self):
        _send(self, 405, {"error": "read-only service"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _ro


class HardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    allow_reuse_address = True


def model_reaper():
    while True:
        time.sleep(min(60, max(5, MODEL_IDLE_SECONDS // 2)))
        if tier1.unload_model_if_idle(MODEL_IDLE_SECONDS):
            print(f"[serve] modelo descargado por inactividad ({MODEL_IDLE_SECONDS}s)", flush=True)


def main():
    srv = HardServer((BIND, PORT), Handler)
    if PRELOAD_MODEL:
        try:
            tier1.get_model()
        except Exception as e:
            print(f"[serve] aviso: modelo no cargó ({e}); modo degradado FTS", flush=True)
    threading.Thread(target=model_reaper, daemon=True).start()
    print(f"[serve] mapa-serve read-only en http://{BIND}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
