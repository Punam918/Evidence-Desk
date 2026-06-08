"""Lightweight JSON quoting shared by context and memory; no inference imports."""

import json
import re


def quoted(value: object) -> str:
    # JSON escaping prevents reference text from creating records/roles or XML delimiters.
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")
    return re.sub(r"\[(E\d+)\]", lambda m: r"\u005b" + m[1] + r"\u005d", encoded)
