# Gork Build maintenance control plane

Privacy patches, upstream lock, and contracts for replaying Gork hard-offs onto
new [`xai-org/grok-build`](https://github.com/xai-org/grok-build) monorepo syncs.

## The flywheel (no-intervention path)

Daily, fully automated when the patch series applies cleanly:

1. **`upstream-watch.yml`** (cron) — detects upstream drift, dispatches replay.
2. **`upstream-replay.yml`** — replays the patch series on the pinned upstream
   SHA, runs privacy contracts, `finalize-sync`s the lock, pushes the
   `sync/upstream-<ver>-<sha7>` branch, then publishes a **wholesale-tree merge
   branch** (`sync-merge/<ver>-<sha7>`: merge commit with parents
   `(main, sync-tip)` and tree = sync tip) and opens a ready PR. When contracts
   passed and no branding patch was skipped it dispatches the required-check
   workflows onto that branch and arms **auto-merge** — the PR merges itself
   once the 8 branch-protection contexts are green.
3. **`release.yml`** (cron) — releases once per product version: when `main`
   carries a version with no `v<version>-gork.1` release yet, it tags and
   builds the six-target matrix upstream ships prebuilds for
   (darwin/linux/win32 × arm64/x64 — same set as
   `crates/codegen/xai-grok-pager/npm/`) and publishes a GitHub release with
   `SHA256SUMS.txt`.

Fail-closed exits from the loop (human/agent needed):

- Critical patch conflict → issue labeled `upstream-sync`,
  `security-review-required`; resolve by replaying manually (see Commands),
  amending fixes into the owning patch commit, and re-running `finalize-sync`.
- Privacy contracts failed or branding patches skipped → PR opens but
  auto-merge is **not** armed.
- Required check red on the sync-merge PR (e.g. `cargo audit`) → PR waits.

## Commands

```bash
python maint/scripts/patchctl.py detect
python maint/scripts/patchctl.py export --tip HEAD   # sets patch_tip to last *functional* commit
python maint/scripts/patchctl.py apply --upstream <SHA>
python maint/scripts/patchctl.py verify --skip-expensive
python maint/scripts/patchctl.py lint                # static + roundtrip vs HEAD (and product_tip)
python maint/scripts/patchctl.py finalize-sync --upstream <SHA> --version X --source-rev Y
python maint/scripts/patchctl.py roundtrip
python maint/scripts/patchctl.py report --new <sha> --json
```

## Apply policy

- **Critical** patches: conflict → fail-closed (exit 3), no PR.
- **Trailing non-critical** patches (`product-identity`, `package-publishing`, `branding-docs`):
  conflict → skip remainder, exit 4, PR opens with `branding-required` (no auto-merge).
- Control plane (`maint/`, control workflows, dependabot config) is always
  restored via `control-files.toml`.
- Community docs/assets live under `maint/overlays/` and apply even when branding patches skip.

## Lock and dependencies

`upstream.lock.toml` records the authoring base triple. After a successful sync,
`finalize-sync` updates it to the new upstream and re-exports the series.

`Cargo.lock` is **not** in the patch series (`lock-policy.toml`:
`inherit-upstream`) — the lockfile follows upstream verbatim, with one
exception: **security floors** (`security-floors.toml`, applied by
`scripts/apply_security_floors.py` during every apply). A floor bumps a locked
package to a RustSec-fixed version only while upstream's lock is below it;
once upstream catches up the floor is a no-op and should be deleted. Never add
ordinary version bumps here — drift from upstream is the failure mode, and the
`cargo audit` required check is the gate that tells us when a floor is needed.

Dependabot is deliberately muted (`.github/dependabot.yml`,
automated-security-fixes off): automated bump PRs would decouple the tree from
upstream. Action pins in workflows are bumped manually in `maint/control/`
templates + live copies together.

## CodeQL

Advanced setup (`.github/workflows/codeql.yml`) excludes upstream *test* paths
from alerting — upstream test fixtures trip cleartext-logging/hard-coded-key
rules we cannot fix without diverging. Product-code findings inherited from
upstream are dismissed as "won't fix (upstream-inherited)" unless they touch a
privacy egress path, in which case they become a patch in the series.
