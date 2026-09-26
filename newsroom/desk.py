"""The reporter's desk: one record per investigation, updated at every stage. Astra DB when configured.

SQLite (newsroom/db.py) stays the source of truth for pipeline state: stories, sub-hypotheses, findings,
events, and the page text that quotes are checked against. The desk is the shared, searchable record of
what the Reporter did, so that:
  * teammates on other machines see an investigation's progress live (status, stage, assignments, verdict);
  * the Reporter remembers: before it starts, it looks up past investigations of similar hypotheses by
    meaning, not just by id, and reads their kill memos instead of chasing the same thing twice;
  * McLovin and the council can read verdicts and spin-offs back.

A record is compact: statements, assignments, quotes (capped) and decisions. Never page text, never secrets.

Three implementations share one contract, like the signals store:
  * InMemoryDesk: the reference, used by tests (NEWSROOM_DESK=memory). Similarity is word overlap.
  * FileDesk: InMemoryDesk saved to runs/desk.json (NEWSROOM_DESK=file, the default without Astra).
  * AstraDesk, in newsroom/desk_astra.py: an Astra DB collection with vector search (NEWSROOM_DESK=astra,
    the default whenever Astra is configured). Must pass tests/test_desk.py (NEWSROOM_TEST_ASTRA=1).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Protocol

from .signals import _astra_configured, _overlap, now

TERMINAL = ("published", "held", "killed", "parked", "spiked", "failed")


def text_of(record: dict) -> str:
    """What gets embedded: what the investigation was about and what it concluded."""
    memo = record.get("verdict", {}).get("memo") or {}
    return "\n".join(str(x) for x in (record.get("hypothesis", ""), record.get("accountable_party", ""),
                                      record.get("gap", ""), memo.get("found", "")) if x)


class Desk(Protocol):
    def save(self, record: dict) -> str:
        """Insert or replace the record with this "_id" (set on first save). Returns the id."""

    def get(self, record_id: str) -> dict | None: ...

    def for_hypothesis(self, hypothesis_id: str) -> list[dict]:
        """Every investigation of this McLovin hypothesis id, newest first."""

    def recent(self, *, limit: int = 50, status: str | None = None) -> list[dict]:
        """Newest first by updated_at."""

    def similar(self, text: str, *, k: int = 5) -> list[tuple[dict, float]]:
        """Past investigations most similar to `text`, with a 0-1 score, best first."""


class InMemoryDesk:
    def __init__(self) -> None:
        self._by_id: dict[str, dict] = {}

    def save(self, record: dict) -> str:
        record = copy.deepcopy(record)
        record["_id"] = record.get("_id") or f"inv-{os.urandom(6).hex()}"
        record.setdefault("created_at", now())
        record["updated_at"] = now()
        self._by_id[record["_id"]] = record
        return record["_id"]

    def get(self, record_id: str) -> dict | None:
        r = self._by_id.get(record_id)
        return copy.deepcopy(r) if r else None

    def for_hypothesis(self, hypothesis_id: str) -> list[dict]:
        rows = [r for r in self._by_id.values() if r.get("hypothesis_id") == hypothesis_id]
        return copy.deepcopy(sorted(rows, key=lambda r: r["updated_at"], reverse=True))

    def recent(self, *, limit: int = 50, status: str | None = None) -> list[dict]:
        rows = [r for r in self._by_id.values() if status is None or r.get("status") == status]
        return copy.deepcopy(sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:limit])

    def similar(self, text: str, *, k: int = 5) -> list[tuple[dict, float]]:
        scored = [(r, _overlap(text, text_of(r))) for r in self._by_id.values()]
        return [(copy.deepcopy(r), s) for r, s in sorted((x for x in scored if x[1] > 0), key=lambda x: x[1], reverse=True)[:k]]


class FileDesk(InMemoryDesk):
    def __init__(self, path: str | os.PathLike) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            self._by_id = {r["_id"]: r for r in json.loads(self.path.read_text(encoding="utf-8"))}

    def save(self, record: dict) -> str:
        record_id = super().save(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._by_id.values()), indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.path)
        return record_id


_desk: Desk | None = None


def get_desk() -> Desk:
    """From NEWSROOM_DESK: astra (the default when Astra is configured), file (runs/desk.json), or memory."""
    global _desk
    if _desk is None:
        mode = os.getenv("NEWSROOM_DESK", "").lower() or ("astra" if _astra_configured() else "file")
        if mode == "memory":
            _desk = InMemoryDesk()
        elif mode == "file":
            _desk = FileDesk(os.getenv("NEWSROOM_DESK_FILE", "runs/desk.json"))
        elif mode == "astra":
            from .desk_astra import AstraDesk
            _desk = AstraDesk()
        else:
            raise RuntimeError(f"NEWSROOM_DESK={mode!r}: use astra, file or memory")
    return _desk
