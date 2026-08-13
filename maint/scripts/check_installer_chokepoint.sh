#!/usr/bin/env bash
# Static inventory: vendor install chokepoints must remain gated.
set -euo pipefail
CONTROL="$(cd "$(dirname "$0")/../.." && pwd)"
TREE="${GORK_WORK_SRC:-$CONTROL}"
cd "$TREE"

# minimum_version.rs was folded into version_policy.rs upstream (0.2.111);
# the policy path no longer installs, so only auto_update.rs carries the gate.
grep -n 'vendor_auto_update_forbidden' \
  crates/codegen/xai-grok-update/src/auto_update.rs

# version_policy must never call the installer (it only refuses to start).
if grep -n 'run_install_script' crates/codegen/xai-grok-update/src/version_policy.rs; then
  echo "version_policy.rs must not reach the installer" >&2
  exit 1
fi

grep -n 'if vendor_auto_update_forbidden' \
  crates/codegen/xai-grok-update/src/auto_update.rs

grep -n 'https://x.ai/cli' crates/codegen/xai-grok-update/src/version.rs

echo "installer chokepoint inventory ok"
