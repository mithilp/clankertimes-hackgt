"""The signals store: what Bossman finds on the live web, and what McLovin searches.

This is the newsroom's vector database. Everything else (stories, hypotheses, scout findings,
verdicts, events, the LLM cache, fetched page text) stays in SQLite in newsroom/db.py.

    Bossman  --add()-->  signals store  --recent() / similar()-->  McLovin
                                        <--mark_used()-- McLovin, when a signal feeds a hypothesis

Three implementations share this contract:
  * InMemorySignals, below: the reference implementation, used by the tests
    (NEWSROOM_SIGNALS=memory). Similarity is word overlap, not embeddings. Forgets on exit.
  * FileSignals, below: InMemorySignals saved to a JSON file (NEWSROOM_SIGNALS=file, the default
    when Astra isn't configured), so agents run as separate commands can hand off to each other.
  * AstraSignals, in newsroom/signals_astra.py: DataStax Astra DB with real vector search.
    Not written yet; see docs/prompts/astra-db.md. It must pass tests/test_signals.py.

A signal is one Bossman item. The fields mirror agents/bossman/playbook.md.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


class FileSignals(InMemorySignals):
    """InMemorySignals persisted to one JSON file after every write. For local runs, not production."""

    def __init__(self, path: str | os.PathLike) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for row in json.loads(self.path.read_text(encoding="utf-8")):
                signal = Signal(**row)
                self._by_id[signal.id] = signal

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([s.to_dict() for s in self._by_id.values()], indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, signal: Signal) -> tuple[str, bool]:
        result = super().add(signal)
        self._save()
        return result

    def mark_used(self, signal_id: str, hypothesis_id: str) -> None:
        super().mark_used(signal_id, hypothesis_id)
        self._save()

    def mark_ignored(self, signal_id: str, reason: str) -> None:
        super().mark_ignored(signal_id, reason)
        self._save()


_store: SignalStore | None = None


def get_store() -> SignalStore:
    """Which store to use, from NEWSROOM_SIGNALS:
      astra   Astra DB (the default whenever ASTRA_DB_API_ENDPOINT is set)
      file    a local JSON file at NEWSROOM_SIGNALS_FILE (the default otherwise)
      memory  in-process only; forgotten on exit (tests)
    """
    global _store
    if _store is None:
        mode = os.getenv("NEWSROOM_SIGNALS", "").lower() or ("astra" if os.getenv("ASTRA_DB_API_ENDPOINT") else "file")
        if mode == "memory":
            _store = InMemorySignals()
        elif mode == "file":
            _store = FileSignals(os.getenv("NEWSROOM_SIGNALS_FILE", "runs/signals.json"))
        elif mode == "astra":
            if not os.getenv("ASTRA_DB_API_ENDPOINT"):
                raise RuntimeError("NEWSROOM_SIGNALS=astra but ASTRA_DB_API_ENDPOINT is not set in .env.")
            from .signals_astra import AstraSignals   # written against docs/prompts/astra-db.md
            _store = AstraSignals()
        else:
            raise RuntimeError(f"NEWSROOM_SIGNALS={mode!r}: use astra, file or memory")
    return _store
