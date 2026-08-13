//! Agent builder, definition parsing, and system prompt assembly.
//!
//! An `Agent` bundles tools, system prompt, system-reminder policy, compaction policy, and model configuration into one object any host can consume.

pub mod agent;
pub mod builder;
pub mod compaction;
pub mod config;
pub mod discovery;
pub mod error;
pub mod plugins;
pub mod prompt;
pub mod repo;
pub mod system_reminder;
pub mod timing;

pub use agent::Agent;
pub use builder::AgentBuilder;
pub use compaction::CompactionPolicy;
pub use config::AgentDefinition;
pub use config::preset_names;
pub use config::toolset_for_preset;
pub use config::workspace_grok_build_toolset;
pub use error::AgentBuildError;
pub use prompt::catalog::{
    EffectivePrompt, PresetWriteMode, PromptCatalogError, PromptCategory, PromptDefinition,
    PromptId, PromptPresetInfo, PromptSource, activate_session, active_preset_name, apply_preset,
    clear_active_preset, clear_all_overrides, current_session_id, deactivate_session, default_body,
    delete_preset, duplicate_preset, effective_prompt, has_override, list_presets, list_prompts,
    effective_prompt_for, materialize_for_edit, override_path, preset_exists, presets_root,
    prompts_dir, rename_preset, reset_override, resolve_body, resolve_body_for, save_override,
    save_preset, session_source, set_preset_description, set_session_source, source_for_session,
    suggest_preset_name, use_defaults_only, validate_preset_name,
};
pub use prompt::context::{DEFAULT_SYSTEM_PROMPT_LABEL, PromptContext};
pub use prompt::template::{
    apply_patch_template_source, base_template_source, subagent_template_source,
    COMPACT_SYSTEM_PROMPT,
};
pub use system_reminder::ReminderPolicy;
