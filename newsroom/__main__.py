"""Command line: python -m newsroom <command>. Run with --help for the list."""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import openai

from . import claims, config, count, db, hunt, llm, record_signals, reporter, signals, tryit, web
from .bossman import run_dir
from .sources import bluesky, caers, cpsc, faers, fda_recalls, maude, nhtsa, osha

SOURCES = ["nhtsa", "maude", "faers", "caers", "osha", "bluesky"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="newsroom", description="AI investigative newsroom")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest-nhtsa", help="step 1: download and store NHTSA vehicle complaints")
    p.add_argument("--period", default="2025-2026", help='5-year file such as "2025-2026", or "all"')
    p.add_argument("--file", type=Path, help="use an already-downloaded zip instead of downloading")

    p = sub.add_parser("ingest-maude", help="step 1: fetch FDA MAUDE device reports (read by the model)")
    p.add_argument("--since", type=date.fromisoformat, required=True)
    p.add_argument("--until", type=date.fromisoformat, default=date.today())
    p.add_argument("--product-code", help="FDA product code, e.g. FRN for infusion pumps")
    p.add_argument("--limit", type=int, default=5000)

    p = sub.add_parser("ingest-faers", help="step 1: fetch FDA drug adverse-event reports (coded, no tokens)")
    p.add_argument("--since", type=date.fromisoformat, required=True)
    p.add_argument("--until", type=date.fromisoformat, default=date.today())
    p.add_argument("--drug", help="brand name, e.g. OZEMPIC")
    p.add_argument("--include-non-serious", action="store_true")
    p.add_argument("--limit", type=int, default=5000)

    p = sub.add_parser("ingest-caers", help="step 1: fetch FDA food/supplement/cosmetic reports (coded, no tokens)")
    p.add_argument("--since", type=date.fromisoformat, required=True)
    p.add_argument("--until", type=date.fromisoformat, default=date.today())
    p.add_argument("--limit", type=int, default=5000)

    p = sub.add_parser("ingest-osha", help="step 1: download OSHA severe injury reports (coded, no tokens)")
    p.add_argument("--file", type=Path, help="use an already-downloaded zip instead of downloading")

    p = sub.add_parser("ingest-bluesky", help="step 1: search Bluesky for product problems (read by the model)")
    p.add_argument("--query", action="append", help=f"repeatable; default: {', '.join(bluesky.DEFAULT_QUERIES)}")
    p.add_argument("--days", type=int, default=30, help="how far back to search")
    p.add_argument("--limit", type=int, default=300, help="posts per query")

    p = sub.add_parser("ingest-investigations", help="download NHTSA defect investigations, for the scouts (no tokens)")
    p.add_argument("--file", type=Path, help="use an already-downloaded zip instead of downloading")

    p = sub.add_parser("ingest-recalls", help="download NHTSA, FDA and CPSC recalls (no tokens)")
    p.add_argument("--since", type=date.fromisoformat, default=date(2025, 1, 1), help="recalls dated on or after this")
    p.add_argument("--nhtsa-file", type=Path, help="use an already-downloaded FLAT_RCL_POST_2010.zip")
    p.add_argument("--only", choices=["nhtsa", "fda", "cpsc"], action="append", help="repeatable; default: all three")

    p = sub.add_parser("record-signals", help="write what is moving in the official records to the signals store "
                                              "(spikes in reports, new recalls and investigations; no tokens)")
    p.add_argument("--dataset", choices=record_signals.DATASETS, action="append", help="repeatable; default: all")
    p.add_argument("--max-per-dataset", type=int, default=record_signals.MAX_PER_DATASET)
    p.add_argument("--dry-run", action="store_true", help="build and check the signals, save them under runs/records/, "
                                                           "but don't write to the store")

    p = sub.add_parser("claims", help="step 2: read complaints and write down their claims")
    p.add_argument("--limit", type=int, help="read at most this many complaints (useful to test cost first)")
    p.add_argument("--workers", type=int, default=4)

    p = sub.add_parser("count", help="step 3: group the claims and count distinct people")
    p.add_argument("--as-of", type=date.fromisoformat, help="only complaints received by this date")

    p = sub.add_parser("top", help="show the biggest claim groups")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--as-of", type=date.fromisoformat)
    p.add_argument("--source", choices=SOURCES)

    p = sub.add_parser("leads", help="the reporter reads the top claim groups and says which are worth a story "
                                     "(one model call, no searches)")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--source", choices=SOURCES)

    p = sub.add_parser("hunt", help="depth-first: scan one product at a time and investigate each lead as soon as "
                                    "it's found (reads complaints only as needed)")
    p.add_argument("--stories", type=int, default=1, help="stop after investigating this many leads")
    p.add_argument("--source", choices=SOURCES, help="only scan this source")
    p.add_argument("--max-products", type=int, default=50, help="stop after scanning this many products")

    p = sub.add_parser("run", help="steps 4-8: pick claims, send scouts, decide, write")
    p.add_argument("--stories", type=int, default=1)
    p.add_argument("--source", choices=SOURCES, help="only pick claims from this source")

    p = sub.add_parser("show", help="the full trail of one story")
    p.add_argument("story_id", type=int)

    sub.add_parser("stories", help="list stories and their status")
    sub.add_parser("usage", help="DeepSeek tokens used so far")

    tryit.add_parser(sub)

    args = parser.parse_args(argv)
    if args.command == "try":
        sys.stdout.reconfigure(encoding="utf-8")
        return tryit.main(args)
    settings = config.load()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows terminals otherwise garble curly quotes
    with db.session() as conn:
        if args.command == "ingest-nhtsa":
            path = args.file or nhtsa.download(args.period, settings.db_path.parent / "raw")
            print(f"stored {db.save_complaints(conn, nhtsa.parse(path))} new complaints from {path.name}")
        elif args.command == "ingest-maude":
            reports = maude.fetch(args.since, args.until, product_code=args.product_code, limit=args.limit)
            print(f"stored {db.save_complaints(conn, reports)} new device reports")
        elif args.command == "ingest-faers":
            reports = faers.fetch(args.since, args.until, drug=args.drug, serious_only=not args.include_non_serious,
                                  limit=args.limit)
            print(f"stored {db.save_complaints(conn, reports)} new drug reactions (one per reaction per report)")
        elif args.command == "ingest-caers":
            reports = caers.fetch(args.since, args.until, limit=args.limit)
            print(f"stored {db.save_complaints(conn, reports)} new food/supplement/cosmetic reactions")
        elif args.command == "ingest-osha":
            path = args.file or osha.download(settings.db_path.parent / "raw")
            print(f"stored {db.save_complaints(conn, osha.parse(path))} new severe injury reports from {path.name}")
        elif args.command == "ingest-bluesky":
            since = date.today() - timedelta(days=args.days)
            read, stored = bluesky.ingest(conn, args.query or bluesky.DEFAULT_QUERIES, since=since, limit=args.limit)
            print(f"read {read} new posts; stored {stored} that report a product problem")
        elif args.command == "ingest-investigations":
            path = args.file or nhtsa.download_investigations(settings.db_path.parent / "raw")
            investigations, vehicles = nhtsa.save_investigations(conn, path)
            print(f"stored {investigations} NHTSA investigations covering {vehicles} vehicles")
        elif args.command == "ingest-recalls":
            loaders = {
                "nhtsa": lambda: nhtsa.parse_recalls(args.nhtsa_file or nhtsa.download_recalls(settings.db_path.parent / "raw"),
                                                     since=args.since.isoformat()),
                "fda": lambda: fda_recalls.fetch(args.since, date.today()),
                "cpsc": lambda: cpsc.fetch(args.since),
            }
            for name in args.only or loaders:
                try:
                    print(f"stored {db.save_recalls(conn, loaders[name]())} {name} recalls since {args.since}")
                except httpx.HTTPError as error:  # one agency being down shouldn't lose the others
                    print(f"{name} recalls failed ({error}); run again with --only {name}")
        elif args.command == "record-signals":
            store = None if args.dry_run else signals.get_store()
            out = run_dir("records")
            report = record_signals.run(conn, store, args.dataset, max_per_dataset=args.max_per_dataset, out_dir=out)
            for slug, r in report.items():
                written = "" if args.dry_run else f", {r['created']} new, {r['merged']} merged into existing"
                print(f"{slug:<22} {r['built']:>3} signals{written}  ({r['note']}"
                      + (f"; data to {r['as_of']})" if r["as_of"] else ")"))
                for f in r["failed_checks"]:
                    print(f"    failed checks: {f['summary'][:80]}: {'; '.join(f['problems'])}")
            print(f"saved {out}/")
        elif args.command == "claims":
            done = claims.extract(conn, min_complaints=settings.min_complaints, limit=args.limit, workers=args.workers,
                                  progress=lambda d, t: print(f"\r{d}/{t} complaints read", end="", flush=True))
            print(f"\nwrote claims for {done} complaints")
        elif args.command == "count":
            as_of, n = count.count(conn, as_of=args.as_of)
            print(f"{n} claim groups as of {as_of}" if as_of else "no complaints yet: run an ingest command first")
        elif args.command == "top":
            for g in count.top(conn, args.n, as_of=args.as_of, source=args.source):
                print(f"{g['total']:>6} people  {g['last_90']:>5} last 90d  {g['severe']:>5} severe  "
                      f"[{g['source']}] {g['product']}: {g['label']}")
        elif args.command == "leads":
            leads = reporter.rank(conn, count.top(conn, args.n, source=args.source, skip_reported=True))
            for lead in (lead for lead in leads if lead["worth"]):
                g = lead["group"]
                print(f"{g['total']:>6} people  [{g['source']}] {g['product']}: {g['label']}\n        {lead['reason']}")
            print(f"\n{sum(not lead['worth'] for lead in leads)} more groups judged not worth a story")
        elif args.command == "hunt":
            hunt.hunt(conn, stories=args.stories, source=args.source, max_products=args.max_products,
                      say=lambda line: print(line, flush=True))
        elif args.command == "run":
            for story_id in reporter.run(conn, args.stories, source=args.source):
                story = conn.execute("select status, note, article from stories where id = ?", (story_id,)).fetchone()
                print(f"story {story_id}: {story['status']}  {story['article'] or story['note'] or ''}")
        elif args.command == "stories":
            for s in conn.execute("select id, status, product, label from stories order by id"):
                print(f"{s['id']:>4}  {s['status']:<10} {s['product']}: {s['label']}")
        elif args.command == "show":
            show(conn, args.story_id)
        elif args.command == "usage":
            for u in conn.execute("select model, count(*) calls, sum(prompt_tokens) prompt, sum(completion_tokens) completion"
                                  " from llm_usage group by model"):
                print(f"{u['model']}: {u['calls']} calls, {u['prompt']} prompt tokens, {u['completion']} completion tokens")


def show(conn, story_id: int) -> None:
    story = conn.execute("select * from stories where id = ?", (story_id,)).fetchone()
    if story is None:
        sys.exit(f"no story {story_id}")
    counts = json.loads(story["counts"])
    print(f"Story {story_id}: {story['status']}\n{story['product']} ({story['company']}): {story['label']}")
    print(f"{counts['total']} people ({counts['last_90']} in last 90 days, {counts['severe']} severe), as of {counts['as_of']}")
    if story["angle"]:
        print(f"Angle: {story['angle']}")
    for h in conn.execute("select * from hypotheses where story_id = ? order by n", (story_id,)):
        print(f"\nH{h['n']}: {h['statement']}")
        for f in conn.execute("select * from findings where hypothesis_id = ?", (h["id"],)):
            print(f"  [{f['finding']}] {f['source_type']}: \"{f['quote'][:160]}\"\n      {f['url']}")
    print("\nLog:")
    for e in conn.execute("select * from events where story_id = ? order by id", (story_id,)):
        print(f"  {e['at']}  {e['actor']}: {e['action']}  {e['reason']}")
    if story["article"]:
        print(f"\nArticle: {story['article']}")
    elif story["note"]:
        print(f"\nNote: {story['note']}")


if __name__ == "__main__":
    try:
        main()
    except (llm.LLMError, web.SearchError) as error:
        sys.exit(f"error: {error}")
    except (openai.APIConnectionError, httpx.TransportError) as error:
        sys.exit(f"network error ({error}): check your connection and run the same command again; "
                 "finished work is saved, so it picks up where it stopped")
