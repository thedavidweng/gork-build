# Gork Build (shell / agent runtime)

Terminal-based AI coding assistant and agentic harness — community distribution
of Grok Build with vendor telemetry and branding removed.

Use it interactively as a TUI, or integrate it into your own apps via headless
mode and the Agent Client Protocol (ACP).

> Upstream source: [xai-org/grok-build](https://github.com/xai-org/grok-build)  
> This fork: [thedavidweng/gork-build](https://github.com/thedavidweng/gork-build)

## Quick Start

```bash
# From this repository (recommended)
cargo run -p xai-grok-pager-bin
# binary name: gork

# Headless (for scripts/automation)
gork -p "Explain this codebase"

# Agent mode (for IDE/app integration)
gork agent stdio
```

Build a release binary:

```bash
cargo build -p xai-grok-pager-bin --release
# → target/release/gork
```

## Contents

Detailed guides live under
[`crates/codegen/xai-grok-pager/docs/user-guide/`](../xai-grok-pager/docs/user-guide/).

- [Getting Started](../xai-grok-pager/docs/user-guide/01-getting-started.md)
- [Authentication](../xai-grok-pager/docs/user-guide/02-authentication.md)
- [Configuration](../xai-grok-pager/docs/user-guide/05-configuration.md)
- [Privacy model](../../../PRIVACY.md)

## Privacy

Gork Build hard-disables product analytics and research trace uploads. See
[`PRIVACY.md`](../../../PRIVACY.md). Model inference still uses the Grok API
with your credentials when you use cloud models.

## License

Apache-2.0. Upstream copyright retained; see [`LICENSE`](../../../LICENSE) and
[`NOTICE`](../../../NOTICE).
