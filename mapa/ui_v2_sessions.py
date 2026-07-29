#!/usr/bin/env python3
"""Short-lived browser sessions layered on top of existing Bearer users."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie

try:
    from mapa_config import DMAPA
except ImportError:  # runtime historico del host, anterior a mapa_config.py
    _ROOT_RAW = os.environ.get("MAPA_ROOT")
    if not _ROOT_RAW:
        raise RuntimeError("MAPA_ROOT is required when mapa_config is unavailable")
    _ROOT = os.path.abspath(_ROOT_RAW)
    DMAPA = os.path.abspath(os.environ.get("MAPA_DATA", os.path.join(_ROOT, ".mapa")))

IDLE_SECONDS = 12 * 60 * 60
ABSOLUTE_SECONDS = 7 * 24 * 60 * 60


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SessionStore:
    def __init__(self, cookie_name, path=None, secure=False):
        self.cookie_name = cookie_name
        self.path = path or os.path.join(DMAPA, "discovery", "ui_sessions.db")
        self.secure = bool(secure)
        self._init()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=4)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=4000")
        return con

    def _init(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        con = self._connect()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS browser_sessions(
          session_hash TEXT PRIMARY KEY,
          user TEXT NOT NULL,
          csrf_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          last_seen INTEGER NOT NULL,
          absolute_expires INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS session_expiry_idx
          ON browser_sessions(last_seen,absolute_expires);
        """)
        con.commit()
        con.close()
        os.chmod(self.path, 0o660)

    def create(self, user):
        now = int(time.time())
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        con = self._connect()
        con.execute("DELETE FROM browser_sessions WHERE last_seen<? OR absolute_expires<?",
                    (now - IDLE_SECONDS, now))
        con.execute("INSERT INTO browser_sessions VALUES (?,?,?,?,?,?)",
                    (_hash(raw), user, _hash(csrf), now, now, now + ABSOLUTE_SECONDS))
        con.commit()
        con.close()
        return raw, csrf, now + ABSOLUTE_SECONDS

    def cookie_value(self, raw, clear=False):
        value = "" if clear else raw
        max_age = 0 if clear else ABSOLUTE_SECONDS
        parts = [f"{self.cookie_name}={value}", "Path=/pg", "HttpOnly", "SameSite=Strict",
                 f"Max-Age={max_age}"]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def raw_from_header(self, header):
        try:
            cookie = SimpleCookie()
            cookie.load(header or "")
            morsel = cookie.get(self.cookie_name)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def authenticate(self, header, user_is_active):
        raw = self.raw_from_header(header)
        if not raw:
            return None
        now = int(time.time())
        digest = _hash(raw)
        con = self._connect()
        row = con.execute("SELECT * FROM browser_sessions WHERE session_hash=?", (digest,)).fetchone()
        if not row or now - row["last_seen"] > IDLE_SECONDS or now >= row["absolute_expires"]:
            con.execute("DELETE FROM browser_sessions WHERE session_hash=?", (digest,))
            con.commit()
            con.close()
            return None
        if not user_is_active(row["user"]):
            con.execute("DELETE FROM browser_sessions WHERE session_hash=?", (digest,))
            con.commit()
            con.close()
            return None
        if now - row["last_seen"] >= 60:
            con.execute("UPDATE browser_sessions SET last_seen=? WHERE session_hash=?", (now, digest))
            con.commit()
        con.close()
        return {"user": row["user"], "session_hash": digest,
                "csrf_hash": row["csrf_hash"], "absolute_expires": row["absolute_expires"]}

    def rotate_csrf(self, session_hash):
        csrf = secrets.token_urlsafe(24)
        con = self._connect()
        con.execute("UPDATE browser_sessions SET csrf_hash=? WHERE session_hash=?",
                    (_hash(csrf), session_hash))
        con.commit()
        con.close()
        return csrf

    def verify_csrf(self, session, token):
        return bool(token) and hmac.compare_digest(session.get("csrf_hash", ""), _hash(token))

    def delete(self, header):
        raw = self.raw_from_header(header)
        if raw:
            con = self._connect()
            con.execute("DELETE FROM browser_sessions WHERE session_hash=?", (_hash(raw),))
            con.commit()
            con.close()
