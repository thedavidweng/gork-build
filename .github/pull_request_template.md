## Summary

<!-- What does this PR change and why? -->

## How verified

```sh
# e.g.
cargo fmt --all -- --check
cargo clippy --no-deps -p xai-grok-version -p xai-mixpanel -p xai-grok-telemetry -p xai-grok-update --lib -- -D warnings
cargo test -p xai-mixpanel --lib
```

## Privacy / security

- [ ] Does **not** re-enable Mixpanel, research GCS upload, or vendor auto-update
- [ ] Coding-data retention stays opt-out only (if this touches that path)
- [ ] No secrets, `.env`, or large generated artifacts committed

## Checklist

- [ ] Docs updated if user-facing behavior changed (`README.md`, `PRIVACY.md`, …)
- [ ] Focused change (mechanical renames split from behavior when practical)
