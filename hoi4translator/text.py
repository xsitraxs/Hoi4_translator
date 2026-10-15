"""Text helpers: placeholder protection, typography and request chunking."""
import re
from typing import List

from .localisation import PLACEHOLDER_RE


def zip_equal(*iterables):
    """zip() that refuses to silently drop the tail of a longer sequence."""
    lengths = [len(item) for item in iterables]
    if len(set(lengths)) > 1:
        raise ValueError(f"Несовпадение длин последовательностей: {lengths}")
    return zip(*iterables)


# ---------------------------------------------------------------------------
# 5. ПОСТОБРАБОТКА ПЕРЕВОДА
# ---------------------------------------------------------------------------

class PostProcessor:
    """Apply conservative, target-language-aware typography fixes."""

    _DOUBLE_SPACE_RE = re.compile(r'  +')
    _SPACE_BEFORE_PUNCT_RE = re.compile(r' +([,;:!?])')
    _FRENCH_SPACE_BEFORE_COMMA_RE = re.compile(r' +,')
    _FRENCH_SPACE_BEFORE_PUNCT_RE = re.compile(r' +([;:!?])')
    _QUOTED_PHRASE_RE = re.compile(r'"([^"\\]+)"')
    # Only trim horizontal spaces around the literal HOI4 escape; \s would
    # also consume physical CR/LF characters and hide a malformed translation.
    _SPACE_AROUND_NEWLINE_RE = re.compile(r'[ \t]*\\n[ \t]*')
    _LEFTOVER_PH_RE = re.compile(r'<<PH\d+>>')
    _QUOTE_STYLES = {
        "en": ("“", "”", ""), "fr": ("«", "»", "\u202f"),
        "de": ("„", "“", ""), "es": ("«", "»", ""),
        "pt": ("“", "”", ""), "pl": ("„", "”", ""),
        "ru": ("«", "»", ""), "ja": ("「", "」", ""),
        "zh-cn": ("「", "」", ""), "ko": ("“", "”", ""),
    }

    @classmethod
    def process(cls, text: str, target_lang: str = "ru") -> str:
        """Normalize basic punctuation without editing HOI4 tokens or casing."""
        if not text:
            return text

        # HOI4 newline escapes are literal two-character sequences in a value.
        text = cls._SPACE_AROUND_NEWLINE_RE.sub(r'\\n', text)

        tokens = []
        marker_prefix = "\uE000HOI4PP_"
        while marker_prefix in text:
            marker_prefix += "_"

        def protect_token(match):
            index = len(tokens)
            tokens.append(match.group(0))
            return f"{marker_prefix}{index}\uE001"

        protected = PLACEHOLDER_RE.sub(protect_token, text)
        language = target_lang.replace("_", "-").casefold()
        quote_open, quote_close, quote_space = cls._QUOTE_STYLES.get(
            language, ("“", "”", "")
        )
        protected = cls._QUOTED_PHRASE_RE.sub(
            lambda match: (
                f"{quote_open}{quote_space}{match.group(1)}"
                f"{quote_space}{quote_close}"
            ),
            protected,
        )
        protected = cls._DOUBLE_SPACE_RE.sub(' ', protected)

        if language == "fr":
            protected = cls._FRENCH_SPACE_BEFORE_COMMA_RE.sub(',', protected)
            protected = cls._FRENCH_SPACE_BEFORE_PUNCT_RE.sub(
                lambda match: "\u202f" + match.group(1), protected
            )
        else:
            protected = cls._SPACE_BEFORE_PUNCT_RE.sub(r'\1', protected)
        protected = protected.strip(' ')

        marker_re = re.compile(re.escape(marker_prefix) + r'(\d+)\uE001')
        protected = marker_re.sub(
            lambda match: tokens[int(match.group(1))]
            if int(match.group(1)) < len(tokens) else match.group(0),
            protected,
        )
        return cls._LEFTOVER_PH_RE.sub('', protected)


def protect_placeholders(text: str):
    tokens = []
    def replacer(m):
        tokens.append(m.group(0))
        return f'<<PH{len(tokens)-1}>>'

    # 1. Защищаем маркеры
    protected = PLACEHOLDER_RE.sub(replacer, text)
    # 2. Вставляем невидимый разделитель между соседними маркерами, чтобы движок не склеил их в одно слово
    # FIX: \u200B в строке замены re.sub вызывает bad escape — используем lambda
    protected = re.sub(r'(<<PH\d+>>)(<<PH\d+>>)', lambda m: m.group(1) + '\u200B' + m.group(2), protected)
    return protected, tokens

def restore_placeholders(text: str, tokens: List[str]) -> str:
    # Убираем невидимый разделитель
    text = text.replace('\u200B', '')

    def replacer(m):
        try:
            # Извлекаем только цифры, игнорируя пробелы и регистр (<< PH 0 >> -> 0)
            idx_str = re.sub(r'\D', '', m.group(0))
            idx = int(idx_str)
            return tokens[idx] if idx < len(tokens) else m.group(0)
        except Exception:
            return m.group(0)
    #Матчим любой вариант: <<PH0>>, << PH 0 >>, <<ph0>>, <<Ph 0 >> и т.д.
    return re.sub(r'<<\s*ph\s*\d+\s*>>', replacer, text, flags=re.IGNORECASE)


_PH_MARKER_RE = re.compile(r'<<PH\d+>>')
_SENTENCE_TERMINATORS = ".!?。！？"


def _find_chunk_boundary(text: str, start: int, hard_end: int) -> int:
    """Pick a natural split point before hard_end that never cuts a marker."""
    safe_end = hard_end
    for marker in _PH_MARKER_RE.finditer(text):
        if marker.start() >= hard_end:
            break
        if marker.end() > hard_end:
            safe_end = marker.start() if marker.start() > start else marker.end()
            break

    sentence_boundary = -1
    word_boundary = -1
    for index in range(safe_end - 1, start, -1):
        if not text[index].isspace():
            continue
        if word_boundary < 0:
            word_boundary = index + 1
        previous = index - 1
        while previous >= start and text[previous].isspace():
            previous -= 1
        if previous >= start and text[previous] in _SENTENCE_TERMINATORS:
            sentence_boundary = index + 1
            break

    end = (
        sentence_boundary if sentence_boundary > start
        else word_boundary if word_boundary > start
        else safe_end
    )

    # The chosen boundary must also stay outside every protection marker.
    for marker in _PH_MARKER_RE.finditer(text):
        if marker.start() >= end:
            break
        if marker.end() > end:
            end = marker.start() if marker.start() > start else marker.end()
            break

    if end <= start:
        end = safe_end if safe_end > start else hard_end
    return end


# Providers reject very long queries, so values are split; the Java
# service mirrors this limit so both engines send comparable requests.
MAX_QUERY_CHARS = 400


def split_long_text(text: str, limit: int = MAX_QUERY_CHARS) -> List[str]:
    """Split a long value into translatable chunks, never dropping characters.

    Mirrors the Java service so both engines send comparable requests and are
    bounded by the same query length.
    """
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    start = 0
    length = len(text)
    while start < length:
        hard_end = min(start + limit, length)
        if hard_end == length:
            chunks.append(text[start:])
            break
        end = _find_chunk_boundary(text, start, hard_end)
        chunks.append(text[start:end])
        start = end
    return chunks
