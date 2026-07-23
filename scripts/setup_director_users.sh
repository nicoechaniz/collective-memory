#!/usr/bin/env bash
# setup_director_users.sh — crea los usuarios de sistema del director.
#
# LEER docs/seguridad.md ANTES. El director corre un CLI de agente con shell
# sobre tu maquina. La contencion es que ese proceso no tiene el corpus a la
# vista — no que este sandboxeado: puede leer el resto del host y salir a
# internet. Es una decision, no un default.
#
# La arquitectura son DOS unidades con usuarios distintos:
#   A (mediador) — habla con el indice y la GPU, escribe el ledger, SIN internet.
#   B (modelo)   — el CLI de agente. Namespace sin el corpus. Con internet.
# La frontera es de procesos, no de configuracion del CLI.
#
# Uso: sudo scripts/setup_director_users.sh
set -Eeuo pipefail

[[ "$(id -u)" == "0" ]] || { echo "Requiere root." >&2; exit 1; }

U_MCP="${MAPA_USER_MCP:-mapa-director-mcp}"
U_BRAIN="${MAPA_USER_BRAIN:-mapa-director-brain}"
G_IPC="${MAPA_GROUP_IPC:-mapa-director-ipc}"
G_SERVICE="${MAPA_GROUP:-mapa}"
STATE="${MAPA_DIRECTOR_STATE:-/var/lib/mapa-director}"

mkgroup() { getent group "$1" >/dev/null || { groupadd -r "$1"; echo "  grupo $1 creado"; }; }
mkuser()  {
  id -u "$1" >/dev/null 2>&1 || {
    useradd -r -g "$1" -s /usr/sbin/nologin -M -d /nonexistent "$1" 2>/dev/null \
      || { groupadd -r "$1"; useradd -r -g "$1" -s /usr/sbin/nologin -M -d /nonexistent "$1"; }
    echo "  usuario $1 creado"; }
}

echo "== grupos =="
mkgroup "$G_SERVICE"; mkgroup "$G_IPC"
echo "== usuarios =="
mkuser "$U_MCP"; mkuser "$U_BRAIN"

# El canal entre las dos unidades es un socket; ambos usuarios comparten solo
# ese grupo, nada mas.
usermod -aG "$G_IPC" "$U_MCP"
usermod -aG "$G_IPC" "$U_BRAIN"
usermod -aG "$G_SERVICE" "$U_MCP"
echo "  membresias: $U_MCP y $U_BRAIN comparten solo $G_IPC"

# El control plane vive FUERA del corpus a proposito: la unidad del modelo
# enmascara el corpus entero, y un config dir adentro seria el mismo subarbol
# que su montaje de escritura — la unidad no arrancaria.
echo "== control plane en $STATE =="
mkdir -p "$STATE"
chown root:"$G_IPC" "$STATE"
chmod 0750 "$STATE"
echo "  listo"

cat <<EOF

Siguiente paso: scripts/setup_director_backend.sh, que prepara el directorio de
configuracion del CLI de agente. Hace falta una sesion del CLI ya autenticada.

Las unidades quedan instaladas pero DESHABILITADAS. Encenderlas es tu decision.
EOF
