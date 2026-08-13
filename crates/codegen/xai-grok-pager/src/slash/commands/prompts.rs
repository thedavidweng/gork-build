//! `/prompts` — open the LLM prompt templates browser, or drive presets by name.

use xai_grok_agent::{PresetWriteMode, PromptSource};

use crate::app::actions::Action;
use crate::slash::command::{CommandExecCtx, CommandResult, SlashCommand};

/// Open the prompts catalog modal.
pub struct PromptsCommand;

impl SlashCommand for PromptsCommand {
    fn name(&self) -> &str {
        "prompts"
    }

    fn aliases(&self) -> &[&str] {
        &["prompt", "system-prompts"]
    }

    fn description(&self) -> &str {
        "Browse and edit system / subagent prompt templates"
    }

    fn usage(&self) -> &str {
        "/prompts [list | use <name> | save <name> | update <name> | defaults]"
    }

    fn run(&self, ctx: &mut CommandExecCtx, args: &str) -> CommandResult {
        let args = args.trim();
        if args.is_empty() {
            return CommandResult::Action(Action::OpenPromptsModal);
        }

        // Presets are per session: bind the catalog to the caller's session so
        // a switch lands here and not on whichever session ran last.
        if let Some(session_id) = ctx.session_id
            && let Err(e) = xai_grok_agent::activate_session(session_id.0.as_ref())
        {
            return CommandResult::Error(format!("Could not read prompt presets: {e}"));
        }

        let (verb, rest) = args
            .split_once(char::is_whitespace)
            .unwrap_or((args, ""));
        let name = rest.trim();

        match verb {
            "list" | "ls" => list_presets_message(),
            "use" | "apply" => {
                if name.is_empty() {
                    return CommandResult::Error("Usage: /prompts use <name>".into());
                }
                match xai_grok_agent::apply_preset(name) {
                    Ok(()) => CommandResult::Message(format!(
                        "This session now uses prompt preset `{name}`."
                    )),
                    Err(e) => CommandResult::Error(e.to_string()),
                }
            }
            "save" | "new" => {
                if name.is_empty() {
                    return CommandResult::Error("Usage: /prompts save <name>".into());
                }
                match xai_grok_agent::save_preset(name, PresetWriteMode::Create) {
                    Ok(()) => CommandResult::Message(format!(
                        "Saved this session's prompts as preset `{name}`."
                    )),
                    Err(xai_grok_agent::PromptCatalogError::PresetExists(_)) => {
                        CommandResult::Error(format!(
                            "Preset `{name}` already exists. Use `/prompts update {name}` to replace it."
                        ))
                    }
                    Err(e) => CommandResult::Error(e.to_string()),
                }
            }
            "update" => {
                if name.is_empty() {
                    return CommandResult::Error("Usage: /prompts update <name>".into());
                }
                if !xai_grok_agent::preset_exists(name) {
                    return CommandResult::Error(format!(
                        "No preset named `{name}`. Use `/prompts save {name}` to create it."
                    ));
                }
                match xai_grok_agent::save_preset(name, PresetWriteMode::Overwrite) {
                    Ok(()) => CommandResult::Message(format!("Preset `{name}` updated.")),
                    Err(e) => CommandResult::Error(e.to_string()),
                }
            }
            "defaults" => match xai_grok_agent::use_defaults_only() {
                Ok(()) => CommandResult::Message(
                    "This session now runs on the built-in prompts. Presets are untouched.".into(),
                ),
                Err(e) => CommandResult::Error(e.to_string()),
            },
            other => CommandResult::Error(format!(
                "Unknown /prompts subcommand `{other}`. Try: list, use, save, update, defaults."
            )),
        }
    }
}

fn list_presets_message() -> CommandResult {
    let presets = match xai_grok_agent::list_presets() {
        Ok(p) => p,
        Err(e) => return CommandResult::Error(e.to_string()),
    };
    let source = xai_grok_agent::session_source();
    if presets.is_empty() {
        return CommandResult::Message(format!(
            "No prompt presets yet (this session reads from: {}).\n\
             Save one with `/prompts save <name>`.",
            source.label()
        ));
    }
    let mut out = format!("Prompt presets (this session reads from: {}):", source.label());
    for p in &presets {
        let marker = if p.is_active { "*" } else { " " };
        out.push_str(&format!(
            "\n{marker} {} — {} override(s)",
            p.name, p.override_count
        ));
        if let Some(ref description) = p.description {
            out.push_str(&format!(" — {description}"));
        }
    }
    if matches!(source, PromptSource::Defaults) {
        out.push_str("\n\nNo preset is applied here; prompts come from the binary.");
    }
    CommandResult::Message(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::acp::model_state::ModelState;
    use crate::app::bundle::BundleState;
    use crate::settings::PagerLocalSnapshot;

    fn exec_ctx<'a>(models: &'a ModelState, bundle: &'a BundleState) -> CommandExecCtx<'a> {
        CommandExecCtx {
            models,
            session_id: None,
            bundle_state: bundle,
            screen_mode: crate::app::ScreenMode::Inline,
            billing_surface_visible: true,
            usage_command_visible: true,
            pager_state: PagerLocalSnapshot::default(),
        }
    }

    #[test]
    fn opens_prompts_modal() {
        let models = ModelState::default();
        let bundle = BundleState::default();
        let result = PromptsCommand.run(&mut exec_ctx(&models, &bundle), "");
        assert!(matches!(
            result,
            CommandResult::Action(Action::OpenPromptsModal)
        ));
    }

    #[test]
    fn rejects_an_unknown_subcommand() {
        let models = ModelState::default();
        let bundle = BundleState::default();
        let result = PromptsCommand.run(&mut exec_ctx(&models, &bundle), "frobnicate");
        assert!(matches!(result, CommandResult::Error(_)));
    }

    #[test]
    fn use_without_a_name_explains_the_usage() {
        let models = ModelState::default();
        let bundle = BundleState::default();
        let result = PromptsCommand.run(&mut exec_ctx(&models, &bundle), "use");
        match result {
            CommandResult::Error(msg) => assert!(msg.contains("/prompts use <name>")),
            other => panic!("expected a usage error, got {other:?}"),
        }
    }
}
