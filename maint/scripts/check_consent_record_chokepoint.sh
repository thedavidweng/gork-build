#!/usr/bin/env bash
# Static inventory: consent acknowledgments must stay local in privacy builds.
#
# The dispatch-level behavior is covered by the `consent-record-off` contract
# (accepting a notice emits no RecordConsentUpstream). What a runtime test
# cannot prove is that the shipped send site still gates on PRIVACY_BUILD and
# that no second send path appeared — hence this grep.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

LIFECYCLE=crates/codegen/xai-grok-pager/src/app/dispatch/session/lifecycle.rs
EFFECTS=crates/codegen/xai-grok-pager/src/app/effects/mod.rs

# 1. The dispatcher only emits the effect outside privacy builds.
grep -n 'PRIVACY_BUILD' "$LIFECYCLE"

# 2. Send-site chokepoint: the handler consults PRIVACY_BUILD within the lines
#    leading up to the ACP method literal.
if ! grep -B 14 '"x.ai/consent/record"' "$EFFECTS" | grep -q 'PRIVACY_BUILD'; then
  echo "x.ai/consent/record send site is not guarded by PRIVACY_BUILD" >&2
  exit 1
fi
grep -n '"x.ai/consent/record"' "$EFFECTS"

# 3. Exactly one send site: the ACP method literal appears once in the pager.
count=$(grep -rn '"x.ai/consent/record"' --include='*.rs' \
  crates/codegen/xai-grok-pager/src/ | wc -l)
if [ "$count" != "1" ]; then
  echo "expected exactly 1 x.ai/consent/record send site, found $count" >&2
  grep -rn '"x.ai/consent/record"' --include='*.rs' crates/ >&2
  exit 1
fi

echo "consent record chokepoint inventory ok"
