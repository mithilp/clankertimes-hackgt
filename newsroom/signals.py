"""The signals store: what Bossman finds on the live web, and what McLovin searches.

This is the newsroom's vector database. Everything else (stories, hypotheses, scout findings,
verdicts, events, the LLM cache, fetched page text) stays in SQLite in newsroom/db.py.

    Bossman  --add()-->  signals store  --recent() / similar()-->  McLovin
                                        <--mark_used()-- McLovin, when a signal feeds a hypothesis

Two implementations share this contract:
  * InMemorySignals, below: the reference implementation, used by the tests and for offline runs
    (NEWSROOM_SIGNALS=memory). Similarity is word overlap, not embeddings.
  * AstraSignals, in newsroom/signals_astra.py: DataStax Astra DB with real vector search.
    Not written yet; see docs/prompts/astra-db.md. It must pass tests/test_signals.py.

A signal is one Bossman item. The fields mirror agents/bossman/playbook.md.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse

# Two signals whose text overlaps this much, about the same accountable party, are one signal.
MERGE_SIMILARITY = float(os.getenv("NEWSROOM_SIGNAL_MERGE", "0.8"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Signal:
    summary: str                    # one or two plain sentences: what happened, according to whom
    why_interesting: str            # which of Bossman's criteria it meets, specifically
    checkable_claim: str            # the claim underneath that records could settle
    origin: str                     # where the claim first appeared: a URL, or a named source
    accountable_party: str = ""     # company, agency, official or institution, if known
    records_trail: list[str] = field(default_factory=list)   # where the records probably are
    sources: list[dict] = field(default_factory=list)        # [{"url": ..., "seen_at": ...}]
    spike: dict = field(default_factory=dict)                 # evidence it is moving now
    source_types: list[str] = field(default_factory=list)    # e.g. ["x", "reddit", "kalshi", "gov"]
    # Set by the store:
    id: str = ""
    first_seen: str = ""
    last_seen: str = ""
    status: str = "new"             # new | used | ignored
    used_by: list[str] = field(default_factory=list)          # hypothesis ids built from it
    ignored_reason: str = ""

    def text(self) -> str:
        """What gets embedded: the parts that say what this is about."""
        return f"{self.summary}\n{self.checkable_claim}\n{self.accountable_party}"

    def to_dict(self) -> dict:
        return asdict(self)


def origin_key(origin: str) -> str:
    """Normalise an origin so reposts of the same thing collide: host + path, lowercased, no query."""
    o = origin.strip().lower()
    if o.startswith(("http://", "https://")):
        u = urlparse(o)
        return f"{(u.hostname or '').removeprefix('www.')}{u.path.rstrip('/')}"
    return re.sub(r"\s+", " ", o)


class SignalStore(Protocol):
    def add(self, signal: Signal) -> tuple[str, bool]:
        """Store a signal. Returns (id, created). If it duplicates an existing signal (same origin, or
        near-identical text about the same accountable party), merges its sources into that one,
        bumps last_seen, and returns (existing id, False)."""

    def get(self, signal_id: str) -> Signal | None: ...

    def recent(self, since: str, *, limit: int = 200, status: str | None = None) -> list[Signal]:
        """Signals last seen at or after `since` (ISO timestamp), newest first."""

    def similar(self, text: str, *, k: int = 20, since: str | None = None) -> list[tuple[Signal, float]]:
        """The k signals most similar to `text`, with a 0-1 score, best first."""

    def mark_used(self, signal_id: str, hypothesis_id: str) -> None: ...

    def mark_ignored(self, signal_id: str, reason: str) -> None: ...


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


class InMemorySignals:
    """Reference implementation. Word-overlap similarity stands in for vector search."""

    def __init__(self) -> None:
        self._by_id: dict[str, Signal] = {}

    def _find_duplicate(self, signal: Signal) -> Signal | None:
        key = origin_key(signal.origin)
        for existing in self._by_id.values():
            if origin_key(existing.origin) == key:
                return existing
            same_party = existing.accountable_party.strip().lower() == signal.accountable_party.strip().lower()
            if same_party and _overlap(existing.text(), signal.text()) >= MERGE_SIMILARITY:
                return existing
        return None

    def add(self, signal: Signal) -> tuple[str, bool]:
        seen = signal.last_seen or now()
        if existing := self._find_duplicate(signal):
            known = {s.get("url") for s in existing.sources}
            existing.sources += [s for s in signal.sources if s.get("url") not in known]
            existing.source_types = sorted(set(existing.source_types) | set(signal.source_types))
            existing.last_seen = max(existing.last_seen, seen)
            if signal.spike:
                existing.spike = signal.spike            # the newest evidence it is moving
            return existing.id, False
        signal.id = signal.id or uuid.uuid4().hex
        signal.first_seen = signal.first_seen or seen
        signal.last_seen = seen
        self._by_id[signal.id] = signal
        return signal.id, True

    def get(self, signal_id: str) -> Signal | None:
        return self._by_id.get(signal_id)

    def recent(self, since: str, *, limit: int = 200, status: str | None = None) -> list[Signal]:
        rows = [s for s in self._by_id.values()
                if s.last_seen >= since and (status is None or s.status == status)]
        return sorted(rows, key=lambda s: s.last_seen, reverse=True)[:limit]

    def similar(self, text: str, *, k: int = 20, since: str | None = None) -> list[tuple[Signal, float]]:
        pool = [s for s in self._by_id.values() if since is None or s.last_seen >= since]
        scored = [(s, _overlap(text, s.text())) for s in pool]
        return sorted((x for x in scored if x[1] > 0), key=lambda x: x[1], reverse=True)[:k]

    def mark_used(self, signal_id: str, hypothesis_id: str) -> None:
        signal = self._by_id[signal_id]
        signal.status = "used"
        if hypothesis_id not in signal.used_by:
            signal.used_by.append(hypothesis_id)

    def mark_ignored(self, signal_id: str, reason: str) -> None:
        signal = self._by_id[signal_id]
        signal.status, signal.ignored_reason = "ignored", reason


_store: SignalStore | None = None


def get_store() -> SignalStore:
    """Astra DB when it's configured; the in-memory store only when asked for explicitly."""
    global _store
    if _store is None:
        if os.getenv("NEWSROOM_SIGNALS", "astra").lower() == "memory":
            _store = InMemorySignals()
        else:
            if not os.getenv("ASTRA_DB_API_ENDPOINT"):
                raise RuntimeError("ASTRA_DB_API_ENDPOINT is not set. Configure Astra DB in .env, "
                                   "or set NEWSROOM_SIGNALS=memory for an offline run.")
            from .signals_astra import AstraSignals   # written against docs/prompts/astra-db.md
            _store = AstraSignals()
    return _store
