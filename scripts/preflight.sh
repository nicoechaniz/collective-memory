#!/usr/bin/env bash
# preflight.sh — verifica que la maquina pueda correr esto, ANTES de instalar.
#
# Cada chequeo esta aca porque su ausencia produce un fallo dificil de
# diagnosticar mas adelante: una dependencia que no es obvia, una capacidad del
# interprete que no se ve en la version, o una garantia de seguridad que systemd
# acepta sin aplicar.
#
# Uso: scripts/preflight.sh [--with-systemd]
# Exit: 0 todo bien · 1 hay bloqueantes
set -uo pipefail

WITH_SYSTEMD=0
[[ "${1:-}" == "--with-systemd" ]] && WITH_SYSTEMD=1

PY="${MAPA_PYTHON:-python3}"
fail=0
warn=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  \033[33m!\033[0m %s\n' "$1"; warn=$((warn+1)); }

echo "== binarios =="
need_bin() {
  if command -v "$1" >/dev/null 2>&1; then ok "$1 — $2"
  else bad "falta '$1' ($2). Instalá: $3"; fi
}
need_bin python3 "interprete"                    "python3"
need_bin node    "layout del grafo del atlas"    "nodejs (el pipeline lo ejecuta en cada corrida, no solo en el build)"
need_bin npm     "build del frontend"            "npm"
need_bin pdftotext "extraccion de texto de PDF"  "poppler-utils"
need_bin flock   "single-writer del bibliotecario" "util-linux"
command -v nvidia-smi >/dev/null 2>&1 \
  && ok "GPU detectada — el indexado va a ser bastante mas rapido" \
  || note "sin GPU: funciona igual en CPU, pero el indexado inicial es lento"

echo
echo "== capacidades del interprete =="
# No alcanza con mirar la version: muchos builds de Python (macOS del sistema,
# varios pyenv, imagenes slim) vienen sin extensiones cargables o sin FTS5, y el
# fallo recien aparece al indexar.
$PY - <<'EOF' 2>/dev/null && ok "sqlite con extensiones cargables" || bad "el Python de '$PY' no soporta extensiones cargables de sqlite (necesita --enable-loadable-sqlite-extensions). Usá otro interprete."
import sqlite3, sys
con = sqlite3.connect(":memory:")
con.enable_load_extension(True)
EOF

$PY - <<'EOF' 2>/dev/null && ok "sqlite con FTS5" || bad "el Python de '$PY' no trae FTS5, necesario para la busqueda lexica."
import sqlite3
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
EOF

echo
echo "== configuracion =="
if [[ -z "${MAPA_ROOT:-}" ]]; then
  note "MAPA_ROOT no esta definido todavia (lo define install.sh)"
else
  CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../mapa" && pwd)"
  out="$(MAPA_ROOT="$MAPA_ROOT" $PY -c "
import sys; sys.path.insert(0, '$CODE')
import mapa_config as c
for p in c.validate_config(with_systemd=$([[ $WITH_SYSTEMD -eq 1 ]] && echo True || echo False)): print(p)
" 2>&1)"
  if [[ -z "$out" ]]; then ok "MAPA_ROOT=$MAPA_ROOT es usable"
  else while IFS= read -r l; do bad "$l"; done <<<"$out"; fi
fi

if [[ $WITH_SYSTEMD -eq 1 ]]; then
  echo
  echo "== filtro de red de systemd =="
  # IPAddressDeny/Allow es LA garantia de no-exposicion, pero necesita cgroup v2
  # unificado + BPF. Donde no hay soporte systemd loguea un warning y arranca
  # IGUAL, sin filtrar: la unidad parece bien y la garantia no existe.
  supported=1
  [[ -f /sys/fs/cgroup/cgroup.controllers ]] || supported=0
  systemd-analyze --version >/dev/null 2>&1 || supported=0
  if [[ $supported -eq 1 ]]; then
    ok "cgroup v2 unificado presente: IPAddressDeny/Allow va a aplicar"
  else
    bind="${MAPA_BIND:-127.0.0.1}"
    if [[ "$bind" == 127.* || "$bind" == "::1" ]]; then
      note "sin cgroup v2 el filtro de red no se aplica, pero el bind es loopback: no hay exposicion que filtrar"
    else
      bad "sin cgroup v2 unificado el filtro de red NO se aplica, y el bind ($bind) no es loopback: el servicio quedaria sin la capa que garantiza la no-exposicion"
    fi
  fi
  command -v systemctl >/dev/null 2>&1 && ok "systemctl disponible" || bad "falta systemd para instalar las unidades"
  [[ "$(id -u)" == "0" ]] && ok "corriendo como root (necesario para las unidades)" \
                          || bad "instalar unidades systemd requiere root"
fi

echo
if [[ $fail -gt 0 ]]; then
  echo "preflight: $fail bloqueante(s), $warn aviso(s)."
  exit 1
fi
echo "preflight: todo en orden ($warn aviso(s))."
