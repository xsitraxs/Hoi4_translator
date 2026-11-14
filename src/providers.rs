//! Engine metadata: language codes and the fallback chain.

/// Engines to try when the selected one cannot answer, in order.
pub fn fallback_chain(engine: &str) -> &'static [&'static str] {
    match engine {
        "Google" => &["MyMemory"],
        "DeepL" => &["Google", "MyMemory"],
        "MyMemory" => &["Google"],
        _ => &[],
    }
}

/// Engines a run may be started with.
pub const ENGINES: [&str; 3] = ["Google", "DeepL", "MyMemory"];

/// The async Google path used to talk to the unofficial endpoint directly,
/// while the `Google` engine name is what the user selects.
pub const GOOGLE_ENDPOINT_ENGINE: &str = "GoogleEndpoint";

/// MyMemory only accepts full locales; other engines want plain codes.
fn mymemory_locale(code: &str) -> Option<&'static str> {
    let normalized = code.replace('_', "-").to_lowercase();
    let locale = match normalized.as_str() {
        "en" | "en-us" | "en-gb" => "en-US",
        "de" | "de-de" => "de-DE",
        "fr" | "fr-fr" => "fr-FR",
        "es" | "es-es" => "es-ES",
        "pl" | "pl-pl" => "pl-PL",
        "ru" | "ru-ru" => "ru-RU",
        "uk" | "uk-ua" => "uk-UA",
        "it" | "it-it" => "it-IT",
        "pt" | "pt-br" => "pt-BR",
        "zh" | "zh-cn" => "zh-CN",
        "ja" | "ja-jp" => "ja-JP",
        "ko" | "ko-kr" => "ko-KR",
        "tr" | "tr-tr" => "tr-TR",
        "cs" | "cs-cz" => "cs-CZ",
        "hu" | "hu-hu" => "hu-HU",
        "ro" | "ro-ro" => "ro-RO",
        _ => return None,
    };
    Some(locale)
}

/// Map application language codes to DeepL's API codes and variants.
pub fn deepl_language_code(language: &str, target: bool) -> String {
    let normalized = language.replace('_', "-").to_lowercase();
    if matches!(normalized.as_str(), "zh" | "zh-cn" | "zh-hans") {
        // Simplified Chinese is target-only as ZH-HANS; DeepL expects the
        // unqualified ZH code when Chinese is the source language.
        return if target { "ZH-HANS" } else { "ZH" }.to_string();
    }
    if target && matches!(normalized.as_str(), "pt" | "pt-br") {
        return "PT-BR".to_string();
    }
    normalized.to_uppercase()
}

/// Map an application language code to the code a provider expects.
///
/// Every engine has its own dialect of language codes; keeping the mapping in
/// one place stops "Brazilian Portuguese" from silently meaning three different
/// things on three different code paths.
pub fn provider_language_code(engine: &str, code: &str, target: bool) -> String {
    if code.is_empty() {
        return code.to_string();
    }
    if engine == "DeepL" {
        return deepl_language_code(code, target);
    }
    let normalized = code.replace('_', "-").to_lowercase();
    if engine == "MyMemory" {
        return mymemory_locale(&normalized).map(str::to_string).unwrap_or_else(|| {
            format!("{code}-{}", code.to_uppercase())
        });
    }
    if engine == GOOGLE_ENDPOINT_ENGINE {
        // braz_por is a separate HOI4 database: ask for the Brazilian variant.
        if target && matches!(normalized.as_str(), "pt" | "pt-br") {
            return "pt-BR".to_string();
        }
        return code.to_string();
    }
    code.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fallback_chain_never_loops_back() {
        assert_eq!(fallback_chain("DeepL"), &["Google", "MyMemory"]);
        assert_eq!(fallback_chain("Google"), &["MyMemory"]);
        for engine in ENGINES {
            assert!(!fallback_chain(engine).contains(&engine));
        }
    }

    #[test]
    fn deepl_uses_its_own_variants() {
        assert_eq!(deepl_language_code("pt", true), "PT-BR");
        assert_eq!(deepl_language_code("pt", false), "PT");
        assert_eq!(deepl_language_code("zh-CN", true), "ZH-HANS");
        assert_eq!(deepl_language_code("zh-CN", false), "ZH");
        assert_eq!(deepl_language_code("ru", true), "RU");
    }

    #[test]
    fn mymemory_needs_full_locales() {
        assert_eq!(provider_language_code("MyMemory", "ru", true), "ru-RU");
        assert_eq!(provider_language_code("MyMemory", "zh-CN", true), "zh-CN");
    }

    #[test]
    fn google_asks_for_brazilian_portuguese() {
        assert_eq!(
            provider_language_code(GOOGLE_ENDPOINT_ENGINE, "pt", true),
            "pt-BR"
        );
        assert_eq!(
            provider_language_code(GOOGLE_ENDPOINT_ENGINE, "pt", false),
            "pt"
        );
    }
}
