# Grok Privacy maintenance control plane

Privacy patches, upstream lock, and contracts for replaying the privacy hard-offs onto
new [`xai-org/grok-build`](https://github.com/xai-org/grok-build) monorepo syncs.

## Atelier vs client

```bash
grok rebuild                # menu (↑↓) — atelier : source de vérité du repo
grok rebuild --check
grok rebuild --upstream
grok rebuild --finalize
grok rebuild --push-repo    # push origin (confirm)

grok update                 # binaire client installé (releases GH)
grok update --check
```

Voir [`scripts/README-rebuild.md`](../scripts/README-rebuild.md).

## Commands (patchctl bas niveau)

```bash
python maint/scripts/patchctl.py detect
python maint/scripts/patchctl.py export --tip HEAD   # sets patch_tip to last *functional* commit
python maint/scripts/patchctl.py apply --upstream <SHA>
python maint/scripts/patchctl.py apply --upstream <SHA> --keep-conflict  # portage manuel
python maint/scripts/patchctl.py apply --upstream <SHA> --auto-resolve-conflicts  # rerere + résolveur, gardé par les contrats
python maint/scripts/patchctl.py apply --continue                        # reprise après résolution
python maint/scripts/patchctl.py verify --skip-expensive
python maint/scripts/patchctl.py lint                # static + roundtrip vs HEAD (and product_tip)
python maint/scripts/patchctl.py finalize-sync --upstream <SHA> --version X --source-rev Y
python maint/scripts/patchctl.py roundtrip
python maint/scripts/patchctl.py report --new <sha> --json
```

## Apply policy

- **Critical** patches: conflict → fail-closed (exit 3), no draft PR. The conflict
  report is written to `<git-dir>/grok-apply-conflict.diff` before the abort, so the
  evidence survives. `--keep-conflict` leaves the `am` in progress instead: resolve,
  `git add`, then `apply --continue` replays the rest of the series **and** the apply
  tail (overlays, lock policy, control commits) that `finalize-sync` depends on.
  Resume state lives in `<git-dir>/grok-apply-state.json` — never in the product tree.
- **Conflict resolution** happens in two stages, and only the second one is
  optional. **rerere** replays a resolution this repository has already
  recorded — no model, no network, nothing to switch off, so a conflict
  answered once is answered for every later sync. What rerere has never seen
  goes to the **resolver command** (`PATCHCTL_CONFLICT_RESOLVER_CMD`, default
  `claude -p`), one conflict region at a time: the file is reassembled around
  the replacements, so content outside the regions cannot be rewritten. Which
  patches the resolver may touch is the `auto_resolve` key in `patchset.toml`,
  defaulting to "those that declare contracts";
  `patchctl apply --auto-resolve-conflicts` (menu entry *Résolveur IA si
  conflit inédit*, off with `grok rebuild --no-auto-resolve`) enables it for a
  run. With it off, recorded resolutions still replay and only a genuinely new
  conflict stops for a hand port. The apply then **must** pass the auto-resolved patches' contracts or it
  fails closed (exit 3, branch left for inspection). A patch opted in with
  `auto_resolve = true` and no contract is accepted on that opt-in, warned about
  on stderr, and marked `unverified` in `last-apply-status.json`. Auto-resolutions are marked `AUTO-RESOLVED` in the output and listed
  under `auto_resolved` in `last-apply-status.json`; a rejected resolver proposal is
  appended to the conflict report. Tests: `maint/scripts/tests/test_patchctl_auto_resolve.py`.
- **Trailing non-critical** patches (`product-identity`, `package-publishing`, `branding-docs`):
  conflict → skip remainder, exit 4, draft PR with `branding-required`.
- Control plane (`maint/`, control workflows) is always restored via `control-files.toml`.
- Community docs/assets live under `maint/overlays/` and apply even when branding patches skip.

## Lock

`upstream.lock.toml` records the authoring base triple. After a successful sync PR,
`finalize-sync` updates it to the new upstream and re-exports the series.

`Cargo.lock` is **not** in the patch series (`lock-policy.toml`: `inherit-upstream`).
