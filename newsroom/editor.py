"""Managing editor: the one coordinator. Singleton by advisory lock; extra replicas are standbys.

Wakes on NOTIFY (new_lead, appeal, story_approved) within milliseconds and also sweeps every
30 seconds, so nothing depends on a notification arriving. Mostly code; the model is called
only to confirm possible duplicates and to rule on scouts' budget appeals.
"""

import logging
import time

from pydantic import BaseModel

from . import db, memory, web
from .config import settings
from .llm import structured

log = logging.getLogger(__name__)
AGENT = "managing-editor"
ACTIVE = ("assigned", "planning", "researching", "drafting", "writing", "in_review", "approved")


# --- leads -------------------------------------------------------------------------------


async def triage_leads() -> None:
    p = await db.pool()
    for lead in await p.fetch(f"select {db.LEAD_COLS} from leads where status = 'new' order by created_at"):
        if lead["score"] < settings.promote_threshold:
            await p.execute("update leads set status = 'below_threshold' where id = $1", lead["id"])
            await db.event(AGENT, "lead_held", f"score {lead['score']} below {settings.promote_threshold}: "
                           f"{lead['score_reason']}", lead_id=lead["id"])
            continue
        dup = await p.fetchrow(
            """select o.id, o.hypothesis, 1 - (o.embedding <=> l.embedding) as sim
               from leads l join leads o on o.id <> l.id and o.status = 'promoted' and o.embedding is not null
               where l.id = $1 and l.embedding is not null
               order by o.embedding <=> l.embedding limit 1""",
            lead["id"],
        )
        if dup and dup["sim"] >= settings.duplicate_similarity and await same_hypothesis(lead, dup):
            await p.execute("update leads set status = 'duplicate', duplicate_of = $2 where id = $1",
                            lead["id"], dup["id"])
            await db.event(AGENT, "lead_duplicate", f"{dup['sim']:.2f} similar to: {dup['hypothesis']}",
                           lead_id=lead["id"])

    if await db.over_budget():
        return
    active = await p.fetchval("select count(*) from stories where status::text = any($1::text[])", list(ACTIVE))
    free = settings.max_active_stories - active
    if free <= 0:
        return
    for lead in await p.fetch(
        f"select {db.LEAD_COLS} from leads where status = 'new' order by score desc, created_at limit $1", free
    ):
        await promote(lead)


class SameCheck(BaseModel):
    same: bool
    reason: str


async def same_hypothesis(lead, other) -> bool:
    """Similarity only flags a possible duplicate; a model call decides."""
    ruling = await structured(
        AGENT, settings.models.editor,
        "Decide whether two newsroom leads test the same hypothesis: same actor, same claim, same records would "
        "settle both. Different contracts, dates, or agencies mean different hypotheses.",
        f"A: {lead['hypothesis']}\nB: {other['hypothesis']}", SameCheck, effort="low",
    )
    return ruling.same


async def promote(lead) -> None:
    p = await db.pool()
    score = float(lead["score"])
    async with p.acquire() as conn, conn.transaction():
        claimed = await conn.execute("update leads set status = 'promoted' where id = $1 and status = 'new'",
                                     lead["id"])
        if claimed.endswith(" 0"):
            return
        story_id = await conn.fetchval("insert into stories (lead_id) values ($1) returning id", lead["id"])
    await db.event(AGENT, "handoff", f"promoted at score {score:.2f}; {lead['score_reason']}",
                   story_id=story_id, lead_id=lead["id"], detail={"hypothesis": lead["hypothesis"]})


# --- appeals -----------------------------------------------------------------------------


class Ruling(BaseModel):
    grant: bool
    extra_calls: int
    reason: str


APPEAL_SYSTEM = """You are the managing editor ruling on a scout's request for more research calls on one
sub-claim. Grant only when the scout names specific checks that could plausibly change its verdict and hasn't
already hit diminishing returns on similar sources. "Keep looking" is a denial. A grant is 3-10 extra calls.
Your reason is shown publicly; one or two sentences."""


async def decide_appeals() -> None:
    p = await db.pool()
    for appeal in await p.fetch("select * from budget_appeals where decision is null order by created_at"):
        sid, sub_id = appeal["story_id"], appeal["sub_claim_id"]
        granted_before = await p.fetchval(
            "select count(*) from budget_appeals where sub_claim_id = $1 and decision = 'granted'", sub_id)
        if granted_before >= settings.max_appeals_granted or await db.over_budget():
            ruling = Ruling(grant=False, extra_calls=0, reason="This sub-claim has already had its extension.")
        else:
            ctx = await p.fetchrow(
                """select l.hypothesis, sc.claim, sc.core, sc.calls_used, sc.calls_granted,
                          (select count(*) from facts f where f.sub_claim_id = sc.id) as facts
                   from sub_claims sc join stories s on s.id = sc.story_id join leads l on l.id = s.lead_id
                   where sc.id = $1""", sub_id)
            precedent = await memory.recall(f"budget appeal ruling {appeal['next_checks']}", agent_id=AGENT, k=3)
            prompt = (
                f"Hypothesis: {ctx['hypothesis']}\nSub-claim ({'core' if ctx['core'] else 'supporting'}): "
                f"{ctx['claim']}\nUsed {ctx['calls_used']}/{ctx['calls_granted']} calls; "
                f"{ctx['facts']} facts recorded.\n"
                f"Requested: {appeal['extra_calls']} more calls.\nNext checks: {appeal['next_checks']}\n"
                f"Why it could change the verdict: {appeal['why_could_change']}\n\n"
                "Past rulings:\n" + ("\n".join(f"- {x}" for x in precedent) or "- none")
            )
            ruling = await structured(AGENT, settings.models.editor, APPEAL_SYSTEM, prompt, Ruling, effort="low")
        extra = max(0, min(ruling.extra_calls, 10)) if ruling.grant else 0
        decision = "granted" if ruling.grant and extra else "denied"
        async with p.acquire() as conn, conn.transaction():
            await conn.execute(
                "update budget_appeals set decision = $2, decision_reason = $3, decided_at = now() where id = $1",
                appeal["id"], decision, ruling.reason,
            )
            if decision == "granted":
                await conn.execute("update sub_claims set calls_granted = calls_granted + $2 where id = $1",
                                   sub_id, extra)
        await db.event(AGENT, f"appeal_{decision}", ruling.reason, story_id=sid, sub_claim_id=sub_id)
        await memory.remember(f"Appeal {decision} ({appeal['next_checks']}): {ruling.reason}", agent_id=AGENT,
                              kind="editorial_precedent")


# --- publishing --------------------------------------------------------------------------


async def timeline(story_id) -> str:
    p = await db.pool()
    rows = await p.fetch(
        """select started_at, agent, tool, reason from tool_calls where story_id = $1
           union all
           select created_at, agent, action, reason from agent_events where story_id = $1
           order by 1""", story_id)
    return "\n".join(f"{r[0]:%H:%M:%S} {r['agent']} {r['tool']}: {r['reason']}" for r in rows)


async def publish_gate(story_id) -> str | None:
    """Deterministic: every claim verified by the verifier and every quote still found in its source.
    Returns None if the story may publish, else the reason it may not."""
    p = await db.pool()
    claims = await p.fetch("""select c.position, c.verified, c.quoted_span, s.url from claims c
                              join sources s on s.id = c.source_id where c.story_id = $1""", story_id)
    if not claims:
        return "no claims"
    unverified = [c["position"] for c in claims if c["verified"] is not True]
    if unverified:
        return f"claims {unverified} not verified"
    missing = [c["position"] for c in claims if not web.span_in_source(c["url"], c["quoted_span"])]
    if missing:
        return f"quotes for claims {missing} not found in archived sources"
    return None


async def publish_approved() -> None:
    p = await db.pool()
    for story in await p.fetch("select * from stories where status = 'approved'"):
        failed = await publish_gate(story["id"])
        if failed:
            await p.execute("""update stories set status = 'dormant', resolution = 'unresolved', kill_memo = $2
                               where id = $1 and status = 'approved'""", story["id"], f"Publish gate: {failed}")
            await db.event(AGENT, "publish_blocked", failed, story_id=story["id"])
            continue
        tl = await timeline(story["id"])
        done = await p.execute(
            """update stories set status = 'published', published_at = now(), timeline = $2
               where id = $1 and status = 'approved'""", story["id"], tl)
        if done.endswith(" 1"):
            await db.event(AGENT, "published", story["headline"] or "", story_id=story["id"],
                           lead_id=story["lead_id"])


# --- dormant stories ---------------------------------------------------------------------

_last_wake_check: dict = {}


async def wake_dormant() -> None:
    p = await db.pool()
    for story in await p.fetch("select s.*, src.url from stories s left join sources src on src.id = s.wake_source_id "
                               "where s.status = 'dormant' and (s.wake_at is not null or s.wake_source_id is not null)"):
        reason = None
        if story["wake_at"] and story["wake_at"] <= await p.fetchval("select now()"):
            reason = f"wake time reached: {story['wake_condition']}"
        elif story["url"] and time.monotonic() - _last_wake_check.get(story["id"], 0) > 900:
            _last_wake_check[story["id"]] = time.monotonic()
            try:
                _, changed = await web.archive(story["url"], await web.fetch(story["url"]), AGENT)
            except Exception as e:  # noqa: BLE001
                log.info("wake check failed for %s: %s", story["url"], e)
                continue
            if changed:
                reason = f"watched page changed: {story['url']}"
        if reason and not await db.over_budget():
            # Reopen the parts that couldn't be settled; if none (e.g. parked by review), rewrite.
            reopened = await p.execute(
                """update sub_claims set status = 'pending', finding = null, calls_granted = calls_used + $2
                   where story_id = $1 and status in ('not_found', 'gated')""",
                story["id"], settings.sub_claim_calls,
            )
            next_status = "drafting" if reopened.endswith(" 0") else "researching"
            await p.execute("update stories set status = ($2::text)::story_status, wake_at = null "
                            "where id = $1 and status = 'dormant'", story["id"], next_status)
            await db.event(AGENT, "woke", f"{reason} -> {next_status}", story_id=story["id"])


# --- loop --------------------------------------------------------------------------------


async def main() -> None:
    async with db.singleton(AGENT):
        waker = await db.Waker("new_lead", "appeal", "story_approved").start()
        last_wake_sweep = 0.0
        await db.event(AGENT, "online", f"worker {settings.worker_id} is managing editor")
        while True:
            for step in (decide_appeals, triage_leads, publish_approved):
                try:
                    await step()
                except Exception:  # noqa: BLE001
                    log.exception("editor step %s failed", step.__name__)
            if time.monotonic() - last_wake_sweep > 60:
                last_wake_sweep = time.monotonic()
                try:
                    await wake_dormant()
                except Exception:  # noqa: BLE001
                    log.exception("wake sweep failed")
            await waker.wait(30)
