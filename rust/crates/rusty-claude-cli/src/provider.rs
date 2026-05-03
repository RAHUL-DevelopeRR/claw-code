//! LLM provider resolution and connectivity probing.
//!
//! Determines which API backend to use: Azure AI Foundry, OpenRouter, or a custom endpoint.

use std::env;
use api::{detect_provider_kind, ProviderKind};
use crate::brand::*;

/// XOR-deobfuscate an embedded credential at runtime.
pub fn deobfuscate_key() -> String {
    const ENCODED: &str = "eygGJQ4jaigfMTYXBTlqAXoBIhFfCGhzLSIaNR0AEC8YXTg/Ag0ZJwggGCQJCBcffiYGECU/CQF3XDY2Li0QEgYTQyolXS94DyQ0My4tFwx3KRsr";
    const SALT: &[u8] = b"NeuronXK";
    use base64::{engine::general_purpose, Engine as _};
    let bytes = general_purpose::STANDARD.decode(ENCODED).unwrap_or_default();
    bytes.iter()
        .enumerate()
        .map(|(i, b)| (b ^ SALT[i % SALT.len()]) as char)
        .collect()
}

/// Resolves the LLM provider in priority order:
///   1. Environment overrides (OPENAI_API_KEY + OPENAI_BASE_URL already set)
///   2. Azure AI Foundry (44K tokens/day quota)
///   3. OpenRouter free tier (fallback)
///
/// Returns (api_key, base_url, model, provider_label) tuple.
pub fn resolve_provider() -> (String, String, String, &'static str) {
    // Priority 1: Environment override
    if let (Ok(key), Ok(url)) = (env::var("OPENAI_API_KEY"), env::var("OPENAI_BASE_URL")) {
        if !key.is_empty() && !url.is_empty() {
            let model = env::var("NEURON_MODEL").unwrap_or_else(|_| "gpt-5.5".to_string());
            return (key, url, model, "custom");
        }
    }

    // Priority 2: Azure AI Foundry (model-router, quota-limited)
    let quota = crate::quota::QuotaState::load();
    if !quota.is_azure_exhausted() {
        let azure_key = env::var("AZURE_OPENAI_API_KEY").unwrap_or_else(|_| deobfuscate_key());
        let azure_base = env::var("AZURE_OPENAI_ENDPOINT").unwrap_or_else(|_| {
            "https://rahul-mok8ryyn-eastus2.services.ai.azure.com/openai/v1".to_string()
        });
        let azure_model = env::var("AZURE_OPENAI_MODEL")
            .unwrap_or_else(|_| "Kimi-K2.5".to_string());

        if azure_api_probe(&azure_key, &azure_base) {
            eprintln!(
                "\x1b[32m\u{2713}\x1b[0m Azure AI Foundry ({}) \u{00b7} Quota: {}",
                azure_model, quota.display_compact()
            );
            return (azure_key, azure_base, azure_model, "azure");
        }
        eprintln!("\x1b[33m\u{26a0}\x1b[0m Azure unavailable \u{2013} falling back to OpenRouter");
    } else {
        eprintln!(
            "\x1b[33m\u{26a0}\x1b[0m Azure daily quota exhausted ({}) \u{2013} using fallback",
            quota.display_compact()
        );
    }

    // Priority 3: OpenRouter free (via existing PKCE auth)
    if let Some(openrouter_key) = crate::auth::ensure_api_key() {
        return (
            openrouter_key,
            "https://openrouter.ai/api/v1".to_string(),
            "qwen/qwen3-coder-480b-a35b-instruct:free".to_string(),
            "openrouter",
        );
    }

    // Nothing works
    eprintln!("\x1b[31m\u{2717}\x1b[0m No API provider available. Set OPENAI_API_KEY or authenticate via neuron auth.");
    std::process::exit(1);
}

/// Quick non-blocking probe to check if the Azure endpoint is reachable.
pub fn azure_api_probe(api_key: &str, base_url: &str) -> bool {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build();
    let client = match client {
        Ok(c) => c,
        Err(_) => return false,
    };
    let url = format!("{}/chat/completions", base_url.trim_end_matches('/'));
    let body = serde_json::json!({
        "model": "Kimi-K2.5",
        "messages": [{"role": "user", "content": "ping"}],
        "max_completion_tokens": 1
    });
    match client
        .post(&url)
        .header("content-type", "application/json")
        .header("api-key", api_key)
        .bearer_auth(api_key)
        .json(&body)
        .send()
    {
        Ok(resp) => {
            let status = resp.status().as_u16();
            status == 200 || status == 429 || status == 400 || status == 401
        }
        Err(_) => false,
    }
}

pub fn provider_label(kind: ProviderKind) -> &'static str {
    match kind {
        ProviderKind::Anthropic => "anthropic",
        ProviderKind::Xai => "xai",
        ProviderKind::OpenAi => "openai",
    }
}

pub fn format_connected_line(model: &str) -> String {
    let provider = provider_label(detect_provider_kind(model));
    format!("{GREEN}{BOLD}\u{2713}{R} {DIM}Connected:{R} {BLUE}{BOLD}{model}{R} {DIM}via{R} {ORANGE}{provider}{R}")
}
