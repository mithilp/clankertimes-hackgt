"""Bossman: decide what's worth adding to the signals store. Instructions: agents/bossman/.

Two steps, kept separate so each can be tested alone:
    gather()  - pull candidates from the live web (newsroom/gather.py). Network, no model.
    judge()   - decide which candidates become signals and fill in their fields. Model, no network.

Every pass saves what it saw to runs/bossman/<timestamp>/, so a judge can be rerun on the exact
same candidates later (replay) to compare two versions of the playbook.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import gather as gatherers
from . import llm, playbooks
from .signals import Signal, SignalStore

BATCH = 40

OUTPUT = """
# Your output

You are shown a numbered batch of candidates gathered from the live web just now. Decide which ones
are worth adding as signals, following the playbook above. Several candidates about the same event
become ONE signal listing all of their ids.

Reply with JSON only:
{
  "signals": [
    {
      "candidate_ids": ["<id>", ...],
      "summary": "one or two plain sentences: what happened, according to whom",
      "why_interesting": "which criteria it meets, specifically",
      "accountable_party": "company, agency, official or institution, or empty",
      "checkable_claim": "the claim underneath that records could settle",
      "records_trail": ["where the records probably are", ...],
      "origin": "where the claim first appeared, as a URL if you can tell, else the named source"
    }
  ],
  "skipped": [{"candidate_id": "<id>", "reason": "short reason"}]
}
Every candidate id must appear exactly once, in a signal or in skipped. Most candidates should be skipped.
"""

HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}|\bu/[A-Za-z0-9_-]{3,}")


def run_dir(kind: str = "bossman") -> Path:
    d = Path("runs") / kind / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d.mkdir(parents=True, exist_ok=True)
    return d


def gather(sources: list[str] | None = None) -> tuple[list[dict], dict[str, str]]:
    return gatherers.gather(sources)


def judge(candidates: list[dict]) -> dict:
    """Ask the model which candidates become signals. Returns {"signals": [...], "skipped": [...]}."""
    system = playbooks.load("bossman") + "\n" + OUTPUT
    decisions = {"signals": [], "skipped": []}
    for start in range(0, len(candidates), BATCH):
        batch = candidates[start:start + BATCH]
        listing = "\n\n".join(
            f"[{c['id']}] ({c['source_type']}) {c['title']}\n  url: {c['url']}\n  spike: {c['spike'].get('value', '')}"
            + (f"\n  {c['snippet']}" if c["snippet"] else "")
            for c in batch
        )
        reply = llm.ask_json(system, f"Candidates gathered at {batch[0]['seen_at']}:\n\n{listing}",
                             model=None, max_tokens=8000)
        decisions["signals"] += [s for s in reply.get("signals", []) if isinstance(s, dict)]
        decisions["skipped"] += [s for s in reply.get("skipped", []) if isinstance(s, dict)]
    return decisions


def to_signals(decisions: dict, candidates: list[dict]) -> list[Signal]:
    """Turn the model's decisions into Signals. Sources, spike and timing come from the gathered data,
    never from the model, so nothing it writes can invent a URL or a number."""
    by_id = {c["id"]: c for c in candidates}
    by_hash = {c["id"].split(":", 1)[-1]: c for c in candidates}

    def resolve(raw) -> dict | None:
        # Models often drop the "source:" prefix or keep the brackets from the listing.
        key = str(raw).strip().strip("[]")
        return by_id.get(key) or by_hash.get(key.split(":", 1)[-1])

    out = []
    for d in decisions.get("signals", []):
        cands = [c for c in (resolve(i) for i in d.get("candidate_ids", [])) if c]
        if not cands:
            continue
        spikes = [c["spike"] for c in cands if c.get("spike", {}).get("value")]
        out.append(Signal(
            summary=str(d.get("summary", "")).strip(),
            why_interesting=str(d.get("why_interesting", "")).strip(),
            checkable_claim=str(d.get("checkable_claim", "")).strip(),
            accountable_party=str(d.get("accountable_party", "")).strip(),
            records_trail=[str(r) for r in d.get("records_trail", []) if str(r).strip()],
            origin=str(d.get("origin", "")).strip() or cands[0]["url"],
            sources=[{"url": c["url"], "seen_at": c["seen_at"]} for c in cands if c["url"]],
            spike=spikes[0] if len(spikes) == 1 else ({"kind": "multiple", "value": "; ".join(s["value"] for s in spikes)} if spikes else {}),
            source_types=sorted({c["source_type"] for c in cands}),
            last_seen=max(c["seen_at"] for c in cands),
        ))
    return out


def hard_checks(signal: Signal) -> list[str]:
    """agents/bossman/rubric.md, the parts code can check. Empty means it passes."""
    problems = []
    for name in ("summary", "why_interesting", "checkable_claim", "origin"):
        if not getattr(signal, name):
            problems.append(f"missing {name}")
    if not signal.spike.get("value"):
        problems.append("no concrete spike (a count, rank, price move or date)")
    if not any(str(s.get("url", "")).startswith("http") for s in signal.sources):
        problems.append("no source URL")
    text = " ".join([signal.summary, signal.why_interesting, signal.checkable_claim, signal.accountable_party])
    if m := HANDLE.search(text):
        problems.append(f"personal handle in the signal text: {m.group(0)}")
    return problems


def run_once(store: SignalStore, *, sources: list[str] | None = None, replay: Path | None = None,
             say=print) -> dict:
    """One pass: gather (or load a saved gather), judge, check, store. Saves everything it saw."""
    out = run_dir()
    if replay:
        candidates, errors = json.loads(Path(replay).read_text(encoding="utf-8")), {}
        say(f"replaying {len(candidates)} saved candidates from {replay}")
    else:
        candidates, errors = gather(sources)
        say(f"gathered {len(candidates)} candidates" + (f"; failed sources: {errors}" if errors else ""))
    (out / "candidates.json").write_text(gatherers.dump(candidates), encoding="utf-8")

    decisions = judge(candidates)
    (out / "decisions.json").write_text(json.dumps(decisions, indent=1, ensure_ascii=False), encoding="utf-8")

    report = {"candidates": len(candidates), "created": [], "merged": [], "failed_checks": [],
              "skipped": len(decisions.get("skipped", [])), "source_errors": errors, "run_dir": str(out)}
    for signal in to_signals(decisions, candidates):
        if problems := hard_checks(signal):
            report["failed_checks"].append({"summary": signal.summary, "problems": problems})
            continue
        sid, created = store.add(signal)
        (report["created"] if created else report["merged"]).append({"id": sid, "summary": signal.summary})
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report
