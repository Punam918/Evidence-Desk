"""Conservative literal safeguards for optional English follow-up rewriting."""

import re
import unicodedata
from collections import Counter

NUMBER = re.compile(r"(?<![\w.])[+\-−]?\d+(?:[.,:/-]\d+)*(?:\s*%)?(?!\w)")
NEGATION = re.compile(
    r"\b(?:not|no|never|without|except|excluding|cannot|\w+n['’]t)\b", re.IGNORECASE
)
IDENTIFIER = re.compile(
    r"(?<![\w@])(?:[A-Za-z][A-Za-z0-9]*(?:[-_/.:][A-Za-z0-9]+)+|[A-Z]{2,}[A-Z0-9]*|[A-Za-z]+[0-9][A-Za-z0-9]*)(?![\w@])"
)
LITERAL = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|«([^»\n]+)»')


def preserves_literals(original: str, rewritten: str) -> bool:
    def numbers(value: str) -> Counter[str]:
        return Counter(re.sub(r"\s", "", n).replace("−", "-") for n in NUMBER.findall(value))

    def negatives(value: str) -> Counter[str]:
        # Contraction expansion may change surface form, never polarity or count.
        return Counter(
            "not" if n == "cannot" or n.endswith(("n't", "n’t")) else n
            for n in NEGATION.findall(value.casefold())
        )

    def ids(value: str) -> set[str]:
        return {token.casefold() for token in IDENTIFIER.findall(value)}

    before = unicodedata.normalize("NFC", original)
    after = unicodedata.normalize("NFC", rewritten)
    literals = [next(part for part in groups if part) for groups in LITERAL.findall(before)]
    return (
        numbers(before) <= numbers(after)
        and negatives(before) == negatives(after)
        and ids(before) <= ids(after)
        and all(literal in after for literal in literals)
    )
