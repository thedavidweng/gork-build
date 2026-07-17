# Gork Build privacy model

Gork Build is a **VSCodium-style** community distribution of xAI Grok Build:
same agent capabilities, **no product tracking**, **no research data
collection** on the client.

Background (why the hard-offs exist):
[wire analysis of Grok Build 0.2.93](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547).

## Hard guarantees (this build)

These are compile-time / resolver-level hard-offs. Remote settings, env vars,
and config files **cannot** re-enable them while
`xai_grok_version::PRIVACY_BUILD == true` (including product telemetry
env vars such as `GROK_TELEMETRY_*` and vendor update settings).

**Installer integration tests only:** the optional Cargo feature
`updater-integration-tests` (never enabled by `cargo run` / product builds)
plus `GORK_TEST_ALLOW_UPDATE=1` can relax the vendor-update gate so local
mock download suites work. That feature is not part of any product binary;
ordinary debug and release builds have no runtime escape hatch.

1. **Research / trace uploads** — `resolve_trace_upload()` always returns
   `false`. `get_trace_context` never builds an upload method for GCS session
   traces, turn_messages archives, etc.
2. **Data-collection flags** — `AuthManager::is_data_collection_disabled()` is
   always `true`; `allows_data_collection()` is always `false`.
3. **Product telemetry** — `resolve_telemetry_mode()` is always `Disabled`.
   Mixpanel clients are never constructed; `Mixpanel::{track,engage}` are
   no-ops if anything still calls them.
4. **Repo change packaging** — `[repo_changes_dedup] enabled` defaults to
   `false`.
5. **Vendor auto-update** — hard-disabled. Gork Build never installs from
   x.ai update channels (`x.ai/cli/install.*`); that path would replace this
   fork with official Grok Build. Policy is enforced at the bottom-level
   chokepoint `run_install_script` (and every caller of it). Leader hourly
   update and minimum-version enforcement also **fail closed** under the
   privacy build — they refuse vendor install rather than overwrite the binary.
   Update by rebuilding from this repository or community releases.
   Product binaries (debug or release) cannot re-enable vendor install via
   env; see the test-only Cargo feature note above.
6. **Sentry** — no compile-time DSN; only an explicit runtime `SENTRY_DSN`
   can enable crash reporting.

## Required network (not “telemetry”)

To run a cloud coding agent you still need:

| Destination | Why |
|-------------|-----|
| Grok / cli-chat-proxy model API | Inference (`/v1/responses` etc.) |
| Auth endpoints (OIDC / login) | Your login |
| Optional user-configured tools | MCP servers, web search, etc. you enable |

**Anything the agent reads for a task can appear in the model request body.**
That is inference context, not Gork Build research upload. Prefer not opening
`.env` / key material in the session; secret redaction exists for some
telemetry paths but is not a complete guarantee on the model path.

Research / product-analytics hard-offs stop packaging of session traces,
whole-repo research uploads, and Mixpanel-style events shown in the
[wire analysis](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547).
They do **not** mean the model never sees file contents you ask the agent to
read — that is still how cloud coding works.

## Network egress inventory

Channels the binary *can* touch, and how Gork Build treats them:

| Channel | Default in this build | Notes |
|---------|----------------------|--------|
| **Model API** (cli-chat-proxy / `/v1/responses` etc.) | On when you use the agent | Agent-selected context (prompts, tools, file contents read for the task) goes here — required for cloud coding |
| **Auth** (OIDC / login) | On when you sign in | Credentials / tokens for your account |
| **Remote settings / managed config** | May fetch when configured | Feature/config pull; cannot re-enable research or product telemetry hard-offs |
| **Model catalog** | May fetch when listing models | Model metadata for the picker |
| **Assets** (themes, announcements, static assets) | Optional / as needed | Non-secret presentation content |
| **Feedback** | User-initiated only | Only if you explicitly submit feedback |
| **External OTLP** | Off unless you configure an exporter | Product telemetry mode is hard-`Disabled`; custom OTLP is user opt-in |
| **Sentry** | Off | Only if you set `SENTRY_DSN` |
| **MCP / plugins** | Off until you enable | User-configured servers and marketplaces you add |
| **Web search** | Off until you enable | Tool you turn on for the agent |
| **Update metadata / install** | Vendor install **hard-off** | No `x.ai/cli` install scripts; `run_install_script` fail-closed; leader + minimum-version refuse vendor overwrite. Rebuild from source or community releases |

## Server-side retention

Client hard-offs do not control what xAI’s **API servers** log for inference.
Gork Build locks **coding-data retention to opt-out** (no `/privacy opt-in`,
settings cannot enable sharing). Local state always stays opted out; the shell
refuses opt-in over ACP.

Deleting historical research data already uploaded by **upstream** Grok Build
is a **server-side** operation (upstream `/privacy` + their storage policies).
This fork cannot wipe objects already in `grok-code-session-traces` by itself.

## What we are not

- Not an offline / local-only LLM product
- Not affiliated with xAI
- Not a claim that “zero bytes leave your machine”

## Verification ideas

1. Run under a local HTTPS proxy; confirm no `api.mixpanel.com` and no
   research `…/v1/storage` content uploads during a normal session.
2. Inspect logs for `trace.upload.decision` with `uploads_enabled: false` and
   `data_collection_disabled: true`.
3. Confirm `xai_grok_version::PRIVACY_BUILD` is `true` in your build.
