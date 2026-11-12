"""Unicode normalization for untrusted text (documents, emails, transcripts).

Strips characters used to hide or reorder content (bidi controls, zero-width spaces, BOM,
control characters) while KEEPING ZWNJ/ZWJ (U+200C/U+200D), which Bangla conjuncts need.
"""
import unicodedata

_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))  # LRE..RLO, LRI..PDI
_ALWAYS_STRIP = _BIDI | {0x200B, 0x2060, 0xFEFF, 0x200E, 0x200F, 0x061C}
_KEEP_CONTROLS = {0x0A, 0x09}  # newline, tab
CRLF = chr(13) + chr(10)
LF = chr(10)


def sanitize_text(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, flags). Flags name what was removed, for injection_flags."""
    flags: set[str] = set()
    out: list[str] = []
    text = text.replace(CRLF, LF)
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if cp in _ALWAYS_STRIP:
            flags.add("bidi_or_invisible" if cp in _BIDI or cp != 0xFEFF else "bom")
            continue
        if cp == 0x0D:
            out.append("\n")
            continue
        category = unicodedata.category(ch)
        if category == "Cc" and cp not in _KEEP_CONTROLS:
            flags.add("control_chars")
            continue
        out.append(ch)
    return "".join(out), sorted(flags)


def strip_xml_invalid(text: str) -> str:
    """Remove characters that are illegal in XML 1.0 (needed before writing into DOCX)."""
    return "".join(
        ch for ch in text
        if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD
        or 0x10000 <= ord(ch) <= 0x10FFFF
    )
