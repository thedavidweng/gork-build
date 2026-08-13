# Gork Build maintenance control plane

Privacy patches, upstream lock, and contracts for replaying Gork hard-offs onto
new [`xai-org/grok-build`](https://github.com/xai-org/grok-build) monorepo syncs.

## Layout (recipe repo)

This git tree is the **control plane** only: `maint/patches`, overlays,
contracts, and workflows. The product source is **not** committed.

```
python3 maint/scripts/patchctl.py checkout   # -> .work/src  (gitignored)
```

`.work/src` is a worktree of `xai-org/grok-build` at the locked SHA with the
series applied. `cargo run` from there.

## The flywheel (human-gated)

Daily, when the patch series applies cleanly:

1. **`upstream-watch.yml`** (cron) — detects upstream drift, dispatches replay.
2. **`upstream-replay.yml`** — applies the series onto `.work/src`, runs
   privacy contracts, and only then `finalize-sync`s the lock and re-exports
   patches. It opens a **draft** PR that touches only `maint/` (lock +
   patches). No wholesale product-tree merge. No auto-merge. A sensitive-path
   hit adds `security-review-required`.
3. **`release.yml`** (cron) — materializes `.work/src` and publishes
   `v<upstream>-gork.N` (N increments so a patch-only fix can ship) for the
   six-target matrix. Each archive is extracted and the binary is executed
   (`--version` / `--help` / `update` must refuse vendor installers) before
   publish. The same packaging path is smoked on every PR (`Build gork`).
   `published-release-smoke.yml` re-downloads the GitHub Release assets the
   way a user would and smokes them again.

Fail-closed exits from the loop (human/agent needed):

- Critical patch conflict → issue labeled `upstream-sync`,
  `security-review-required`; resolve by replaying manually (see Commands),
  amending the owning patch, and re-running `finalize-sync`.
- Privacy contracts failed → **no** finalize-sync, **no** PR.
- Branding skipped or sensitive paths changed → draft PR, human review.

## Commands

```bash
python3 maint/scripts/patchctl.py checkout           # .work/src at lock.commit + series
python3 maint/scripts/patchctl.py detect
python3 maint/scripts/patchctl.py export --tip HEAD  # from .work/src
python3 maint/scripts/patchctl.py apply --upstream <SHA>
python3 maint/scripts/patchctl.py verify --skip-expensive
python3 maint/scripts/patchctl.py lint               # static + apply-only roundtrip
python3 maint/scripts/patchctl.py finalize-sync --upstream <SHA> --version X --source-rev Y
python3 maint/scripts/patchctl.py report --new <sha> --json
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
