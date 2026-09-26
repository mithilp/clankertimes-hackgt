"""Acceptance tests for the hypothesis-first pipeline. No API keys needed: no model calls.

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
os.environ["LEASE_SECONDS"] = "6"         # short leases so the lease tests run in seconds
os.environ["MAX_APPEALS_GRANTED"] = "0"   # appeals are denied by rule, so no model call is needed
os.environ["MAX_ACTIVE_STORIES"] = "10"   # don't inherit a first-run limit from .env
os.environ["MEMORY_ENABLED"] = "false"    # mem0 would make a model call

from newsroom import __main__ as cli, db, editor, web  # noqa: E402
from newsroom.reporter import rollup  # noqa: E402
from newsroom.scoring import fingerprint  # noqa: E402
from newsroom.scout import advance_story  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


async def insert_lead(p, hypothesis: str, score: float):
    return await p.fetchval(
        """insert into leads (tipster, beat, hypothesis, why_now, who_would_know, would_settle_it,
                              score, score_components, score_reason, fingerprint)
           values ('test', 'test', $1, 'test trigger', 'purchasing office', 'the contract file',
                   $2, '{}'::jsonb, 'manual test lead', $3)
           on conflict (fingerprint) do nothing returning id""",
        hypothesis, score, fingerprint(hypothesis),
    )


async def add_sub_claims(p, story_id, specs: list[tuple[str, bool]]) -> None:
    for i, (claim, core) in enumerate(specs, start=1):
        await p.execute(
            """insert into sub_claims (story_id, position, claim, core, where_to_look, would_confirm,
                                       would_refute, calls_granted)
               values ($1,$2,$3,$4,'test','test','test',10)""", story_id, i, claim, core)


def rollup_cases() -> None:
    print("Roll-up: code decides from verdicts")

    def sub(pos, core, status, targets=None):
        return {"position": pos, "core": core, "status": status, "targets": targets}

    cases = [
        ("all core supported -> draft",
         [sub(1, True, "supported"), sub(2, True, "supported"), sub(3, False, "not_found")], "draft"),
        ("a core part contradicted -> kill",
         [sub(1, True, "supported"), sub(2, True, "contradicted")], "kill"),
        ("a core part gated -> unresolved",
         [sub(1, True, "supported"), sub(2, True, "gated")], "unresolved"),
        ("supporting part contradicted doesn't kill",
         [sub(1, True, "supported"), sub(2, False, "contradicted")], "draft"),
        ("follow-up supported settles its core part",
         [sub(1, True, "supported"), sub(2, True, "not_found"), sub(3, False, "supported", targets=2)], "draft"),
        ("follow-up contradicted kills",
         [sub(1, True, "supported"), sub(2, True, "not_found"), sub(3, False, "contradicted", targets=2)], "kill"),
        ("follow-up not found stays unresolved",
         [sub(1, True, "supported"), sub(2, True, "not_found"), sub(3, False, "not_found", targets=2)], "unresolved"),
    ]
    for name, subs, want in cases:
        got, _ = rollup(subs)
        check(name, got == want, f"got {got}")


async def full_flow(p) -> None:
    print("Full flow: lead -> plan -> parallel scouts -> roll-up -> draft -> review -> publish")
    lead_id = await insert_lead(p, "Test County paid Vendor A $1M without a bid", 0.80)
    await editor.triage_leads()
    story = await p.fetchrow("select * from stories where lead_id = $1", lead_id)
    check("editor promotes the lead into a story", story is not None and story["status"] == "assigned")

    planning = await db.claim_story(["assigned"], "planning", "reporter-1")
    check("a reporter claims it to plan", planning is not None and planning["status"] == "planning")
    await add_sub_claims(p, story["id"], [("County paid Vendor A $1M", True), ("No bid was held", True),
                                         ("Local outlets have not reported it", False)])
    check("planner hands it to scouts", await db.release(story["id"], "reporter-1", status="researching"))

    claims = await asyncio.gather(*(db.claim(["pending"], "researching", f"scout-{i}", table="sub_claims")
                                    for i in range(5)))
    got = [c for c in claims if c is not None]
    check("five scouts race for three sub-claims: each goes to exactly one scout",
          len(got) == 3 and len({c["id"] for c in got}) == 3, f"{len(got)} claimed")

    url = "https://example.gov/contracts/123"
    text = "The county approved a $1,000,000 contract with Vendor A. No bids were solicited."
    source_id, _ = await web.archive(url, text, "test")
    by_pos = {c["position"]: c for c in got}
    for pos, span in ((1, "approved a $1,000,000 contract with Vendor A"), (2, "No bids were solicited")):
        await p.execute("""insert into facts (story_id, sub_claim_id, fact, source_id, quoted_span, bearing)
                           values ($1,$2,$3,$4,$5,'supports')""", story["id"], by_pos[pos]["id"], span,
                        source_id, span)

    for pos, verdict in ((1, "supported"), (2, "supported")):
        await db.release(by_pos[pos]["id"], by_pos[pos]["lease_owner"], table="sub_claims", status=verdict,
                         finding="test")
        await advance_story(story["id"])
    status = await p.fetchval("select status from stories where id = $1", story["id"])
    check("story waits while a sub-claim is still out", status == "researching", status)

    await db.release(by_pos[3]["id"], by_pos[3]["lease_owner"], table="sub_claims", status="not_found",
                     finding="test")
    await advance_story(story["id"])
    status = await p.fetchval("select status from stories where id = $1", story["id"])
    check("last verdict moves the story to drafting", status == "drafting", status)
    subs = [dict(r) for r in await p.fetch("select position, core, status, targets from sub_claims "
                                           "where story_id = $1", story["id"])]
    check("roll-up says draft", rollup(subs)[0] == "draft")

    writing = await db.claim_story(["drafting"], "writing", "reporter-2")
    check("a reporter claims it to write", writing is not None and writing["id"] == story["id"])
    for i, f in enumerate(await p.fetch("select id, source_id, quoted_span from facts where story_id = $1",
                                        story["id"])):
        await p.execute("""insert into claims (story_id, position, text, fact_id, source_id, quoted_span)
                           values ($1,$2,$3,$4,$5,$6)""", story["id"], i, f"sentence {i}", f["id"],
                        f["source_id"], f["quoted_span"])
    await db.release(story["id"], "reporter-2", status="in_review", resolution="confirmed", headline="Test")

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


async def leases(p) -> None:
    print("Leases: a busy worker keeps its work; a dead one loses it")
    lead_id = await insert_lead(p, "Test County paid Vendor C $2M on an expired contract", 0.80)
    await editor.triage_leads()
    sid = await p.fetchval("select id from stories where lead_id = $1", lead_id)
    mine = await db.claim_story(["assigned"], "planning", "worker-busy")
    check("worker claims the story", mine is not None and mine["id"] == sid)
    async with db.lease_keeper(sid, "worker-busy") as lease:
        await asyncio.sleep(10)  # longer than the 6s lease: e.g. stuck in rate-limit retries
        stolen = await db.claim_story(["assigned"], "planning", "worker-other")
        check("while renewing, nobody else can take it", stolen is None and lease["held"])
    await asyncio.sleep(8)  # renewer stopped (worker died): lease lapses
    taken = await db.claim_story(["assigned"], "planning", "worker-other")
    check("after the lease lapses, another worker resumes it", taken is not None and taken["id"] == sid)
    await db.release(sid, "worker-other", status="killed")


async def wake_and_appeals(p) -> None:
    print("Parked stories wake up; appeals over the limit are denied")
    lead_id = await insert_lead(p, "Test City gave Vendor D $400K without a contract", 0.80)
    await editor.triage_leads()
    sid = await p.fetchval("select id from stories where lead_id = $1", lead_id)
    await add_sub_claims(p, sid, [("City paid Vendor D $400K", True), ("There is no contract", True)])
    await p.execute("update sub_claims set status = 'supported' where story_id = $1 and position = 1", sid)
    await p.execute("update sub_claims set status = 'gated', finding = 'held by procurement' "
                    "where story_id = $1 and position = 2", sid)
    await p.execute("update stories set status = 'dormant', wake_at = now() - interval '1 minute' where id = $1", sid)
    await editor.wake_dormant()
    st = await p.fetchval("select status from stories where id = $1", sid)
    sub = await p.fetchval("select status from sub_claims where story_id = $1 and position = 2", sid)
    check("wake reopens the unresolved sub-claim", sub == "pending", sub)
    check("and sends the story back to researching", st == "researching", st)

    sub_id = await p.fetchval("select id from sub_claims where story_id = $1 and position = 2", sid)
    await p.execute("""insert into budget_appeals (story_id, sub_claim_id, extra_calls, next_checks, why_could_change)
                       values ($1,$2,5,'check the minutes','could show the contract')""", sid, sub_id)
    await editor.decide_appeals()
    decision = await p.fetchval("select decision from budget_appeals where sub_claim_id = $1", sub_id)
    check("an appeal past the per-sub-claim limit is denied", decision == "denied", decision)


async def events(p) -> None:
    print("Event logging")
    await db.event("test", "hello", "checking the event log", detail={"n": 1})
    row = await p.fetchrow("select * from agent_events where agent = 'test' and action = 'hello'")
    check("event row appears", row is not None and row["reason"] == "checking the event log")
    check("detail stored as json", row is not None and row["detail"] == {"n": 1})


async def triage(p) -> None:
    print("Editor routes below-threshold, above-threshold and duplicate leads")
    low = await insert_lead(p, "Test City repainted a crosswalk late", 0.30)
    high = await insert_lead(p, "Test City paid Vendor B $3M through an emergency exemption", 0.75)
    dup = await insert_lead(p, "Test city paid vendor B $3M through an emergency exemption!", 0.75)
    check("exact duplicate is rejected at insert", dup is None)
    await editor.triage_leads()
    st = dict(await p.fetch("select id, status from leads where id = any($1::uuid[])", [low, high]))
    check("below threshold is held", st[low] == "below_threshold", st[low])
    check("above threshold is promoted", st[high] == "promoted", st[high])
    events_ = [r["action"] for r in await p.fetch("select action from agent_events where lead_id = any($1::uuid[])",
                                                  [low, high])]
    check("both decisions are logged", "lead_held" in events_ and "handoff" in events_, str(events_))


async def main() -> int:
    rollup_cases()
    admin = await asyncpg.connect(BASE_URL)
    await admin.execute(f'drop database if exists "{TEST_DB}" with (force)')
    await admin.execute(f'create database "{TEST_DB}"')
    try:
        await cli.initdb()
        p = await db.pool()
        for step in (full_flow, leases, wake_and_appeals, events, triage):
            await step(p)
        await p.close()
    finally:
        await admin.execute(f'drop database if exists "{TEST_DB}" with (force)')
        await admin.close()
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
