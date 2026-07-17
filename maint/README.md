# Gork Build maintenance control plane

Privacy patches, upstream lock, and contracts for replaying Gork hard-offs onto
new [`xai-org/grok-build`](https://github.com/xai-org/grok-build) monorepo syncs.

## Commands

```bash
python maint/scripts/patchctl.py detect
python maint/scripts/patchctl.py export --tip HEAD   # sets patch_tip to last *functional* commit
python maint/scripts/patchctl.py apply --upstream <SHA>
python maint/scripts/patchctl.py verify --skip-expensive
python maint/scripts/patchctl.py lint                # static + roundtrip
python maint/scripts/patchctl.py finalize-sync --upstream <SHA> --version X --source-rev Y
python maint/scripts/patchctl.py roundtrip
python maint/scripts/patchctl.py report --new <sha> --json
```

## Apply policy

- **Critical** patches: conflict → fail-closed (exit 3), no draft PR.
- **Trailing non-critical** patches (`product-identity`, `package-publishing`, `branding-docs`):
  conflict → skip remainder, exit 4, draft PR with `branding-required`.
- Control plane (`maint/`, control workflows) is always restored via `control-files.toml`.
- Community docs/assets live under `maint/overlays/` and apply even when branding patches skip.

## Lock

`upstream.lock.toml` records the authoring base triple. After a successful sync PR,
`finalize-sync` updates it to the new upstream and re-exports the series.

`Cargo.lock` is **not** in the patch series (`lock-policy.toml`: `inherit-upstream`).
