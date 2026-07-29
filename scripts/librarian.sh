#!/usr/bin/env bash
# Bibliotecario del mapa de memoria colectiva — harness mecánico.
# Single-writer bajo flock. Publica por snapshot swap atómico. Nunca edita el snapshot vivo in-place.
# La compilacion SEMANTICA (proyecto -> sub-mapa) la hace el paso LLM (ver librarian.md);
# este harness hace lo mecanico/atomico y es idempotente en el contenido de los nodos.
set -euo pipefail

ROOT="${MAPA_ROOT:?MAPA_ROOT no definido (directorio del corpus)}"
CODE_HOME="${MAPA_CODE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../mapa" && pwd)}"
MAPA="$ROOT/mapa"
INBOX="$ROOT/inbox"
D="${MAPA_DATA:-$ROOT/.mapa}"
SNAPSHOTS="${MAPA_SNAPSHOTS_DIR:-$D/snapshots}"
KEEP="${MAPA_KEEP_SNAPSHOTS:-5}"

# --- Exclusion mutua (single-writer) ---
exec 9>"$D/lock"
if ! flock -n 9; then
  echo "[bibliotecario] Ya hay una corrida activa (flock). Abortando." >&2
  exit 3
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%S.%NZ)"   # nanosegundos: unico aun en corridas sub-segundo
NOW="$(date -u +%FT%TZ)"
CUR=""
[ -L "$MAPA" ] && CUR="$(readlink -f "$MAPA")" || true

# --- Autorregenerar el spool si falta (robustez) ---
mkdir -p "$INBOX"/new "$INBOX"/processing "$INBOX"/done "$INBOX"/error "$D"/events "$D"/staging "$SNAPSHOTS"

# --- Staging desde el snapshot actual ---
STAGE="$D/staging/$RUN_ID"
rm -rf "$STAGE"; mkdir -p "$STAGE/proyectos"
if [ -n "$CUR" ] && [ -d "$CUR" ]; then cp -a "$CUR/." "$STAGE/"; fi

# --- Overlays: fuentes canonicas que pisan el staging (allowlist estricta, bajo el flock) ---
if [ -f "$D/overlays/GUIA_CONSULTA.md" ] && [ ! -L "$D/overlays/GUIA_CONSULTA.md" ]; then
  install -m 0644 "$D/overlays/GUIA_CONSULTA.md" "$STAGE/GUIA_CONSULTA.md"
fi
# Fuentes canonicas de la capa semantica. El agente compila aca; el harness las
# aplica al staging bajo el mismo flock y nunca escribe el snapshot vivo.
if [ -f "$D/overlays/mapa/index.md" ] && [ ! -L "$D/overlays/mapa/index.md" ]; then
  install -m 0644 "$D/overlays/mapa/index.md" "$STAGE/index.md"
fi
if [ -d "$D/overlays/mapa/proyectos" ] && [ ! -L "$D/overlays/mapa/proyectos" ]; then
  find "$D/overlays/mapa/proyectos" -type f -name '*.md' -print0 | while IFS= read -r -d '' f; do
    rel="${f#"$D/overlays/mapa/proyectos/"}"
    mkdir -p "$STAGE/proyectos/$(dirname "$rel")"
    install -m 0644 "$f" "$STAGE/proyectos/$rel"
  done
fi
# Sintesis destilada (F8) y hallazgos de discovery (F9): reflejar el set canonico
# completo de cada overlay (huerfanos borrados desaparecen).
for layer in sintesis hallazgos; do
  rm -rf "$STAGE/$layer"
  if [ -d "$D/overlays/$layer" ]; then
    mkdir -p "$STAGE/$layer"
    find "$D/overlays/$layer" -maxdepth 1 -type f -name '*.md' -print0 | while IFS= read -r -d '' f; do
      install -m 0644 "$f" "$STAGE/$layer/$(basename "$f")"
    done
  fi
done

# --- Ingest: consumir notas del inbox (cola durable: new -> processing -> done) ---
shopt -s nullglob
for note in "$INBOX"/new/*; do
  [ -e "$note" ] || continue
  b="$(basename "$note")"
  mv "$note" "$INBOX/processing/$b"
  printf '{"ts":"%s","run_id":"%s","event":"ingest_note","note":"%s"}\n' "$NOW" "$RUN_ID" "$b" >> "$D/events/events.jsonl"
  mv "$INBOX/processing/$b" "$INBOX/done/$b"
done

# --- Manifest: hash por nodo, clavado por id/path logico (NO por path fisico del snapshot) ---
python3 - "$STAGE" "$ROOT" > "$D/manifest.json" <<'PY'
import sys, os, hashlib, json, re, glob
stage, root = sys.argv[1], sys.argv[2]
def sha(paths):
    h = hashlib.sha256()
    for p in paths:
        ap = p if os.path.isabs(p) else os.path.join(root, p)
        try:
            with open(ap, 'rb') as f: h.update(f.read())
        except Exception:
            h.update(b'\0MISSING\0' + ap.encode())
    return h.hexdigest()
def parse_paths(fm, key):
    sm = re.search(r'^' + key + r':\s*(\[.*?\])\s*$', fm, re.S | re.M)
    if not sm: return []
    raw = sm.group(1)
    # JSON primero (doc_ids reales contienen comas); fallback split legacy para nodos viejos.
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except Exception:
        return [x.strip().strip('"').strip("'") for x in raw[1:-1].split(',') if x.strip()]
nodes = {}
# Raices explicitas: cada carpeta deriva su logical_path correcto (un glob unico daria paths rotos).
mapa_base = os.path.join(root, 'mapa')
for subdir in ('proyectos', 'sintesis', 'hallazgos'):
  logical_base = os.path.join(mapa_base, subdir) + '/'
  for md in sorted(glob.glob(os.path.join(stage, subdir, '**', '*.md'), recursive=True)):
    rel = os.path.relpath(md, os.path.join(stage, subdir))
    txt = open(md, encoding='utf-8', errors='replace').read()
    m = re.search(r'^---\n(.*?)\n---', txt, re.S)
    nid = os.path.splitext(rel)[0].replace(os.sep, '.'); src = []
    extra = {}
    if m:
        fm = m.group(1)
        idm = re.search(r'^id:\s*(\S+)', fm, re.M)
        if idm: nid = idm.group(1)
        src = parse_paths(fm, 'source_paths')
        # Aristas no primarias de hallazgos (F9): viajan por el manifest, ui_builder no
        # parsea archivos publicados. El hash de trazabilidad sigue siendo solo de source_paths.
        for key in ('counter_paths', 'bridge_paths', 'target_paths'):
            vals = parse_paths(fm, key)
            if vals: extra[key] = vals
    nodes[nid] = {'logical_path': logical_base + rel,
                  'source_paths': src, 'hash': (sha(src) if src else None), **extra}
print(json.dumps({'nodes': nodes}, ensure_ascii=False, indent=2, sort_keys=True))
PY

# --- status.md ---
# Con pipefail, un directorio opcional ausente haria abortar una instalacion nueva.
backlog="$( { find "$INBOX/new" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')"
mapped="$( { find "$STAGE/proyectos" -name '*.md' 2>/dev/null || true; } | wc -l | tr -d ' ')"
hallazgos_n="$( { find "$STAGE/hallazgos" -name '*.md' 2>/dev/null || true; } | wc -l | tr -d ' ')"
cat > "$STAGE/status.md" <<EOF
---
id: status
type: status
last_compiled_at: $NOW
run_id: $RUN_ID
---
# Estado del mapa

- **Ultima compilacion:** $NOW  (run_id \`$RUN_ID\`)
- **Backlog inbox/new:** $backlog
- **Nodos con sub-mapa:** $mapped
- **Hallazgos publicados:** $hallazgos_n
- **Cobertura:** $mapped nodos con sub-mapa (todos los proyectos del root).
- **SLO:** stale si snapshot > 24 h o backlog > 20.
EOF

# --- Evento de publicacion + render de log.md ---
printf '{"ts":"%s","run_id":"%s","event":"publish","mapped":%s,"backlog":%s}\n' "$NOW" "$RUN_ID" "$mapped" "$backlog" >> "$D/events/events.jsonl"
{ echo "# Log del bibliotecario"; echo; echo '```jsonl'; tail -n 200 "$D/events/events.jsonl"; echo '```'; } > "$STAGE/log.md"

# --- Publish atomico: staging -> snapshot (ubicacion parametrizable) ---
mkdir -p "$SNAPSHOTS"
NEW="$SNAPSHOTS/mapa.$RUN_ID"
if [ -e "$NEW" ]; then echo "[bibliotecario] Colision de run_id ($RUN_ID). Abortando sin publicar." >&2; exit 4; fi
mv "$STAGE" "$NEW"
TMP="$D/.maplink.$RUN_ID"
ln -sfn "$NEW" "$TMP"
mv -Tf "$TMP" "$MAPA"

# --- Poda con grace: retener los KEEP snapshots mas nuevos ---
ls -1dt "$SNAPSHOTS"/mapa.* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  [ "$old" = "$NEW" ] && continue
  rm -rf "$old"
done

# --- Tier-1/2: reindexar el indice de consulta (hibrido si hay venv; si no FTS) ---
tier1_msg="Tier1 ok"
MAPA_INDEX_SCOPE="${MAPA_INDEX_SCOPE:-total}"
TIER1_PY="${MAPA_PYTHON:-$CODE_HOME/../venv/bin/python}"; [ -x "$TIER1_PY" ] || TIER1_PY=python3
"$TIER1_PY" "$CODE_HOME/tier1.py" index --scope "$MAPA_INDEX_SCOPE" >/dev/null 2>&1 || tier1_msg="Tier1 FALLO"

# --- Atlas visual: proyecciones UI read-only (no bloqueante) ---
# MAPA_UI_MODE=full refresca tambien las aristas semanticas (usa GPU); fast no las recalcula.
ui_msg="UI ok"
MAPA_UI_MODE="${MAPA_UI_MODE:-fast}"
if [ -f "$CODE_HOME/ui_builder.py" ]; then
  "$TIER1_PY" "$CODE_HOME/ui_builder.py" build --mode "$MAPA_UI_MODE" >/dev/null 2>&1 || ui_msg="UI FALLO"
else
  ui_msg="UI no instalada"
fi

echo "[bibliotecario] OK - run_id=$RUN_ID - mapa -> $(readlink "$MAPA") - nodos=$mapped - backlog=$backlog - index_scope=$MAPA_INDEX_SCOPE - $tier1_msg - $ui_msg"

# --- Trigger opt-in del director autonomo (F19) ---
# Condiciones (TODAS): MAPA_DIRECTOR=1 + Tier-1 OK + UI OK + corrida full +
# doc_vectors.db presente y coherente con la generacion publicada (el artefacto
# real, no el modo: con indice FTS-only un build full sale OK sin crearlo).
if [ "${MAPA_DIRECTOR:-0}" = "1" ]; then
  director_skip=""
  [ "$tier1_msg" = "Tier1 ok" ] || director_skip="Tier-1 fallo"
  [ -z "$director_skip" ] && { [ "$ui_msg" = "UI ok" ] || director_skip="UI fallo/ausente"; }
  [ -z "$director_skip" ] && { [ "$MAPA_UI_MODE" = "full" ] || director_skip="MAPA_UI_MODE=$MAPA_UI_MODE (necesita full)"; }
  if [ -z "$director_skip" ]; then
    "$TIER1_PY" - "$D" <<'PYEOF' || director_skip="doc_vectors.db ausente o incoherente"
import json, os, sqlite3, sys
D = sys.argv[1]
m = json.load(open(f"{D}/ui/manifest.json"))
con = sqlite3.connect(f"file:{D}/index.db?mode=ro", uri=True)
idx = dict(con.execute("SELECT k,v FROM meta").fetchall()).get("index_generation")
con.close()
sys.exit(0 if os.path.isfile(f"{D}/ui/doc_vectors.db") and str(idx) == str(m.get("index_generation")) else 1)
PYEOF
  fi
  if [ -z "$director_skip" ]; then
    systemd-run --unit="mapa-director-trigger-$RUN_ID" --collect \
      "$CODE_HOME/../scripts/director.sh" >/dev/null 2>&1 \
      && echo "[bibliotecario] director disparado (detached; el flock del librarian se libera al salir)" \
      || echo "[bibliotecario] director NO pudo dispararse (systemd-run fallo)"
  else
    echo "[bibliotecario] director NO disparado: $director_skip"
  fi
fi
