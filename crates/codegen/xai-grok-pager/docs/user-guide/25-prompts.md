# Prompt templates (`/prompts`)

Browse, edit, and preset the LLM system prompts Grok injects into sessions and subagents. Defaults always come from the **installed binary** (embedded templates); your personal overrides and presets live only under `$GROK_HOME` (typically `~/.grok`), outside any project repository.

```
/prompts
```

Aliases: `/prompt`, `/system-prompts`.

You can also open the modal from the command palette, or drive presets without it:

```
/prompts list              # presets, and what this session reads from
/prompts use <name>        # bind this session to a preset
/prompts save <name>       # save this session's prompts as a new preset
/prompts update <name>     # replace an existing preset from this session
/prompts defaults          # run this session on built-in prompts only
```

---

## Why this exists

- **See what the model actually gets** — catalog entries include the base session system prompt, compaction prompt, apply-patch profile, and built-in subagent personas.
- **Customize without forking the binary** — save an override; leave a template alone and a future Grok update still refreshes its stock default.
- **One preset per session** — two sessions can run two different prompt sets at the same time, and a resumed session comes back on the preset it ran on.
- **Keep personal prompts off GitHub** — overrides and presets are written under `~/.grok`, never under the project tree. Rebuilds, updates, and force-pushes of the source repo do not ship your private prompt text.

---

## Per-session presets

Each session resolves prompts from exactly one **source**:

| Source | Meaning |
|--------|---------|
| A preset | `~/.grok/prompt-presets/<name>/` — the normal case |
| Unnamed overrides | `~/.grok/prompts/` — the legacy scratch set, when no preset is bound |
| Built-in defaults | Nothing on disk is read; prompts come from the binary |

The source is chosen per session and remembered by session id in `~/.grok/prompt-presets/.state.json`, so:

- Applying a preset in one session **never** disturbs a session running elsewhere.
- Editing a prompt writes into the bound preset only — no shared working copy, no silent write-through to a preset you started from.
- Resuming a session restores the preset it used.
- A session with no binding of its own starts on `.active`, the last preset you selected anywhere; failing that, on the unnamed overrides (empty ones simply resolve to the built-in prompts).

Create as many presets as you like: there is no cap, names just have to be unique. Saving under a name that already exists **asks first** (`o` / `Enter` to replace, `Esc` to keep); `u` on the Presets tab is the explicit "replace this one" path.

The modal's status line always names the source the session reads from.

---

## Defaults vs overrides

| Source | Where it lives | When it updates |
|--------|----------------|-----------------|
| **Default** | Embedded MiniJinja templates / constants in the binary | Every install or rebuild that ships new templates |
| **Preset body** | `~/.grok/prompt-presets/<name>/<id>.md` | Only when you save from `/prompts` or edit the file |
| **Preset metadata** | `~/.grok/prompt-presets/<name>/meta.json` | Description and timestamps, written by the modal |

Resolution for each catalog id:

1. If the session's source holds a **non-empty** file for that id → use it.
2. Otherwise → use the binary default.

So a preset only carries the prompts you actually changed: everything else keeps tracking the stock template, and a Grok update still refreshes it. Saving an override that is **byte-identical** to the current default **clears** that file. Reset (`r`) does the same.

Override `GROK_HOME` to relocate all of this (and the rest of Grok's user data). See [Configuration](05-configuration.md#file-locations).

### Upgrading from the pre-preset layout

If `~/.grok/prompts/` already held overrides, they are promoted **once** into a preset named `perso`, which becomes the default for new sessions. The original files are left untouched, so an older binary still finds them.

---

## Catalog entries

### Session

| Title | File id (`<source>/<id>.md`) | Role |
|-------|------------------------------|------|
| Base system | `base-system` | Primary session system-prompt template (`prompt.md`). MiniJinja with `${{ }}` delimiters. |
| Subagent shell | `subagent-shell` | Base system template for subagents; persona body is appended (extend mode). |
| Apply-patch | `apply-patch` | Coding / apply-patch profile system template. |
| Compact system | `compact-system` | Short system prompt used after conversation compaction replaces full history (plain text, not MiniJinja). |

### Subagents

| Title | File id | Role |
|-------|---------|------|
| general-purpose | `subagent-general-purpose` | Persona body for the general-purpose subagent. |
| explore | `subagent-explore` | Persona body for the read-only explore subagent. |
| plan | `subagent-plan` | Persona body for the read-only plan / architect subagent. |

> **Not the same as** `prompt_file = ".grok/prompts/researcher.md"` on a **custom role** in `config.toml`. That path is project-scoped role configuration ([Subagents](16-subagents.md)). The `/prompts` catalog only covers the **built-in** session and stock subagent templates.

---

## Using the modal

The modal mirrors other browsers such as `/memory` and `/settings`: chrome tabs, list + preview, filter, fullscreen.

### Tabs

| Tab | Purpose |
|-----|---------|
| **Catalog** | List every built-in prompt, show preview, edit or reset overrides. |
| **Presets** | Named prompt sets: bind one to this session, create, update, rename, duplicate, delete. |

Switch tabs with `Tab` (or the chrome tab strip).

### Catalog shortcuts

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move through the list |
| `/` | Focus the filter field (type to narrow; `Esc` leaves filter) |
| `Enter` | Edit the selected prompt **in the TUI** |
| `r` | Reset selected override to the binary default (confirm with `r` again) |
| `n` | Save this session's prompts as a new named preset |
| `Ctrl+F` | Toggle fullscreen |
| `Esc` | Close the modal (or cancel edit / filter / confirm) |

Mouse: click a row to select; scroll the list or preview; drag scrollbars. In the editor, **click places the caret** like the main prompt field.

### Editing a prompt (in-TUI)

| Key | Action |
|-----|--------|
| (type) | Edit multi-line body |
| `Ctrl+S` | Save into the session's source (or clear it if the body matches the default) |
| `Ctrl+E` | Suspend and open the file in `$VISUAL` / `$EDITOR` |
| `Esc` | Cancel and discard unsaved changes |
| Click | Place the blinking caret in the field |

Editing while the session runs on **built-in defaults** is refused: bind or create a preset first, so an edit always has a home.

### Presets tab

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate presets |
| `/` | Filter by name or description |
| `Enter` | Use the selected preset **in this session** |
| `n` | Name and save a new preset from this session's prompts |
| `u` | Update (replace) the selected preset from this session's prompts |
| `R` | Rename the selected preset (bindings and `.active` follow) |
| `y` | Duplicate the selected preset under a new name |
| `d` | Delete the selected preset (confirm with `d`) |
| `c` | Run this session on built-in defaults (confirm with `c`; deletes nothing) |
| `Tab` | Back to Catalog |

Preset names accept `a-z`, `0-9`, `_`, `-`, and `.`, up to 64 characters, and may not start with a dot. The name field is pre-filled with a free name, so `n` twice in a row never collides.

---

## On-disk layout

```
$GROK_HOME/                    # default: ~/.grok
  prompts/                     # unnamed scratch overrides (legacy)
    base-system.md
    ...
  prompt-presets/
    .active                    # preset new sessions start on
    .state.json                # per-session bindings (versioned)
    my-coding-style/
      meta.json                # description + timestamps
      base-system.md
      ...
```

You may edit these Markdown files outside Grok; the next session / render of `/prompts` picks them up. Prefer the modal when you want preview, reset-to-default, and per-session binding in one place.

---

## Tips

- **Binary update, keep customizations** — only override what you changed; untouched ids track stock templates automatically. Nothing under `$GROK_HOME` is touched by an update, which only replaces the binary.
- **Experiment safely** — duplicate a preset (`y`) before a large edit, then switch back with `Enter` if it goes wrong.
- **MiniJinja** — most templates use `${{ }}` / `${% %}` delimiters. Broken templates can break session start; keep a known-good preset around.
- **Privacy / open source** — do not commit `~/.grok/prompts/` or `~/.grok/prompt-presets/` into a public tree; that is user data, not project config.

---

## Related

- [Slash commands](04-slash-commands.md#prompts) — `/prompts` entry
- [Configuration · File locations](05-configuration.md#file-locations)
- [Subagents and Personas](16-subagents.md) — custom roles / `prompt_file` (separate from this catalog)
- [Project Rules (AGENTS.md)](12-project-rules.md) — project instructions layered with the system prompt
