"""Immutable release prompts, loaded from package resources and fingerprinted."""

import hashlib
from importlib.resources import files

VERSION = "grounded-assistant/v2+answer-schema/v2+compare/v1+output-integrity/v1"


def instructions(compare: bool = False) -> tuple[str, str]:
    names = ["grounded-assistant-v2.txt", "answer-schema-v2.txt"]
    if compare:
        names.append("compare-v1.txt")
    rendered = "\n\n".join(files(__package__).joinpath(name).read_text().strip() for name in names)
    return rendered, hashlib.sha256((VERSION + "\n" + rendered).encode()).hexdigest()
