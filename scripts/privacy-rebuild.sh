#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="${ROOT}/scripts/privacy-simple.sh"
STABLE="${GROK_PRIVACY_STABLE_SCRIPTS:-$HOME/.local/share/grok-privacy/scripts}/privacy-simple.sh"
[[ -f "$S" ]] && exec bash "$S" "$@"
[[ -f "$STABLE" ]] && exec bash "$STABLE" "$@"
echo "privacy-simple.sh introuvable" >&2
exit 127
