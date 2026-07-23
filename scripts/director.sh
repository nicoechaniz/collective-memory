#!/usr/bin/env bash
# director.sh — wrapper del director autónomo de descubrimiento (F19, arquitectura F20).
#
# Arquitectura de DOS unidades con UIDs distintos (la frontera es de procesos):
#   A: mapa-director-mcp   — el MEDIADOR (director_mcp.py serve-socket) + productores + GPU
#      + ledger RW. SIN internet (solo localhost + ollama).
#   B: mapa-director-brain — el MODELO (claude -p | codex exec). Namespace sin ningún
#      dato del disco: solo su config dir + el socket del mediador. Puede tener shell
#      (codex): no hay nada que leer ni DB que escribir.
# La política (única autoridad) vive en el control plane root-owned .mapa/director/private/.
#
# Uso: director.sh [--dry] [--judge-only] [--db <path>] [--backend <claude|codex>]
# Exit: 0 ok · 3 ya hay un director corriendo · 4 backend inválido · 5 preflight/cierre fatal
set -euo pipefail

ROOT="${MAPA_ROOT:?MAPA_ROOT no definido (directorio del corpus)}"
CODE_HOME="${MAPA_CODE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../mapa" && pwd)}"
SCRIPTS_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
D="${MAPA_DATA:-$ROOT/.mapa}"
DIR=$D/director
# El control plane del director vive FUERA de MAPA_ROOT: la unidad del modelo
# enmascara MAPA_ROOT, asi que un config dir adentro seria el mismo subarbol que
# el montaje read-write y la unidad no arrancaria.
PRIV="${MAPA_DIRECTOR_STATE:-/var/lib/mapa-director}"
VENV_PY="${MAPA_PYTHON:-$(dirname "$CODE_HOME")/venv/bin/python}"; [ -x "$VENV_PY" ] || VENV_PY=python3
CLAUDE_BIN="${MAPA_CLAUDE_BIN:-$(command -v claude || true)}"
CODEX_BIN="${MAPA_CODEX_BIN:-$(command -v codex || true)}"
RUNSOCK_DIR=/run/mapa-director

# Usuarios y grupos del sistema: parametrizables para poder levantar una
# instalacion de prueba sin reusar los de produccion.
U_MCP="${MAPA_USER_MCP:-mapa-director-mcp}"
U_BRAIN="${MAPA_USER_BRAIN:-mapa-director-brain}"
G_IPC="${MAPA_GROUP_IPC:-mapa-director-ipc}"
G_SERVICE="${MAPA_GROUP:-mapa}"
# video/render son de acceso a GPU y NO existen en servidores headless ni en
# contenedores; pedirlos sin filtrar impide que la unidad arranque.
SUPP_A="$G_SERVICE $G_IPC"
for g in video render; do getent group "$g" >/dev/null 2>&1 && SUPP_A="$SUPP_A $g"; done

DRY=0; JUDGE_ONLY=0; DB=$D/discovery.db; BACKEND=claude
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry) DRY=1; shift;;
    --judge-only) JUDGE_ONLY=1; shift;;
    --db) DB=$2; shift 2;;
    --backend) BACKEND=$2; shift 2;;
    *) echo "flag desconocido: $1" >&2; exit 4;;
  esac
done

# --- lock del director en el control plane (anti-symlink: verificar antes de abrir) ---
$VENV_PY - "$PRIV/director.lock" <<'EOF' || exit 5
import os, stat, sys
p = sys.argv[1]
try:
    st = os.lstat(p)
    if not stat.S_ISREG(st.st_mode):
        sys.exit(f"[director] {p} no es un archivo regular — abortando")
except FileNotFoundError:
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
    os.close(fd)
EOF
exec 8>"$PRIV/director.lock"
if ! flock -n 8; then
  echo "[director] ya hay un director corriendo — abortando (exit 3)" >&2
  exit 3
fi

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
RUNDIR=$DIR/logs/$RUN_ID
mkdir -p "$RUNDIR" "$DIR/digests"
chown "$U_MCP":"$U_MCP" "$RUNDIR"
LOG=$RUNDIR/wrapper.log
log() { echo "[director $(date -u +%H:%M:%S)] $*" | tee -a "$LOG" >&2; }

# --- backend habilitado (bisagra) ---
read -r ENABLED MODEL EFFORT < <($VENV_PY - "$BACKEND" "$PRIV/backends.json" <<'EOF'
import json, sys
b = json.load(open(sys.argv[2])).get(sys.argv[1]) or {}
print(("1" if b.get("enabled") else "0"), b.get("model") or "-", b.get("effort") or "-")
EOF
)
if [[ "$ENABLED" != "1" ]]; then
  log "backend '$BACKEND' no habilitado (contrato fail-closed) — exit 4"
  exit 4
fi

# --- preflight: librarian, GPU, coherencia, permisos del UID final (fail-closed) ---
if ! flock -w 180 9 9>"$D/lock"; then
  log "lock del librarian sigue tomado tras 180s → modo solo-juzgar"
  JUDGE_ONLY=1
fi
if pgrep -f '[d]iscover\.py run|[t]ier1\.py index|[u]i_builder\.py build' >/dev/null; then
  log "GPU ocupada por productor/indexado ajeno → modo solo-juzgar"
  JUDGE_ONLY=1
fi
if ! $VENV_PY - "$D" <<'EOF'
import json, sqlite3, sys, os
D = sys.argv[1]
try:
    m = json.load(open(f"{D}/ui/manifest.json"))
    con = sqlite3.connect(f"file:{D}/index.db?mode=ro", uri=True)
    idx = dict(con.execute("SELECT k,v FROM meta").fetchall()).get("index_generation")
    con.close()
    sys.exit(0 if os.path.isfile(f"{D}/ui/doc_vectors.db") and str(idx) == str(m.get("index_generation")) else 1)
except Exception:
    sys.exit(1)
EOF
then
  log "índice/UI incoherentes o sin doc_vectors.db → modo solo-juzgar"
  JUDGE_ONLY=1
fi
# Fail-closed: como mapa-director-mcp, gpu.lock adquirible + DB RW + providers legible.
# systemd exige que los paths de ReadWritePaths existan; los sidecars deben quedar
# del UID de A (si root los crea, A no puede escribirlos → readonly database).
touch "$D/gpu.lock" "$DB.lock" "$DB-wal" "$DB-shm" "$DIR/state/state.json" 2>/dev/null || true
chgrp "$G_SERVICE" "$D/gpu.lock" "$DB.lock" 2>/dev/null || true; chmod g+rw "$D/gpu.lock" "$DB.lock" 2>/dev/null || true
chown "$U_MCP" "$DB-wal" "$DB-shm" 2>/dev/null || true; chmod u+rw "$DB-wal" "$DB-shm" 2>/dev/null || true
if ! sudo -u "$U_MCP" $VENV_PY - "$DB" "$DIR/providers.json" <<'EOF'
import fcntl, json, sqlite3, sys
db = sys.argv[1]
with open("/run/mapa/gpu.lock", "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(f, fcntl.LOCK_UN)
con = sqlite3.connect(db); con.execute("BEGIN IMMEDIATE"); con.rollback(); con.close()
json.load(open(sys.argv[2]))
print("preflight-uid OK")
EOF
then
  log "PREFLIGHT FATAL: mapa-director-mcp no puede gpu.lock/DB-RW/providers — corregí permisos antes de correr (exit 5)"
  exit 5
fi

# --- política + tokens (custodia por-UID, jamás grupal) ---
DIGEST=$DIR/digests/$RUN_ID.md
MARKER=$RUNDIR/marker.json
$VENV_PY - "$RUN_ID" "$DB" "$BACKEND" "$MODEL" "$DRY" "$JUDGE_ONLY" "$DIGEST" "$MARKER" "$PRIV/policy.json" "$U_MCP" <<'EOF'
import json, os, sys
run_id, db, backend, model, dry, judge_only, digest, marker = sys.argv[1:9]
pol = {"run_id": run_id, "db": os.path.abspath(db), "backend": backend, "model": model,
       "provider": "gemma4", "dry": dry == "1", "judge_only": judge_only == "1",
       "digest_path": digest, "marker_path": marker,
       "budgets": {"max_campaigns": 2, "max_tasks": 3, "max_tool_calls": 200, "limit_max": 30}}
path = sys.argv[9]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(pol, f, ensure_ascii=False, indent=1); f.flush(); os.fsync(f.fileno())
os.replace(tmp, path)
os.chmod(path, 0o640)
import shutil; shutil.chown(path, "root", sys.argv[10])
EOF
log "política: run=$RUN_ID backend=$BACKEND:$MODEL effort=$EFFORT dry=$DRY judge_only=$JUDGE_ONLY db=$DB"

TOKEN=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
TOK_A=$PRIV/mcp_token.a; TOK_B_DIR=$PRIV/$BACKEND-config; TOK_B=$TOK_B_DIR/mcp_token
echo "$TOKEN" > "$TOK_A"; chown "$U_MCP":"$U_MCP" "$TOK_A"; chmod 0600 "$TOK_A"
echo "$TOKEN" > "$TOK_B"; chown "$U_BRAIN":"$U_BRAIN" "$TOK_B"; chmod 0600 "$TOK_B"
unset TOKEN

# --- unidad A: el mediador ---
UNIT_A=mapa-director-mcp-$RUN_ID
RW_A="$DB $DB-wal $DB-shm $DB.lock $D/gpu.lock $D/emb_cache.db $D/emb_cache.db-wal $D/emb_cache.db-shm $D/hf $D/discovery/knn_cache $DIR/state $DIR/digests $DIR/logs/$RUN_ID /tmp"
touch "$D/emb_cache.db" "$D/emb_cache.db-wal" "$D/emb_cache.db-shm" 2>/dev/null || true
log "lanzando unidad A ($UNIT_A)"
systemd-run --unit="$UNIT_A" --collect --no-block \
  -p User=$U_MCP -p Group=$U_MCP \
  -p SupplementaryGroups="$SUPP_A" \
  -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes \
  -p "ReadOnlyPaths=$ROOT" -p "ReadOnlyPaths=$(dirname "$CODE_HOME")" -p "ReadWritePaths=$RW_A" \
  -p RuntimeDirectory=mapa-director -p RuntimeDirectoryMode=0770 \
  -p IPAddressDeny=any -p "IPAddressAllow=127.0.0.0/8 ${MAPA_LLM_CIDR:-127.0.0.1}" \
  -p RuntimeMaxSec=4000 \
  -p Environment=HF_HOME=$D/hf -p Environment=HF_HUB_OFFLINE=1 -p Environment=TRANSFORMERS_OFFLINE=1 \
  "$VENV_PY" "$CODE_HOME/director_mcp.py" serve-socket "$RUNSOCK_DIR/mcp.sock" "$TOK_A" \
  >>"$LOG" 2>&1
for i in $(seq 1 30); do [ -S "$RUNSOCK_DIR/mcp.sock" ] && break; sleep 1; done
if [ ! -S "$RUNSOCK_DIR/mcp.sock" ]; then
  log "FATAL: el socket del mediador no apareció"; systemctl stop "$UNIT_A" 2>/dev/null; exit 5
fi
chgrp "$G_IPC" "$RUNSOCK_DIR"; chmod 0750 "$RUNSOCK_DIR"
# El proxy va junto al socket en el runtime dir (world-readable code, sin secretos):
# así la unidad B lo alcanza sin depender de atravesar director/ (0750 root:mapa).
install -m 0755 -o root -g root "$SCRIPTS_HOME/mcp_proxy.py" "$RUNSOCK_DIR/mcp_proxy.py"

# --- prompt (runbook inline) ---
PROMPT="$(cat "$SCRIPTS_HOME/../prompts/director.md")

---
## Contexto de esta corrida
- run_id: $RUN_ID
- modo: $([[ $DRY == 1 ]] && echo 'SOMBRA (dry) — tus verdicts van solo al digest' || echo 'real')$([[ $JUDGE_ONLY == 1 ]] && echo ' · SOLO-JUZGAR (productores vedados)' || true)
- backend: $BACKEND:$MODEL
Empezá con context()."

# --- unidad B: el cerebro ---
UNIT_B=mapa-director-brain-$RUN_ID
CFG=$TOK_B_DIR
PROXY_B=$RUNSOCK_DIR/mcp_proxy.py   # copiado junto al socket (arriba); /run es compartido con B
SOCK_B=$RUNSOCK_DIR/mcp.sock
# B: el árbol de datos desaparece (TemporaryFileSystem); solo re-expone su config dir.
# El socket y el proxy viven en /run/mapa-director (fuera del corpus), visibles sin bind.
BIND_COMMON="-p TemporaryFileSystem=$ROOT:ro -p BindPaths=$CFG"
# Base común a ambos backends. ProtectHome NO va acá: codex (shell irrenunciable)
# exige ProtectHome=yes; claude no tiene herramientas de filesystem (Bash/Read
# deshabilitados) así que /root le es inofensivo, y su binario vive bajo /root.
HARDEN_B="-p User=$U_BRAIN -p Group=$U_BRAIN -p SupplementaryGroups=$G_IPC \
 -p NoNewPrivileges=yes -p ProtectSystem=strict -p PrivateTmp=yes \
 -p InaccessiblePaths=/var/log -p InaccessiblePaths=/var/backups \
 -p ProtectProc=invisible -p ProcSubset=pid -p RuntimeMaxSec=3600 -p WorkingDirectory=$RUNSOCK_DIR"

set +e
if [[ "$BACKEND" == "codex" ]]; then
  log "lanzando unidad B codex ($UNIT_B, effort=$EFFORT)"
  systemd-run --unit="$UNIT_B" --collect --wait --pipe --quiet \
    $HARDEN_B $BIND_COMMON -p ProtectHome=yes \
    -p Environment=CODEX_HOME=$CFG -p Environment=HOME=$CFG \
    "$CODEX_BIN" exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check \
      --dangerously-bypass-approvals-and-sandbox \
      -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\"" \
      -c "mcp_servers.director.command=\"/usr/bin/python3\"" \
      -c "mcp_servers.director.args=[\"$PROXY_B\",\"$SOCK_B\",\"$CFG/mcp_token\"]" \
      -c "mcp_servers.director.startup_timeout_sec=60" \
      --json -o "$CFG/last_message.txt" "$PROMPT" \
      > "$RUNDIR/output.jsonl" 2> "$RUNDIR/stderr.log"
  UNIT_RC=$?
else
  log "lanzando unidad B claude ($UNIT_B)"
  cat > "$CFG/mcp.json" <<EOF
{"mcpServers": {"director": {"command": "/usr/bin/python3", "args": ["$PROXY_B", "$SOCK_B", "$CFG/mcp_token"]}}}
EOF
  chown "$U_BRAIN":"$U_BRAIN" "$CFG/mcp.json"
  TOOLS="mcp__director__context,mcp__director__eligible,mcp__director__doc,mcp__director__sql,mcp__director__write_digest"
  [[ $DRY == 0 ]] && TOOLS="$TOOLS,mcp__director__review,mcp__director__abstain"
  [[ $JUDGE_ONLY == 0 ]] && TOOLS="$TOOLS,mcp__director__campaign,mcp__director__task"
  # claude (self-contained, Bun) vive bajo /root (0700): mapa-director-brain no
  # puede atravesarlo. Se copia al runtime dir (tmpfs) para que B lo ejecute.
  # ProtectHome=yes acá también (B no necesita /root una vez copiado el binario).
  CLAUDE_REAL=$(readlink -f "$CLAUDE_BIN")
  install -m 0755 -o root -g root "$CLAUDE_REAL" "$RUNSOCK_DIR/claude-bin"
  systemd-run --unit="$UNIT_B" --collect --wait --pipe --quiet \
    $HARDEN_B $BIND_COMMON -p ProtectHome=yes \
    -p Environment=HOME=$CFG -p Environment=CLAUDE_CONFIG_DIR=$CFG \
    -p Environment=MCP_TIMEOUT=60000 -p Environment=MCP_TOOL_TIMEOUT=1750000 \
    "$RUNSOCK_DIR/claude-bin" -p "$PROMPT" \
      --output-format json \
      --mcp-config "$CFG/mcp.json" --strict-mcp-config \
      --setting-sources user \
      --allowedTools "$TOOLS" \
      --disallowedTools "Bash,Read,Write,Edit,Grep,Glob,WebFetch,WebSearch,Task,NotebookEdit" \
      > "$RUNDIR/output.json" 2> "$RUNDIR/stderr.log"
  UNIT_RC=$?
fi
set -e
log "unidad B terminó rc=$UNIT_RC"

# --- cierre: apagar A, validar/regenerar digest, limpiar tokens ---
systemctl stop "$UNIT_A" 2>/dev/null || true
rm -f "$TOK_A" "$TOK_B"
if ! $VENV_PY "$CODE_HOME/director_mcp.py" postmortem >> "$LOG" 2>&1; then
  log "ADVERTENCIA: postmortem falló — revisá $RUNDIR"
fi
if [[ -f "$DIGEST" ]]; then
  chown "$U_MCP":"$G_SERVICE" "$DIGEST" 2>/dev/null || true
  ln -sfn "$DIGEST" "$DIR/digests/latest.md"
  log "digest: $DIGEST"
else
  log "ERROR: no hay digest tras el cierre — revisá $RUNDIR"; exit 5
fi
exit 0
