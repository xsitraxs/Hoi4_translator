"""Engine metadata: language codes and the fallback chain."""


# ---------------------------------------------------------------------------
# 7. ФОЛБЭК-ЦЕПОЧКА ДВИЖКОВ
# ---------------------------------------------------------------------------

# Порядок фолбэка: если основной движок упал, пробуем следующие
FALLBACK_CHAIN = {
    "Google":   ["MyMemory"],       # Google упал → MyMemory
    "DeepL":    ["Google", "MyMemory"],  # DeepL упал → Google → MyMemory
    "MyMemory": ["Google"],         # MyMemory упал → Google
    "JavaSim":  ["Google", "MyMemory"],
}

# Коды языков, которые понимает каждый провайдер
# ---------------------------------------------------------------------------
# The asynchronous Google path talks to the unofficial endpoint directly, while
# the synchronous fallback goes through deep-translator, which validates the
# language against its own table (it knows "pt" but rejects "pt-BR").
GOOGLE_ENDPOINT_ENGINE = "GoogleEndpoint"

# MyMemory only accepts full locales (keys are case-folded before lookup).
_MYMEMORY_LOCALES = {
    "en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES",
    "pl": "pl-PL", "ru": "ru-RU", "uk": "uk-UA", "it": "it-IT",
    "pt": "pt-BR", "zh-cn": "zh-CN", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR",
    "tr": "tr-TR", "cs": "cs-CZ", "hu": "hu-HU", "ro": "ro-RO",
}


def deepl_language_code(language: str, *, target: bool) -> str:
    """Map app language codes to DeepL's API codes and variants."""
    normalized = language.replace("_", "-").casefold()
    if normalized in {"zh", "zh-cn", "zh-hans"}:
        # Simplified Chinese is target-only as ZH-HANS; DeepL expects the
        # unqualified ZH code when Chinese is the source language.
        return "ZH-HANS" if target else "ZH"
    if target and normalized in {"pt", "pt-br"}:
        return "PT-BR"
    return normalized.upper()


def provider_language_code(engine: str, code: str, *, target: bool = True) -> str:
    """Map an application language code to the code a provider expects.

    Every engine has its own dialect of language codes; keeping the mapping in
    one place stops "Brazilian Portuguese" from silently meaning three different
    things on three different code paths.
    """
    if not code:
        return code
    if engine == "DeepL":
        return deepl_language_code(code, target=target)
    normalized = code.replace("_", "-").casefold()
    if engine == "MyMemory":
        return _MYMEMORY_LOCALES.get(normalized, f"{code}-{code.upper()}")
    if engine == GOOGLE_ENDPOINT_ENGINE:
        # braz_por is a separate HOI4 database: ask for the Brazilian variant.
        if target and normalized in {"pt", "pt-br"}:
            return "pt-BR"
        return code
    # deep-translator's GoogleTranslator/MyMemory wrappers validate codes
    # themselves, so plain HOI4 codes are passed through unchanged.
    return code
