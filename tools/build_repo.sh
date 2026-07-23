#!/usr/bin/env bash
# build_repo.sh — copia por ALLOWLIST desde el sistema de origen hacia el repo.
#
# Este es el único archivo del repo cuya función es apuntar al sistema privado,
# así que el origen NO tiene default literal: viene por --src o por MAPA_SRC.
# (Un literal acá sería, además, un hallazgo de la regla 1 del leak-check.)
#
# Nunca escribe en el origen. Nunca corre git en el origen.
#
# Uso: tools/build_repo.sh --src <MAPA_SRC> [--repo <dir>] [--dry-run]
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${MAPA_SRC:-}"
DRY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)  SRC="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "flag desconocido: $1" >&2; exit 2;;
  esac
done

[[ -n "$SRC" ]] || { echo "Falta --src (o MAPA_SRC): directorio del sistema de origen." >&2; exit 2; }
[[ -d "$SRC" ]] || { echo "No existe: $SRC" >&2; exit 2; }

MANIFEST="$REPO/tools/manifest_allowlist.txt"
[[ -f "$MANIFEST" ]] || { echo "Falta el manifiesto: $MANIFEST" >&2; exit 2; }

copied=0; authored=0; missing=0

while read -r kind src dst _rest; do
  [[ -z "${kind:-}" || "$kind" == \#* ]] && continue
  case "$kind" in
    COPY)
      s="$SRC/$src"; d="$REPO/$dst"
      if [[ ! -f "$s" ]]; then
        echo "  FALTA en origen: $src" >&2; missing=$((missing+1)); continue
      fi
      if [[ "$DRY" -eq 1 ]]; then
        echo "  copiaría  $src -> $dst"
      else
        mkdir -p "$(dirname "$d")"
        install -m 0644 "$s" "$d"
        echo "  copiado   $dst"
      fi
      copied=$((copied+1))
      ;;
    AUTHOR)
      # Escrito de cero en el repo: no se copia, solo se verifica que exista.
      [[ "$dst" == *'**' ]] && continue
      [[ -e "$REPO/$dst" ]] || echo "  pendiente de escribir: $dst"
      authored=$((authored+1))
      ;;
    EXCEPT) ;;
  esac
done < "$MANIFEST"

echo
echo "[build] copiados=$copied  autorales=$authored  faltantes_en_origen=$missing"
[[ "$missing" -gt 0 ]] && { echo "[build] ABORTA: hay entradas del manifiesto que no existen en el origen." >&2; exit 1; }

if [[ "$DRY" -eq 0 ]]; then
  echo "[build] regenerando hashes para CI…"
  python3 "$REPO/tools/leak_check.py" --emit-hashes --repo "$REPO" --src "$SRC"
  # Aviso si el emit cambió los hashes respecto de lo commiteado: CI usa el
  # archivo commiteado, así que un cambio sin commitear la dejaría ciega.
  if command -v git >/dev/null && ! git -C "$REPO" diff --quiet -- tools/leakcheck_terms.hashed 2>/dev/null; then
    echo "[build] AVISO: tools/leakcheck_terms.hashed cambió — commitealo para que CI lo use."
  fi
  echo "[build] corriendo leak-check (reglas 3/3b activas por --src)…"
  python3 "$REPO/tools/leak_check.py" --repo "$REPO" --src "$SRC"
fi
