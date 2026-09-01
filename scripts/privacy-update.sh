#!/usr/bin/env bash
# Compat : ancien point d’entrée atelier.
# L’atelier s’appelle désormais « grok rebuild » (privacy-rebuild.sh).
# « grok update » = mise à jour du binaire client (privacy-client-update.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -t 2 ]]; then
  printf '!!  « privacy-update » / ancien atelier → utilisez %sgrok rebuild%s\n' $'\033[36m' $'\033[0m' >&2
  printf '    Mise à jour du binaire client : %sgrok update%s\n' $'\033[36m' $'\033[0m' >&2
else
  echo "!! privacy-update → grok rebuild (atelier) ; binaire client = grok update" >&2
fi
exec bash "$HERE/privacy-rebuild.sh" "$@"
