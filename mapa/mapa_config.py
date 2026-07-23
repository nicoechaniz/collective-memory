#!/usr/bin/env python3
"""mapa_config.py — resolución de rutas, identidad y política de bind.

Concentra lo que antes vivía hardcodeado en cada módulo. Tres ideas:

1. **Dos raíces, no una.** `MAPA_ROOT` son los DATOS (el corpus que se indexa);
   `CODE_HOME` es el CÓDIGO. En el sistema original eran el mismo directorio, lo
   que impedía instalarlo en otro lado.

2. **El bind falla cerrado.** El invariante no es "tal IP" sino "esto no queda
   expuesto a internet". `safe_bind()` lo hace portable sin debilitarlo.

3. **Las identidades privilegiadas son reservadas.** El ledger distingue al dueño
   y al director por el texto del campo `reviewer`; si un usuario cualquiera
   puede llamarse así, puede falsificar esas filas.
"""
import ipaddress
import os
import sys

CODE_HOME = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# MAPA_ROOT: env -> mapa.env -> error explícito (nunca un default silencioso)
# --------------------------------------------------------------------------

def _load_env_file():
    for cand in (os.path.join(CODE_HOME, "mapa.env"),
                 os.path.join(os.path.dirname(CODE_HOME), "mapa.env")):
        if not os.path.isfile(cand):
            continue
        out = {}
        for line in open(cand, encoding="utf-8"):
            line = line.split("#")[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out
    return {}


_ENV = _load_env_file()


def _cfg(name, default=None):
    return os.environ.get(name) or _ENV.get(name) or default


ROOT = _cfg("MAPA_ROOT")
if not ROOT:
    sys.exit(
        "[mapa] MAPA_ROOT no está definido.\n"
        "  Es el directorio del corpus que se indexa. Definilo como variable de\n"
        "  entorno o en un archivo mapa.env junto al código.\n"
        "  Ejemplo:  MAPA_ROOT=<ruta-absoluta-a-tu-corpus>"
    )
ROOT = os.path.abspath(os.path.expanduser(ROOT))

# Directorio de datos del sistema (índices, artefactos, estado).
DMAPA = _cfg("MAPA_DATA") or os.path.join(ROOT, ".mapa")
DB = os.path.join(DMAPA, "index.db")
EMB_CACHE = os.path.join(DMAPA, "emb_cache.db")
MANIFEST = os.path.join(DMAPA, "manifest.json")
MAPA = os.path.join(ROOT, "mapa")
POLICY_PATH = os.path.join(DMAPA, "corpus_policy.json")
AUDIT_JSON = os.path.join(DMAPA, "corpus_audit.json")
AUDIT_MD = os.path.join(DMAPA, "corpus_audit.md")
DISCOVERY_DB = os.path.join(DMAPA, "discovery.db")

# Control plane del director: FUERA de MAPA_ROOT a propósito. Si viviera
# adentro, el aislamiento de la unidad del modelo (que enmascara MAPA_ROOT) y
# el montaje read-write de su config serían el mismo subárbol, y la unidad no
# arrancaría.
DIRECTOR_STATE = _cfg("MAPA_DIRECTOR_STATE", "/var/lib/mapa-director")

# Grupo de sistema de los servicios (permisos de users.json y del botón).
SERVICE_GROUP = _cfg("MAPA_GROUP", "mapa")

# --------------------------------------------------------------------------
# Identidades reservadas del ledger
# --------------------------------------------------------------------------

OWNER_REVIEWER = _cfg("MAPA_OWNER_REVIEWER", "owner")
RESERVED_REVIEWER_PREFIXES = ("director:",)
RESERVED_REVIEWERS = {OWNER_REVIEWER, "import"}


def is_reserved_reviewer(name):
    """El dueño, el director y la marca de procedencia no son nombres de usuario.

    El ledger los distingue solo por este texto: quien pueda registrarse con uno
    de ellos puede fabricar filas indistinguibles del ground truth.
    """
    if not name:
        return False
    n = str(name).strip().lower()
    return (n in {r.lower() for r in RESERVED_REVIEWERS}
            or any(n.startswith(p) for p in RESERVED_REVIEWER_PREFIXES))


# --------------------------------------------------------------------------
# Bind: portable, pero nunca más débil que el original
# --------------------------------------------------------------------------

WILDCARDS = {"", "0", "0.0.0.0", "::", "*", "::0"}


def safe_bind(addr=None):
    """Devuelve una dirección de bind segura, o aborta.

    Rechaza sin excepción los comodines y cualquier dirección globalmente
    ruteable. Loopback es el default. Para escuchar en una interfaz de red hay
    que pedirlo explícito con MAPA_BIND_ALLOW_LAN=1, y ahí se advierte algo que
    suele sorprender: **una IP RFC1918 no significa privada**. En AWS/GCP/Azure
    la IP primaria de la máquina es RFC1918 con NAT 1:1 hacia una IP pública, así
    que bindearla deja el servicio accesible desde internet.
    """
    raw = addr if addr is not None else _cfg("MAPA_BIND", "127.0.0.1")
    s = str(raw).strip()

    if s.lower() in WILDCARDS:
        sys.exit(f"[mapa] bind {raw!r} rechazado: escucharía en todas las interfaces.")

    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        sys.exit(f"[mapa] bind {raw!r} rechazado: no es una dirección IP literal.")

    # IPv4 mapeada en IPv6 (::ffff:a.b.c.d) se clasifica sobre el IPv4 real.
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped

    if ip.is_unspecified:
        sys.exit(f"[mapa] bind {raw!r} rechazado: dirección no especificada.")
    if ip.is_global:
        sys.exit(f"[mapa] bind {raw!r} rechazado: es una dirección pública.")

    if not ip.is_loopback and _cfg("MAPA_BIND_ALLOW_LAN") != "1":
        sys.exit(
            f"[mapa] bind {ip} no es loopback.\n"
            "  Para escuchar fuera de la máquina definí MAPA_BIND_ALLOW_LAN=1.\n"
            "  Antes leé esto: una dirección RFC1918 NO garantiza que sea privada.\n"
            "  En la nube (AWS/GCP/Azure) la IP de la máquina es RFC1918 con NAT 1:1\n"
            "  hacia una IP pública, así que el servicio quedaría expuesto a internet.\n"
            "  La protección real la da el filtro de red de las unidades systemd."
        )
    return str(ip)


# --------------------------------------------------------------------------
# Validación de configuración (el precio de haber parametrizado)
# --------------------------------------------------------------------------

# Mientras las rutas eran literales del host, estos valores eran inalcanzables.
# Parametrizadas, son entradas de usuario: hay que rechazarlas temprano.
FORBIDDEN_ROOTS = {"/", "/usr", "/etc", "/var", "/opt", "/boot", "/run", "/bin", "/sbin", "/lib"}


def validate_config(with_systemd=False):
    """Devuelve una lista de problemas. Vacía = configuración usable."""
    problems = []
    r = ROOT.rstrip("/") or "/"

    if r in FORBIDDEN_ROOTS:
        problems.append(f"MAPA_ROOT={ROOT} es un directorio de sistema; elegí uno propio.")
    if not os.path.isdir(ROOT):
        problems.append(f"MAPA_ROOT={ROOT} no existe o no es un directorio.")
    elif os.path.islink(ROOT.rstrip("/")):
        problems.append(f"MAPA_ROOT={ROOT} es un symlink; usá la ruta real.")

    # Las unidades systemd traen ProtectHome=yes, que deja /home y /root VACÍOS
    # dentro del namespace: un corpus ahí sería invisible para los servicios.
    if with_systemd:
        for home in ("/home/", "/root/"):
            if r.startswith(home.rstrip("/")) and (r + "/").startswith(home):
                problems.append(
                    f"MAPA_ROOT={ROOT} está bajo {home.rstrip('/')}, que las unidades "
                    "systemd ocultan con ProtectHome=yes: los servicios no verían el "
                    "corpus. Movelo fuera de /home y /root, o quitá ProtectHome asumiendo "
                    "el debilitamiento (ver docs/seguridad.md)."
                )
    return problems


def existing_groups(names):
    """Filtra a los grupos que existen de verdad.

    `video`/`render` son de acceso a GPU y no existen en servidores headless ni
    en contenedores; pedirlos sin filtrar hace que la unidad no arranque, lo que
    contradice que el sistema deba instalarse en una máquina sin GPU.
    """
    import grp
    out = []
    for n in names:
        try:
            grp.getgrnam(n)
            out.append(n)
        except KeyError:
            pass
    return out


# --------------------------------------------------------------------------
# Modelo de embeddings: offline en runtime, descarga explícita en el bootstrap
# --------------------------------------------------------------------------

MODEL_ID = _cfg("MAPA_MODEL_ID", "BAAI/bge-m3")
HF_HOME = _cfg("MAPA_HF_HOME") or os.path.join(DMAPA, "hf")

os.environ.setdefault("HF_HOME", HF_HOME)
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", os.path.join(HF_HOME, "hub"))
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
if _cfg("MAPA_ALLOW_MODEL_DOWNLOAD") != "1":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def model_cache_dir():
    return os.path.join(HF_HOME, "hub", "models--" + MODEL_ID.replace("/", "--"))


if __name__ == "__main__":
    print(f"CODE_HOME       {CODE_HOME}")
    print(f"MAPA_ROOT       {ROOT}")
    print(f"MAPA_DATA       {DMAPA}")
    print(f"DIRECTOR_STATE  {DIRECTOR_STATE}")
    print(f"OWNER_REVIEWER  {OWNER_REVIEWER}")
    print(f"MODEL_ID        {MODEL_ID}")
    probs = validate_config()
    print("config: OK" if not probs else "config con problemas:")
    for p in probs:
        print("  - " + p)
