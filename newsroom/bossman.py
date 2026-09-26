"""Bossman: decide what's worth adding to the signals store. Instructions: agents/bossman/.

Two steps, kept separate so each can be tested alone:
    gather()  - pull candidates from the live web (newsroom/gather.py). Network, no model.
    judge()   - decide which candidates become signals and fill in their fields. Model, no network.

Every pass saves what it saw to runs/bossman/<timestamp>/, so a judge can be rerun on the exact
same candidates later (replay) to compare two versions of the playbook.

With a beat (agents/bossman/beats/<name>.md), a pass is a small loop instead of fixed feeds:
    plan()     - read the beat and choose which searches and feeds to run.
    gather     - run the plan. Plain code, saved, replayable.
    judge()    - as above, with the beat as the assignment.
    reflect()  - look at what each step yielded: which paths were dead ends, which leads deserve a
                 follow-up search. Follow-ups run once, and their candidates are judged too.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import gather as gatherers
from . import config, llm, playbooks
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

PLAN = """
# Your job right now: plan this pass

You are about to search for candidates on the beat described above. Choose the calls to make. Tools:

{tools}

Choose 8-14 calls. Cover the beat's different kinds of interesting (money, safety, governance...), not
ten phrasings of one search. Prefer sources that show what is moving now. For each call say what you
expect it to surface, so we can later see which expectations held.

Reply with JSON only:
{{"steps": [{{"tool": "<tool name>", "arg": "<argument, or empty for a national feed>", "why": "what you expect it to surface"}}],
 "notes": "anything about the plan worth recording"}}
"""

REFLECT = """
# Your job right now: look back at this pass

You planned the calls below, ran them, and judged what came back. For each call you see how many
candidates it returned, how many of those were kept as evidence for a signal, and why the rest were
skipped. Several kept candidates often feed one signal, so kept counts are not signal counts; the
signals themselves are listed after the calls.

Decide:
- which calls were dead ends, and why (off-beat noise, stale, nothing accountable, a source that
  only repeats press releases...);
- which calls were productive;
- which leads deserve ONE follow-up call now: a signal whose records trail you could start on, or a
  skipped candidate that was close. At most {max_follow} follow-ups, using the same tools;
- what should change in the beat file for next time.

Reply with JSON only:
{{"assessment": "two or three sentences on how the pass went",
 "dead_ends": [{{"step": <n>, "why": "..."}}],
 "productive": [{{"step": <n>, "why": "..."}}],
 "follow_ups": [{{"tool": "...", "arg": "...", "why": "...", "lead": "the signal or candidate it follows"}}],
 "beat_notes": ["a concrete edit to the beat file", ...]}}
"""

CONSOLIDATE = """
These signals were written from separate batches of candidates, so one event can appear more than
once. Group the signals that are about the same underlying event, decision or document. Signals that
merely share a topic or an institution are NOT the same.

Reply with JSON only: {"groups": [[<n>, <n>, ...], ...]}, listing only groups of two or more.
"""

MAX_FOLLOW_UPS = 4
BEATS = playbooks.path("bossman") / "beats"

HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}|\bu/[A-Za-z0-9_-]{3,}")


def run_dir(kind: str = "bossman") -> Path:
    d = Path("runs") / kind / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d.mkdir(parents=True, exist_ok=True)
    return d


def gather(sources: list[str] | None = None) -> tuple[list[dict], dict[str, str]]:
    return gatherers.gather(sources)


def load_beat(beat: str) -> tuple[str, str]:
    """(name, text) for a beat: a name under agents/bossman/beats/, or a path to a .md file."""
    path = Path(beat) if beat.endswith(".md") else BEATS / f"{beat}.md"
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in BEATS.glob("*.md")))
        raise FileNotFoundError(f"no beat at {path}; known beats: {known}")
    return path.stem, path.read_text(encoding="utf-8")


def _system(beat_text: str | None) -> str:
    system = playbooks.load("bossman")
    if beat_text:
        system += ("\n\n# Your beat for this session\n\nEverything above still applies. This narrows it: "
                   "judge candidates for this beat, and skip what is off it.\n\n" + beat_text)
    return system


def judge(candidates: list[dict], beat_text: str | None = None) -> dict:
    """Ask the model which candidates become signals. Returns {"signals": [...], "skipped": [...]}."""
    system = _system(beat_text) + "\n" + OUTPUT

    def one(batch):
        listing = "\n\n".join(
            f"[{c['id']}] ({c['source_type']}) {c['title']}\n  url: {c['url']}\n  spike: {c['spike'].get('value', '')}"
            + (f"\n  {c['snippet']}" if c["snippet"] else "")
            for c in batch
        )
        return llm.ask_json(system, f"Candidates gathered at {batch[0]['seen_at']}:\n\n{listing}",
                            model=None, max_tokens=8000)

    batches = [candidates[i:i + BATCH] for i in range(0, len(candidates), BATCH)]
    decisions = {"signals": [], "skipped": []}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for reply in pool.map(one, batches):
            decisions["signals"] += [s for s in reply.get("signals", []) if isinstance(s, dict)]
            decisions["skipped"] += [s for s in reply.get("skipped", []) if isinstance(s, dict)]
    return decisions


def consolidate(decisions: dict) -> list[list[int]]:
    """Merge signals from different judge batches that are about the same event. Returns the groups merged."""
    signals = decisions["signals"]
    if len(signals) < 2:
        return []
    listing = "\n".join(f"{n}. {s.get('summary', '')} (accountable: {s.get('accountable_party', '')})"
                        for n, s in enumerate(signals))
    reply = llm.ask_json(CONSOLIDATE, listing, model=None, max_tokens=1000)
    groups, used = [], set()
    for g in reply.get("groups", []):
        idx = sorted({int(n) for n in g if str(n).isdigit() and 0 <= int(n) < len(signals)} - used)
        if len(idx) > 1:
            groups.append(idx)
            used.update(idx)
    drop = set()
    for g in groups:
        # Keep the best-sourced write-up; give it everyone's candidates and records trail.
        base = max(g, key=lambda n: len(signals[n].get("candidate_ids", [])))
        for n in g:
            if n != base:
                signals[base]["candidate_ids"] = list(dict.fromkeys(signals[base].get("candidate_ids", []) + signals[n].get("candidate_ids", [])))
                signals[base]["records_trail"] = list(dict.fromkeys(signals[base].get("records_trail", []) + signals[n].get("records_trail", [])))
                drop.add(n)
    decisions["signals"] = [s for n, s in enumerate(signals) if n not in drop]
    decisions.setdefault("merged_within_pass", []).extend(
        [[signals[n].get("summary", "") for n in g] for g in groups])
    return groups


def _tool_listing() -> str:
    lines = [f"- {name}(<{arg}>): {desc}" for name, (_, arg, desc) in gatherers.TOOLS.items()]
    lines.append("- national feeds, no argument: " + ", ".join(gatherers.GATHERERS)
                 + " (whole-country firehoses; rarely useful for a narrow beat)")
    return "\n".join(lines)


def plan(beat_text: str) -> dict:
    """Which searches and feeds to run for this beat."""
    reply = llm.ask_json(_system(beat_text) + "\n" + PLAN.format(tools=_tool_listing()),
                         "Plan this pass.", model=config.load().smart_model, max_tokens=3000)
    steps = [{"tool": str(s.get("tool", "")).strip(), "arg": str(s.get("arg", "") or "").strip(),
              "why": str(s.get("why", "")).strip()}
             for s in reply.get("steps", []) if isinstance(s, dict)]
    return {"steps": steps, "notes": str(reply.get("notes", ""))}


def run_steps(steps: list[dict], *, round_no: int, first: int = 1, known: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Run planned calls in parallel. Returns (new candidates, trace). A candidate found by several calls
    is kept once and remembers every call that found it (found_by)."""
    known = {} if known is None else known

    def one(step):
        try:
            return gatherers.run_step(step["tool"], step["arg"]), ""
        except Exception as e:  # noqa: BLE001 - a dead source is a result worth recording, not a crash
            return [], f"{type(e).__name__}: {e}"[:300]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, steps))
    new, trace = [], []
    for n, (step, (found, error)) in enumerate(zip(steps, results), first):
        fresh = 0
        for c in found:
            if c["id"] in known:
                known[c["id"]].setdefault("found_by", []).append(n)
            else:
                c["found_by"] = [n]
                known[c["id"]] = c
                new.append(c)
                fresh += 1
        trace.append({**step, "step": n, "round": round_no, "returned": len(found), "new": fresh, "error": error})
    return new, trace


def _outcomes(trace: list[dict], candidates: list[dict], decisions: dict) -> None:
    """Fill in, per step, how many of its candidates became signals and why the rest were skipped."""
    by_hash = {c["id"].split(":", 1)[-1]: c for c in candidates}

    def find(raw):
        return by_hash.get(str(raw).strip().strip("[]").split(":", 1)[-1])

    kept = {c["id"] for d in decisions.get("signals", []) for c in (find(i) for i in d.get("candidate_ids", [])) if c}
    reasons: dict[str, str] = {}
    for s in decisions.get("skipped", []):
        if c := find(s.get("candidate_id")):
            reasons[c["id"]] = str(s.get("reason", ""))
    for t in trace:
        mine = [c for c in candidates if t["step"] in c.get("found_by", [])]
        t["kept"] = sum(c["id"] in kept for c in mine)
        t["skip_reasons"] = [reasons[c["id"]] for c in mine if c["id"] in reasons][:8]


def reflect(beat_text: str, trace: list[dict], signals: list[dict]) -> dict:
    """Look back at the pass: dead ends, productive calls, follow-ups, and edits to the beat."""
    listing = "\n\n".join(
        f"step {t['step']}: {t['tool']}({t['arg']!r}) - expected: {t['why']}\n"
        f"  returned {t['returned']} ({t['new']} not already found), {t['kept']} kept as evidence"
        + (f"\n  ERROR: {t['error']}" if t["error"] else "")
        + "".join(f"\n  skipped: {r}" for r in t["skip_reasons"][:5])
        for t in trace)
    kept = "\n".join(f"- {s.get('summary', '')}" for s in signals) or "(none)"
    reply = llm.ask_json(_system(beat_text) + "\n" + REFLECT.format(max_follow=MAX_FOLLOW_UPS),
                         f"CALLS:\n\n{listing}\n\nSIGNALS THIS PASS ({len(signals)}):\n{kept}\n\nTools:\n{_tool_listing()}",
                         model=config.load().smart_model, max_tokens=3000)
    follow = [{"tool": str(f.get("tool", "")).strip(), "arg": str(f.get("arg", "") or "").strip(),
               "why": str(f.get("why", "")).strip(), "lead": str(f.get("lead", "")).strip()}
              for f in reply.get("follow_ups", []) if isinstance(f, dict)][:MAX_FOLLOW_UPS]
    return {"assessment": str(reply.get("assessment", "")),
            "dead_ends": [d for d in reply.get("dead_ends", []) if isinstance(d, dict)],
            "productive": [d for d in reply.get("productive", []) if isinstance(d, dict)],
            "follow_ups": follow,
            "beat_notes": [str(n) for n in reply.get("beat_notes", []) if str(n).strip()]}


def to_signals(decisions: dict, candidates: list[dict], beat: str = "") -> list[Signal]:
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
            origin=_origin(d.get("origin"), cands),
            sources=[{"url": c["url"], "seen_at": c["seen_at"]} for c in cands if c["url"]],
            spike=spikes[0] if len(spikes) == 1 else ({"kind": "multiple", "value": "; ".join(s["value"] for s in spikes)} if spikes else {}),
            source_types=sorted({c["source_type"] for c in cands}),
            last_seen=max(c["seen_at"] for c in cands),
            beats=[beat] if beat else [],
        ))
    return out


def _origin(raw, cands: list[dict]) -> str:
    """The store dedups on the origin URL, so it must be a bare URL. Models often write a name
    ("The Atlanta Journal-Constitution") or a URL with a note after it; then use the earliest-seen
    gathered URL instead."""
    text = str(raw or "").strip()
    if re.fullmatch(r"https?://\S+", text):
        return text
    first = min((c for c in cands if c["url"]), key=lambda c: c["seen_at"], default=None)
    return first["url"] if first else text


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
             beat: str | None = None, say=print) -> dict:
    """One pass: gather (or load a saved gather), judge, check, store. Saves everything it saw.
    With a beat: plan, run the plan, judge, reflect, run the follow-ups, judge those."""
    out = run_dir()
    beat_name, beat_text = load_beat(beat) if beat else ("", None)
    trace, plan_, reflection = [], None, None
    if replay:
        candidates, errors = json.loads(Path(replay).read_text(encoding="utf-8")), {}
        say(f"replaying {len(candidates)} saved candidates from {replay}")
    elif beat_text:
        plan_ = plan(beat_text)
        (out / "plan.json").write_text(json.dumps(plan_, indent=1, ensure_ascii=False), encoding="utf-8")
        say(f"beat {beat_name}: planned {len(plan_['steps'])} calls")
        steps = plan_["steps"] + [{"tool": s, "arg": "", "why": "requested with --sources"} for s in sources or []]
        known: dict = {}
        candidates, trace = run_steps(steps, round_no=1, known=known)
        errors = {f"step {t['step']}": t["error"] for t in trace if t["error"]}
        say(f"gathered {len(candidates)} candidates" + (f"; failed: {errors}" if errors else ""))
    else:
        candidates, errors = gather(sources)
        say(f"gathered {len(candidates)} candidates" + (f"; failed sources: {errors}" if errors else ""))

    decisions = judge(candidates, beat_text)
    consolidate(decisions)

    if plan_ is not None:
        _outcomes(trace, candidates, decisions)
        reflection = reflect(beat_text, trace, decisions["signals"])
        say(f"reflected: {len(reflection['dead_ends'])} dead ends, {len(reflection['follow_ups'])} follow-ups")
        if reflection["follow_ups"]:
            more, trace2 = run_steps(reflection["follow_ups"], round_no=2, first=len(trace) + 1, known=known)
            say(f"follow-ups gathered {len(more)} new candidates")
            if more:
                more_decisions = judge(more, beat_text)
                decisions["signals"] += more_decisions["signals"]
                decisions["skipped"] += more_decisions["skipped"]
                consolidate(decisions)
            candidates += more
            trace += trace2
            _outcomes(trace, candidates, decisions)
        (out / "reflection.json").write_text(json.dumps(reflection, indent=1, ensure_ascii=False), encoding="utf-8")
        (out / "trace.json").write_text(json.dumps(trace, indent=1, ensure_ascii=False), encoding="utf-8")

    (out / "candidates.json").write_text(gatherers.dump(candidates), encoding="utf-8")
    (out / "decisions.json").write_text(json.dumps(decisions, indent=1, ensure_ascii=False), encoding="utf-8")

    report = {"beat": beat_name, "candidates": len(candidates), "created": [], "merged": [], "failed_checks": [],
              "skipped": len(decisions.get("skipped", [])), "source_errors": errors, "run_dir": str(out)}
    for signal in to_signals(decisions, candidates, beat_name):
        if problems := hard_checks(signal):
            report["failed_checks"].append({"summary": signal.summary, "problems": problems})
            continue
        sid, created = store.add(signal)
        (report["created"] if created else report["merged"]).append({"id": sid, "summary": signal.summary})
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if reflection:
        report["reflection"] = reflection
    return report
