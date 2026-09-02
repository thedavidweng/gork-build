//! `/agent` — open a new session on a named agent definition.
//!
//! The agent a session runs on is decided at session creation: the pager
//! stamps `_meta.agentProfile` and the shell gives it priority over
//! `[agent] name` and `GROK_AGENT`. That field was only ever filled from
//! `--agent` at launch, so choosing an agent meant restarting the process.
//! This drives the same field from inside a running session.

use crate::app::actions::Action;
use crate::slash::command::{CommandExecCtx, CommandResult, SlashCommand};

/// Open a new session on a named agent definition.
pub struct AgentCommand;

impl SlashCommand for AgentCommand {
    fn name(&self) -> &str {
        "agent"
    }

    fn aliases(&self) -> &[&str] {
        &["use-agent"]
    }

    fn description(&self) -> &str {
        "Open a new session on a named agent definition"
    }

    fn usage(&self) -> &str {
        "/agent <name>"
    }

    fn run(&self, _ctx: &mut CommandExecCtx, args: &str) -> CommandResult {
        let name = args.trim();
        if name.is_empty() {
            return list_agents_message();
        }
        CommandResult::Action(Action::OpenSessionWithAgent(name.to_string()))
    }
}

/// Names to pass to `/agent`, discovered the same way a session discovers them.
///
/// Answering with the usage line alone left the one question the command
/// raises — *which names are valid here?* — unanswered.
fn list_agents_message() -> CommandResult {
    let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
    let found = xai_grok_agent::discovery::discover(&cwd);
    let mut out = String::from("Usage: /agent <name> — opens a new session running that agent.");
    if found.is_empty() {
        out.push_str(
            "\n\nNo agent definition discovered from this directory. \
             Create one with /agents, or drop a `.md` file in `.grok/agents/` \
             or `~/.grok/agents/`.",
        );
    } else {
        out.push_str("\n\nDiscovered here:");
        for def in &found {
            match first_words(&def.description) {
                Some(summary) => out.push_str(&format!("\n  {} — {summary}", def.name)),
                None => out.push_str(&format!("\n  {}", def.name)),
            }
        }
    }
    out.push_str(
        "\n\nBrowse and edit definitions with /agents. \
         `grok --agent <name>` (or GROK_AGENT=<name>) does the same at launch.",
    );
    CommandResult::Message(out)
}

/// One short line from an agent's description.
///
/// Agent descriptions are written for the model and routinely run several
/// hundred characters; printed whole they bury the names the list exists to
/// show. `None` when there is nothing worth printing.
fn first_words(description: &str) -> Option<String> {
    const WIDTH: usize = 72;
    let line = description.lines().next().unwrap_or("").trim();
    if line.is_empty() {
        return None;
    }
    if line.chars().count() <= WIDTH {
        return Some(line.to_string());
    }
    let cut: String = line.chars().take(WIDTH).collect();
    let cut = cut.rsplit_once(' ').map_or(cut.as_str(), |(head, _)| head);
    Some(format!("{}…", cut.trim_end_matches([',', ';', ':', '.', ' '])))
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
    fn a_name_opens_a_session_on_that_agent() {
        let models = ModelState::default();
        let bundle = BundleState::default();
        let result = AgentCommand.run(&mut exec_ctx(&models, &bundle), "  reviewer  ");
        match result {
            CommandResult::Action(Action::OpenSessionWithAgent(name)) => {
                assert_eq!(name, "reviewer", "the name must be trimmed")
            }
            other => panic!("expected OpenSessionWithAgent, got {other:?}"),
        }
    }

    #[test]
    fn no_name_lists_the_agents_and_the_usage() {
        let models = ModelState::default();
        let bundle = BundleState::default();
        // Which names exist depends on the machine, so only the invariants
        // that hold anywhere are asserted.
        match AgentCommand.run(&mut exec_ctx(&models, &bundle), "") {
            CommandResult::Message(msg) => {
                assert!(msg.contains("/agent <name>"));
                assert!(msg.contains("--agent"), "point at the launch-time equivalent");
            }
            other => panic!("expected the agent list, got {other:?}"),
        }
    }

    #[test]
    fn a_long_description_is_cut_to_one_readable_line() {
        let long = "Spécialiste bridge et remote, ".repeat(20);
        let cut = first_words(&long).expect("a non-empty description yields a summary");
        assert!(cut.chars().count() <= 73, "got {} chars", cut.chars().count());
        assert!(cut.ends_with('…'));
        assert!(first_words("   ").is_none());
        assert_eq!(first_words("short one").as_deref(), Some("short one"));
    }
}
