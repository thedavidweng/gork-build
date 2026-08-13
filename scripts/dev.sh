#!/usr/bin/env bash
# Materialize the product tree and print how to build it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/maint/scripts/patchctl.py" checkout
SRC="$ROOT/.work/src"
echo
echo "Product tree: $SRC"
echo "  cargo run -p xai-grok-pager-bin --manifest-path \"$SRC/Cargo.toml\""
echo "  cargo build -p xai-grok-pager-bin --release --manifest-path \"$SRC/Cargo.toml\""
