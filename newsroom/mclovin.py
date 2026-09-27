"""McLovin: read signals, find patterns, write falsifiable hypotheses. Instructions: agents/mclovin/.

Reads from the signals store (or a saved list of signals, for replay), writes hypotheses to
runs/mclovin/<timestamp>/hypotheses.json, and marks the signals it used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config, llm, playbooks
from .bossman import run_dir
from .signals import Signal, SignalStore, origin_key

OUTPUT = """
# Your output

Below are signals from the store, each with an id. Find the ones that support a falsifiable hypothesis
a reporter could settle, following the playbook above. Most signals will not.

Reply with JSON only:
{
  "hypotheses": [
    {
      "hypothesis": "one sentence that could be true or false",
      "shape": "contradiction | computed_number | promise_vs_actual | status_vs_practice | connection",
      "the_new_fact": "the specific record comparison or number that would be new if the hypothesis holds: record A
                       says X, record B says Y; or the figure nobody has computed, and from which data",
      "why_now": "what in these signals surfaced it",
      "who_would_know": ["offices, companies or people who hold the answer"],
      "would_settle_it": ["specific record types or systems, with date ranges where you can"],
      "accountable_party": "who would be responsible",
      "signal_ids": ["<id>", ...]
    }
  ],
  "watching": [{"signal_ids": ["<id>"], "why_not_yet": "what is missing"}]
}
Do not count sources yourself; the newsroom counts distinct origins from the signal ids.

Rank the hypotheses best first. The newsroom's published stories so far each had one of these shapes: a
bank's own filings against the capital its consent order required (contradiction); a city's write-offs and
its still-growing unpaid balance, from its auditor's tables (computed_number); a company's four filings
showing one deadline pushed back week by week (promise_vs_actual). A hypothesis without a shape and a
specific new fact is a topic, not a story: put it on the watch list.
"""

GENERIC = {"public records", "records", "documents", "data", "news reports", "online sources", "government records"}


def hypothesis_id(text: str) -> str:
    return "h-" + hashlib.sha1(text.strip().lower().encode()).hexdigest()[:10]


def read(signals: list[Signal]) -> dict:
    listing = "\n\n".join(
        f"[{s.id}] {s.summary}\n  claim: {s.checkable_claim}\n  accountable: {s.accountable_party or '?'}"
        f"\n  records trail: {'; '.join(s.records_trail) or '?'}\n  spike: {s.spike.get('value', '')}"
        f"\n  seen on: {', '.join(s.source_types)} ({len(s.sources)} sources)"
        for s in signals
    )
    system = playbooks.load("mclovin") + "\n" + OUTPUT
    return llm.ask_json(system, listing, model=config.load().smart_model, max_tokens=8000)


def finish(reply: dict, signals: list[Signal]) -> list[dict]:
    """Attach ids and code-computed counts, and run the hard checks."""
    by_id = {s.id: s for s in signals}
    out = []
    for h in reply.get("hypotheses", []):
        if not isinstance(h, dict):
            continue
        ids = [i for i in h.get("signal_ids", []) if i in by_id]
        origins = {origin_key(by_id[i].origin) for i in ids}
        record = {
            "id": hypothesis_id(str(h.get("hypothesis", ""))),
            "hypothesis": str(h.get("hypothesis", "")).strip(),
            "why_now": str(h.get("why_now", "")).strip(),
            "who_would_know": [str(x) for x in h.get("who_would_know", []) if str(x).strip()],
            "would_settle_it": [str(x) for x in h.get("would_settle_it", []) if str(x).strip()],
            "accountable_party": str(h.get("accountable_party", "")).strip(),
            "shape": str(h.get("shape", "")).strip(),
            "the_new_fact": str(h.get("the_new_fact", "")).strip(),
            "evidence_so_far": {"signal_ids": ids, "distinct_origins": len(origins),
                                "sources": sum(len(by_id[i].sources) for i in ids)},
        }
        record["problems"] = hard_checks(record, unknown=[i for i in h.get("signal_ids", []) if i not in by_id])
        out.append(record)
    return out


def hard_checks(h: dict, unknown: list[str] = ()) -> list[str]:
    """agents/mclovin/rubric.md, the parts code can check."""
    problems = []
    for name in ("hypothesis", "why_now", "accountable_party"):
        if not h.get(name):
            problems.append(f"missing {name}")
    if not h.get("who_would_know"):
        problems.append("missing who_would_know")
    settle = [s.strip().lower().rstrip(".") for s in h.get("would_settle_it", [])]
    if not settle:
        problems.append("missing would_settle_it")
    elif all(s in GENERIC for s in settle):
        problems.append("would_settle_it is generic; name the record types or systems")
    if h.get("hypothesis", "").count(". ") > 1:
        problems.append("hypothesis is more than one sentence")
    if "the_new_fact" in h and not h["the_new_fact"]:
        problems.append("missing the_new_fact: name the record comparison or the number that would be new")
    if not h.get("evidence_so_far", {}).get("signal_ids"):
        problems.append("cites no known signals")
    if unknown:
        problems.append(f"cites signal ids that don't exist: {', '.join(unknown)}")
    return problems


def run(store: SignalStore, *, since: str | None = None, signals_file: Path | None = None,
        limit: int = 150, mark: bool = True, say=print) -> dict:
    if signals_file:
        signals = [Signal(**row) for row in json.loads(Path(signals_file).read_text(encoding="utf-8"))]
        say(f"read {len(signals)} signals from {signals_file}")
    else:
        signals = store.recent(since or "", limit=limit, status="new")
        say(f"read {len(signals)} new signals from the store" + (f" since {since}" if since else ""))
    out = run_dir("mclovin")
    if not signals:
        return {"hypotheses": [], "watching": [], "run_dir": str(out)}
    reply = read(signals)
    hypotheses = finish(reply, signals)
    result = {"hypotheses": hypotheses, "watching": reply.get("watching", []), "run_dir": str(out),
              "signals_read": len(signals)}
    (out / "input_signals.json").write_text(json.dumps([s.to_dict() for s in signals], indent=1), encoding="utf-8")
    (out / "hypotheses.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    if mark and not signals_file:
        for h in hypotheses:
            if not h["problems"]:
                for sid in h["evidence_so_far"]["signal_ids"]:
                    store.mark_used(sid, h["id"])
    return result
