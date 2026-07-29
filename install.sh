#!/usr/bin/env bash
# install.sh — instalacion de un nodo de memoria colectiva.
#
# Por capas: el nucleo (indexar + buscar + atlas) no necesita systemd, ni
# usuarios de sistema, ni root. Los servicios y el director son opt-in.
#
#   ./install.sh --root <dir-del-corpus>            # nucleo, sin privilegios
#   ./install.sh --root <dir-del-corpus> --structural-only
#   sudo ./install.sh --root <dir-del-corpus> --with-systemd
#
# El director NO se instala aca: ver scripts/setup_director_users.sh y
# docs/seguridad.md, porque habilitarlo cambia el modelo de amenaza.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_HOME="${MAPA_CODE_HOME:-/opt/memoria-colectiva}"
MAPA_ROOT=""
WITH_SYSTEMD=0
BIND_ADDR="127.0.0.1"
SERVE_PORT=8899
PG_PORT=8898
SERVE_USER="${MAPA_SERVE_USER:-mapa-reader}"
SERVICE_GROUP="${MAPA_GROUP:-mapa}"
LLM_CIDR="127.0.0.0/8"
STRUCTURAL_ONLY=0

usage() { sed -n '2,12p' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) MAPA_ROOT="$2"; shift 2;;
    --code-home) CODE_HOME="$2"; shift 2;;
    --bind) BIND_ADDR="$2"; shift 2;;
    --llm-cidr) LLM_CIDR="$2"; shift 2;;
    --structural-only) STRUCTURAL_ONLY=1; shift;;
    --with-systemd) WITH_SYSTEMD=1; shift;;
    -h|--help) usage;;
    *) echo "flag desconocido: $1" >&2; exit 2;;
  esac
done

[[ -n "$MAPA_ROOT" ]] || { echo "Falta --root <directorio del corpus>" >&2; exit 2; }
MAPA_ROOT="$(cd "$MAPA_ROOT" 2>/dev/null && pwd)" || { echo "No existe: $MAPA_ROOT" >&2; exit 2; }

echo "== preflight =="
MAPA_ROOT="$MAPA_ROOT" MAPA_BIND="$BIND_ADDR" \
  "$REPO/scripts/preflight.sh" $([[ $WITH_SYSTEMD -eq 1 ]] && echo --with-systemd)

# --- el bind no-loopback exige decision explicita ---
BIND_ALLOW_LAN=0
if [[ "$BIND_ADDR" != 127.* && "$BIND_ADDR" != "::1" ]]; then
  BIND_ALLOW_LAN=1
  cat >&2 <<EOF

  ATENCION: vas a escuchar en $BIND_ADDR, que no es loopback.
  Una direccion RFC1918 NO garantiza que sea privada: en AWS/GCP/Azure la IP de
  la maquina suele ser RFC1918 con NAT 1:1 hacia una IP publica, asi que el
  servicio quedaria accesible desde internet. Leé docs/seguridad.md.

EOF
  read -r -p "  Escribí 'entiendo' para continuar: " ack
  [[ "$ack" == "entiendo" ]] || { echo "Cancelado."; exit 1; }
fi

# --- el CIDR del LLM tiene que ser privado ---
# IPAddressAllow gobierna tambien el egress. Con un proveedor en la nube no hay
# CIDR estable, y la salida facil (0.0.0.0/0) borra la capa de red entera sin
# que nada lo avise.
if [[ "$LLM_CIDR" == "0.0.0.0/0" || "$LLM_CIDR" == "::/0" ]]; then
  echo "  --llm-cidr $LLM_CIDR desactiva el filtro de red por completo." >&2
  read -r -p "  Escribí 'asumo el riesgo' para continuar: " ack2
  [[ "$ack2" == "asumo el riesgo" ]] || { echo "Cancelado."; exit 1; }
fi

echo "== codigo -> $CODE_HOME =="
mkdir -p "$CODE_HOME"
cp -a "$REPO/mapa/." "$CODE_HOME/"
VENV="$(dirname "$CODE_HOME")/$(basename "$CODE_HOME")-venv"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
VENV_PY="$VENV/bin/python"
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -r "$REPO/requirements.txt"
echo "  dependencias instaladas"

echo "== configuracion =="
MAPA_DATA="$MAPA_ROOT/.mapa"
mkdir -p "$MAPA_DATA"
ENVFILE="$CODE_HOME/mapa.env"
if [[ -f "$ENVFILE" ]]; then
  echo "  $ENVFILE ya existe, no lo piso"
else
  cat > "$ENVFILE" <<EOF
MAPA_ROOT=$MAPA_ROOT
MAPA_DATA=$MAPA_DATA
MAPA_BIND=$BIND_ADDR
MAPA_BIND_ALLOW_LAN=$BIND_ALLOW_LAN
MAPA_PORT=$SERVE_PORT
MAPA_PG_PORT=$PG_PORT
MAPA_GROUP=$SERVICE_GROUP
MAPA_STRUCTURAL_ONLY=$STRUCTURAL_ONLY
EOF
  chmod 600 "$ENVFILE"
  echo "  escrito $ENVFILE"
fi
[[ -f "$MAPA_DATA/corpus_policy.json" ]] || {
  cp "$REPO/config/corpus_policy.example.json" "$MAPA_DATA/corpus_policy.json"
  echo "  politica de corpus inicial instalada"; }
# La capa LLM (discovery + Lab) lee providers.json y es fail-closed sin fallback
# (nunca degrada a un default silencioso). Se materializa desde el ejemplo, con
# ollama local por defecto; editar para apuntar a otro proveedor.
mkdir -p "$MAPA_DATA/director"
[[ -f "$MAPA_DATA/director/providers.json" ]] || {
  cp "$REPO/config/providers.example.json" "$MAPA_DATA/director/providers.json"
  echo "  providers.json (LLM local) instalado"; }

echo "== modelo de embeddings =="
MAPA_ROOT="$MAPA_ROOT" "$VENV_PY" "$REPO/tools/bootstrap_model.py"

echo "== frontend =="
if command -v npm >/dev/null 2>&1; then
  WEB_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  WEB_RELEASE="$MAPA_DATA/web/releases/$WEB_STAMP"
  if (
    cd "$REPO/web"
    npm install --silent
    npm run build --silent
    npm run build:lab --silent
    npm run build:v3:atlas --silent
    if [[ $STRUCTURAL_ONLY -eq 1 ]]; then
      VITE_LAB_STRUCTURAL_ONLY=1 npm run build:v3:lab --silent
    else
      npm run build:v3:lab --silent
    fi
  ); then
    mkdir -p "$WEB_RELEASE"
    cp -a "$REPO/web/dist" "$WEB_RELEASE/dist"
    cp -a "$REPO/web/dist-lab" "$WEB_RELEASE/dist-lab"
    cp -a "$REPO/web/dist-v3-atlas" "$WEB_RELEASE/dist-v3-atlas"
    cp -a "$REPO/web/dist-v3-lab" "$WEB_RELEASE/dist-v3-lab"
    find "$WEB_RELEASE" -type d -exec chmod 755 {} +
    find "$WEB_RELEASE" -type f -exec chmod 644 {} +
    mkdir -p "$MAPA_DATA/web"
    chmod 755 "$MAPA_DATA/web" "$MAPA_DATA/web/releases"
    for bundle in dist dist-lab dist-v3-atlas dist-v3-lab; do
      tmp_link="$MAPA_DATA/web/.${bundle}.${WEB_STAMP}"
      ln -s "releases/$WEB_STAMP/$bundle" "$tmp_link"
      if [[ -e "$MAPA_DATA/web/$bundle" && ! -L "$MAPA_DATA/web/$bundle" ]]; then
        mv "$MAPA_DATA/web/$bundle" "$MAPA_DATA/web/${bundle}.previous.${WEB_STAMP}"
      fi
      mv -Tf "$tmp_link" "$MAPA_DATA/web/$bundle"
    done
    echo "  atlas + lab V1/V3 publicados de forma atomica ($WEB_STAMP)"
  else
    echo "  aviso: el build web fallo; no se modifico la version publicada"
  fi
fi

if [[ $WITH_SYSTEMD -eq 1 ]]; then
  echo "== unidades systemd =="
  getent group "$SERVICE_GROUP" >/dev/null || groupadd -r "$SERVICE_GROUP"
  id -u "$SERVE_USER" >/dev/null 2>&1 || useradd -r -g "$SERVICE_GROUP" -s /usr/sbin/nologin "$SERVE_USER"
  BIND_CIDR="$BIND_ADDR/32"
  for t in "$REPO"/systemd/*.in; do
    unit="$(basename "${t%.in}")"
    sed -e "s|@MAPA_ROOT@|$MAPA_ROOT|g"   -e "s|@MAPA_DATA@|$MAPA_DATA|g" \
        -e "s|@CODE_HOME@|$CODE_HOME|g"   -e "s|@VENV_PY@|$VENV_PY|g" \
        -e "s|@SERVE_USER@|$SERVE_USER|g" -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
        -e "s|@BIND_ADDR@|$BIND_ADDR|g"   -e "s|@SERVE_PORT@|$SERVE_PORT|g" \
        -e "s|@BIND_ALLOW_LAN@|$BIND_ALLOW_LAN|g" \
        -e "s|@BIND_CIDR@|$BIND_CIDR|g"   -e "s|@LLM_CIDR@|$LLM_CIDR|g" \
        -e "s|@PG_PORT@|$PG_PORT|g"       -e "s|@STRUCTURAL_ONLY@|$STRUCTURAL_ONLY|g" \
        "$t" > "/etc/systemd/system/$unit"
    echo "  instalada $unit"
  done
  chown -R root:"$SERVICE_GROUP" "$MAPA_DATA" 2>/dev/null || true
  systemctl daemon-reload
  # Las unidades del director quedan instaladas pero SIN habilitar: encenderlas
  # es una decision aparte (docs/seguridad.md).
  systemctl enable --now mapa-serve.service 2>/dev/null && echo "  mapa-serve activo" || true
  echo "  las unidades del director quedan instaladas y DESHABILITADAS"
fi

cat <<EOF

Listo.

  Indexar:   MAPA_ROOT=$MAPA_ROOT $VENV_PY $CODE_HOME/tier1.py index --scope total
  Buscar:    MAPA_ROOT=$MAPA_ROOT $VENV_PY $CODE_HOME/tier1.py search "tu consulta"
  Servir:    MAPA_ROOT=$MAPA_ROOT $VENV_PY $CODE_HOME/serve.py     -> http://$BIND_ADDR:$SERVE_PORT/atlas-v3/
  Lab V3:    http://$BIND_ADDR:$PG_PORT/lab-v3/descubrir  (si habilitaste mapa-playground)

Probalo primero con el corpus de ejemplo: examples/README.md
EOF
