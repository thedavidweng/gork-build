//! Catalog of user-overridable LLM prompt templates.
//!
//! # Defaults vs user data
//!
//! - **Defaults** always come from the embedded agent templates / constants
//!   shipped in the binary. They are never snapshotted to disk as the source
//!   of truth — a Grok Build update that changes templates updates the
//!   default automatically for any prompt without a user override.
//! - **User data** lives only under `$GROK_HOME` (typically `~/.grok`),
//!   outside any project git tree:
//!   - `$GROK_HOME/prompt-presets/<name>/<id>.md` — preset bodies
//!   - `$GROK_HOME/prompt-presets/<name>/meta.json` — preset metadata
//!   - `$GROK_HOME/prompt-presets/.active` — preset new sessions start on
//!   - `$GROK_HOME/prompt-presets/.state.json` — per-session bindings
//!   - `$GROK_HOME/prompts/<id>.md` — unnamed scratch overrides (legacy)
//!
//! Rebuild / update / force-push of the source repo therefore never ships
//! personal prompts to GitHub; the installed binary still carries the stock
//! defaults, and everything under `$GROK_HOME` is preserved across updates.
//!
//! # Per-session prompt source
//!
//! Each session resolves its own [`PromptSource`]: pure built-in defaults, the
//! unnamed scratch overrides, or one named preset. The binding is keyed by
//! session id and persisted, so resuming a session restores the preset it ran
//! on. Two sessions can therefore run different presets at the same time —
//! nothing is copied into a shared working directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::RwLock;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::prompt::subagent_prompts;
use crate::prompt::template::{
    apply_patch_template_source, base_template_source, subagent_template_source,
    COMPACT_SYSTEM_PROMPT,
};

/// Stable catalog identifier (also the override file stem).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PromptId {
    BaseSystem,
    SubagentShell,
    ApplyPatch,
    CompactSystem,
    SubagentGeneralPurpose,
    SubagentExplore,
    SubagentPlan,
}

impl PromptId {
    pub const ALL: &[PromptId] = &[
        PromptId::BaseSystem,
        PromptId::SubagentShell,
        PromptId::ApplyPatch,
        PromptId::CompactSystem,
        PromptId::SubagentGeneralPurpose,
        PromptId::SubagentExplore,
        PromptId::SubagentPlan,
    ];

    /// File-safe id used as `<source>/<id>.md`.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BaseSystem => "base-system",
            Self::SubagentShell => "subagent-shell",
            Self::ApplyPatch => "apply-patch",
            Self::CompactSystem => "compact-system",
            Self::SubagentGeneralPurpose => "subagent-general-purpose",
            Self::SubagentExplore => "subagent-explore",
            Self::SubagentPlan => "subagent-plan",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        Self::ALL.iter().copied().find(|id| id.as_str() == s)
    }

    pub fn category(self) -> PromptCategory {
        match self {
            Self::BaseSystem | Self::SubagentShell | Self::ApplyPatch | Self::CompactSystem => {
                PromptCategory::Session
            }
            Self::SubagentGeneralPurpose | Self::SubagentExplore | Self::SubagentPlan => {
                PromptCategory::Subagents
            }
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Self::BaseSystem => "Base system",
            Self::SubagentShell => "Subagent shell",
            Self::ApplyPatch => "Apply-patch",
            Self::CompactSystem => "Compact system",
            Self::SubagentGeneralPurpose => "general-purpose",
            Self::SubagentExplore => "explore",
            Self::SubagentPlan => "plan",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::BaseSystem => {
                "Primary session system-prompt template (prompt.md). MiniJinja with ${{ }} delimiters."
            }
            Self::SubagentShell => {
                "Base system template for subagents. Persona body is appended (extend mode)."
            }
            Self::ApplyPatch => {
                "Coding / apply-patch profile system template used by the Codex-style agent."
            }
            Self::CompactSystem => {
                "Short system prompt used after conversation compaction replaces full history."
            }
            Self::SubagentGeneralPurpose => {
                "Persona body for the general-purpose subagent (appended to the subagent shell)."
            }
            Self::SubagentExplore => "Persona body for the read-only explore subagent.",
            Self::SubagentPlan => "Persona body for the read-only plan / architect subagent.",
        }
    }

    /// Whether the body is a MiniJinja template (`${{ }}` / `${% %}`).
    pub fn is_minijinja_template(self) -> bool {
        match self {
            Self::BaseSystem
            | Self::SubagentShell
            | Self::ApplyPatch
            | Self::SubagentGeneralPurpose
            | Self::SubagentExplore
            | Self::SubagentPlan => true,
            Self::CompactSystem => false,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PromptCategory {
    Session,
    Subagents,
}

impl PromptCategory {
    pub fn label(self) -> &'static str {
        match self {
            Self::Session => "Session",
            Self::Subagents => "Subagents",
        }
    }

    pub const ALL: &[PromptCategory] = &[PromptCategory::Session, PromptCategory::Subagents];
}

/// Static definition shown in the `/prompts` catalog.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PromptDefinition {
    pub id: PromptId,
    pub title: &'static str,
    pub description: &'static str,
    pub category: PromptCategory,
    pub is_minijinja_template: bool,
}

impl From<PromptId> for PromptDefinition {
    fn from(id: PromptId) -> Self {
        Self {
            id,
            title: id.title(),
            description: id.description(),
            category: id.category(),
            is_minijinja_template: id.is_minijinja_template(),
        }
    }
}

/// Resolved prompt body plus override metadata.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EffectivePrompt {
    pub id: PromptId,
    pub body: String,
    pub is_override: bool,
    pub override_path: Option<PathBuf>,
}

#[derive(Debug, Error)]
pub enum PromptCatalogError {
    #[error("unknown prompt id: {0}")]
    UnknownId(String),
    #[error("could not resolve GROK_HOME for prompt overrides")]
    NoGrokHome,
    #[error("invalid preset name: {0}")]
    InvalidPresetName(String),
    #[error("preset not found: {0}")]
    PresetNotFound(String),
    #[error("preset already exists: {0}")]
    PresetExists(String),
    #[error("prompt source is read-only: built-in defaults cannot be edited in place")]
    ReadOnlySource,
    #[error("io error for prompt override: {0}")]
    Io(#[from] std::io::Error),
}

/// Where a session reads its prompt bodies from.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "name", rename_all = "snake_case")]
pub enum PromptSource {
    /// Pure built-in templates; on-disk overrides are ignored.
    Defaults,
    /// The unnamed `$GROK_HOME/prompts/` scratch overrides.
    Scratch,
    /// A named preset under `$GROK_HOME/prompt-presets/`.
    Preset(String),
}

impl PromptSource {
    /// Preset name, when this source is a named preset.
    pub fn preset_name(&self) -> Option<&str> {
        match self {
            Self::Preset(name) => Some(name.as_str()),
            _ => None,
        }
    }

    /// Short label for status lines / the modal subtitle.
    pub fn label(&self) -> String {
        match self {
            Self::Defaults => "built-in defaults".to_string(),
            Self::Scratch => "unnamed overrides".to_string(),
            Self::Preset(name) => name.clone(),
        }
    }
}

/// Metadata for a named prompt preset under `$GROK_HOME/prompt-presets/`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptPresetInfo {
    pub name: String,
    /// Number of override files in the preset (catalog ids only).
    pub override_count: usize,
    /// Whether the current session resolves through this preset.
    pub is_active: bool,
    /// Free-form description from `meta.json`.
    pub description: Option<String>,
    /// RFC3339 timestamp of the last write, from `meta.json`.
    pub updated: Option<String>,
}

/// Whether a preset write may replace an existing preset.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PresetWriteMode {
    /// Fail with [`PromptCatalogError::PresetExists`] when the name is taken.
    Create,
    /// Replace the preset's catalog files when the name is taken.
    Overwrite,
}

/// List all catalog definitions in display order (grouped by category).
pub fn list_prompts() -> Vec<PromptDefinition> {
    PromptId::ALL
        .iter()
        .copied()
        .map(PromptDefinition::from)
        .collect()
}

/// Built-in default body for `id` (always fresh; never reads overrides).
pub fn default_body(id: PromptId) -> String {
    match id {
        PromptId::BaseSystem => base_template_source().to_string(),
        PromptId::SubagentShell => subagent_template_source().to_string(),
        PromptId::ApplyPatch => apply_patch_template_source().to_string(),
        PromptId::CompactSystem => COMPACT_SYSTEM_PROMPT.to_string(),
        PromptId::SubagentGeneralPurpose => subagent_prompts::GENERAL_PURPOSE_PROMPT.to_string(),
        PromptId::SubagentExplore => subagent_prompts::EXPLORE_PROMPT.to_string(),
        PromptId::SubagentPlan => subagent_prompts::PLAN_PROMPT.to_string(),
    }
}

/// Optional test-only home override. `grok_home()` is a process-wide
/// `OnceLock`, so tests that need isolation set this instead of `GROK_HOME`.
#[cfg(test)]
static TEST_PROMPTS_HOME: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);

fn grok_home() -> Option<PathBuf> {
    #[cfg(test)]
    {
        if let Ok(guard) = TEST_PROMPTS_HOME.lock()
            && let Some(ref home) = *guard
        {
            return Some(home.clone());
        }
    }
    xai_grok_config::user_grok_home()
}

/// Directory that holds the unnamed scratch overrides (`$GROK_HOME/prompts`).
pub fn prompts_dir() -> Option<PathBuf> {
    grok_home().map(|h| h.join("prompts"))
}

// ── Per-session source state ───────────────────────────────────────────────

#[derive(Debug, Default)]
struct SessionState {
    session_id: Option<String>,
    source: Option<PromptSource>,
}

static SESSION: RwLock<SessionState> = RwLock::new(SessionState {
    session_id: None,
    source: None,
});

fn session_state_read<R>(f: impl FnOnce(&SessionState) -> R) -> R {
    match SESSION.read() {
        Ok(guard) => f(&guard),
        Err(poisoned) => f(&poisoned.into_inner()),
    }
}

fn session_state_write<R>(f: impl FnOnce(&mut SessionState) -> R) -> R {
    match SESSION.write() {
        Ok(mut guard) => f(&mut guard),
        Err(poisoned) => f(&mut poisoned.into_inner()),
    }
}

/// The session id this process currently resolves prompts for, if any.
pub fn current_session_id() -> Option<String> {
    session_state_read(|s| s.session_id.clone())
}

/// Bind this process to `session_id` and resolve the source it should use.
///
/// Resolution order: persisted binding for this session id, then the `.active`
/// preset, then the scratch overrides when they hold anything, then defaults.
/// Called on session start / resume / switch.
pub fn activate_session(session_id: &str) -> Result<PromptSource, PromptCatalogError> {
    ensure_migrated()?;
    let bound = load_state()?
        .bindings
        .get(session_id)
        .map(|b| b.source.clone());
    let source = match bound {
        // A preset that has since been deleted falls back to the default source.
        Some(PromptSource::Preset(name)) if !preset_dir(&name)?.is_dir() => default_source(),
        Some(source) => source,
        None => default_source(),
    };
    session_state_write(|s| {
        s.session_id = Some(session_id.to_string());
        s.source = Some(source.clone());
    });
    Ok(source)
}

/// Forget the process-level session binding (tests, session teardown).
pub fn deactivate_session() {
    session_state_write(|s| {
        s.session_id = None;
        s.source = None;
    });
}

/// Source used by a session with no binding of its own: the `.active` preset,
/// else the unnamed scratch overrides.
///
/// Never [`PromptSource::Defaults`] — an empty scratch already resolves to the
/// built-in bodies, and unlike `Defaults` it is writable, so a first edit has
/// somewhere to land. `Defaults` is only ever reached on explicit request.
fn default_source() -> PromptSource {
    if let Ok(Some(name)) = read_active_marker()
        && preset_dir(&name).map(|d| d.is_dir()).unwrap_or(false)
    {
        return PromptSource::Preset(name);
    }
    PromptSource::Scratch
}

fn scratch_has_overrides() -> bool {
    let Some(dir) = prompts_dir() else {
        return false;
    };
    PromptId::ALL
        .iter()
        .any(|id| non_empty_file(&dir.join(format!("{}.md", id.as_str()))))
}

/// The source this session resolves prompt bodies from.
pub fn session_source() -> PromptSource {
    if let Some(source) = session_state_read(|s| s.source.clone()) {
        return source;
    }
    default_source()
}

/// Point this session at `source`, persisting the binding when a session id is
/// known. A named preset also becomes the `.active` default for new sessions.
pub fn set_session_source(source: PromptSource) -> Result<(), PromptCatalogError> {
    if let PromptSource::Preset(ref name) = source {
        let name = validate_preset_name(name)?;
        if !preset_dir(&name)?.is_dir() {
            return Err(PromptCatalogError::PresetNotFound(name));
        }
        write_active_marker(Some(&name))?;
    }
    if let Some(session_id) = current_session_id() {
        update_state(|state| {
            state.bindings.insert(
                session_id.clone(),
                SessionBinding {
                    source: source.clone(),
                    updated: now_rfc3339(),
                },
            );
            prune_bindings(&mut state.bindings);
        })?;
    }
    session_state_write(|s| s.source = Some(source));
    Ok(())
}

/// The source `session_id` resolves through.
///
/// The binding lives on disk, so a TUI and the leader process that actually
/// runs the agent agree on it even though they are different processes. The
/// in-memory source wins for the session this process is focused on, so a
/// switch takes effect before anything is persisted.
pub fn source_for_session(session_id: Option<&str>) -> PromptSource {
    let Some(session_id) = session_id else {
        return session_source();
    };
    if let Some(source) = session_state_read(|s| {
        (s.session_id.as_deref() == Some(session_id))
            .then(|| s.source.clone())
            .flatten()
    }) {
        return source;
    }
    let bound = load_state()
        .ok()
        .and_then(|state| state.bindings.get(session_id).map(|b| b.source.clone()));
    match bound {
        Some(PromptSource::Preset(name)) if !preset_exists(&name) => default_source(),
        Some(source) => source,
        None => default_source(),
    }
}

/// Directory backing `source`, or `None` for pure defaults.
fn source_dir_for(source: &PromptSource) -> Option<PathBuf> {
    match source {
        PromptSource::Defaults => None,
        PromptSource::Scratch => prompts_dir(),
        PromptSource::Preset(name) => preset_dir(name).ok(),
    }
}

/// Directory backing the session's overrides, or `None` for pure defaults.
fn override_source_dir() -> Option<PathBuf> {
    source_dir_for(&session_source())
}

/// Writable directory backing the session's overrides.
fn writable_source_dir() -> Result<PathBuf, PromptCatalogError> {
    match session_source() {
        PromptSource::Defaults => Err(PromptCatalogError::ReadOnlySource),
        PromptSource::Scratch => prompts_dir().ok_or(PromptCatalogError::NoGrokHome),
        PromptSource::Preset(name) => preset_dir(&name),
    }
}

/// Override file path for `id` in the session's source, or `None` when the
/// session runs on pure defaults (or `GROK_HOME` is unavailable).
pub fn override_path(id: PromptId) -> Option<PathBuf> {
    override_source_dir().map(|d| d.join(format!("{}.md", id.as_str())))
}

/// Override path under an explicit home (tests).
pub fn override_path_in(home: &Path, id: PromptId) -> PathBuf {
    home.join("prompts").join(format!("{}.md", id.as_str()))
}

/// Load override body if the file exists and is non-empty after trim.
pub fn load_override(id: PromptId) -> Result<Option<String>, PromptCatalogError> {
    let Some(path) = override_path(id) else {
        return Ok(None);
    };
    load_override_at(&path)
}

fn load_override_at(path: &Path) -> Result<Option<String>, PromptCatalogError> {
    match std::fs::read_to_string(path) {
        Ok(body) => {
            if body.trim().is_empty() {
                Ok(None)
            } else {
                Ok(Some(body))
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

fn non_empty_file(path: &Path) -> bool {
    path.is_file()
        && std::fs::read_to_string(path)
            .map(|b| !b.trim().is_empty())
            .unwrap_or(false)
}

/// Effective body = the session source's override if present, else default.
pub fn effective_prompt(id: PromptId) -> Result<EffectivePrompt, PromptCatalogError> {
    effective_prompt_for(None, id)
}

/// [`effective_prompt`] resolved for one specific session.
pub fn effective_prompt_for(
    session_id: Option<&str>,
    id: PromptId,
) -> Result<EffectivePrompt, PromptCatalogError> {
    let path = source_dir_for(&source_for_session(session_id))
        .map(|d| d.join(format!("{}.md", id.as_str())));
    if let Some(ref p) = path
        && let Some(body) = load_override_at(p)?
    {
        return Ok(EffectivePrompt {
            id,
            body,
            is_override: true,
            override_path: Some(p.clone()),
        });
    }
    Ok(EffectivePrompt {
        id,
        body: default_body(id),
        is_override: false,
        override_path: path,
    })
}

/// Convenience: body only (falls back to default on IO / missing home).
pub fn resolve_body(id: PromptId) -> String {
    resolve_body_for(None, id)
}

/// [`resolve_body`] resolved for one specific session.
pub fn resolve_body_for(session_id: Option<&str>, id: PromptId) -> String {
    match effective_prompt_for(session_id, id) {
        Ok(ep) => ep.body,
        Err(e) => {
            tracing::warn!(error = %e, prompt = id.as_str(), "prompt override resolve failed; using default");
            default_body(id)
        }
    }
}

/// Whether an active (non-empty) override is in effect for `id`.
///
/// Matches [`effective_prompt`]: a present but empty/whitespace file is
/// treated as no override.
pub fn has_override(id: PromptId) -> bool {
    matches!(load_override(id), Ok(Some(_)))
}

/// Write `body` into the session's source, creating the directory as needed.
///
/// If `body` matches the built-in default (trimmed), the override is **removed**
/// instead so future binary updates keep delivering the stock template.
///
/// Editing while a preset is bound writes into that preset only: no mirroring,
/// no shared working copy, so a parallel session on another preset is untouched.
pub fn save_override(id: PromptId, body: &str) -> Result<PathBuf, PromptCatalogError> {
    let dir = writable_source_dir()?;
    let path = dir.join(format!("{}.md", id.as_str()));
    if body.trim() == default_body(id).trim() {
        reset_override(id)?;
        // Still return the canonical path (may not exist after clear).
        return Ok(path);
    }
    std::fs::create_dir_all(&dir)?;
    std::fs::write(&path, body)?;
    touch_source_meta();
    Ok(path)
}

/// Materialize the default into the override path (for `$EDITOR` open).
///
/// If an override already exists, returns its path unchanged.
pub fn materialize_for_edit(id: PromptId) -> Result<PathBuf, PromptCatalogError> {
    let dir = writable_source_dir()?;
    let path = dir.join(format!("{}.md", id.as_str()));
    if path.is_file() {
        return Ok(path);
    }
    // Force-write even if equal to default (editor needs a path on disk).
    std::fs::create_dir_all(&dir)?;
    std::fs::write(&path, default_body(id))?;
    touch_source_meta();
    Ok(path)
}

/// Delete the override file in the session's source. No-op if absent.
pub fn reset_override(id: PromptId) -> Result<(), PromptCatalogError> {
    let dir = writable_source_dir()?;
    let path = dir.join(format!("{}.md", id.as_str()));
    match std::fs::remove_file(&path) {
        Ok(()) => touch_source_meta(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    Ok(())
}

/// Run this session on pure built-in defaults, without deleting anything.
///
/// The preset (or scratch file set) stays on disk; only this session's source
/// changes, so another session keeps running its own preset.
pub fn use_defaults_only() -> Result<(), PromptCatalogError> {
    set_session_source(PromptSource::Defaults)
}

/// Delete every override file in the unnamed scratch set.
///
/// Presets are untouched. Kept for callers that really want the legacy
/// working directory emptied; the `/prompts` modal uses [`use_defaults_only`].
pub fn clear_all_overrides() -> Result<(), PromptCatalogError> {
    let dir = prompts_dir().ok_or(PromptCatalogError::NoGrokHome)?;
    for id in PromptId::ALL {
        let path = dir.join(format!("{}.md", id.as_str()));
        match std::fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

// ── Presets ────────────────────────────────────────────────────────────────

const PRESETS_SUBDIR: &str = "prompt-presets";
const ACTIVE_MARKER: &str = ".active";
const STATE_FILE: &str = ".state.json";
const META_FILE: &str = "meta.json";
/// On-disk schema version for `meta.json` / `.state.json`. A newer binary
/// reads older files; an older binary ignores fields it does not know.
const STATE_VERSION: u32 = 1;
/// Cap on remembered session bindings (oldest are pruned first).
const MAX_BINDINGS: usize = 256;
/// Preset name the legacy scratch overrides migrate into.
const MIGRATED_PRESET_NAME: &str = "perso";

/// `$GROK_HOME/prompt-presets`.
pub fn presets_root() -> Option<PathBuf> {
    grok_home().map(|h| h.join(PRESETS_SUBDIR))
}

fn presets_root_required() -> Result<PathBuf, PromptCatalogError> {
    presets_root().ok_or(PromptCatalogError::NoGrokHome)
}

fn preset_dir(name: &str) -> Result<PathBuf, PromptCatalogError> {
    let name = validate_preset_name(name)?;
    Ok(presets_root_required()?.join(name))
}

/// Accept `a-zA-Z0-9._-` only, 1..=64 chars, not `.` / `..`, no leading dot
/// (dot-prefixed entries are catalog control files, not presets).
pub fn validate_preset_name(name: &str) -> Result<String, PromptCatalogError> {
    let name = name.trim();
    if name.is_empty()
        || name.starts_with('.')
        || name.len() > 64
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
    {
        return Err(PromptCatalogError::InvalidPresetName(name.to_string()));
    }
    Ok(name.to_string())
}

/// Suggest a free preset name (`base`, `base-2`, `base-3`, …).
///
/// Probes the directory directly rather than going through [`list_presets`],
/// which would re-enter the migration that calls this.
pub fn suggest_preset_name(base: &str) -> String {
    let base = base.trim();
    let base = if validate_preset_name(base).is_ok() {
        base.to_string()
    } else {
        "preset".to_string()
    };
    if !preset_exists(&base) {
        return base;
    }
    (2..1000)
        .map(|i| format!("{base}-{i}"))
        .find(|candidate| !preset_exists(candidate))
        .unwrap_or(base)
}

/// Name of the preset the current session runs on, if any.
pub fn active_preset_name() -> Result<Option<String>, PromptCatalogError> {
    Ok(session_source().preset_name().map(str::to_string))
}

fn read_active_marker() -> Result<Option<String>, PromptCatalogError> {
    let Some(root) = presets_root() else {
        return Ok(None);
    };
    match std::fs::read_to_string(root.join(ACTIVE_MARKER)) {
        Ok(s) => {
            let t = s.trim();
            if t.is_empty() {
                Ok(None)
            } else {
                Ok(Some(t.to_string()))
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

fn write_active_marker(name: Option<&str>) -> Result<(), PromptCatalogError> {
    let root = presets_root_required()?;
    std::fs::create_dir_all(&root)?;
    let marker = root.join(ACTIVE_MARKER);
    match name {
        Some(n) => {
            let n = validate_preset_name(n)?;
            std::fs::write(marker, format!("{n}\n"))?;
        }
        None => match std::fs::remove_file(marker) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        },
    }
    Ok(())
}

// ── Persisted catalog state ────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SessionBinding {
    source: PromptSource,
    updated: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct CatalogState {
    #[serde(default = "state_version")]
    version: u32,
    /// Whether the legacy scratch overrides were already promoted to a preset.
    #[serde(default)]
    migrated_scratch: bool,
    #[serde(default)]
    bindings: BTreeMap<String, SessionBinding>,
}

fn state_version() -> u32 {
    STATE_VERSION
}

impl Default for CatalogState {
    fn default() -> Self {
        Self {
            version: STATE_VERSION,
            migrated_scratch: false,
            bindings: BTreeMap::new(),
        }
    }
}

fn now_rfc3339() -> String {
    chrono::Utc::now().to_rfc3339()
}

fn load_state() -> Result<CatalogState, PromptCatalogError> {
    let Some(root) = presets_root() else {
        return Ok(CatalogState::default());
    };
    match std::fs::read_to_string(root.join(STATE_FILE)) {
        // A corrupt or future-shaped state file must never wedge prompt
        // resolution: fall back to defaults and let the next write heal it.
        Ok(raw) => Ok(serde_json::from_str(&raw).unwrap_or_default()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(CatalogState::default()),
        Err(e) => Err(e.into()),
    }
}

fn update_state(
    f: impl FnOnce(&mut CatalogState),
) -> Result<(), PromptCatalogError> {
    let root = presets_root_required()?;
    let mut state = load_state()?;
    state.version = STATE_VERSION;
    f(&mut state);
    std::fs::create_dir_all(&root)?;
    let body = serde_json::to_string_pretty(&state).unwrap_or_else(|_| "{}".to_string());
    write_atomic(&root.join(STATE_FILE), &body)
}

/// Write via a sibling temp file + rename so a concurrent reader never sees a
/// half-written control file.
fn write_atomic(path: &Path, body: &str) -> Result<(), PromptCatalogError> {
    let tmp = path.with_extension(format!("tmp.{}", std::process::id()));
    std::fs::write(&tmp, body)?;
    match std::fs::rename(&tmp, path) {
        Ok(()) => Ok(()),
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            Err(e.into())
        }
    }
}

fn prune_bindings(bindings: &mut BTreeMap<String, SessionBinding>) {
    if bindings.len() <= MAX_BINDINGS {
        return;
    }
    let mut by_age: Vec<(String, String)> = bindings
        .iter()
        .map(|(k, v)| (k.clone(), v.updated.clone()))
        .collect();
    by_age.sort_by(|a, b| a.1.cmp(&b.1));
    let excess = bindings.len() - MAX_BINDINGS;
    for (key, _) in by_age.into_iter().take(excess) {
        bindings.remove(&key);
    }
}

// ── Preset metadata ────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PresetMeta {
    #[serde(default = "state_version")]
    version: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    description: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    created: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    updated: Option<String>,
}

impl Default for PresetMeta {
    fn default() -> Self {
        Self {
            version: STATE_VERSION,
            description: None,
            created: None,
            updated: None,
        }
    }
}

fn load_meta(dir: &Path) -> PresetMeta {
    std::fs::read_to_string(dir.join(META_FILE))
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

fn write_meta(dir: &Path, meta: &PresetMeta) -> Result<(), PromptCatalogError> {
    std::fs::create_dir_all(dir)?;
    let body = serde_json::to_string_pretty(meta).unwrap_or_else(|_| "{}".to_string());
    write_atomic(&dir.join(META_FILE), &body)
}

/// Stamp `updated` on the preset backing the current session (best effort).
fn touch_source_meta() {
    let Some(name) = session_source().preset_name().map(str::to_string) else {
        return;
    };
    let Ok(dir) = preset_dir(&name) else {
        return;
    };
    let mut meta = load_meta(&dir);
    meta.version = STATE_VERSION;
    meta.created.get_or_insert_with(now_rfc3339);
    meta.updated = Some(now_rfc3339());
    let _ = write_meta(&dir, &meta);
}

/// Set a preset's free-form description.
pub fn set_preset_description(
    name: &str,
    description: Option<&str>,
) -> Result<(), PromptCatalogError> {
    let dir = preset_dir(name)?;
    if !dir.is_dir() {
        return Err(PromptCatalogError::PresetNotFound(name.to_string()));
    }
    let mut meta = load_meta(&dir);
    meta.version = STATE_VERSION;
    meta.created.get_or_insert_with(now_rfc3339);
    meta.updated = Some(now_rfc3339());
    meta.description = description
        .map(str::trim)
        .filter(|d| !d.is_empty())
        .map(str::to_string);
    write_meta(&dir, &meta)
}

fn count_preset_overrides(dir: &Path) -> usize {
    PromptId::ALL
        .iter()
        .filter(|id| non_empty_file(&dir.join(format!("{}.md", id.as_str()))))
        .count()
}

/// List named presets (sorted), marking the one the session runs on.
///
/// Read-only on purpose: promoting the legacy overrides is [`activate_session`]'s
/// job, so merely listing presets never writes into `$GROK_HOME`.
pub fn list_presets() -> Result<Vec<PromptPresetInfo>, PromptCatalogError> {
    let root = match presets_root() {
        Some(r) => r,
        None => return Ok(Vec::new()),
    };
    let active = session_source().preset_name().map(str::to_string);
    let mut out = Vec::new();
    let entries = match std::fs::read_dir(&root) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if validate_preset_name(name).is_err() {
            continue;
        }
        let meta = load_meta(&path);
        out.push(PromptPresetInfo {
            name: name.to_string(),
            override_count: count_preset_overrides(&path),
            is_active: active.as_deref() == Some(name),
            description: meta.description,
            updated: meta.updated,
        });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

/// Whether a preset with this name exists.
pub fn preset_exists(name: &str) -> bool {
    preset_dir(name).map(|d| d.is_dir()).unwrap_or(false)
}

/// Copy the catalog `.md` files from `src` into `dest`, replacing `dest`'s set.
fn copy_catalog_files(src: &Path, dest: &Path) -> Result<(), PromptCatalogError> {
    std::fs::create_dir_all(dest)?;
    for id in PromptId::ALL {
        let file = format!("{}.md", id.as_str());
        let _ = std::fs::remove_file(dest.join(&file));
        let from = src.join(&file);
        if let Ok(body) = std::fs::read_to_string(&from)
            && !body.trim().is_empty()
        {
            std::fs::write(dest.join(&file), body)?;
        }
    }
    Ok(())
}

/// Snapshot the session's current overrides into a named preset and bind the
/// session to it.
///
/// `mode` decides what an existing name means: [`PresetWriteMode::Create`]
/// refuses it, [`PresetWriteMode::Overwrite`] replaces its catalog files.
/// Built-in defaults are never duplicated — only real override files are copied.
pub fn save_preset(name: &str, mode: PresetWriteMode) -> Result<(), PromptCatalogError> {
    let name = validate_preset_name(name)?;
    let dest = preset_dir(&name)?;
    let existed = dest.is_dir();
    if existed && mode == PresetWriteMode::Create {
        return Err(PromptCatalogError::PresetExists(name));
    }
    match override_source_dir() {
        Some(src) if src != dest => copy_catalog_files(&src, &dest)?,
        // Pure defaults, or saving a preset onto itself: keep the files as-is.
        _ => std::fs::create_dir_all(&dest)?,
    }
    let mut meta = load_meta(&dest);
    meta.version = STATE_VERSION;
    meta.created.get_or_insert_with(now_rfc3339);
    meta.updated = Some(now_rfc3339());
    write_meta(&dest, &meta)?;
    set_session_source(PromptSource::Preset(name))
}

/// Copy `src` to a new preset `dest` without touching the session's source.
pub fn duplicate_preset(src: &str, dest: &str) -> Result<(), PromptCatalogError> {
    let src_name = validate_preset_name(src)?;
    let dest_name = validate_preset_name(dest)?;
    let src_dir = preset_dir(&src_name)?;
    if !src_dir.is_dir() {
        return Err(PromptCatalogError::PresetNotFound(src_name));
    }
    let dest_dir = preset_dir(&dest_name)?;
    if dest_dir.is_dir() {
        return Err(PromptCatalogError::PresetExists(dest_name));
    }
    copy_catalog_files(&src_dir, &dest_dir)?;
    let mut meta = load_meta(&src_dir);
    meta.version = STATE_VERSION;
    meta.created = Some(now_rfc3339());
    meta.updated = Some(now_rfc3339());
    write_meta(&dest_dir, &meta)
}

/// Rename a preset, carrying the `.active` marker and session bindings over.
pub fn rename_preset(old: &str, new: &str) -> Result<(), PromptCatalogError> {
    let old_name = validate_preset_name(old)?;
    let new_name = validate_preset_name(new)?;
    if old_name == new_name {
        return Ok(());
    }
    let old_dir = preset_dir(&old_name)?;
    if !old_dir.is_dir() {
        return Err(PromptCatalogError::PresetNotFound(old_name));
    }
    let new_dir = preset_dir(&new_name)?;
    if new_dir.exists() {
        return Err(PromptCatalogError::PresetExists(new_name));
    }
    std::fs::rename(&old_dir, &new_dir)?;
    let mut meta = load_meta(&new_dir);
    meta.version = STATE_VERSION;
    meta.updated = Some(now_rfc3339());
    write_meta(&new_dir, &meta)?;
    if read_active_marker()?.as_deref() == Some(old_name.as_str()) {
        write_active_marker(Some(&new_name))?;
    }
    update_state(|state| {
        for binding in state.bindings.values_mut() {
            if binding.source == PromptSource::Preset(old_name.clone()) {
                binding.source = PromptSource::Preset(new_name.clone());
                binding.updated = now_rfc3339();
            }
        }
    })?;
    session_state_write(|s| {
        if s.source == Some(PromptSource::Preset(old_name.clone())) {
            s.source = Some(PromptSource::Preset(new_name.clone()));
        }
    });
    Ok(())
}

/// Bind the session to `name` (the preset must exist).
pub fn apply_preset(name: &str) -> Result<(), PromptCatalogError> {
    let name = validate_preset_name(name)?;
    if !preset_dir(&name)?.is_dir() {
        return Err(PromptCatalogError::PresetNotFound(name));
    }
    set_session_source(PromptSource::Preset(name))
}

/// Delete a named preset and drop every binding that pointed at it.
pub fn delete_preset(name: &str) -> Result<(), PromptCatalogError> {
    let name = validate_preset_name(name)?;
    let dir = preset_dir(&name)?;
    if !dir.exists() {
        return Err(PromptCatalogError::PresetNotFound(name));
    }
    std::fs::remove_dir_all(&dir)?;
    if read_active_marker()?.as_deref() == Some(name.as_str()) {
        write_active_marker(None)?;
    }
    let gone = PromptSource::Preset(name.clone());
    update_state(|state| {
        state.bindings.retain(|_, b| b.source != gone);
    })?;
    session_state_write(|s| {
        if s.source.as_ref() == Some(&gone) {
            s.source = None;
        }
    });
    Ok(())
}

/// Detach from any preset without deleting it: fall back to the unnamed
/// scratch overrides.
pub fn clear_active_preset() -> Result<(), PromptCatalogError> {
    set_session_source(PromptSource::Scratch)
}

// ── Migration ──────────────────────────────────────────────────────────────

/// Promote pre-existing scratch overrides into a real preset, once.
///
/// Non-destructive: the scratch files stay where they are, so an older binary
/// still finds them. Runs at most once per `$GROK_HOME` (recorded in the state
/// file) and is a no-op when there is nothing to promote.
fn ensure_migrated() -> Result<(), PromptCatalogError> {
    let state = load_state()?;
    if state.migrated_scratch {
        return Ok(());
    }
    if !scratch_has_overrides() {
        // Nothing to promote; record it so we stop probing on every call.
        return update_state(|s| s.migrated_scratch = true);
    }
    let src = prompts_dir().ok_or(PromptCatalogError::NoGrokHome)?;
    let name = suggest_preset_name(MIGRATED_PRESET_NAME);
    let dest = preset_dir(&name)?;
    copy_catalog_files(&src, &dest)?;
    let meta = PresetMeta {
        version: STATE_VERSION,
        description: Some("Migrated from the unnamed prompt overrides.".to_string()),
        created: Some(now_rfc3339()),
        updated: Some(now_rfc3339()),
    };
    write_meta(&dest, &meta)?;
    if read_active_marker()?.is_none() {
        write_active_marker(Some(&name))?;
    }
    update_state(|s| s.migrated_scratch = true)
}

/// Test helpers: operate under an explicit home directory.
#[cfg(test)]
pub mod test_support {
    use super::*;

    pub fn save_override_in(home: &Path, id: PromptId, body: &str) -> PathBuf {
        let dir = home.join("prompts");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("{}.md", id.as_str()));
        std::fs::write(&path, body).unwrap();
        path
    }

    pub fn load_override_in(home: &Path, id: PromptId) -> Option<String> {
        let path = override_path_in(home, id);
        load_override_at(&path).unwrap()
    }

    pub fn resolve_body_in(home: &Path, id: PromptId) -> String {
        load_override_in(home, id).unwrap_or_else(|| default_body(id))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;
    use tempfile::TempDir;

    fn with_temp_grok_home<R>(f: impl FnOnce(&Path) -> R) -> R {
        let tmp = TempDir::new().unwrap();
        // Isolate from process-wide `grok_home()` OnceLock via test hook.
        *TEST_PROMPTS_HOME.lock().unwrap() = Some(tmp.path().to_path_buf());
        deactivate_session();
        let result = f(tmp.path());
        deactivate_session();
        *TEST_PROMPTS_HOME.lock().unwrap() = None;
        result
    }

    /// Put the session on the scratch source, the pre-preset default.
    fn use_scratch() {
        session_state_write(|s| s.source = Some(PromptSource::Scratch));
    }

    #[test]
    fn list_prompts_covers_all_ids() {
        let list = list_prompts();
        assert_eq!(list.len(), PromptId::ALL.len());
        assert_eq!(list[0].id, PromptId::BaseSystem);
        assert!(list.iter().any(|d| d.id == PromptId::SubagentExplore));
    }

    #[test]
    fn parse_round_trips() {
        for id in PromptId::ALL {
            assert_eq!(PromptId::parse(id.as_str()), Some(*id));
        }
        assert_eq!(PromptId::parse("nope"), None);
    }

    #[test]
    fn default_bodies_non_empty() {
        for id in PromptId::ALL {
            let body = default_body(*id);
            assert!(
                !body.trim().is_empty(),
                "default body empty for {}",
                id.as_str()
            );
        }
    }

    #[test]
    #[serial]
    fn save_load_reset_override() {
        with_temp_grok_home(|home| {
            use_scratch();
            let id = PromptId::CompactSystem;
            assert!(!has_override(id));
            let ep = effective_prompt(id).unwrap();
            assert!(!ep.is_override);
            assert_eq!(ep.body, COMPACT_SYSTEM_PROMPT);

            let custom = "You are a custom compact agent.";
            let path = save_override(id, custom).unwrap();
            assert_eq!(
                path.file_name().and_then(|n| n.to_str()),
                Some("compact-system.md")
            );
            assert!(path.is_file(), "override written under {}", path.display());
            let _ = home; // GROK_HOME points here; path may be re-resolved
            assert!(has_override(id));

            let ep = effective_prompt(id).unwrap();
            assert!(ep.is_override);
            assert_eq!(ep.body, custom);
            assert_eq!(resolve_body(id), custom);

            reset_override(id).unwrap();
            assert!(!has_override(id));
            assert_eq!(resolve_body(id), COMPACT_SYSTEM_PROMPT);
        });
    }

    #[test]
    #[serial]
    fn materialize_for_edit_creates_file_once() {
        with_temp_grok_home(|_| {
            use_scratch();
            let id = PromptId::BaseSystem;
            let path1 = materialize_for_edit(id).unwrap();
            assert!(path1.is_file());
            let body1 = std::fs::read_to_string(&path1).unwrap();
            assert_eq!(body1, default_body(id));

            // Second call keeps existing content (user may have edited).
            std::fs::write(&path1, "edited by user").unwrap();
            let path2 = materialize_for_edit(id).unwrap();
            assert_eq!(path1, path2);
            assert_eq!(std::fs::read_to_string(&path2).unwrap(), "edited by user");
        });
    }

    #[test]
    #[serial]
    fn empty_override_falls_through_to_default() {
        with_temp_grok_home(|_| {
            use_scratch();
            let id = PromptId::CompactSystem;
            // Force-write empty via direct IO (save_override clears defaults only).
            let path = override_path(id).unwrap();
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, "   \n  ").unwrap();
            let ep = effective_prompt(id).unwrap();
            assert!(!ep.is_override);
            assert_eq!(ep.body, COMPACT_SYSTEM_PROMPT);
        });
    }

    #[test]
    #[serial]
    fn save_equal_to_default_clears_override() {
        with_temp_grok_home(|_| {
            use_scratch();
            let id = PromptId::CompactSystem;
            save_override(id, "custom").unwrap();
            assert!(has_override(id));
            save_override(id, COMPACT_SYSTEM_PROMPT).unwrap();
            assert!(!has_override(id));
            assert_eq!(resolve_body(id), COMPACT_SYSTEM_PROMPT);
        });
    }

    #[test]
    #[serial]
    fn preset_save_apply_delete() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            let id = PromptId::CompactSystem;
            save_override(id, "preset-body-a").unwrap();
            save_preset("alpha", PresetWriteMode::Create).unwrap();
            assert_eq!(active_preset_name().unwrap().as_deref(), Some("alpha"));
            assert_eq!(resolve_body(id), "preset-body-a");

            use_defaults_only().unwrap();
            assert!(!has_override(id));
            assert!(active_preset_name().unwrap().is_none());
            assert_eq!(resolve_body(id), COMPACT_SYSTEM_PROMPT);

            apply_preset("alpha").unwrap();
            assert_eq!(resolve_body(id), "preset-body-a");
            assert_eq!(active_preset_name().unwrap().as_deref(), Some("alpha"));

            let list = list_presets().unwrap();
            assert_eq!(list.len(), 1);
            assert!(list[0].is_active);
            assert_eq!(list[0].override_count, 1);
            assert!(list[0].updated.is_some(), "meta.json stamps updated");

            delete_preset("alpha").unwrap();
            assert!(list_presets().unwrap().is_empty());
            // The session no longer resolves through the deleted preset, and
            // nothing points at it any more.
            assert!(active_preset_name().unwrap().is_none());
            assert!(!preset_exists("alpha"));
        });
    }

    #[test]
    fn reject_bad_preset_names() {
        assert!(validate_preset_name("").is_err());
        assert!(validate_preset_name("..").is_err());
        assert!(validate_preset_name(".active").is_err());
        assert!(validate_preset_name("has space").is_err());
        assert!(validate_preset_name("ok-name_1").is_ok());
    }

    #[test]
    #[serial]
    fn create_mode_refuses_to_clobber_an_existing_preset() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            save_override(PromptId::CompactSystem, "first").unwrap();
            save_preset("dup", PresetWriteMode::Create).unwrap();

            use_defaults_only().unwrap();
            use_scratch();
            save_override(PromptId::CompactSystem, "second").unwrap();

            let err = save_preset("dup", PresetWriteMode::Create).unwrap_err();
            assert!(
                matches!(err, PromptCatalogError::PresetExists(ref n) if n == "dup"),
                "expected PresetExists, got {err:?}"
            );
            // The original preset is intact.
            apply_preset("dup").unwrap();
            assert_eq!(resolve_body(PromptId::CompactSystem), "first");

            // Overwrite is the explicit path and does replace it.
            use_scratch();
            save_preset("dup", PresetWriteMode::Overwrite).unwrap();
            assert_eq!(resolve_body(PromptId::CompactSystem), "second");
        });
    }

    #[test]
    #[serial]
    fn sessions_bind_to_their_own_preset() {
        with_temp_grok_home(|_| {
            // Session 1 builds and runs on `alpha`.
            activate_session("s-1").unwrap();
            save_override(PromptId::CompactSystem, "body-alpha").unwrap();
            save_preset("alpha", PresetWriteMode::Create).unwrap();

            // Session 2 builds and runs on `beta`.
            activate_session("s-2").unwrap();
            use_defaults_only().unwrap();
            use_scratch();
            save_override(PromptId::CompactSystem, "body-beta").unwrap();
            save_preset("beta", PresetWriteMode::Create).unwrap();
            assert_eq!(resolve_body(PromptId::CompactSystem), "body-beta");

            // Coming back to session 1 restores its own preset and body.
            let source = activate_session("s-1").unwrap();
            assert_eq!(source, PromptSource::Preset("alpha".into()));
            assert_eq!(resolve_body(PromptId::CompactSystem), "body-alpha");

            // And session 2 still resolves its own.
            assert_eq!(
                activate_session("s-2").unwrap(),
                PromptSource::Preset("beta".into())
            );
            assert_eq!(resolve_body(PromptId::CompactSystem), "body-beta");
        });
    }

    #[test]
    #[serial]
    fn editing_a_preset_leaves_the_others_untouched() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            save_override(PromptId::CompactSystem, "shared").unwrap();
            save_preset("one", PresetWriteMode::Create).unwrap();
            save_preset("two", PresetWriteMode::Create).unwrap();

            apply_preset("one").unwrap();
            save_override(PromptId::CompactSystem, "only-in-one").unwrap();

            apply_preset("two").unwrap();
            assert_eq!(
                resolve_body(PromptId::CompactSystem),
                "shared",
                "editing `one` must not write through to `two`"
            );
        });
    }

    #[test]
    #[serial]
    fn new_session_starts_on_the_last_used_preset() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            save_override(PromptId::CompactSystem, "inherited").unwrap();
            save_preset("shared", PresetWriteMode::Create).unwrap();

            // A session with no binding inherits the `.active` default.
            let source = activate_session("fresh").unwrap();
            assert_eq!(source, PromptSource::Preset("shared".into()));
            assert_eq!(resolve_body(PromptId::CompactSystem), "inherited");
        });
    }

    #[test]
    #[serial]
    fn defaults_only_is_not_destructive() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            save_override(PromptId::CompactSystem, "kept").unwrap();
            save_preset("keeper", PresetWriteMode::Create).unwrap();

            use_defaults_only().unwrap();
            assert_eq!(resolve_body(PromptId::CompactSystem), COMPACT_SYSTEM_PROMPT);
            assert!(save_override(PromptId::CompactSystem, "x").is_err());

            apply_preset("keeper").unwrap();
            assert_eq!(resolve_body(PromptId::CompactSystem), "kept");
        });
    }

    #[test]
    #[serial]
    fn rename_and_duplicate_preserve_bodies_and_binding() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            save_override(PromptId::CompactSystem, "body").unwrap();
            save_preset("orig", PresetWriteMode::Create).unwrap();

            duplicate_preset("orig", "copy").unwrap();
            assert!(preset_exists("copy"));
            // Duplicating does not move the session.
            assert_eq!(active_preset_name().unwrap().as_deref(), Some("orig"));

            let err = duplicate_preset("orig", "copy").unwrap_err();
            assert!(matches!(err, PromptCatalogError::PresetExists(_)));

            rename_preset("orig", "renamed").unwrap();
            assert!(!preset_exists("orig"));
            assert_eq!(active_preset_name().unwrap().as_deref(), Some("renamed"));
            assert_eq!(resolve_body(PromptId::CompactSystem), "body");

            // The persisted binding followed the rename.
            let source = activate_session("s-1").unwrap();
            assert_eq!(source, PromptSource::Preset("renamed".into()));
        });
    }

    #[test]
    #[serial]
    fn scratch_overrides_migrate_into_a_preset_once() {
        with_temp_grok_home(|home| {
            // Pre-existing layout written by an older binary.
            test_support::save_override_in(home, PromptId::CompactSystem, "legacy-body");

            let source = activate_session("s-1").unwrap();
            assert_eq!(source, PromptSource::Preset(MIGRATED_PRESET_NAME.into()));
            assert_eq!(resolve_body(PromptId::CompactSystem), "legacy-body");

            // Non-destructive: the legacy file is still readable by an old binary.
            assert_eq!(
                test_support::load_override_in(home, PromptId::CompactSystem).as_deref(),
                Some("legacy-body")
            );

            // Idempotent: a second activation does not create `perso-2`.
            activate_session("s-2").unwrap();
            let names: Vec<String> = list_presets()
                .unwrap()
                .into_iter()
                .map(|p| p.name)
                .collect();
            assert_eq!(names, vec![MIGRATED_PRESET_NAME.to_string()]);
        });
    }

    #[test]
    #[serial]
    fn presets_survive_a_binary_update_that_changes_defaults() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            // Only the prompts the user actually customized are stored…
            save_override(PromptId::CompactSystem, "user-body").unwrap();
            save_preset("kept", PresetWriteMode::Create).unwrap();

            let dir = preset_dir("kept").unwrap();
            assert!(dir.join("compact-system.md").is_file());
            assert!(
                !dir.join("base-system.md").exists(),
                "untouched prompts must not be snapshotted, so updates keep delivering them"
            );

            // …so an updated binary shipping a new default still wins for them.
            assert_eq!(resolve_body(PromptId::BaseSystem), default_body(PromptId::BaseSystem));
            assert_eq!(resolve_body(PromptId::CompactSystem), "user-body");
        });
    }

    #[test]
    #[serial]
    fn suggested_names_avoid_collisions() {
        with_temp_grok_home(|_| {
            activate_session("s-1").unwrap();
            assert_eq!(suggest_preset_name("perso"), "perso");
            save_override(PromptId::CompactSystem, "b").unwrap();
            save_preset("perso", PresetWriteMode::Create).unwrap();
            assert_eq!(suggest_preset_name("perso"), "perso-2");
        });
    }

    #[test]
    #[serial]
    fn corrupt_state_file_does_not_wedge_resolution() {
        with_temp_grok_home(|_| {
            let root = presets_root().unwrap();
            std::fs::create_dir_all(&root).unwrap();
            std::fs::write(root.join(STATE_FILE), "{ not json").unwrap();
            // Falls back to a fresh state instead of erroring out.
            let source = activate_session("s-1").unwrap();
            assert_eq!(source, PromptSource::Scratch);
            assert_eq!(resolve_body(PromptId::CompactSystem), COMPACT_SYSTEM_PROMPT);
        });
    }
}
