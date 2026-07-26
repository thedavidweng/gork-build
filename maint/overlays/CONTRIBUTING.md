# Contributing to Gork Build

Thanks for helping improve **Gork Build** — the VSCodium-style community
distribution of [Grok Build](https://github.com/xai-org/grok-build).

This project accepts external contributions. By submitting a pull request or
other contribution, you agree that your work is licensed under the same terms
as the project: the **Apache License, Version 2.0** (see [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE)).

## Before you start

1. Read [`PRIVACY.md`](PRIVACY.md). Gork Build hard-disables vendor research
   telemetry; changes that re-enable product analytics, Mixpanel, or research
   GCS uploads without an explicit, documented opt-in will be rejected.
2. Search [existing issues](https://github.com/thedavidweng/gork-build/issues)
   and PRs to avoid duplicates.
3. For large design changes, open an issue first so we can align on direction.

## Development setup

Requirements:

- Rust toolchain from [`rust-toolchain.toml`](rust-toolchain.toml) (`rustup`
  installs it automatically)
- `protoc` — see [`bin/protoc`](bin/protoc) or install a system `protoc` /
  set `$PROTOC`

```sh
git clone https://github.com/thedavidweng/gork-build.git
cd gork-build
cargo check -p xai-grok-pager-bin
cargo run -p xai-grok-pager-bin          # launches the TUI binary `gork`
```

Useful checks (same gates as GitHub Actions CI):

```sh
cargo fmt --all -- --check
cargo clippy --no-deps \
  -p xai-grok-version -p xai-mixpanel -p xai-grok-telemetry -p xai-grok-update \
  --lib -- -D warnings
cargo clippy --no-deps -p xai-grok-pager-bin --bins -- -D warnings
cargo test -p xai-mixpanel --lib
cargo test -p xai-grok-version --lib
```

Prefer focused tests for the crate you touch over a full workspace run unless
you are changing shared infrastructure. Full-workspace `cargo clippy -D warnings`
is not required — the monorepo snapshot is too noisy; CI uses `--no-deps` on the
privacy crates and the `gork` binary.

## Branching and commits

- Branch from `main`: `git checkout -b fix/short-description`
- Keep commits focused; one logical change per commit when practical
- Prefer **imperative, present-tense** subjects (Conventional Commits style is
  welcome but not mandatory):

  ```
  fix(privacy): keep trace_upload hard-off under remote settings
  docs: rephrase getting-started for community builds
  chore: refresh NOTICE attribution
  ```

- Do not force-push shared branches other people are using
- Do not commit secrets, `.env` files, or large generated artifacts

## Pull requests

1. **Fork** (if you are not a maintainer) and open a PR against `main`.
2. Fill in a clear description:
   - What problem does this solve?
   - How did you verify it (commands, tests, manual steps)?
   - Any privacy or security impact?
3. Keep the PR reviewable: small diffs beat mega-patches. Split mechanical
   renames from behavioral changes when possible.
4. Update docs (`README.md`, `PRIVACY.md`, user-guide) when behavior changes.
5. Ensure CI (when configured) and local checks relevant to your change pass.
6. Expect review feedback; please respond or push follow-up commits rather than
   opening a parallel PR.

### Review bar

- Correctness and safety first
- Privacy hard-offs remain hard-offs
- No silent reintroduction of vendor branding or tracking
- Match existing Rust style and module boundaries; avoid drive-by refactors

## Issues

- Use clear titles and reproduction steps for bugs
- Feature requests should explain the use case, not only the solution
- Security issues: see [`SECURITY.md`](SECURITY.md) — do not file public issues
  that include exploit details or secrets

## Code of conduct expectations

Be respectful. No harassment, spam, or bad-faith contributions. Maintainers may
close or lock discussions that derail the project.

## Upstream relationship

Gork Build periodically tracks
[`xai-org/grok-build`](https://github.com/xai-org/grok-build). When porting an
upstream fix:

- Prefer a clean cherry-pick or minimal reimplementation
- Preserve Apache-2.0 attribution (do not strip upstream copyright headers)
- Re-apply Gork Build privacy/branding deltas if upstream reintroduces tracking

## Questions

Open a GitHub Discussion or issue with the `question` label if unsure how to
proceed. For license questions, start from [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).
