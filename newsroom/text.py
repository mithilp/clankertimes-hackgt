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
    """True if quote appears word for word in source, ignoring case, spacing and curly punctuation. A quote
    shortened with an ellipsis ("..." or "…") matches when each piece appears word for word, in order."""
    # An ellipsis marks a cut and [brackets] mark an editor's change ("[w]hile", "[the agency]"): the pieces
    # between them must each appear word for word, in order.
    pieces = [squash(p).strip(" .,;:'\"") for p in re.split(r"\.\.\.|…|\[[^\]]{0,40}\]", quote)]
    pieces = [p for p in pieces if p]
    if not pieces or sum(map(len, pieces)) < 10:
        return False
    text, at = squash(source), 0
    for piece in pieces:
        at = text.find(piece, at)
        if at < 0:
            return False
        at += len(piece)
    return True
