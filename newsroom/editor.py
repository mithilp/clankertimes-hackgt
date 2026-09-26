"""Managing editor: the one coordinator. Singleton by advisory lock; extra replicas are standbys.

Wakes on NOTIFY (new_lead, appeal, story_approved) within milliseconds and also sweeps every
30 seconds, so nothing depends on a notification arriving. Mostly code; the model is called
only to rule on budget appeals.
"""

import logging
import time
from datetime import timedelta

from pydantic import BaseModel

from . import db, memory, web
from .config import settings
from .llm import structured

log = logging.getLogger(__name__)
AGENT = "managing-editor"
ACTIVE = ("assigned", "reporting", "in_review", "approved")


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
        if dup and dup["sim"] >= settings.duplicate_similarity:
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


async def promote(lead) -> None:
    p = await db.pool()
    score = float(lead["score"])
    calls = settings.base_tool_calls + round(score * settings.extra_tool_calls)
    minutes = settings.base_minutes + round(score * settings.extra_minutes)
    async with p.acquire() as conn, conn.transaction():
        claimed = await conn.execute("update leads set status = 'promoted' where id = $1 and status = 'new'",
                                     lead["id"])
        if claimed.endswith(" 0"):
            return
        story_id = await conn.fetchval("insert into stories (lead_id) values ($1) returning id", lead["id"])
        await conn.execute(
            "insert into budgets (story_id, tool_calls_granted, wall_clock_granted) values ($1,$2,$3)",
            story_id, calls, timedelta(minutes=minutes),
        )
    await db.event(AGENT, "handoff", f"promoted at score {score:.2f}; budget {calls} calls / {minutes} min",
                   story_id=story_id, lead_id=lead["id"], detail={"hypothesis": lead["hypothesis"]})


# --- appeals -----------------------------------------------------------------------------


class Ruling(BaseModel):
    grant: bool
    extra_calls: int
    reason: str


APPEAL_SYSTEM = """You are the managing editor ruling on a reporter's request for more research budget.
Grant only when the reporter names specific checks that could plausibly change the outcome and hasn't already
hit diminishing returns on similar sources. "Keep looking" is a denial. A grant is 5-20 extra calls.
Your reason is shown publicly; one or two sentences."""


async def decide_appeals() -> None:
    p = await db.pool()
    for appeal in await p.fetch("select * from budget_appeals where decision is null order by created_at"):
        sid = appeal["story_id"]
        granted_before = await p.fetchval(
            "select count(*) from budget_appeals where story_id = $1 and decision = 'granted'", sid)
        if granted_before >= settings.max_appeals_granted or await db.over_budget():
            ruling = Ruling(grant=False, extra_calls=0, reason="This story has already had its extension.")
        else:
            ctx = await p.fetchrow(
                """select l.hypothesis, b.tool_calls_used, b.tool_calls_granted,
                          (select count(*) from facts f where f.story_id = s.id) as facts,
                          (select count(*) from evidence_plan e where e.story_id = s.id and e.status = 'pending') as pending,
                          (select count(*) from evidence_plan e where e.story_id = s.id) as planned
                   from stories s join leads l on l.id = s.lead_id join budgets b on b.story_id = s.id
                   where s.id = $1""", sid)
            precedent = await memory.recall(f"budget appeal ruling {appeal['next_checks']}", agent_id=AGENT, k=3)
            prompt = (
                f"Hypothesis: {ctx['hypothesis']}\nUsed {ctx['tool_calls_used']}/{ctx['tool_calls_granted']} calls; "
                f"{ctx['facts']} facts recorded; {ctx['pending']}/{ctx['planned']} plan items unchecked.\n"
                f"Requested: {appeal['extra_calls']} more calls.\nNext checks: {appeal['next_checks']}\n"
                f"Why it could change the outcome: {appeal['why_could_change']}\n\n"
                "Past rulings:\n" + ("\n".join(f"- {x}" for x in precedent) or "- none")
            )
            ruling = await structured(AGENT, settings.models.editor, APPEAL_SYSTEM, prompt, Ruling, effort="low")
        extra = max(0, min(ruling.extra_calls, 20)) if ruling.grant else 0
        decision = "granted" if ruling.grant and extra else "denied"
        async with p.acquire() as conn, conn.transaction():
            await conn.execute(
                "update budget_appeals set decision = $2, decision_reason = $3, decided_at = now() where id = $1",
                appeal["id"], decision, ruling.reason,
            )
            if decision == "granted":
                await conn.execute(
                    """update budgets set tool_calls_granted = tool_calls_granted + $2,
                              wall_clock_granted = wall_clock_granted + interval '10 minutes' where story_id = $1""",
                    sid, extra,
                )
        await db.event(AGENT, f"appeal_{decision}", ruling.reason, story_id=sid)
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


async def publish_approved() -> None:
    p = await db.pool()
    for story in await p.fetch("select * from stories where status = 'approved'"):
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
            await p.execute(
                """update budgets set tool_calls_granted = tool_calls_used + $2, started_at = now(),
                          wall_clock_granted = make_interval(mins => $3) where story_id = $1""",
                story["id"], settings.base_tool_calls // 2, settings.base_minutes,
            )
            await p.execute("update stories set status = 'assigned', wake_at = null where id = $1 and status = 'dormant'",
                            story["id"])
            await db.event(AGENT, "woke", reason, story_id=story["id"])


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
