# Gork Build privacy model

Gork Build is a **VSCodium-style** community distribution of xAI Grok Build:
same agent capabilities, **no product tracking**, **no research data
collection** on the client.

Background (why the hard-offs exist):
[wire analysis of Grok Build 0.2.93](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547).

## Hard guarantees (this build)

These are compile-time / resolver-level hard-offs. Remote settings, env vars,
and config files **cannot** re-enable them while
`xai_grok_version::PRIVACY_BUILD == true`.

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
5. **Auto-update from x.ai** — off unless the user sets
   `[cli] auto_update = true` in config.
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

The README claim
“[no secrets send to xAI](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547)”
refers to **not packaging research traces / whole-repo uploads / product
analytics** that were shown to exfiltrate secrets in the wire analysis. It
does **not** mean the model never sees file contents you ask the agent to
read — that is still how cloud coding works.

## Server-side retention

Client hard-offs do not control what xAI’s **API servers** log or retain for
inference. For account-level retention preference, use `/privacy opt-out`
(or the coding-data-sharing setting). Gork Build defaults that preference to
**opt-out** where the client owns the default.

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
