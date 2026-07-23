#!/usr/bin/env python3
"""Watcher del botón del director (F20). Corre como root vía mapa-director-button.service.

Consume los requests que la web (mapa-reader) deja en discovery/director_requests/
y, si hay uno válido y fresco, dispara UNA corrida del director con parámetros
FIJOS. El request NO parametriza nada: la web aprieta un timbre, este código
decide qué se ejecuta. Anti-symlink (O_NOFOLLOW), anti-DoS (caps), anti-replay
(frescura + consumo total), idempotente (el propio director.lock).
"""
import json
import os
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import DMAPA, CODE_HOME  # noqa: E402

REQ_DIR = os.path.join(DMAPA, "discovery", "director_requests")
DIRECTOR_SH = os.path.join(os.path.dirname(CODE_HOME), "scripts", "director.sh")
FRESH_SEC = 120
MAX_ENTRIES = 50
MAX_SIZE = 4096

# El watcher corre como root y dispara un agente con shell. La única defensa
# contra que cualquier proceso local escale a root escribiendo un JSON es
# verificar que el request lo escribió el usuario del servicio web. Se resuelve
# EN CÓDIGO (no por permisos del directorio), y falla cerrado: si no se puede
# resolver ese uid, no se dispara nada.
_SERVE_USER = os.environ.get("MAPA_SERVE_USER", "mapa-reader")


def _writer_uid():
    import pwd
    try:
        return pwd.getpwnam(_SERVE_USER).pw_uid
    except KeyError:
        return None


def consume():
    """Devuelve True si algún request válido justifica disparar."""
    fire = False
    seen = 0
    writer_uid = _writer_uid()
    if writer_uid is None:
        # Fail-closed: sin poder resolver el uid del servicio web no hay forma de
        # distinguir un request legítimo de uno inyectado. No se dispara.
        sys.stderr.write(f"[button] no se pudo resolver el uid de '{_SERVE_USER}'; "
                         "no disparo (fail-closed). Definí MAPA_SERVE_USER.\n")
        return False
    try:
        entries = list(os.scandir(REQ_DIR))
    except OSError:
        return False
    for e in entries:
        seen += 1
        if seen > MAX_ENTRIES:
            _unlink(e.path)  # excedente: descartar sin parsear
            continue
        if not e.name.startswith("req-"):
            _unlink(e.path)
            continue
        try:
            fd = os.open(e.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            # Núcleo del anti-escalada: el request TIENE que haberlo escrito el
            # usuario del servicio web. Cualquier otro dueño = intento de inyección.
            if st.st_uid != writer_uid:
                _unlink(e.path)
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SIZE:
                continue
            body = os.read(fd, MAX_SIZE)
        finally:
            os.close(fd)
        _unlink(e.path)  # consumo total: válido o no, se remueve
        try:
            req = json.loads(body)
            ts = int(req["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if 0 <= time.time() - ts <= FRESH_SEC:
            fire = True
    return fire


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def main():
    if not consume():
        return 0
    # Disparo detached: el director dura hasta 1 h, este oneshot muere en 30 s.
    # subprocess.run (NO Popen): systemd-run --no-block registra la unidad transient
    # vía D-Bus y retorna; si el oneshot saliera antes, su cgroup mataría al
    # systemd-run hijo y la unidad nunca se crearía. La unidad transient NO es hija
    # del cgroup del botón, así que sobrevive. Su propio flock la hace idempotente.
    subprocess.run(
        ["systemd-run", f"--unit=mapa-director-run-{int(time.time())}", "--collect",
         "--no-block", DIRECTOR_SH, "--backend", "codex"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
