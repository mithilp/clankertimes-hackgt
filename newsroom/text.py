"""Small text helpers shared by counting, scouts and the article check."""

import re

_STRAIGHTEN = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def squash(text: str) -> str:
    """Lowercase, straighten quotes and dashes, and collapse whitespace, for forgiving comparisons."""
    return re.sub(r"\s+", " ", text.translate(_STRAIGHTEN)).strip().lower()


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def contains_quote(source: str, quote: str) -> bool:
    """True if quote appears word for word in source, ignoring case, spacing and curly punctuation."""
    q = squash(quote).strip(" .,;:'\"")
    return len(q) >= 10 and q in squash(source)
