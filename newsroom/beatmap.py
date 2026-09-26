"""A beat's map: the beat broken into specific parts, so a long-running Bossman covers a different part
each pass instead of repeating the same searches every 15 minutes.

The first pass on a beat is a survey: a broad gather, then the model breaks the beat into 15-40 leaves,
each an institution or recurring decision plus where its records live, with a few searches that would
check it. Every later pass picks one or two leaves to focus on, favouring leaves not visited lately and
leaves that have produced signals, with some randomness. Leaves that come up empty several visits in a
row are pruned; the look back after each pass can add leaves the map was missing.

The map lives at runs/beats/<beat>/map.json (git-ignored, local to each machine). It is readable and
editable: delete it to rebuild from scratch.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm

PRUNE_AFTER = 3          # empty visits in a row before a leaf is dropped
MAX_LEAVES = 60

BUILD = """
# Your job right now: map this beat

You will cover this beat over many passes, focusing on one or two parts of it each pass. Break the
beat into 15-40 specific parts to rotate through.

A good part is an institution, program or recurring decision, plus where its records live, narrow
enough that 2-4 searches can check it for news: "Georgia Tech Athletic Association finances: Form 990,
NCAA financial report" rather than "athletics". Group the parts under a few top-level areas.

You are given a survey of what is live on this beat right now. Use it, and what you know. Anything you
name from memory may be out of date (renamed offices, officials who left), so describe each part as a
place to look, never as a fact.

Also name 2-4 cheap calls to run on EVERY pass, whatever the focus, so breaking news on the beat is
never missed (for example the newest posts in the main subreddit, one broad news search).

Tools for the searches:
{tools}

Reply with JSON only:
{{"leaves": [{{"path": ["Top-level area", "Specific part"], "look_for": "what news here would look like",
              "records": ["where the records are"], "queries": [{{"tool": "...", "arg": "..."}}]}}],
 "sweep": [{{"tool": "...", "arg": "...", "why": "..."}}]}}
"""


def path(beat: str) -> Path:
    return Path("runs") / "beats" / beat / "map.json"


def load(beat: str) -> dict | None:
    p = path(beat)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(beat: str, m: dict) -> None:
    p = path(beat)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _leaf(raw: dict, n: int, added_by: str) -> dict | None:
    path_ = [str(x).strip() for x in raw.get("path", []) if str(x).strip()]
    if not path_:
        return None
    return {"id": f"l{n}", "path": path_, "look_for": str(raw.get("look_for", "")).strip(),
            "records": [str(r) for r in raw.get("records", []) if str(r).strip()],
            "queries": [{"tool": str(q.get("tool", "")), "arg": str(q.get("arg", ""))}
                        for q in raw.get("queries", []) if isinstance(q, dict)][:4],
            "added_by": added_by, "added_at": _now(), "status": "active",
            "visits": 0, "last_visit": None, "kept": 0, "empty_visits": 0}


def build(beat: str, system: str, tools: str, survey: list[dict]) -> dict:
    """Break the beat into leaves, from the beat file plus a survey of what is live now."""
    listing = "\n".join(f"- ({c['source_type']}) {c['title'][:140]}" for c in survey[:200]) or "(nothing gathered)"
    reply = llm.ask_json(system + "\n" + BUILD.format(tools=tools), f"SURVEY, gathered just now:\n{listing}",
                         model=config.load().smart_model, max_tokens=12000)
    leaves = [x for x in (_leaf(r, n, "survey") for n, r in enumerate(
        [r for r in reply.get("leaves", []) if isinstance(r, dict)][:MAX_LEAVES], 1)) if x]
    sweep = [{"tool": str(s.get("tool", "")), "arg": str(s.get("arg", "")), "why": str(s.get("why", ""))}
             for s in reply.get("sweep", []) if isinstance(s, dict)][:4]
    m = {"beat": beat, "built_at": _now(), "leaves": leaves, "sweep": sweep}
    save(beat, m)
    return m


def active(m: dict) -> list[dict]:
    return [leaf for leaf in m["leaves"] if leaf["status"] == "active"]


def weight(leaf: dict, now: datetime | None = None) -> float:
    """Unvisited and long-unvisited leaves first; leaves that produced signals get more visits; leaves
    that keep coming up empty fewer."""
    now = now or datetime.now(timezone.utc)
    if leaf["last_visit"]:
        hours = (now - datetime.fromisoformat(leaf["last_visit"])).total_seconds() / 3600
        recency = min(max(hours, 0.25), 48)
    else:
        recency = 48
    return recency * (1 + leaf["kept"]) / (1 + leaf["empty_visits"])


def pick(m: dict, k: int = 2, rng: random.Random | None = None) -> list[dict]:
    """k distinct leaves, drawn at random in proportion to weight()."""
    rng = rng or random.Random()
    pool, chosen = active(m), []
    while pool and len(chosen) < k:
        leaf = rng.choices(pool, weights=[weight(x) for x in pool])[0]
        chosen.append(leaf)
        pool = [x for x in pool if x is not leaf]
    return chosen


def describe(leaves: list[dict]) -> str:
    return "\n".join(
        f"[{x['id']}] {' > '.join(x['path'])}\n  look for: {x['look_for']}\n  records: {'; '.join(x['records'])}"
        + "".join(f"\n  suggested: {q['tool']}({q['arg']!r})" for q in x["queries"])
        for x in leaves)


def record_visits(m: dict, leaves: list[dict], trace: list[dict]) -> None:
    """After a pass: count what each focus leaf's calls kept, and prune leaves that keep coming up empty."""
    for leaf in leaves:
        kept = sum(t.get("kept", 0) for t in trace if t.get("leaf") == leaf["id"])
        leaf["visits"] += 1
        leaf["last_visit"] = _now()
        leaf["kept"] += kept
        leaf["empty_visits"] = 0 if kept else leaf["empty_visits"] + 1
        if leaf["empty_visits"] >= PRUNE_AFTER:
            leaf["status"] = "pruned"


def add(m: dict, raw_leaves: list[dict]) -> list[dict]:
    """Leaves the look back found missing. Skips ones whose path already exists."""
    known = {tuple(p.lower() for p in x["path"]) for x in m["leaves"]}
    added = []
    for raw in raw_leaves[:3]:
        n = len(m["leaves"]) + 1
        leaf = _leaf(raw, n, "reflection")
        if leaf and tuple(p.lower() for p in leaf["path"]) not in known and len(active(m)) < MAX_LEAVES:
            m["leaves"].append(leaf)
            known.add(tuple(p.lower() for p in leaf["path"]))
            added.append(leaf)
    return added
