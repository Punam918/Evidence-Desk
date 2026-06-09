"""NFC text with stable paragraph separators. Original block text is retained for mapping."""

import re
import unicodedata


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cs"))
    value = unicodedata.normalize("NFC", value)
    # Preserve line/code indentation; strip trailing horizontal whitespace, not internal spacing.
    return re.sub(r"[ \t]+(?=\n|$)", "", value).strip("\n")
