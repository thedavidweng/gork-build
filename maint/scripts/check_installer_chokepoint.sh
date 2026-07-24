#!/usr/bin/env bash
# Static inventory: vendor install chokepoints must remain gated.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

grep -n 'vendor_auto_update_forbidden' \
  crates/codegen/xai-grok-update/src/auto_update.rs \
  crates/codegen/xai-grok-update/src/minimum_version.rs

grep -n 'if vendor_auto_update_forbidden' \
  crates/codegen/xai-grok-update/src/auto_update.rs

grep -n 'https://x.ai/cli' crates/codegen/xai-grok-update/src/version.rs

echo "installer chokepoint inventory ok"
