//! Privacy hard-off regression tests for telemetry / trace-upload resolvers.
//!
//! Integration tests (not `#[cfg(test)]` unit tests) so they compile against the
//! normal shell library — full `xai-grok-shell --lib` unit tests currently fail
//! upstream due to missing test helpers.
//!
//! These drive the **shipped** `Config::resolve_telemetry_mode` /
//! `resolve_trace_upload` entry points with env, config, and remote settings
//! that would re-enable product telemetry on upstream Grok Build.

use serial_test::serial;
use xai_grok_config_types::RemoteSettings;
use xai_grok_shell::agent::config::{Config, TelemetryMode};

#[test]
#[serial]
fn privacy_build_telemetry_mode_ignores_env_config_and_remote() {
    assert!(
        xai_grok_version::research_data_collection_forbidden(),
        "this fork must lock research collection off"
    );
    // SAFETY: #[serial]
    unsafe {
        std::env::set_var("GROK_TELEMETRY_ENABLED", "1");
    }
    let mut cfg = Config::default();
    cfg.features.telemetry = Some(TelemetryMode::Enabled);
    cfg.requirements.telemetry.pin(
        TelemetryMode::Enabled,
        xai_grok_shell::config::RequirementSource::Unknown,
    );
    cfg.remote_settings = Some(RemoteSettings {
        telemetry_enabled: Some(true),
        telemetry_mode: Some("enabled".into()),
        ..Default::default()
    });
    let r = cfg.resolve_telemetry_mode();
    assert!(
        r.value.is_disabled(),
        "privacy hard-off must win over env/config/remote: mode={:?}",
        r.value
    );
    unsafe {
        std::env::remove_var("GROK_TELEMETRY_ENABLED");
    }
}

#[test]
#[serial]
fn privacy_build_trace_upload_ignores_env_config_and_remote() {
    assert!(xai_grok_version::research_data_collection_forbidden());
    // SAFETY: #[serial]
    unsafe {
        std::env::set_var("GROK_TELEMETRY_ENABLED", "1");
        std::env::set_var("GROK_TELEMETRY_TRACE_UPLOAD", "1");
    }
    let mut cfg = Config::default();
    cfg.features.telemetry = Some(TelemetryMode::Enabled);
    cfg.telemetry.trace_upload = Some(true);
    cfg.requirements
        .trace_upload
        .pin(true, xai_grok_shell::config::RequirementSource::Unknown);
    cfg.remote_settings = Some(RemoteSettings {
        telemetry_enabled: Some(true),
        telemetry_mode: Some("enabled".into()),
        trace_upload_enabled: Some(true),
        ..Default::default()
    });
    let r = cfg.resolve_trace_upload();
    assert!(
        !r.value,
        "privacy hard-off must win over env/config/remote for trace upload"
    );
    assert!(!cfg.is_trace_upload_enabled());
    unsafe {
        std::env::remove_var("GROK_TELEMETRY_ENABLED");
        std::env::remove_var("GROK_TELEMETRY_TRACE_UPLOAD");
    }
}
