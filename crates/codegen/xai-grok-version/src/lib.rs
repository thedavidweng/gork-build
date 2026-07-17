//! Installed Gork Build CLI version, lockstepped with shipping binaries.
//!
//! **Gork Build** is a community privacy fork of xAI Grok Build (same role as
//! VSCodium vs VS Code): same codebase, no product telemetry, no research
//! trace uploads, no xAI branding. Model inference still uses the user's
//! credentials against the Grok API — that is the only network path required
//! for the agent to work.

use semver::Version;

/// Compile-time privacy fork switch. Always `true` in Gork Build.
/// Upstream Grok Build would set this to `false`.
pub const PRIVACY_BUILD: bool = true;

/// User-facing product name for this fork.
pub const PRODUCT_NAME: &str = "Gork Build";

/// Preferred CLI binary / command name for this fork.
pub const PRODUCT_CLI: &str = "gork";

/// One-line positioning (README, `--version` help, welcome copy).
pub const PRODUCT_TAGLINE: &str =
    "VSCodium-style community build of Grok Build — vendor telemetry removed";

/// `true` when research telemetry, Mixpanel, GCS session traces, and similar
/// non-inference uploads must stay off. Always true while [`PRIVACY_BUILD`].
#[inline]
pub fn research_data_collection_forbidden() -> bool {
    PRIVACY_BUILD
}

/// `true` when coding-data retention is locked to **opt-out** (no UI/API path
/// may opt the account into sharing/training retention).
#[inline]
pub fn coding_data_retention_locked_opt_out() -> bool {
    PRIVACY_BUILD
}

pub const TEST_VERSION_ENV: &str = "GROK_TEST_VERSION";

pub const VERSION: &str = match option_env!("GROK_VERSION") {
    Some(v) => v,
    None => env!("CARGO_PKG_VERSION"),
};

/// [`TEST_VERSION_ENV`] override first, then [`VERSION`]. Trimmed so
/// non-semver-aware callers can pass the result straight into parsing.
pub fn installed() -> String {
    std::env::var(TEST_VERSION_ENV)
        .map(|v| v.trim().to_string())
        .unwrap_or_else(|_| VERSION.to_string())
}

pub fn installed_semver() -> Result<Version, semver::Error> {
    Version::parse(&installed())
}

/// Format the compiled version with a channel label for user-facing display.
///
/// `channel_label` is a pre-formatted suffix such as `" [alpha]"`, `" [stable]"`,
/// or `""` (empty when no cached pointer is available). Obtain it from
/// `xai_grok_update::channel_label()`.
///
/// Example: `"0.2.5 [stable]"` or `"0.2.5 [alpha]"`.
pub fn display_version(channel_label: &str) -> String {
    format!("{}{}", VERSION, channel_label)
}

/// Format a version-with-commit string with a channel label.
///
/// Same semantics as [`display_version`] but for the full
/// `"0.2.5 (abc1234)"` string.
pub fn display_version_with_commit(version_with_commit: &str, channel_label: &str) -> String {
    format!("{}{}", version_with_commit, channel_label)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Gork Build privacy policy constants — compile-time hard-offs that
    /// resolvers and updaters consult. These must stay true for this fork.
    #[test]
    fn privacy_build_locks_research_and_retention_policy() {
        assert!(
            PRIVACY_BUILD,
            "Gork Build must ship with PRIVACY_BUILD=true"
        );
        assert!(
            research_data_collection_forbidden(),
            "research_data_collection_forbidden must follow PRIVACY_BUILD"
        );
        assert!(
            coding_data_retention_locked_opt_out(),
            "coding data retention must be locked opt-out under PRIVACY_BUILD"
        );
        assert_eq!(PRODUCT_CLI, "gork");
        assert_eq!(PRODUCT_NAME, "Gork Build");
    }

    /// Display formatting invariant matrix — verifies label appending
    /// works correctly across all label states (alpha, stable, empty).
    #[test]
    fn test_display_version_formatting_matrix() {
        let cases: &[(&str, &str, &str)] = &[
            // (version_with_commit,    label,        expected_suffix)
            ("0.2.5 (abc1234)", " [alpha]", "0.2.5 (abc1234) [alpha]"),
            ("0.2.5 (abc1234)", " [stable]", "0.2.5 (abc1234) [stable]"),
            ("0.2.5 (abc1234)", "", "0.2.5 (abc1234)"),
            (
                "0.1.220-alpha.2 (def0)",
                " [alpha]",
                "0.1.220-alpha.2 (def0) [alpha]",
            ),
        ];
        for (vwc, label, expected) in cases {
            assert_eq!(
                display_version_with_commit(vwc, label),
                *expected,
                "display_version_with_commit({:?}, {:?})",
                vwc,
                label,
            );
        }
        // display_version uses compiled VERSION — just verify the label appends
        assert_eq!(display_version(""), VERSION);
        assert!(display_version(" [stable]").ends_with("[stable]"));
    }
}
