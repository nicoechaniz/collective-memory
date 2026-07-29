#!/usr/bin/env python3
"""Discovery Playground — laboratorio multi-usuario de la memoria colectiva (F15).

Servicio hermano del atlas (serve.py): la mesa chica de descubrimiento corre
campañas de discovery en un SANDBOX propio (.mapa/discovery/playground.db),
revisa candidatos y propone; la publicación sigue siendo single-writer de
el dueño (discover.py inbox/import → review → promote → librarian).

Invariantes:
  - Bind resuelto por safe_bind(): loopback por defecto, nunca comodin ni IP publica.
  - Identidad por token Bearer (hasheado sha256 en .mapa/discovery/users.json);
    cuotas y ownership cuelgan del token, jamás de un parámetro cliente.
  - Escritura SOLO bajo .mapa/discovery/ (además garantizado por systemd:
    ReadWritePaths= sandbox + /run/mapa).
  - GETs espejo del atlas (/search /doc /ui/*) read-only y sin token, como el atlas.

CLI: playground.py serve | user add <nombre> | user disable <nombre> | user list
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import (  # noqa: E402
    DMAPA, CODE_HOME, SERVICE_GROUP, safe_bind, OWNER_REVIEWER, is_reserved_reviewer)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, parse_qs  # noqa: E402

import discover  # noqa: E402
import playground_queue as pq  # noqa: E402
import tier1  # noqa: E402
from ui_v2_api import V2API  # noqa: E402
from ui_v2_sessions import SessionStore  # noqa: E402

BIND = safe_bind()
PORT = int(os.environ.get("MAPA_PG_PORT", "8898"))
STRUCTURAL_ONLY = os.environ.get("MAPA_STRUCTURAL_ONLY", "0") == "1"
STRUCTURAL_OPERATORS = ("latent_bridge", "cluster_frontier", "outlier")
USERS_PATH = os.path.join(DMAPA, "discovery", "users.json")
LAB_DIST = os.path.join(DMAPA, "web", "dist-lab")
LAB_V2_DIST = os.path.join(DMAPA, "web", "dist-v2-lab")
LAB_V3_DIST = os.path.join(DMAPA, "web", "dist-v3-lab")
UI_LINK = os.path.join(DMAPA, "ui")
Q_MAX = 512
BODY_MAX = 16 * 1024
_sem = threading.Semaphore(4)
V2_API = V2API(UI_LINK, allow_vector=False)
_sessions = None

CSP = ("default-src 'self'; script-src 'self'; worker-src 'self' blob:; "
       "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
       "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")

# Jueces ofrecidos en el Lab. La lista es también el WHITELIST del servidor: por la API
# solo se puede pedir uno de estos providers, nunca un modelo arbitrario. Las descripciones
# salen de lo que medimos leyendo el razonamiento crudo de cada uno (no marketing).
# Modelos ofrecidos. El selector queda (van a sumarse más), pero Qwen 3.6 salió del
# menú: como juez rinde 1/10 y tarda 8× más, y como agente no sostiene el loop.
JUDGES = [
    {"id": "gemma4", "model": "gemma4:26b", "label": "Gemma 4 26B (MoE)", "default": True,
     "desc": "Modelo actual de Google, Mixture-of-Experts (4B activos). Tool-calling nativo. "
             "Único modelo habilitado por ahora: es el que sostiene el loop del agente y juzga bien."},
]
ALLOWED_PROVIDERS = {j["id"] for j in JUDGES}
DEFAULT_PROVIDER = next(j["id"] for j in JUDGES if j.get("default"))

REVIEW_STATUSES = ("interesting", "actionable", "discarded")
# Estados desde los que el dueño puede operar. 'importing'/'imported'/'proposed'/'discarded'
# NO: una vez propuesto/en importación, el candidato pertenece al flujo del dueño.
REVIEWABLE_FROM = ("candidate", "interesting", "actionable")
PROPOSABLE_FROM = ("candidate", "interesting", "actionable")


# ---------- Usuarios (tokens hasheados) ----------

def load_users():
    try:
        with open(USERS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(users):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, USERS_PATH)
    # root escribe; el usuario del servicio solo lee.
    os.chmod(USERS_PATH, 0o640)
    _chgrp_or_fail(USERS_PATH, SERVICE_GROUP)


def _chgrp_or_fail(path, group):
    """Asigna el grupo del servicio, y si no puede lo DICE.

    Antes esto se tragaba KeyError/PermissionError en silencio. Con el grupo
    parametrizable, un nombre equivocado dejaba el archivo como root:root 0640:
    nadie podia autenticarse y el boton quedaba muerto, sin un solo mensaje.
    Cerrado en seguridad pero silencioso en operacion, que es el peor modo.
    """
    import grp as _g
    try:
        gid = _g.getgrnam(group).gr_gid
    except KeyError:
        sys.exit(f"[playground] el grupo de servicio '{group}' no existe. "
                 f"Creralo o defini MAPA_GROUP con el nombre correcto.")
    try:
        os.chown(path, 0, gid)
    except PermissionError:
        sys.exit(f"[playground] sin permiso para asignar {path} al grupo '{group}'. "
                 f"Corre este comando como root.")


def user_from_token(token):
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    for name, u in load_users().items():
        if not u.get("disabled") and hmac.compare_digest(u.get("sha256", ""), digest):
            return name
    return None


def user_is_active(name):
    user = load_users().get(name) or {}
    return bool(user) and not user.get("disabled")


def sessions():
    global _sessions
    if _sessions is None:
        raw = os.environ.get("MAPA_SESSION_COOKIE", f"mapa_pg_session_{PORT}")
        cookie_name = "".join(c if c.isalnum() or c == "_" else "_" for c in raw)
        _sessions = SessionStore(cookie_name, secure=os.environ.get("MAPA_SESSION_SECURE") == "1")
    return _sessions


def cli_user(argv):
    action = argv[0] if argv else "list"
    if action == "add":
        name = argv[1]
        if not name.isidentifier() and not name.replace("-", "_").isidentifier():
            sys.exit("[playground] nombre inválido (letras/números/_/-)")
        if is_reserved_reviewer(name):
            sys.exit(f"[playground] «{name}» es una identidad reservada del ledger "
                     "(dueño, director o marca de procedencia): elegí otro nombre.")
        users = load_users()
        token = secrets.token_urlsafe(32)
        users[name] = {"sha256": hashlib.sha256(token.encode()).hexdigest(),
                       "disabled": False, "created_at": discover.now_iso()}
        save_users(users)
        print(f"[playground] usuario «{name}» creado. Token (mostrado UNA vez):\n{token}")
    elif action == "disable":
        name = argv[1]
        users = load_users()
        if name not in users:
            sys.exit(f"[playground] no existe: {name}")
        users[name]["disabled"] = True
        save_users(users)
        pq.cancel_user_jobs(name)  # sin esto, un token revocado sigue consumiendo GPU hasta 30 min
        print(f"[playground] «{name}» deshabilitado; jobs cancelados/terminados")
    else:
        for name, u in sorted(load_users().items()):
            print(f"{name}  disabled={bool(u.get('disabled'))}  created={u.get('created_at')}")
    return 0


# ---------- Botón del director (F20) ----------
# Separación de privilegios: el playground (mapa-reader) NO ejecuta systemd; solo
# valida la contraseña y deja un request file en discovery/director_requests/. Un
# watcher root (mapa-director-button.path) lo consume y dispara director.sh con
# parámetros FIJOS. La web aprieta un timbre; jamás elige qué se ejecuta.
DIRECTOR_CP = os.path.join(DMAPA, "director")             # control plane (root:mapa, RO para la web)
BUTTON_HASH = os.path.join(DIRECTOR_CP, "private", "button.json")   # solo root lo escribe; la web NO lo lee
BUTTON_HASH_PUB = os.path.join(DIRECTOR_CP, "button_pub.json")      # hash legible por grupo mapa (verificación)
REQ_DIR = os.path.join(DMAPA, "discovery", "director_requests")     # único canal web→root
ATTEMPTS_PATH = os.path.join(DMAPA, "discovery", "director_button_attempts.json")
DIGEST_LATEST = os.path.join(DIRECTOR_CP, "digests", "latest.md")
DIR_LOCK = os.path.join(DIRECTOR_CP, "private", "director.lock")
_attempts_lock = threading.Lock()


def _scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def cli_director_pass(argv):
    """playground.py director-pass set  — lee la contraseña de stdin (root)."""
    if not argv or argv[0] != "set":
        sys.exit("uso: playground.py director-pass set   (contraseña por stdin)")
    pw = sys.stdin.readline().strip()
    if len(pw) < 8:
        sys.exit("[playground] contraseña mínima 8 chars")
    salt = secrets.token_bytes(16)
    rec = {"salt": salt.hex(), "hash": _scrypt(pw, salt), "set_at": discover.now_iso()}
    os.makedirs(os.path.dirname(BUTTON_HASH), exist_ok=True)
    for path, mode, grp_name in ((BUTTON_HASH, 0o600, None), (BUTTON_HASH_PUB, 0o640, SERVICE_GROUP)):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path); os.chmod(path, mode)
        if grp_name:
            _chgrp_or_fail(path, grp_name)
    print("[playground] contraseña del director actualizada.")
    return 0


def _check_button_password(pw):
    try:
        with open(BUTTON_HASH_PUB) as f:
            rec = json.load(f)
        return hmac.compare_digest(_scrypt(pw, bytes.fromhex(rec["salt"])), rec["hash"])
    except (OSError, ValueError, KeyError):
        return False


def _button_rate_ok(user):
    """Máx 5 intentos/hora por usuario, persistidos (sobreviven restart). True si OK."""
    import time
    now = int(time.time())
    with _attempts_lock:
        try:
            with open(ATTEMPTS_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        hist = [t for t in data.get(user, []) if now - t < 3600]
        if len(hist) >= 5:
            data[user] = hist
            _write_attempts(data)
            return False
        hist.append(now)
        data[user] = hist
        _write_attempts(data)
        return True


def _write_attempts(data):
    tmp = ATTEMPTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, ATTEMPTS_PATH)


def _director_running():
    """El director corre si su lock está tomado (flock no bloqueante de prueba)."""
    import fcntl
    try:
        f = open(DIR_LOCK, "r")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        f.close()


def _last_request_ts():
    try:
        return max((int(e.name.split("-")[1]) for e in os.scandir(REQ_DIR)
                    if e.name.startswith("req-")), default=0)
    except OSError:
        return 0


# ---------- Helpers HTTP (patrón serve.py) ----------

def _send(h, code, obj, headers=None):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Cache-Control", "no-cache")
    for key, value in (headers or {}).items():
        h.send_header(key, value)
    h.end_headers()
    h.wfile.write(body)


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


def _safe_join(root, rel):
    p = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    return p if _inside(p, os.path.realpath(root)) else None


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".woff2": "font/woff2", ".gz": "application/gzip"}


def _send_file(h, path, mime=None):
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        return _send(h, 404, {"error": "not found"})
    mime = mime or MIME.get(os.path.splitext(path)[1], "application/octet-stream")
    h.send_response(200)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Cache-Control", "no-cache")
    if mime.startswith("text/html"):
        h.send_header("Content-Security-Policy", CSP)
    h.end_headers()
    h.wfile.write(body)


def _static(h, root, prefix, spa=False, index="index.html"):
    rel = urlparse(h.path).path[len(prefix):] or index
    p = _safe_join(root, rel)
    if not p or not os.path.isfile(p):
        if spa and not rel.startswith("assets/"):
            p = os.path.join(root, index)
            if not os.path.isfile(p):
                return _send(h, 503, {"error": "lab UI no construida", "hint": "npm run build:lab"})
        else:
            return _send(h, 404, {"error": "not found"})
    return _send_file(h, p)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------- Handler ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --- auth ---
    def _identity(self):
        cached = getattr(self, "_identity_cache", None)
        if cached is not None:
            return cached
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        user = user_from_token(token)
        if user:
            self._identity_cache = {"user": user, "mode": "bearer", "session": None}
            return self._identity_cache
        session = sessions().authenticate(self.headers.get("Cookie", ""), user_is_active)
        self._identity_cache = ({"user": session["user"], "mode": "session", "session": session}
                                if session else {})
        return self._identity_cache

    def _user(self):
        return self._identity().get("user")

    def _same_origin(self):
        origin = self.headers.get("Origin", "")
        if not origin:
            return False
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host", "")

    # --- GET ---
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/lab-v3" or u.path.startswith("/lab-v3/"):
                return _static(self, LAB_V3_DIST, "/lab-v3/", spa=True, index="v3-lab.html")
            if u.path == "/lab-v2" or u.path.startswith("/lab-v2/"):
                return _static(self, LAB_V2_DIST, "/lab-v2/", spa=True, index="v2-lab.html")
            if u.path == "/lab" or u.path.startswith("/lab/"):
                return _static(self, LAB_DIST, "/lab/", spa=True, index="lab.html")
            if u.path == "/pg/health":
                con = pq.open_jobs()
                jobs = dict(con.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
                cands = con.execute("SELECT count(*) FROM candidates").fetchone()[0]
                con.close()
                return _send(self, 200, {"service": "discovery playground", "sandbox": True,
                                         "mode": "structural-only" if STRUCTURAL_ONLY else "full",
                                         "llm_screening": not STRUCTURAL_ONLY,
                                         "jobs": jobs, "candidates": cands,
                                         "operators": list(STRUCTURAL_OPERATORS if STRUCTURAL_ONLY else pq.VALID_OPERATORS)})
            if u.path == "/search":
                q = (qs.get("q", [""])[0] or "").strip()
                if not q:
                    return _send(self, 400, {"error": "missing q"})
                if len(q) > Q_MAX:
                    return _send(self, 413, {"error": "q too long"})
                try:
                    k = max(1, min(int(qs.get("k", ["10"])[0]), 50))
                except Exception:
                    k = 10
                if not _sem.acquire(timeout=5):
                    return _send(self, 429, {"error": "busy"})
                try:
                    return _send(self, 200, tier1.search_json(
                        q, k, allow_vector=False,
                        kind=qs.get("kind", [None])[0], project=qs.get("project", [None])[0]))
                finally:
                    _sem.release()
            if u.path == "/doc":
                d = tier1.get_doc(qs.get("id", [""])[0] or "")
                return _send(self, 200, d) if d else _send(self, 404, {"error": "not found"})
            if u.path.startswith("/ui/v2"):
                code, obj = V2_API.dispatch(u.path, qs)
                return _send(self, code, obj)
            if u.path.startswith("/ui/"):
                return self._handle_ui(u, qs)
            # --- autenticado de acá para abajo ---
            identity = self._identity()
            user = identity.get("user")
            if not user:
                return _send(self, 401, {"error": "sesión o token Bearer requerido"})
            if u.path == "/pg/session":
                csrf = sessions().rotate_csrf(identity["session"]["session_hash"]) if identity["mode"] == "session" else None
                return _send(self, 200, {"user": user, "auth_mode": identity["mode"], "csrf": csrf,
                                         "absolute_expires": (identity.get("session") or {}).get("absolute_expires")})
            if u.path == "/pg/operators":
                return _send(self, 200, {"operators": list(STRUCTURAL_OPERATORS if STRUCTURAL_ONLY else pq.VALID_OPERATORS),
                                         "judges": [] if STRUCTURAL_ONLY else JUDGES,
                                         "default_judge": "" if STRUCTURAL_ONLY else DEFAULT_PROVIDER,
                                         "mode": "structural-only" if STRUCTURAL_ONLY else "full",
                                         "llm_screening": not STRUCTURAL_ONLY,
                                         "limits": {"limit_max": pq.LIMIT_MAX,
                                                    "queued_per_user": pq.MAX_QUEUED_PER_USER,
                                                    "jobs_per_day": pq.MAX_JOBS_PER_USER_DAY}})
            if u.path == "/pg/jobs":
                con = pq.open_jobs()
                rows = [dict(r) for r in con.execute(
                    "SELECT id, owner, status, params_json, created_at, started_at, finished_at, error "
                    "FROM jobs WHERE owner=? ORDER BY id DESC LIMIT 50", (user,))]
                con.close()
                return _send(self, 200, {"user": user, "jobs": rows})
            if u.path == "/pg/campaigns":
                con = pq.open_jobs()
                rows = [dict(r) for r in con.execute(
                    "SELECT id, name, owner, status, created_at, finished_at FROM campaigns "
                    "ORDER BY id DESC LIMIT 50")]
                con.close()
                return _send(self, 200, {"campaigns": rows})
            if u.path == "/pg/director":
                running = _director_running()
                out = {"running": running, "last_digest": None}
                try:
                    st = os.stat(DIGEST_LATEST)
                    with open(DIGEST_LATEST) as f:
                        head = [ln.strip() for ln in f.read().splitlines()[:3] if ln.strip()]
                    out["last_digest"] = {"mtime": int(st.st_mtime),
                                          "title": head[0] if head else "",
                                          "veredicto": next((h for h in head if "Veredicto" in h), "")}
                except OSError:
                    pass
                return _send(self, 200, out)
            if u.path == "/pg/digest":
                try:
                    with open(DIGEST_LATEST) as f:
                        return _send(self, 200, {"markdown": f.read()[:50000]})
                except OSError:
                    return _send(self, 404, {"error": "sin digest todavía"})
            if u.path == "/pg/candidates":
                scope = (qs.get("scope", ["all"])[0] or "all")
                status = qs.get("status", [None])[0]
                dtype = qs.get("type", [None])[0]
                text_q = (qs.get("q", [""])[0] or "").strip()
                if len(text_q) > 160:
                    return _send(self, 413, {"error": "q too long", "max": 160})
                try:
                    limit = max(1, min(int(qs.get("limit", ["200"])[0]), 200))
                    offset = max(0, min(int(qs.get("offset", ["0"])[0]), 1_000_000))
                except (TypeError, ValueError):
                    return _send(self, 400, {"error": "limit/offset inválidos"})
                surprise_raw = qs.get("min_surprise", [None])[0]
                try:
                    min_surprise = None if surprise_raw in (None, "") else float(surprise_raw)
                except (TypeError, ValueError):
                    return _send(self, 400, {"error": "min_surprise inválido"})
                if min_surprise is not None and not 0 <= min_surprise <= 1:
                    return _send(self, 400, {"error": "min_surprise debe estar entre 0 y 1"})
                q = ("SELECT id, owner, discovery_type, status, title, novelty_score, support_score, "
                     "unexpectedness_score, flags_json, created_at FROM candidates WHERE 1=1")
                params = []
                if scope == "mine":
                    q += " AND owner=?"
                    params.append(user)
                if status:
                    q += " AND status=?"
                    params.append(status)
                if dtype:
                    q += " AND discovery_type=?"
                    params.append(dtype)
                if text_q:
                    escaped = text_q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                    q += " AND (title LIKE ? ESCAPE '!' OR claim LIKE ? ESCAPE '!')"
                    params.extend((f"%{escaped}%", f"%{escaped}%"))
                if min_surprise is not None:
                    q += " AND unexpectedness_score>=?"
                    params.append(min_surprise)
                con = pq.open_jobs()
                total = con.execute(f"SELECT count(*) FROM ({q})", params).fetchone()[0]
                q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
                rows = [dict(r) for r in con.execute(q, params + [limit, offset])]
                con.close()
                return _send(self, 200, {"user": user, "total": total, "shown": len(rows),
                                         "offset": offset, "candidates": rows})
            if u.path == "/pg/graph":
                # Grafo de TUS candidatos (no de los hallazgos publicados: eso es el atlas).
                # Nodos: candidato + sus documentos fuente. Aristas tipadas por rol.
                scope = (qs.get("scope", ["mine"])[0] or "mine")
                con = pq.open_jobs()
                q = "SELECT id, owner, discovery_type, status, title, flags_json FROM candidates WHERE 1=1"
                params = []
                if scope == "mine":
                    q += " AND owner=?"
                    params.append(user)
                status = (qs.get("status", [""])[0] or "")[:64]
                dtype = (qs.get("type", [""])[0] or "")[:64]
                if status:
                    q += " AND status=?"
                    params.append(status)
                if dtype:
                    q += " AND discovery_type=?"
                    params.append(dtype)
                cands = [dict(r) for r in con.execute(q, params)]
                nodes, edges, seen = {}, {}, set()
                # Siembra circular: ForceAtlas2 NO puede separar nodos que arrancan en el
                # mismo punto (repulsión sin dirección) — todos quedarían apilados en uno.
                import math as _m
                _i = [0]

                def _xy():
                    a = _i[0] * 2.399963  # ángulo áureo → reparto parejo
                    r = 1 + _i[0] * 0.35
                    _i[0] += 1
                    return round(r * _m.cos(a), 3), round(r * _m.sin(a), 3)
                ROLE_EDGE = {"support": "source_of", "counter": "challenges",
                             "bridge": "bridges", "target": "flags_freshness", "context": "contains"}
                for c in cands:
                    cid = f"cand:{c['id']}"
                    cx, cy = _xy()
                    nodes[cid] = {"id": cid, "label": (c["title"] or c["id"])[:60], "kind": "discovery",
                                  "project": c["owner"] or "?", "title": c["title"] or c["id"],
                                  "doc_id": None, "path_rel": c["id"], "size": 3, "degree": 0,
                                  "curated": False, "duplicate_of": None, "x": cx, "y": cy}
                    for s in con.execute("SELECT role, doc_id, project, kind, title FROM candidate_sources "
                                         "WHERE candidate_id=?", (c["id"],)):
                        did = f"doc:{s['doc_id']}"
                        if did not in nodes:
                            dx, dy = _xy()
                            nodes[did] = {"id": did, "label": (s["title"] or s["doc_id"])[:60],
                                          "kind": s["kind"] or "fs_doc", "project": s["project"] or "?",
                                          "title": s["title"] or s["doc_id"], "doc_id": s["doc_id"],
                                          "path_rel": s["doc_id"], "size": 1, "degree": 0,
                                          "curated": False, "duplicate_of": None, "x": dx, "y": dy}
                        et = ROLE_EDGE.get(s["role"], "contains")
                        eid = f"{et}:{c['id']}:{s['doc_id']}"
                        if eid not in seen:
                            seen.add(eid)
                            edges[eid] = {"id": eid, "source": cid, "target": did, "type": et,
                                          "weight": 1.5, "directed": True}
                            nodes[cid]["degree"] += 1
                            nodes[did]["degree"] += 1
                con.close()
                from collections import Counter
                return _send(self, 200, {
                    "schema_version": 1, "generation": "sandbox", "truncated": False,
                    "nodes": list(nodes.values()), "edges": list(edges.values()),
                    "counts": {"nodes": len(nodes), "edges": len(edges),
                               "by_kind": dict(Counter(n["kind"] for n in nodes.values())),
                               "by_project": dict(Counter(n["project"] for n in nodes.values())),
                               "by_edge_type": dict(Counter(e["type"] for e in edges.values()))}})
            if u.path == "/pg/candidate":
                cid = (qs.get("id", [""])[0] or "").strip()
                if not cid or len(cid) > 128:
                    return _send(self, 400, {"error": "missing id"})
                con = pq.open_jobs()
                row = con.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
                if not row:
                    con.close()
                    return _send(self, 404, {"error": "not found"})
                cand = dict(row)
                cand["sources"] = [dict(s) for s in con.execute(
                    "SELECT role, doc_id, project, kind, title, snippet, score, rank "
                    "FROM candidate_sources WHERE candidate_id=? ORDER BY role, rank", (cid,))]
                cand["reviews"] = [dict(r) for r in con.execute(
                    "SELECT reviewer, from_status, to_status, note, created_at "
                    "FROM candidate_reviews WHERE candidate_id=? ORDER BY created_at", (cid,))]
                con.close()
                return _send(self, 200, cand)
            return _send(self, 404, {"error": "unknown endpoint"})
        except Exception:
            return _send(self, 500, {"error": "internal"})

    def _handle_ui(self, u, qs):
        root = os.path.realpath(UI_LINK) if os.path.islink(UI_LINK) else UI_LINK
        if not os.path.isdir(root):
            return _send(self, 503, {"error": "ui projection missing"})
        if u.path == "/ui/manifest":
            return _send_file(self, os.path.join(root, "manifest.json"), MIME[".json"])
        if u.path == "/ui/stats":
            return _send_file(self, os.path.join(root, "stats.json"), MIME[".json"])
        if u.path == "/ui/tree":
            return _send_file(self, os.path.join(root, "tree.json"), MIME[".json"])
        if u.path != "/ui/graph":
            return _send(self, 404, {"error": "not found"})
        view = (qs.get("view", [""])[0] or "").strip()
        files = {"global": "graph_global.json", "macro": "graph_macro.json",
                 "cross": "edges_cross.json", "discovery": "graph_discovery.json"}
        if view in files:
            return _send_file(self, os.path.join(root, files[view]), MIME[".json"])
        if view == "project":
            proj = (qs.get("project", [""])[0] or "")[:128]
            man = _read_json(os.path.join(root, "manifest.json")) or {}
            for p in man.get("projects", []):
                if p.get("project") == proj:
                    target = _safe_join(root, p.get("graph_file", ""))
                    if target:
                        return _send_file(self, target, MIME[".json"])
            return _send(self, 404, {"error": "project not found"})
        if view == "neighbors":
            doc_id = (qs.get("id", [""])[0] or "")[:Q_MAX]
            nm = _read_json(os.path.join(root, "neighbors_map.json")) or {}
            rel = nm.get("map", nm).get(doc_id)
            target = _safe_join(root, rel) if rel else None
            if target:
                return _send_file(self, target, MIME[".json"])
            return _send(self, 404, {"error": "no neighbors"})
        return _send(self, 400, {"error": "invalid view"})

    # --- POST ---
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return _send(self, 415, {"error": "Content-Type debe ser application/json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= BODY_MAX:
                return _send(self, 413, {"error": f"body inválido (max {BODY_MAX} bytes)"})
            try:
                body = json.loads(self.rfile.read(length))
                assert isinstance(body, dict)
            except Exception:
                return _send(self, 400, {"error": "JSON inválido"})

            if u.path == "/pg/session":
                token = str(body.get("token") or "")
                user = user_from_token(token)
                if not user:
                    return _send(self, 401, {"error": "token inválido"})
                raw, csrf, expires = sessions().create(user)
                return _send(self, 201, {"user": user, "auth_mode": "session", "csrf": csrf,
                                         "absolute_expires": expires},
                             {"Set-Cookie": sessions().cookie_value(raw)})

            identity = self._identity()
            user = identity.get("user")
            if not user:
                return _send(self, 401, {"error": "sesión o token Bearer requerido"})
            if identity["mode"] == "session":
                if not self._same_origin():
                    return _send(self, 403, {"error": "origen inválido"})
                if not sessions().verify_csrf(identity["session"], self.headers.get("X-CSRF-Token", "")):
                    return _send(self, 403, {"error": "CSRF inválido"})

            if u.path == "/pg/run":
                if STRUCTURAL_ONLY:
                    if str(body.get("task") or "").strip():
                        return _send(self, 503, {"error": "agente dirigido desactivado: esta instancia ofrece operadores estructurales sin LLM"})
                    ops = body.get("operators")
                    if ops == "all":
                        ops = list(STRUCTURAL_OPERATORS)
                    if not isinstance(ops, list) or not ops or any(o not in STRUCTURAL_OPERATORS for o in ops):
                        return _send(self, 400, {"error": "solo están habilitados: " + ", ".join(STRUCTURAL_OPERATORS)})
                    body["operators"] = ops
                # Whitelist de juez: por la API solo los providers de JUDGES, nunca un modelo
                # arbitrario. Ausente → el juez por defecto (gemma).
                prov = body.get("provider") or DEFAULT_PROVIDER
                if prov not in ALLOWED_PROVIDERS:
                    return _send(self, 400, {"error": f"juez inválido (válidos: {', '.join(sorted(ALLOWED_PROVIDERS))})"})
                body["provider"] = prov
                jid, err = pq.enqueue(user, body)
                if err:
                    return _send(self, err[0], {"error": err[1]})
                return _send(self, 200, {"job_id": jid, "status": "queued"})

            if u.path == "/pg/delete":
                # Borrado definitivo de candidatos PROPIOS. No se puede borrar lo ya propuesto
                # o importado (está en el flujo de publicación del dueño).
                ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
                if not isinstance(ids, list) or not ids or len(ids) > 100:
                    return _send(self, 400, {"error": "mandá id o ids[] (máx 100)"})
                con = pq.open_jobs()
                try:
                    borrados, rechazados = [], []
                    for cid in [str(i)[:128] for i in ids]:
                        row = con.execute("SELECT owner, status FROM candidates WHERE id=?", (cid,)).fetchone()
                        if not row:
                            continue
                        if row["owner"] != user:
                            rechazados.append({"id": cid, "razón": "no sos el dueño"})
                            continue
                        if row["status"] in ("proposed", "importing", "imported"):
                            rechazados.append({"id": cid, "razón": f"ya está {row['status']} (en el flujo de publicación)"})
                            continue
                        con.execute("BEGIN IMMEDIATE")
                        con.execute("DELETE FROM candidate_sources WHERE candidate_id=?", (cid,))
                        con.execute("DELETE FROM candidate_reviews WHERE candidate_id=?", (cid,))
                        con.execute("DELETE FROM candidates WHERE id=?", (cid,))
                        con.commit()
                        borrados.append(cid)
                    return _send(self, 200, {"borrados": len(borrados), "rechazados": rechazados})
                finally:
                    con.close()
            if u.path in ("/pg/review", "/pg/propose"):
                cid = str(body.get("id") or "")[:128]
                note = str(body.get("note") or "")[:2000]
                con = pq.open_jobs()
                try:
                    row = con.execute("SELECT id, owner, status FROM candidates WHERE id=?", (cid,)).fetchone()
                    if not row:
                        return _send(self, 404, {"error": "candidato no encontrado"})
                    if row["owner"] != user:
                        return _send(self, 403, {"error": "solo el dueño puede modificar su candidato"})
                    if u.path == "/pg/review":
                        to = str(body.get("status") or "")
                        if to not in REVIEW_STATUSES:
                            return _send(self, 400, {"error": f"status válidos: {', '.join(REVIEW_STATUSES)}"})
                        if row["status"] not in REVIEWABLE_FROM:
                            return _send(self, 409, {"error": f"no editable desde status={row['status']} "
                                                     "(ya propuesto/importado)"})
                    else:
                        to = "proposed"
                        if row["status"] not in PROPOSABLE_FROM:
                            return _send(self, 409, {"error": f"no proponible desde status={row['status']}"})
                    if not discover.cas_update_status(con, cid, row["status"], to,
                                                      review=(user, note)):
                        return _send(self, 409, {"error": "conflicto: el candidato cambió de estado — releé"})
                    return _send(self, 200, {"id": cid, "status": to})
                finally:
                    con.close()
            if u.path == "/pg/director":
                if STRUCTURAL_ONLY:
                    return _send(self, 503, {"error": "director desactivado en modo estructural sin LLM"})
                # Botón: valida contraseña + rate limit, deja un request file. NO ejecuta nada.
                import time
                pw = str(body.get("password") or "")
                if not _button_rate_ok(user):
                    return _send(self, 429, {"error": "demasiados intentos — esperá una hora"})
                if not pw or not _check_button_password(pw):
                    return _send(self, 403, {"error": "denegado"})
                if _director_running():
                    return _send(self, 409, {"error": "el director ya está corriendo"})
                now = int(time.time())
                if now - _last_request_ts() < 600:
                    return _send(self, 429, {"error": "hay un disparo reciente — esperá 10 min"})
                os.makedirs(REQ_DIR, exist_ok=True)
                nonce = secrets.token_hex(8)
                path = os.path.join(REQ_DIR, f"req-{now}-{nonce}.json")
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
                with os.fdopen(fd, "w") as f:
                    json.dump({"ts": now, "user": user, "nonce": nonce}, f)
                    f.flush(); os.fsync(f.fileno())
                return _send(self, 202, {"status": "encolado", "nonce": nonce})
            return _send(self, 404, {"error": "unknown endpoint"})
        except Exception:
            return _send(self, 500, {"error": "internal"})

    def _method_not_allowed(self):
        return _send(self, 405, {"error": "method not allowed"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path != "/pg/session":
            return self._method_not_allowed()
        identity = self._identity()
        if not identity.get("user") or identity.get("mode") != "session":
            return _send(self, 401, {"error": "sesión requerida"})
        if not self._same_origin():
            return _send(self, 403, {"error": "origen inválido"})
        if not sessions().verify_csrf(identity["session"], self.headers.get("X-CSRF-Token", "")):
            return _send(self, 403, {"error": "CSRF inválido"})
        sessions().delete(self.headers.get("Cookie", ""))
        return _send(self, 200, {"status": "signed_out"},
                     {"Set-Cookie": sessions().cookie_value("", clear=True)})

    do_PUT = do_PATCH = _method_not_allowed


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "user":
        return cli_user(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "director-pass":
        return cli_director_pass(sys.argv[2:])
    # Invariante: safe_bind() ya rechazo comodines y direcciones publicas.
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.timeout = 15
    worker = pq.Worker()
    worker.start()
    print(f"[playground] sirviendo en http://{BIND}:{PORT} (lab: /lab/, API: /pg/*)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        worker.stop_event.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
