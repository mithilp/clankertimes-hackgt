"""`python -m newsroom try <agent>`: run one agent on its own and see exactly what it did.

Every run saves its inputs and outputs under runs/<agent>/<timestamp>/, so it can be replayed.
How-tos for each agent live in agents/<agent>/RUN.md.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bossman, config, council, db, mclovin, reporter, reporter_agent
from .bossman import run_dir
from .signals import get_store


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
    src.add_argument("--from-mclovin", metavar="HYPOTHESES_JSON",
                     help="a runs/mclovin/<ts>/hypotheses.json, or 'latest' for the newest McLovin run")
    src.add_argument("--story", type=int, help="rerun an existing story from the complaint pipeline")
    r.add_argument("--pick", type=int, default=1, help="with --from-mclovin: which hypothesis (1-based)")
    r.add_argument("--all", action="store_true", help="with --from-mclovin: work every hypothesis that passed its checks")
    r.add_argument("--budget", type=int, default=12, help="searches plus page reads per scout, per assignment")
    r.add_argument("--rounds", type=int, default=reporter_agent.MAX_ROUNDS, help="scout rounds before the verdict")
    r.add_argument("--judges", default="skeptic,virality,novelty", help="council judges; empty to skip the council")
    r.add_argument("--no-write", action="store_true", help="stop at the verdict; don't draft")
    r.add_argument("--force", action="store_true", help="work a hypothesis again even if it was already killed/parked/published")

    s = agents.add_parser("scout", help="research one hypothesis and bring back quoted findings")
    s.add_argument("--hypothesis", required=True)
    s.add_argument("--context", default="", help="a few words naming the product, company or agency")
    s.add_argument("--budget", type=int, default=12)

    a = agents.add_parser("articles", help="list published articles, or load/remove the website's sample articles")
    a.add_argument("--seed-samples", action="store_true", help="load web/data/sample-articles.json into Astra, marked sample")
    a.add_argument("--remove-samples", action="store_true", help="delete every sample article from Astra")

    c = agents.add_parser("council", help="judge a draft, or measure the council with seeded errors")
    c.add_argument("--draft", type=Path, help="draft JSON (default: tests/fixtures/council_draft.json)")
    c.add_argument("--judges", default="skeptic,virality,novelty")
    c.add_argument("--seeded", action="store_true", help="plant known errors and report what gets caught")
    c.add_argument("--no-web", action="store_true", help="novelty judge skips the prior-coverage search")

    d = agents.add_parser("desk", help="what the reporter has worked: the desk (Astra DB when configured)")
    d.add_argument("--limit", type=int, default=20)
    d.add_argument("--status", help="only this status: reporting, published, held, killed, parked, spiked, failed")
    d.add_argument("--show", metavar="DESK_ID", help="print one investigation in full, as JSON")


def main(args: argparse.Namespace) -> None:
    settings = config.load()
    print(f"[{args.agent}] model: {settings.llm_provider} ({settings.fast_model} / {settings.smart_model})"
          f" · search: {settings.search_backend}")
    {"bossman": _bossman, "mclovin": _mclovin, "reporter": _reporter, "scout": _scout,
     "council": _council, "articles": _articles, "desk": _desk}[args.agent](args)


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
        path = _latest_mclovin() if args.from_mclovin == "latest" else Path(args.from_mclovin)
        hyps = json.loads(path.read_text(encoding="utf-8"))["hypotheses"]
        if args.all:
            todo = [h for h in hyps if not h.get("problems")]
            print(f"{len(todo)} of {len(hyps)} hypotheses in {path} passed McLovin's checks")
        else:
            if not 1 <= args.pick <= len(hyps):
                raise SystemExit(f"--pick {args.pick}: {path} has {len(hyps)} hypotheses")
            todo = [hyps[args.pick - 1]]
    else:
        todo = [args.hypothesis]
    judges = tuple(j.strip() for j in args.judges.split(",") if j.strip())

    for n, h in enumerate(todo, 1):
        text = h if isinstance(h, str) else h["hypothesis"]
        print(f"\n{'=' * 100}\n[{n}/{len(todo)}] hypothesis: {text}")
        result = reporter_agent.investigate(h, budget=args.budget, rounds=args.rounds, write=not args.no_write,
                                            judges=judges, force=args.force)
        if result.get("skipped"):
            continue
        _print_investigation(result)


def _latest_mclovin() -> Path:
    runs = sorted(Path("runs/mclovin").glob("*/hypotheses.json"))
    if not runs:
        raise SystemExit("no McLovin runs under runs/mclovin/: run `try mclovin` first, or pass a file")
    return runs[-1]


def _print_investigation(result: dict) -> None:
    sizing = result.get("sizing") or {}
    print(f"\n{sizing.get('count', len(result['sub_hypotheses']))} scouts for {sizing.get('elements', '?')} elements"
          f" ({sizing.get('load_bearing', '?')} load-bearing), {sizing.get('mclovin_records', 0)} McLovin records"
          + (f", {len(sizing['deferred'])} deferred" if sizing.get("deferred") else "") + f": {sizing.get('why', '')}")
    print("\nsub-hypotheses:")
    for s in result["sub_hypotheses"]:
        flag = " (dropped)" if s.get("dropped") else ""
        print(f"  {s['id']} {s['status'].upper():<12} [{s['needed_for']}, {s['priority']}, sent {s['dispatches']}x]"
              f" {s['statement']}{flag}")
        for f in [f for f in s["findings"] if f["finding"] != "unclear"][:3]:
            print(f"      [{f['finding']}] {f['source_type']}  {f['url']}\n         \"{f['quote'][:160]}\"")
    v = result["verdict"]
    memo = v.get("memo") or {}
    if v["verdict"] != "write" and memo:
        print(f"\nmemo\n  checked: {memo.get('checked', '')}\n  found: {memo.get('found', '')}"
              f"\n  would change it: {memo.get('would_change_it', '')}"
              + (f"\n  wake when: {memo['wake_condition']}" if memo.get("wake_condition") else ""))
    for s in result.get("spinoffs", []):
        print(f"\nspin-off for McLovin: {s['hypothesis']}  ({s['why']})")
    print(f"\nstory {result['story_id']}: {result['final']['status'].upper()}  {result['final'].get('article') or result['final'].get('note', '')}")
    if result.get("checks"):
        print("rubric flags: " + "; ".join(result["checks"]))


def _desk(args) -> None:
    from .desk import get_desk
    desk = get_desk()
    print(f"desk: {type(desk).__name__}")
    if args.show:
        record = desk.get(args.show)
        print(json.dumps(record, indent=1, ensure_ascii=False, default=str) if record else f"no desk record {args.show}")
        return
    rows = desk.recent(limit=args.limit, status=args.status)
    for r in rows:
        subs = r.get("sub_hypotheses") or []
        held = sum(s.get("status") in ("supported", "disputed") for s in subs)
        print(f"{r.get('updated_at', '')[:16]}  {r.get('status', ''):<10} {r.get('stage', ''):<20} "
              f"{held}/{len(subs)} supported  {r['_id']}\n    {r.get('hypothesis', '')[:110]}")
    if not rows:
        print("nothing on the desk yet: run `try reporter` first")


# --- articles ------------------------------------------------------------------------------------

def _articles(args) -> None:
    from . import articles_store
    if not articles_store.configured():
        raise SystemExit("Astra isn't configured in .env; the website shows web/data/sample-articles.json instead.")
    store = articles_store.AstraArticles()
    if args.seed_samples:
        print(f"loaded {store.seed_samples()} sample articles into the {store.name} collection")
    if args.remove_samples:
        print(f"removed {store.remove_samples()} sample articles")
    for doc in store.list():
        tag = " [sample]" if doc.get("sample") else ""
        print(f"{doc.get('published_at', '')[:10]}  {doc.get('status', ''):<9} {', '.join(doc.get('beats', [])):<14} "
              f"{doc['headline'][:80]}{tag}")


# --- scout -------------------------------------------------------------------------------------

def _scout(args) -> None:
    from . import scout_agent
    task = {"id": "H1", "statement": args.hypothesis, "assignment": {}}
    out = scout_agent.run(task, budget=args.budget, context=args.context, say=print)
    findings, report = out["findings"], out["report"]
    print()
    for f in findings:
        print(f"[{f.get('finding')}] {f.get('source_type', '')}  {f.get('url', '')}\n   \"{str(f.get('quote', ''))[:200]}\""
              + (f"\n   {f['note']}" if f.get("note") else ""))
    tally = {k: sum(f.get("finding") == k for f in findings) for k in ("supports", "contradicts", "unclear")}
    print(f"\n{len(findings)} findings: {tally} in {out['used']} calls")
    print(f"REPORT: {report.get('verdict', '').upper()} ({report.get('confidence', '?')})  {report.get('summary', '')}"
          + (f"\n  held back: {report['held_back']}" if report.get("held_back") else "")
          + "".join(f"\n  not found: {x}" for x in report.get("not_found", []))
          + "".join(f"\n  proposes: {p.get('hypothesis')}" for p in report.get("proposals", []))
          + (f"\n  next check: {report['next_check']}" if report.get("next_check") else ""))
    print(f"saved {_save('scout', 'findings.json', {'hypothesis': args.hypothesis, **out})}")


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
