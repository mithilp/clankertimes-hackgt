"""`python -m newsroom try <agent>`: run one agent on its own and see exactly what it did.

Every run saves its inputs and outputs under runs/<agent>/<timestamp>/, so it can be replayed.
How-tos for each agent live in agents/<agent>/RUN.md.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bossman, config, council, db, llm, mclovin, playbooks, reporter, scout
from .bossman import run_dir
from .signals import get_store

SUBHYPOTHESES = """
# Your output

You are given one hypothesis. Break it into 3-5 plain sub-hypotheses that would ALL have to be true for
the story to hold, following the playbook above. Each must be checkable on the public web. Also give
the minimum story (worth publishing even if the full hypothesis fails) and the maximum story.

Reply with JSON only:
{"sub_hypotheses": ["...", "..."], "context": "a few words naming the product, company or agency",
 "minimum_story": "...", "maximum_story": "..."}
"""


def add_parser(sub) -> None:
    p = sub.add_parser("try", help="run one agent on its own (see agents/<agent>/RUN.md)")
    agents = p.add_subparsers(dest="agent", required=True)

    b = agents.add_parser("bossman", help="gather live candidates and decide which become signals")
    b.add_argument("--beat", help="focus on a beat: a name under agents/bossman/beats/ (e.g. georgia-tech) or a .md path")
    b.add_argument("--sources", help="comma-separated; default all: google_trends,google_news,bluesky,reddit,polymarket,gov,hacker_news."
                   " With --beat, these national feeds are added to the planned calls")
    b.add_argument("--replay", type=Path, help="rerun the judge on a saved runs/bossman/<ts>/candidates.json")
    b.add_argument("--gather-only", action="store_true", help="fetch and save candidates; no model calls")
    b.add_argument("--loop", type=float, metavar="MINUTES", help="keep running a pass every N minutes")
    b.add_argument("--passes", type=int, help="with --loop: stop after this many passes")
    b.add_argument("--calls", type=int, default=40, help="with --beat: a ceiling on calls per pass; the plan picks how many (default 40)")

    m = agents.add_parser("mclovin", help="read signals and write falsifiable hypotheses")
    m.add_argument("--hours", type=float, default=24, help="read signals last seen in the past N hours")
    m.add_argument("--input", type=Path, help="read signals from a saved JSON list instead of the store")
    m.add_argument("--no-mark", action="store_true", help="don't mark the signals as used")

    r = agents.add_parser("reporter", help="work one hypothesis (or an existing story) with scouts")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--hypothesis", help="a hypothesis to work")
    src.add_argument("--from-mclovin", type=Path, metavar="HYPOTHESES_JSON", help="a runs/mclovin/<ts>/hypotheses.json")
    src.add_argument("--story", type=int, help="rerun an existing story from the complaint pipeline")
    r.add_argument("--pick", type=int, default=1, help="with --from-mclovin: which hypothesis (1-based)")
    r.add_argument("--budget", type=int, default=12, help="searches plus page reads per scout")

    s = agents.add_parser("scout", help="research one hypothesis and bring back quoted findings")
    s.add_argument("--hypothesis", required=True)
    s.add_argument("--context", default="", help="a few words naming the product, company or agency")
    s.add_argument("--budget", type=int, default=12)

    c = agents.add_parser("council", help="judge a draft, or measure the council with seeded errors")
    c.add_argument("--draft", type=Path, help="draft JSON (default: tests/fixtures/council_draft.json)")
    c.add_argument("--judges", default="skeptic,virality,novelty")
    c.add_argument("--seeded", action="store_true", help="plant known errors and report what gets caught")
    c.add_argument("--no-web", action="store_true", help="novelty judge skips the prior-coverage search")


def main(args: argparse.Namespace) -> None:
    settings = config.load()
    print(f"[{args.agent}] model: {settings.llm_provider} ({settings.fast_model} / {settings.smart_model})"
          f" · search: {settings.search_backend}")
    {"bossman": _bossman, "mclovin": _mclovin, "reporter": _reporter, "scout": _scout,
     "council": _council}[args.agent](args)


def _save(kind: str, name: str, data) -> Path:
    path = run_dir(kind) / name
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path


# --- bossman -----------------------------------------------------------------------------------

def _bossman(args) -> None:
    sources = args.sources.split(",") if args.sources else None
    if args.gather_only:
        candidates, errors = bossman.gather(sources)
        path = _save("bossman", "candidates.json", candidates)
        by_type: dict[str, int] = {}
        for c in candidates:
            by_type[c["source_type"]] = by_type.get(c["source_type"], 0) + 1
        print(f"gathered {len(candidates)} candidates: {by_type}" + (f"\nfailed: {errors}" if errors else ""))
        print(f"saved {path}  (judge them later with --replay {path})")
        return
    store = get_store()
    previous, n, quiet = None, 0, 0
    while True:
        n += 1
        if args.loop:
            print(f"\n=== pass {n} · {datetime.now().astimezone():%a %H:%M %Z} ===")
        try:
            report = bossman.run_once(store, sources=sources, replay=args.replay, beat=args.beat,
                                      max_calls=args.calls, previous=previous)
        except Exception as e:  # noqa: BLE001 - in a long run, one bad pass shouldn't end the loop
            if not args.loop:
                raise
            print(f"pass {n} failed: {type(e).__name__}: {e}")
            report = None
        if report is not None:
            _print_beat_pass(report)
            if report.get("reflection"):
                previous = {"reflection": report["reflection"], "trace": report.get("trace"),
                            "when": f"{datetime.now().astimezone():%H:%M %Z}",
                            "stored": [s["summary"] for s in report["created"] + report["merged"]]}
            print(f"\n{report['candidates']} candidates -> {len(report['created'])} new signals, "
                  f"{len(report['merged'])} merged into existing, {report['skipped']} skipped, "
                  f"{len(report['failed_checks'])} failed checks")
            for s in report["created"]:
                print(f"  + {s['summary']}")
            for s in report["merged"]:
                print(f"  = {s['summary']}")
            for f in report["failed_checks"]:
                print(f"  ✗ {f['summary'] or '(no summary)'}: {'; '.join(f['problems'])}")
            print(f"saved {report['run_dir']}/")
        if not args.loop or args.replay or (args.passes and n >= args.passes):
            return
        # A pass that stores nothing new doubles the wait, up to an hour; a new signal resets it.
        quiet = 0 if report and report["created"] else quiet + 1
        wait = min(args.loop * 2 ** quiet, max(args.loop, 60))
        print(f"next pass in {wait:g} minutes" + (f" ({quiet} quiet pass{'es' if quiet > 1 else ''} in a row)" if quiet else "")
              + " (ctrl-c to stop)")
        time.sleep(wait * 60)


def _print_beat_pass(report: dict) -> None:
    run = Path(report["run_dir"])
    if not (run / "trace.json").exists():
        return
    print("\ncalls:")
    for t in json.loads((run / "trace.json").read_text(encoding="utf-8")):
        tag = "follow-up " if t["round"] == 2 else ""
        result = f"ERROR {t['error'][:80]}" if t["error"] else (
            f"{t['returned']} found, {t['new']} new, {t.get('already_judged', 0)} judged before, {t['kept']} kept")
        leaf = f"[{t['leaf']}] " if t.get("leaf") else ""
        print(f"  {t['step']:>2}. {tag}{leaf}{t['tool']}({t['arg']!r}): {result}")
    r = report.get("reflection") or {}
    m = r.get("map") or {}
    if m.get("note"):
        print(f"\nmap: {m['note']}")
    if m.get("focus"):
        print(f"\nfocus this pass: {' | '.join(m['focus'])}")
    for x in m.get("added", []):
        print(f"  map + {x}")
    for x in m.get("pruned", []):
        print(f"  map - {x} (came up empty too many visits in a row)")
    if r.get("assessment"):
        print(f"\nlooking back: {r['assessment']}")
    for d in r.get("dead_ends", []):
        print(f"  dead end, step {d.get('step')}: {d.get('why', '')}")
    for n in r.get("beat_notes", []):
        print(f"  beat note: {n}")


# --- mclovin -----------------------------------------------------------------------------------

def _mclovin(args) -> None:
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat(timespec="seconds")
    result = mclovin.run(get_store(), since=since, signals_file=args.input, mark=not args.no_mark)
    for n, h in enumerate(result["hypotheses"], 1):
        ev = h["evidence_so_far"]
        flag = "✗ " + "; ".join(h["problems"]) if h["problems"] else "ok"
        print(f"\n{n}. {h['hypothesis']}\n   accountable: {h['accountable_party']}  ·  "
              f"{ev['distinct_origins']} distinct origins across {ev['sources']} sources  ·  {flag}"
              f"\n   settle with: {'; '.join(h['would_settle_it'])}")
    for w in result.get("watching", []):
        print(f"\n   watching {', '.join(w.get('signal_ids', []))}: {w.get('why_not_yet', '')}")
    if not result["hypotheses"]:
        print("no hypotheses this pass")
    print(f"\nsaved {result['run_dir']}/")


# --- reporter ----------------------------------------------------------------------------------

def _reporter(args) -> None:
    if args.story:
        reporter.report(args.story)
        with db.session() as conn:
            s = conn.execute("select status, note, article from stories where id = ?", (args.story,)).fetchone()
        print(f"story {args.story}: {s['status']}  {s['article'] or s['note'] or ''}"
              f"\nfull trail: python -m newsroom show {args.story}")
        return
    if args.from_mclovin:
        hyps = json.loads(args.from_mclovin.read_text(encoding="utf-8"))["hypotheses"]
        hypothesis = hyps[args.pick - 1]["hypothesis"]
    else:
        hypothesis = args.hypothesis
    print(f"hypothesis: {hypothesis}")

    plan = llm.ask_json(playbooks.load("reporter") + "\n" + SUBHYPOTHESES, hypothesis,
                        model=config.load().smart_model, max_tokens=2000)
    subs = [s for s in plan.get("sub_hypotheses", []) if isinstance(s, str) and s.strip()][:5]
    context = plan.get("context", "")
    print(f"minimum story: {plan.get('minimum_story', '')}\nmaximum story: {plan.get('maximum_story', '')}")
    for n, s in enumerate(subs, 1):
        print(f"  H{n}. {s}")

    with ThreadPoolExecutor(max_workers=min(3, len(subs) or 1)) as pool:
        findings = list(pool.map(lambda h: scout.research(h, context, args.budget), subs))
    verdict, why = reporter.decide(findings)

    for n, (s, fs) in enumerate(zip(subs, findings), 1):
        tally = {k: sum(f.get("finding") == k for f in fs) for k in ("supports", "contradicts", "unclear")}
        print(f"\nH{n}: {tally}")
        for f in fs[:4]:
            print(f"   [{f.get('finding')}] {f.get('url', '')}\n      \"{str(f.get('quote', ''))[:160]}\"")
    print(f"\nVERDICT: {verdict}  —  {why}")
    path = _save("reporter", "result.json", {"hypothesis": hypothesis, "plan": plan, "sub_hypotheses": subs,
                                              "findings": findings, "verdict": verdict, "why": why})
    print(f"saved {path}")


# --- scout -------------------------------------------------------------------------------------

def _scout(args) -> None:
    findings = scout.research(args.hypothesis, args.context, args.budget)
    for f in findings:
        print(f"[{f.get('finding')}] {f.get('source_type', '')}  {f.get('url', '')}\n   \"{str(f.get('quote', ''))[:200]}\""
              + (f"\n   {f['note']}" if f.get("note") else ""))
    tally = {k: sum(f.get("finding") == k for f in findings) for k in ("supports", "contradicts", "unclear")}
    print(f"\n{len(findings)} findings: {tally}")
    print(f"saved {_save('scout', 'findings.json', {'hypothesis': args.hypothesis, 'findings': findings})}")


# --- council -----------------------------------------------------------------------------------

def _council(args) -> None:
    judges = tuple(j.strip() for j in args.judges.split(",") if j.strip())
    draft = council.load_draft(args.draft)
    if args.seeded:
        seeded_judges = tuple(j for j in judges if j == "skeptic") or ("skeptic",)
        print(f"planting {len(council.MUTATIONS)} known errors into the draft; judges: {', '.join(seeded_judges)}")
        result = council.seeded(draft, judges=seeded_judges)
        mech = sum(r["caught_by"] == "mechanical" for r in result["rows"])
        print(f"\ncaught {result['caught']}/{result['total']} ({result['rate']:.0%}): "
              f"{mech} by mechanical checks, {result['caught'] - mech} by judges")
        print(f"saved {_save('council', 'seeded.json', result)}")
        return
    result = council.review(draft, judges, web_search=not args.no_web)
    if result["stage"] == "mechanical":
        print("blocked by mechanical checks before the council:")
        for p in result["mechanical"]:
            print(f"  ✗ {p}")
    for j in result["judges"]:
        print(f"\n{j['judge']}: {j['verdict'].upper()}")
        for p in j["problems"]:
            print(f"  • \"{str(p.get('sentence', ''))[:120]}\"\n    {p.get('issue', '')}\n    fix: {p.get('fix', '')}")
        if j["notes"]:
            print(f"  notes: {j['notes']}")
    print(f"\nCOUNCIL: {result['verdict'].upper()}")
    print(f"saved {_save('council', 'review.json', result)}")
