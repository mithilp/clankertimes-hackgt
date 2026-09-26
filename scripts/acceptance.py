"""Acceptance tests for IMPLEMENTATION_PLAN.md steps 1, 2 and 4. No API keys needed.

Runs against a throwaway database (<name>_acceptance) created next to DATABASE_URL's
database and dropped afterwards, so it never touches real newsroom data.

    docker compose up -d db
    docker compose run --rm --entrypoint python editor scripts/acceptance.py
"""

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `newsroom` imports

BASE_URL = os.environ.get("DATABASE_URL", "postgresql://newsroom:newsroom@localhost:5432/newsroom")
parts = urlsplit(BASE_URL)
TEST_DB = parts.path.lstrip("/") + "_acceptance"
TEST_URL = urlunsplit(parts._replace(path="/" + TEST_DB))
os.environ["DATABASE_URL"] = TEST_URL  # before importing newsroom, which reads settings at import
os.environ.setdefault("ARCHIVE_DIR", "/tmp/acceptance-archive")
os.environ["LEASE_SECONDS"] = "6"  # short leases so the lease tests run in seconds

from newsroom import __main__ as cli, db, editor, web  # noqa: E402
from newsroom.scoring import fingerprint  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


async def insert_lead(p, hypothesis: str, score: float):
    return await p.fetchval(
        """insert into leads (scout, beat, hypothesis, why_now, who_would_know, would_settle_it,
                              score, score_components, score_reason, fingerprint)
           values ('test', 'test', $1, 'test trigger', 'purchasing office', 'the contract file',
                   $2, '{}'::jsonb, 'manual test lead', $3)
           on conflict (fingerprint) do nothing returning id""",
        hypothesis, score, fingerprint(hypothesis),
    )


async def step1_state_machine(p) -> None:
    print("Step 1: lead -> story -> claim -> review -> publish")
    lead_id = await insert_lead(p, "Test County paid Vendor A $1M without a bid", 0.80)
    check("create lead", lead_id is not None)

    await editor.triage_leads()
    story = await p.fetchrow("select * from stories where lead_id = $1", lead_id)
    check("editor promotes it and creates a story", story is not None and story["status"] == "assigned")
    check("story gets a budget", await p.fetchval("select 1 from budgets where story_id = $1", story["id"]) == 1)

    a, b = await asyncio.gather(
        db.claim_story(["assigned"], "reporting", "reporter-A"),
        db.claim_story(["assigned"], "reporting", "reporter-B"),
    )
    winners = [r for r in (a, b) if r is not None]
    check("two reporters race; exactly one claims it", len(winners) == 1, f"{len(winners)} claimed")
    owner = winners[0]["lease_owner"]
    other = "reporter-B" if owner == "reporter-A" else "reporter-A"
    check("the loser can't release it", not await db.release(story["id"], other, status="killed"))

    url = "https://example.gov/contracts/123"
    text = "The county approved a $1,000,000 contract with Vendor A. No bids were solicited."
    source_id, _ = await web.archive(url, text, "test")
    await p.execute("insert into claims (story_id, position, text, source_id, quoted_span) values ($1,0,$2,$3,$4)",
                    story["id"], "The county approved a $1 million contract with Vendor A.", source_id,
                    "approved a $1,000,000 contract with Vendor A")
    check("owner moves it to review", await db.release(story["id"], owner, status="in_review"))

    await p.execute("update stories set status = 'approved' where id = $1", story["id"])
    await editor.publish_approved()
    status = await p.fetchval("select status from stories where id = $1", story["id"])
    check("publish gate refuses unverified claims", status == "dormant", status)

    await p.execute("update claims set verified = true where story_id = $1", story["id"])
    await p.execute("update stories set status = 'approved' where id = $1", story["id"])
    await editor.publish_approved()
    row = await p.fetchrow("select status, timeline from stories where id = $1", story["id"])
    check("publishes once verified and quotes found", row["status"] == "published", row["status"])
    check("timeline written", bool(row["timeline"]))


async def lease_renewal(p) -> None:
    print("Leases: a busy worker keeps its story; a dead one loses it")
    lead_id = await insert_lead(p, "Test County paid Vendor C $2M on an expired contract", 0.80)
    await editor.triage_leads()
    sid = await p.fetchval("select id from stories where lead_id = $1", lead_id)
    mine = await db.claim_story(["assigned"], "reporting", "worker-busy")
    check("worker claims the story", mine is not None and mine["id"] == sid)
    async with db.lease_keeper(sid, "worker-busy") as lease:
        await asyncio.sleep(10)  # longer than the 6s lease: e.g. stuck in rate-limit retries
        stolen = await db.claim_story(["assigned"], "reporting", "worker-other")
        check("while renewing, nobody else can take it", stolen is None and lease["held"])
    await asyncio.sleep(8)  # renewer stopped (worker died): lease lapses
    taken = await db.claim_story(["assigned"], "reporting", "worker-other")
    check("after the lease lapses, another worker resumes it", taken is not None and taken["id"] == sid)
    await db.release(sid, "worker-other", status="killed")


async def step2_events(p) -> None:
    print("Step 2: event logging")
    await db.event("test", "hello", "checking the event log", detail={"n": 1})
    row = await p.fetchrow("select * from agent_events where agent = 'test' and action = 'hello'")
    check("event row appears", row is not None and row["reason"] == "checking the event log")
    check("detail stored as json", row is not None and row["detail"] == {"n": 1})


async def step4_triage(p) -> None:
    print("Step 4: editor routes below-threshold, above-threshold and duplicate leads")
    low = await insert_lead(p, "Test City repainted a crosswalk late", 0.30)
    high = await insert_lead(p, "Test City paid Vendor B $3M through an emergency exemption", 0.75)
    dup = await insert_lead(p, "Test city paid vendor B $3M through an emergency exemption!", 0.75)
    check("exact duplicate is rejected at insert", dup is None)

    await editor.triage_leads()
    st = dict(await p.fetch("select id, status from leads where id = any($1::uuid[])", [low, high]))
    check("below threshold is held", st[low] == "below_threshold", st[low])
    check("above threshold is promoted", st[high] == "promoted", st[high])

    events = [r["action"] for r in await p.fetch("select action from agent_events where lead_id = any($1::uuid[])",
                                                 [low, high])]
    check("both decisions are logged", "lead_held" in events and "handoff" in events, str(events))


async def main() -> int:
    admin = await asyncpg.connect(BASE_URL)
    await admin.execute(f'drop database if exists "{TEST_DB}" with (force)')
    await admin.execute(f'create database "{TEST_DB}"')
    try:
        await cli.initdb()
        p = await db.pool()
        await step1_state_machine(p)
        await lease_renewal(p)
        await step2_events(p)
        await step4_triage(p)
        await p.close()
    finally:
        await admin.execute(f'drop database if exists "{TEST_DB}" with (force)')
        await admin.close()
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
