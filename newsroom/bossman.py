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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gather as gatherers
from . import beatmap, config, db, llm, playbooks
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
{focus}
Make as many calls as this pass needs: usually 8-25, never more than {max_calls}. Don't pad the plan:
ten phrasings of one search is one search. Prefer sources that show what is moving now. For each call
say what you expect it to surface, so we can later see which expectations held.

Reply with JSON only:
{{"steps": [{{"tool": "<tool name>", "arg": "<argument, or empty for a national feed>", "why": "what you expect it to surface", "leaf": "<id of the focus part it serves, or empty>"}}],
 "notes": "anything about the plan worth recording"}}
"""

SURVEY = """
## This is a survey pass

No map of this beat exists yet. Plan a broad pass across the whole beat. After it, the beat will be
broken into specific parts from what you find, and later passes will take those parts one or two at
a time.
"""

FOCUS = """
## Focus for this pass

This beat is covered a part at a time. Spend most of this pass's calls on the parts below, going
deeper than a broad search would: their records, the institutions' own sites and feeds, the offices
and programs by name. Tag each call with the id of the part it serves. These sweep calls for breaking
news already run on every pass, so don't repeat them: {sweep}

{leaves}
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
- what should change in the beat file for next time;
- which parts of the beat this pass revealed that the map (listed after the signals) doesn't cover:
  at most 3, each specific enough that 2-4 searches could check it.

Reply with JSON only:
{{"assessment": "two or three sentences on how the pass went",
 "dead_ends": [{{"step": <n>, "why": "..."}}],
 "productive": [{{"step": <n>, "why": "..."}}],
 "follow_ups": [{{"tool": "...", "arg": "...", "why": "...", "lead": "the signal or candidate it follows"}}],
 "beat_notes": ["a concrete edit to the beat file", ...],
 "new_leaves": [{{"path": ["area", "specific part"], "look_for": "...", "records": ["..."], "queries": [{{"tool": "...", "arg": "..."}}]}}]}}
"""

CONSOLIDATE = """
These signals were written from separate batches of candidates, so one event can appear more than
once. Group the signals that are about the same underlying event, decision or document. Signals that
merely share a topic or an institution are NOT the same.

Reply with JSON only: {"groups": [[<n>, <n>, ...], ...]}, listing only groups of two or more.
"""

SAME_EVENT = """
A new signal is about to be stored. Below it are the stored signals most similar to it. Is the new
signal about the same underlying event, decision or document as one of them? A different development
in the same story (a vote after a proposal, a lawsuit after an incident) is NOT the same event, and
neither is a different claim about the same institution.

Reply with JSON only: {"same_as": <the stored signal's number>} or {"same_as": null}
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


def _previous(prev: dict | None) -> str:
    """What the last pass learned, for the next plan: so a long-running loop varies its calls and
    stops repeating dead ends instead of making the same plan every 15 minutes."""
    if not prev:
        return ""
    r, calls = prev.get("reflection") or {}, {t["step"]: t for t in prev.get("trace") or []}

    def call(step):
        t = calls.get(step, {})
        return f"{t.get('tool', '?')}({t.get('arg', '')!r})"

    lines = [f"Assessment: {r.get('assessment', '')}"]
    lines += [f"Dead end: {call(d.get('step'))}: {d.get('why', '')}" for d in r.get("dead_ends", [])]
    lines += [f"Productive: {call(d.get('step'))}: {d.get('why', '')}" for d in r.get("productive", [])]
    lines += [f"Beat note: {n}" for n in r.get("beat_notes", [])]
    lines += [f"Stored last pass: {s}" for s in prev.get("stored", [])]
    return ("\n\nLAST PASS, {when}. Don't repeat its dead ends. Productive calls can run again, since the "
            "news moves. Spend the rest on parts of the beat it didn't reach.\n").format(when=prev.get("when", "")) + "\n".join(lines)


def plan(beat_text: str, *, max_calls: int = 40, previous: dict | None = None, focus: str = "") -> dict:
    """Which searches and feeds to run for this beat."""
    reply = llm.ask_json(_system(beat_text) + "\n" + PLAN.format(tools=_tool_listing(), max_calls=max(max_calls, 8), focus=focus),
                         "Plan this pass." + _previous(previous), model=config.load().smart_model,
                         max_tokens=1500 + 150 * max_calls)
    steps = [{"tool": str(s.get("tool", "")).strip(), "arg": str(s.get("arg", "") or "").strip(),
              "why": str(s.get("why", "")).strip(), "leaf": str(s.get("leaf", "") or "").strip()}
             for s in reply.get("steps", []) if isinstance(s, dict)][:max(max_calls, 8)]
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


def reflect(beat_text: str, trace: list[dict], signals: list[dict], max_follow: int = MAX_FOLLOW_UPS,
            map_parts: str = "") -> dict:
    """Look back at the pass: dead ends, productive calls, follow-ups, and edits to the beat."""
    listing = "\n\n".join(
        f"step {t['step']}: {t['tool']}({t['arg']!r}) - expected: {t['why']}\n"
        f"  returned {t['returned']} ({t['new']} not already found), {t['kept']} kept as evidence"
        + (f"\n  ERROR: {t['error']}" if t["error"] else "")
        + "".join(f"\n  skipped: {r}" for r in t["skip_reasons"][:5])
        for t in trace)
    kept = "\n".join(f"- {s.get('summary', '')}" for s in signals) or "(none)"
    reply = llm.ask_json(_system(beat_text) + "\n" + REFLECT.format(max_follow=max_follow),
                         f"CALLS:\n\n{listing}\n\nSIGNALS THIS PASS ({len(signals)}):\n{kept}"
                         f"\n\nTHE MAP'S PARTS:\n{map_parts or '(no map yet)'}\n\nTools:\n{_tool_listing()}",
                         model=config.load().smart_model, max_tokens=3000)
    follow = [{"tool": str(f.get("tool", "")).strip(), "arg": str(f.get("arg", "") or "").strip(),
               "why": str(f.get("why", "")).strip(), "lead": str(f.get("lead", "")).strip()}
              for f in reply.get("follow_ups", []) if isinstance(f, dict)][:max_follow]
    return {"assessment": str(reply.get("assessment", "")),
            "dead_ends": [d for d in reply.get("dead_ends", []) if isinstance(d, dict)],
            "productive": [d for d in reply.get("productive", []) if isinstance(d, dict)],
            "follow_ups": follow,
            "beat_notes": [str(n) for n in reply.get("beat_notes", []) if str(n).strip()],
            "new_leaves": [x for x in reply.get("new_leaves", []) if isinstance(x, dict)]}


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


JUDGED_DAYS = 14


def drop_judged(beat: str, candidates: list[dict], trace: list[dict]) -> list[dict]:
    """Leave out candidates this beat already judged in the last JUDGED_DAYS days, and note per call
    how many were left out. Quiet passes then cost almost nothing."""
    since = (datetime.now(timezone.utc) - timedelta(days=JUDGED_DAYS)).isoformat(timespec="seconds")
    with db.session() as conn:
        seen = {r["id"] for r in conn.execute("select id from judged where beat = ? and at >= ?", (beat, since))}
    for t in trace:
        t["already_judged"] = sum(c["id"] in seen for c in candidates if t["step"] in c.get("found_by", []))
    return [c for c in candidates if c["id"] not in seen]


def mark_judged(beat: str, candidates: list[dict], decisions: dict) -> None:
    by_hash = {c["id"].split(":", 1)[-1]: c["id"] for c in candidates}
    kept = {by_hash.get(str(i).strip().strip("[]").split(":", 1)[-1])
            for d in decisions.get("signals", []) for i in d.get("candidate_ids", [])}
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.session() as conn:
        conn.executemany("insert or replace into judged (beat, id, at, outcome) values (?, ?, ?, ?)",
                         [(beat, c["id"], at, "kept" if c["id"] in kept else "skipped") for c in candidates])


def same_event(store: SignalStore, signal: Signal) -> Signal | None:
    """An already-stored signal about the same event, if any. Word overlap misses rewrites of one story
    (two passes wrote the Fanning chancellor pick in different words), so the closest stored signals
    go to a quick model check."""
    try:
        hits = [h for h, _ in store.similar(signal.text(), k=3) if h.id]
    except Exception:  # noqa: BLE001 - a search failure just means no merge
        return None
    if not hits:
        return None
    listing = "\n".join(f"{n}. {h.summary} (accountable: {h.accountable_party}; first seen {h.first_seen[:10]})"
                        for n, h in enumerate(hits))
    reply = llm.ask_json(SAME_EVENT, f"NEW SIGNAL:\n{signal.summary} (accountable: {signal.accountable_party})"
                                     f"\n\nSTORED:\n{listing}", model=None, max_tokens=200)
    n = reply.get("same_as")
    return hits[n] if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(hits) else None


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
             beat: str | None = None, max_calls: int = 40, previous: dict | None = None, say=print) -> dict:
    """One pass: gather (or load a saved gather), judge, check, store. Saves everything it saw.
    With a beat: plan, run the plan, judge, reflect, run the follow-ups, judge those. The first pass on
    a beat is a survey that maps it (newsroom/beatmap.py); later passes focus on one or two parts of the
    map, plus a sweep for breaking news, and skip anything the beat already judged."""
    out = run_dir()
    beat_name, beat_text = load_beat(beat) if beat else ("", None)
    trace, plan_, reflection, bmap, focus_leaves, map_note = [], None, None, None, [], ""
    if replay:
        candidates, errors = json.loads(Path(replay).read_text(encoding="utf-8")), {}
        say(f"replaying {len(candidates)} saved candidates from {replay}")
    elif beat_text:
        bmap = beatmap.load(beat_name)
        if bmap and not beatmap.active(bmap):
            bmap = None                      # every part was pruned (or the map came back empty): survey again
        if bmap:
            focus_leaves = beatmap.pick(bmap, 2)
            sweep = [{**s, "leaf": "sweep"} for s in bmap.get("sweep", [])]
            focus = FOCUS.format(sweep="; ".join(f"{s['tool']}({s['arg']!r})" for s in sweep) or "none",
                                 leaves=beatmap.describe(focus_leaves))
            say(f"beat {beat_name}: focus on " + " | ".join(" > ".join(x["path"]) for x in focus_leaves))
        else:
            sweep, focus = [], SURVEY
            say(f"beat {beat_name}: no map yet, so this pass is a survey")
        plan_ = plan(beat_text, max_calls=max_calls, previous=previous, focus=focus)
        plan_["focus"] = [{"id": x["id"], "path": x["path"]} for x in focus_leaves]
        (out / "plan.json").write_text(json.dumps(plan_, indent=1, ensure_ascii=False), encoding="utf-8")
        say(f"planned {len(plan_['steps'])} calls, plus {len(sweep)} sweep calls")
        steps = sweep + plan_["steps"] + [{"tool": s, "arg": "", "why": "requested with --sources"} for s in sources or []]
        known: dict = {}
        gathered, trace = run_steps(steps, round_no=1, known=known)
        candidates = drop_judged(beat_name, gathered, trace)
        errors = {f"step {t['step']}": t["error"] for t in trace if t["error"]}
        say(f"gathered {len(gathered)} candidates, {len(gathered) - len(candidates)} already judged on earlier passes"
            + (f"; failed: {errors}" if errors else ""))
        if bmap is None:
            bmap = beatmap.build(beat_name, _system(beat_text), _tool_listing(), gathered)
            map_note = f"mapped the beat into {len(bmap['leaves'])} parts ({beatmap.path(beat_name)})"
            say(map_note)
    else:
        candidates, errors = gather(sources)
        say(f"gathered {len(candidates)} candidates" + (f"; failed sources: {errors}" if errors else ""))

    decisions = judge(candidates, beat_text)
    consolidate(decisions)

    if plan_ is not None:
        _outcomes(trace, candidates, decisions)
        reflection = reflect(beat_text, trace, decisions["signals"], max_follow=max(MAX_FOLLOW_UPS, max_calls // 5),
                             map_parts="\n".join(" > ".join(x["path"]) for x in beatmap.active(bmap)) if bmap else "")
        say(f"reflected: {len(reflection['dead_ends'])} dead ends, {len(reflection['follow_ups'])} follow-ups")
        if reflection["follow_ups"]:
            more, trace2 = run_steps(reflection["follow_ups"], round_no=2, first=len(trace) + 1, known=known)
            more = drop_judged(beat_name, more, trace2)
            say(f"follow-ups gathered {len(more)} new candidates")
            if more:
                more_decisions = judge(more, beat_text)
                decisions["signals"] += more_decisions["signals"]
                decisions["skipped"] += more_decisions["skipped"]
                consolidate(decisions)
            candidates += more
            trace += trace2
            _outcomes(trace, candidates, decisions)
        mark_judged(beat_name, candidates, decisions)
        if bmap is not None:
            beatmap.record_visits(bmap, focus_leaves, trace)
            added = beatmap.add(bmap, reflection.get("new_leaves", []))
            beatmap.save(beat_name, bmap)
            pruned = [" > ".join(x["path"]) for x in focus_leaves if x["status"] == "pruned"]
            reflection["map"] = {"note": map_note, "focus": [" > ".join(x["path"]) for x in focus_leaves],
                                 "added": [" > ".join(x["path"]) for x in added], "pruned": pruned,
                                 "active_parts": len(beatmap.active(bmap))}
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
        if match := same_event(store, signal):
            # Same event already stored: take its origin, so the store merges this into it.
            signal.origin = match.origin
        sid, created = store.add(signal)
        (report["created"] if created else report["merged"]).append({"id": sid, "summary": signal.summary})
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if reflection:
        report["reflection"] = reflection
        report["trace"] = trace
    return report
