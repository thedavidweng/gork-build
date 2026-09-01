#!/usr/bin/env bash
# Static inventory: session writeback must stay gated on the /config opt-in.
#
# The decision matrix itself is covered by the `session-writeback-off` contract
# (unit-level, drives StorageMode::resolve_privacy). What a runtime test cannot
# prove is that the shipped resolvers still *call* the gate — an upstream
# refactor that drops the call would leave those tests green. Hence this grep.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

CONFIG=crates/codegen/xai-grok-shell/src/config/mod.rs
PERSIST=crates/codegen/xai-grok-shell/src/session/persistence.rs

# 1. The single switch reads [cli].session_writeback and fails closed.
grep -n 'fn session_writeback_opt_in' "$CONFIG"
grep -n '"session_writeback"' "$CONFIG"

# 2. StorageMode::resolve hands every privacy build to the fork's resolver
#    before upstream's CLI > env > remote chain can run.
grep -n 'PRIVACY_BUILD' "$CONFIG"
grep -n 'return Self::resolve_privacy' "$CONFIG"

# 3. Last-line chokepoint: no RemoteSync is built without the opt-in.
grep -n 'session_writeback_opt_in' "$PERSIST"

# 4. The session backend host is denylisted for the egress smoke test.
grep -n 'code\\\.grok\\\.com' maint/contracts/network-denylist.txt

# 5. Nothing else may construct a RemoteSync: init_remote_sync is the only
#    place that calls RemoteSync::new outside tests.
callers=$(grep -rn 'RemoteSync::new(' --include='*.rs' crates/ \
  | grep -v 'src/remote/sync.rs' \
  | grep -vc 'test' || true)
if [ "$callers" != "1" ]; then
  echo "expected exactly 1 non-test RemoteSync::new caller (init_remote_sync), found $callers" >&2
  grep -rn 'RemoteSync::new(' --include='*.rs' crates/ >&2
  exit 1
fi

echo "writeback chokepoint inventory ok"
