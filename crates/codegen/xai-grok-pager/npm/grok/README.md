# Grok Privacy (`@lycaos/grok-privacy`)

Community VSCodium-style distribution of Grok Build — **vendor telemetry and
branding removed**.

**[Repository](https://github.com/lycaos/grok-privacy)** ·
**[Privacy](https://github.com/lycaos/grok-privacy/blob/main/PRIVACY.md)** ·
**[Wire analysis (why)](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547)**

Upstream credit: based on [xai-org/grok-build](https://github.com/xai-org/grok-build)
(Apache-2.0). Not affiliated with SpaceXAI / xAI.

## Install

Prefer building from source for the latest privacy hard-offs:

```bash
git clone https://github.com/lycaos/grok-privacy.git
cd grok-privacy
cargo build -p xai-grok-pager-bin --release
# → target/release/grok
```

npm packages (when published) install as:

```bash
npm i -g @lycaos/grok-privacy
grok
```

## Get started

```bash
grok                    # interactive TUI
grok -p "Explain this"  # one-shot
```

Authenticate with your Grok / xAI account (or `XAI_API_KEY`) — model inference
still uses the Grok API. Research uploads and product analytics stay off in
this build.

## License

Apache-2.0 — see the repository `LICENSE` and `NOTICE`.
