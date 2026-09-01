#!/usr/bin/env bash
set -euo pipefail
export PATH="${HOME}/.cargo/bin:${PATH}"
REAL_BIN="${GROK_PRIVACY_BIN:-$HOME/.grok/bin/grok}"
ROOT="${GROK_PRIVACY_REPO:-$HOME/grok-privacy}"
STABLE="${GROK_PRIVACY_STABLE_SCRIPTS:-$HOME/.local/share/grok-privacy/scripts}"
find_script() {
  local name="$1" c
  for c in "${ROOT}/scripts/${name}" "${STABLE}/${name}"; do
    [[ -f "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}
case "${1:-}" in
  rebuild|--rebuild)
    shift
    S="$(find_script privacy-simple.sh || find_script privacy-rebuild.sh || true)"
    [[ -n "${S:-}" ]] || { echo "privacy-simple.sh introuvable" >&2; exit 127; }
    exec bash "$S" "$@"
    ;;
  update|--update)
    shift
    C="$(find_script privacy-client-update.sh || true)"
    [[ -n "${C:-}" ]] || { echo "privacy-client-update.sh manquant — utilise: grok rebuild" >&2; exit 127; }
    exec bash "$C" "$@"
    ;;
  *)
    [[ -e "$REAL_BIN" ]] || { echo "binaire manquant: $REAL_BIN — lance: grok rebuild" >&2; exit 127; }
    exec "$REAL_BIN" "$@"
    ;;
esac
