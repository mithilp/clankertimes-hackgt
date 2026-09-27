"""The newsroom end to end, from McLovin's results to the website:

    Bossman -> signals (Astra)  ->  McLovin -> mclovin_results (Astra)
                                                  │
                                    pipeline: every result the desk hasn't worked yet
                                                  │
                                    Reporter (frame, size, assign, direct)
                                        └─ Scouts (DB + records + browser), reporting back
                                    verdict -> draft -> Skeptic + Virality
                                        fail -> back to the Reporter: fix it, or spike it
                                        pass -> published: the `articles` collection the site reads

McLovin's collection belongs to McLovin: the pipeline only reads it. What the Reporter did with each result is
recorded on the desk (`reporter_desk`), keyed by the result's id, so a result is never worked twice.

    python -m newsroom pipeline                   work every new McLovin result once
    python -m newsroom pipeline --limit 2 --loop 30
    python -m newsroom pipeline --dry-run         show what would be worked, spend nothing
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import reporter_agent
from .desk import TERMINAL, get_desk

# Field names McLovin's documents may use, most likely first. The first non-empty one wins.
ALIASES = {
    "hypothesis": ("hypothesis", "statement", "claim", "text", "title", "summary"),
    "why_now": ("why_now", "why", "reason", "rationale"),
    "accountable_party": ("accountable_party", "party", "accountable", "entity", "company", "agency"),
    "who_would_know": ("who_would_know", "sources_to_ask", "who_knows"),
    "would_settle_it": ("would_settle_it", "records", "evidence_needed", "settle_with", "records_to_check"),
    "signal_ids": ("signal_ids", "signals", "evidence_signal_ids"),
}
NOT_READY = {"watching", "rejected", "ignored", "draft", "not_ready"}
# McLovin's own verdicts that mean "don't report this": the story is already out, or the signals didn't connect.
SKIP_OUTCOMES = {"already_reported", "no_connection", "rejected", "duplicate"}
MAX_FAILURES = 3             # a result that keeps failing stops being retried
IN_PROGRESS_HOURS = 3        # a desk record this fresh and unfinished means someone is working it now


def _first(doc: dict, names: tuple[str, ...]):
    for name in names:
        value = doc.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v.get("id") if isinstance(v, dict) else v) for v in value if str(v).strip()]


def from_mclovin(doc: dict) -> dict | None:
    """One McLovin result, in whatever shape it was stored, as the reporter's input. None if it isn't a
    hypothesis the reporter should work (no statement, flagged by McLovin's checks, or marked not ready)."""
    evidence = doc.get("evidence_so_far") if isinstance(doc.get("evidence_so_far"), dict) else {}
    text = _first(doc, ALIASES["hypothesis"])
    if not isinstance(text, str) or not text.strip():
        return None
    if doc.get("problems") or str(doc.get("status", "")).lower() in NOT_READY:
        return None
    if str(doc.get("outcome", "")).lower() in SKIP_OUTCOMES:
        return None                     # McLovin found it already reported, or not a connected story
    if doc.get("kind") and doc["kind"] != "hypothesis":
        return None                     # McLovin's run logs and notes live in the same collection
    raw_id = doc.get("_id") or doc.get("id")
    prior = doc.get("prior_coverage") if isinstance(doc.get("prior_coverage"), dict) else {}
    connection = doc.get("connection")
    return {
        # What McLovin already worked out: why it matters, the connection it drew, and the coverage it found.
        "why_interesting": str(doc.get("why_interesting") or ""),
        "connection": str(connection.get("text", "") if isinstance(connection, dict) else connection or ""),
        "coverage_queries": _list(doc.get("coverage_queries") or prior.get("queries")),
        "known_coverage": [{"url": c.get("url", ""), "title": c.get("title", ""), "covers": c.get("covers", "")}
                           for c in prior.get("coverage") or [] if isinstance(c, dict) and c.get("url")],
        "id": f"mclov:{raw_id}" if raw_id else reporter_agent.hypothesis_id(text),
        "hypothesis": text.strip(),
        "why_now": str(_first(doc, ALIASES["why_now"]) or ""),
        "accountable_party": str(_first(doc, ALIASES["accountable_party"]) or ""),
        "who_would_know": _list(_first(doc, ALIASES["who_would_know"])),
        "would_settle_it": _list(_first(doc, ALIASES["would_settle_it"])),
        "evidence_so_far": {"signal_ids": _list(evidence.get("signal_ids") or _first(doc, ALIASES["signal_ids"])),
                            "distinct_origins": evidence.get("distinct_origins") or doc.get("distinct_origins")},
        "mclovin": {"collection_id": raw_id, "created": doc.get("created_at") or doc.get("created") or doc.get("first_seen")},
    }


class AstraMcLovin:
    """McLovin's results collection in Astra (NEWSROOM_MCLOVIN_COLLECTION, default mclovin_results). Read only."""

    def __init__(self, name: str | None = None) -> None:
        from .signals_astra import connect
        self.name = name or os.getenv("NEWSROOM_MCLOVIN_COLLECTION", "mclovin_results")
        self.database = connect()
        if self.name not in self.database.list_collection_names():
            raise RuntimeError(f"no {self.name!r} collection in this Astra database "
                               f"(collections: {', '.join(self.database.list_collection_names())})")
        self.collection = self.database.get_collection(self.name)

    def results(self, limit: int = 200) -> list[dict]:
        docs = list(self.collection.find({}, limit=limit, projection={"$vector": False}))
        docs.sort(key=lambda d: str(d.get("created_at") or d.get("created") or d.get("last_seen") or ""), reverse=True)
        return docs

    def sample(self, n: int = 3) -> list[dict]:
        return list(self.collection.find({}, limit=n, projection={"$vector": False}))


class FileMcLovin:
    """runs/mclovin/<ts>/hypotheses.json files, newest run first: McLovin's output when it runs from this repo."""

    def __init__(self, root: str | Path = "runs/mclovin") -> None:
        self.root = Path(root)
        self.name = str(self.root)

    def results(self, limit: int = 200) -> list[dict]:
        out = []
        for path in sorted(self.root.glob("*/hypotheses.json"), reverse=True):
            run = path.parent.name              # e.g. 20260926T182229Z
            created = f"{run[:4]}-{run[4:6]}-{run[6:8]}T{run[9:11]}:{run[11:13]}:{run[13:15]}Z" if len(run) >= 15 else ""
            for h in json.loads(path.read_text(encoding="utf-8")).get("hypotheses", []):
                out.append({**h, "id": f"{run}:{h['id']}" if h.get("id") else None, "created_at": created,
                            "kind": "hypothesis"})
        return out[:limit]


class BothMcLovin:
    """Every McLovin: the team's results in Astra, and the runs made from this repo, newest first."""

    def __init__(self) -> None:
        self.sources = [AstraMcLovin(), FileMcLovin()]
        self.name = " + ".join(s.name for s in self.sources)

    def results(self, limit: int = 200) -> list[dict]:
        docs = [d for s in self.sources for d in s.results(limit)]
        docs.sort(key=lambda d: str(d.get("created_at") or d.get("created") or ""), reverse=True)
        return docs[:limit]


def source(kind: str = "auto"):
    if kind == "file":
        return FileMcLovin()
    from .signals import _astra_configured
    if kind == "astra":
        return AstraMcLovin()
    if kind == "auto" and _astra_configured():
        return BothMcLovin()
    return FileMcLovin()


def pending(src, desk=None, limit: int = 200) -> list[dict]:
    """McLovin results the reporter hasn't finished, newest first. Skips results already worked to a verdict,
    results someone is working right now, and results that failed MAX_FAILURES times."""
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=IN_PROGRESS_HOURS)).isoformat(timespec="seconds")
    desk = desk if desk is not None else get_desk()
    out = []
    for doc in src.results(limit=limit):
        h = from_mclovin(doc)
        if h is None:
            continue
        records = desk.for_hypothesis(h["id"])
        if any(r.get("status") in TERMINAL and r.get("status") != "failed" for r in records):
            continue                    # already worked to a verdict
        if any(r.get("status") not in TERMINAL and str(r.get("updated_at", "")) >= fresh for r in records):
            continue                    # being worked right now, maybe on another machine
        if sum(r.get("status") == "failed" for r in records) >= MAX_FAILURES:
            continue
        out.append(h)
    return out


def run(*, limit: int = 1, budget: int = 10, kind: str = "auto", dry_run: bool = False, say=print) -> list[dict]:
    """One pass: work up to `limit` new McLovin results end to end. Returns one summary per result."""
    src = source(kind)
    desk = get_desk()
    todo = pending(src, desk)
    say(f"McLovin ({src.name}): {len(todo)} result(s) the desk hasn't worked" + (f"; working {min(limit, len(todo))}" if todo else ""))
    summaries = []
    for n, h in enumerate(todo[:limit], 1):
        say(f"\n{'=' * 100}\n[{n}/{min(limit, len(todo))}] {h['hypothesis']}")
        if dry_run:
            summaries.append({"hypothesis": h["hypothesis"], "status": "dry run"})
            continue
        try:
            result = reporter_agent.investigate(h, desk=desk, budget=budget, say=say)
        except Exception as e:  # noqa: BLE001 - one bad story must not stop the pass; it's recorded as failed
            say(f"failed: {type(e).__name__}: {e}")
            summaries.append({"hypothesis": h["hypothesis"], "status": "failed", "note": str(e)})
            continue
        final = result.get("final") or {"status": result.get("status", "skipped")}
        summaries.append({"hypothesis": h["hypothesis"], "status": final.get("status"),
                          "note": final.get("headline") or final.get("note", ""), "story_id": result.get("story_id")})
    if summaries:
        say("\n" + "\n".join(f"  {s['status'].upper():<10} {s['hypothesis'][:90]}" for s in summaries))
    return summaries


def loop(minutes: float, **kw) -> None:
    say = kw.get("say", print)
    while True:
        run(**kw)
        say(f"next pass in {minutes:g} minutes (ctrl-c to stop)")
        time.sleep(minutes * 60)
