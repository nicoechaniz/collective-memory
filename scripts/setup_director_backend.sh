#!/usr/bin/env bash
# setup_director_backend.sh — prepara el directorio de configuracion del backend.
#
# Por que existe: el wrapper del director monta ese directorio como HOME de la
# unidad del modelo y escribe adentro (config del MCP, ultimo mensaje, sesion).
# Es un INSUMO, no estado que se cree solo: sin el, el arranque falla al intentar
# escribir el token. Y tiene que contener una sesion del CLI ya autenticada,
# porque la unidad corre sin poder abrir un navegador ni pedir input.
#
# Uso: sudo scripts/setup_director_backend.sh <codex|claude>
set -Eeuo pipefail

[[ "$(id -u)" == "0" ]] || { echo "Requiere root." >&2; exit 1; }
BACKEND="${1:-}"
[[ "$BACKEND" == "codex" || "$BACKEND" == "claude" ]] || {
  echo "Uso: $0 <codex|claude>" >&2; exit 2; }

U_BRAIN="${MAPA_USER_BRAIN:-mapa-director-brain}"
STATE="${MAPA_DIRECTOR_STATE:-/var/lib/mapa-director}"
CFG="$STATE/${BACKEND}-config"

id -u "$U_BRAIN" >/dev/null 2>&1 || {
  echo "Falta el usuario $U_BRAIN. Corré antes: scripts/setup_director_users.sh" >&2; exit 1; }

mkdir -p "$CFG"
chown "$U_BRAIN":"$U_BRAIN" "$CFG"
chmod 0700 "$CFG"
echo "  creado $CFG (dueño $U_BRAIN, 0700)"

# El wrapper lee backends.json del control plane; sin él el director sale por
# "backend no habilitado" aunque estén los usuarios. Se materializa desde el
# ejemplo (todo deshabilitado) para que el operador lo edite y habilite uno.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -f "$STATE/backends.json" ]]; then
  cp "$REPO_DIR/config/backends.example.json" "$STATE/backends.json"
  chmod 0640 "$STATE/backends.json"
  echo "  backends.json instalado (TODO deshabilitado — editá y habilitá '$BACKEND')"
fi

if [[ -z "$(ls -A "$CFG" 2>/dev/null)" ]]; then
  cat <<EOF

  El directorio esta vacio: falta la sesion autenticada del CLI.

  Autenticá el CLI '$BACKEND' usando ese directorio como HOME, por ejemplo:

      sudo -u $U_BRAIN env HOME=$CFG $BACKEND login

  La unidad corre sin terminal ni navegador: si la sesion no esta hecha de
  antemano, el director falla al arrancar.

  Nada de esto se versiona: el directorio guarda credenciales vivas.
EOF
else
  echo "  ya contiene configuracion — no la toco"
fi
