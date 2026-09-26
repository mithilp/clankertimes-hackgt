"""Reporters: a pool of concurrent slots per process, any number of processes.

Each slot claims one story via a lease and works it until it resolves (draft, park, kill)
or its budget runs out. All progress is written to Postgres as it happens, so a story whose
worker dies is picked up by another worker and resumed from the plan, facts and claims so far.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from . import db, memory, web
from .config import settings
from .llm import AgentContext, Refused, Tool, run_agent
from .tools import research_tools

log = logging.getLogger(__name__)

SYSTEM = """You are a reporter in an automated local newsroom. You are handed one falsifiable hypothesis and work it
until it is confirmed, killed, or parked. Your tool-call reasons are published, so make them honest and specific.

Work in this order:
1. write_evidence_plan: the ranked sources that could settle the hypothesis. "Done" is measured against this plan.
2. Prior coverage: news_search for what has already been reported and by whom. Credit it; never rewrite it as ours.
3. The gap: what nobody has established. Everything after this points at the gap.
4. Go get it: records, filings, datasets, agency and company sites, the browser for portals that need JavaScript,
   public discourse for leads. Use memory_search for hints about where to look. After each check, update_plan_item.
   Every time a source tells you something new that bears on the hypothesis, record_fact with a verbatim quote.
5. Resolve with exactly one of:
   - submit_draft: the evidence confirms it (or the contradiction is itself a story). Every sentence is a claim with
     the URL it came from and a verbatim quote from that page. Quotes are checked mechanically against what you
     fetched; paraphrases fail.
   - park: no public evidence exists yet, or it is held by a person or office we can't reach. Say what would wake it.
   - kill: the evidence you checked contradicts the hypothesis. Write the kill memo: what was checked, what was
     found, what would change our mind.

Rules:
- We do not contact people: no email, no calls, no records requests.
- No allegations of wrongdoing against named private individuals. For officials and organizations, write only what
  the records show, attributed to the record ("according to the county's contract file").
- Don't get past CAPTCHAs or bot checks. If a page is blocked, look for the same record elsewhere.
- Messages starting with [newsroom] are from the desk: budget, diminishing returns, plan coverage. Take them seriously."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


async def _source_id(url: str):
    p = await db.pool()
    return await p.fetchval("select id from sources where url_hash = $1", db.url_hash(url))


async def _budget_state(story_id) -> dict:
    p = await db.pool()
    b = await p.fetchrow("select * from budgets where story_id = $1", story_id)
    elapsed = datetime.now(timezone.utc) - b["started_at"]
    return {
        "used": b["tool_calls_used"], "granted": b["tool_calls_granted"],
        "elapsed_min": elapsed.total_seconds() / 60, "limit_min": b["wall_clock_granted"].total_seconds() / 60,
        "exhausted": b["tool_calls_used"] >= b["tool_calls_granted"] or elapsed >= b["wall_clock_granted"],
    }


async def _yield_state(story_id) -> tuple[int, int]:
    """(new facts from the last N page fetches, N actually available). Searches don't count."""
    p = await db.pool()
    row = await p.fetchrow(
        """with recent as (select id from tool_calls where story_id = $1 and tool in ('fetch_url', 'browse')
                           order by id desc limit $2)
           select (select count(*) from recent) as calls,
                  (select count(*) from facts where tool_call_id in (select id from recent)) as facts""",
        story_id, settings.yield_window,
    )
    return row["facts"], row["calls"]


async def _plan_state(story_id) -> dict:
    p = await db.pool()
    rows = await p.fetch("select rank, status from evidence_plan where story_id = $1 order by rank", story_id)
    top = [r for r in rows if r["rank"] <= 3]
    return {
        "exists": bool(rows),
        "pending": sum(r["status"] == "pending" for r in rows),
        "top_all_checked": bool(top) and all(r["status"] != "pending" for r in top),
        "top_none_moved": bool(top) and not any(r["status"] == "checked_moved" for r in top),
    }


class StoryWork:
    def __init__(self, story, worker: str):
        self.story = story
        self.worker = worker
        self.ctx = AgentContext(agent=f"reporter:{worker}", story_id=story["id"], lead_id=story["lead_id"])
        self.overrun_turns = 0

    # --- terminal actions ----------------------------------------------------------------

    async def _finish(self, action: str, reason: str, **fields) -> str:
        if not await db.release(self.story["id"], self.worker, **fields):
            raise RuntimeError("lost the lease on this story; another worker has it")
        await db.event(self.ctx.agent, action, reason, story_id=self.story["id"], lead_id=self.story["lead_id"])
        return "Done."

    async def submit_draft(self, inp: dict) -> str:
        claims = inp["claims"]
        if len(claims) < 3:
            raise ValueError("a story needs at least 3 claims")
        problems = []
        for i, c in enumerate(claims):
            if not web.span_in_source(c["source_url"], c["quoted_span"]):
                problems.append(f"claim {i}: quoted_span not found verbatim in the fetched text of {c['source_url']} "
                                f"(fetch it first, and quote exactly)")
        if problems:
            raise ValueError("draft rejected:\n" + "\n".join(problems))
        p = await db.pool()
        async with p.acquire() as conn, conn.transaction():
            await conn.execute("delete from claims where story_id = $1", self.story["id"])
            for i, c in enumerate(claims):
                await conn.execute(
                    """insert into claims (story_id, position, paragraph, text, source_id, quoted_span)
                       values ($1,$2,$3,$4,$5,$6)""",
                    self.story["id"], i, int(c.get("paragraph", 0)), c["text"], await _source_id(c["source_url"]),
                    c["quoted_span"],
                )
        paragraphs: dict[int, list[str]] = {}
        for c in claims:
            paragraphs.setdefault(int(c.get("paragraph", 0)), []).append(c["text"])
        body = "\n\n".join(" ".join(paragraphs[k]) for k in sorted(paragraphs))
        return await self._finish("draft_submitted", inp["headline"], status="in_review", resolution="confirmed",
                                  headline=inp["headline"], body=body)

    async def park(self, inp: dict) -> str:
        wake_source_id = None
        if inp.get("wake_url"):
            text = await web.fetch(inp["wake_url"])  # baseline hash; the editor re-hashes it to detect changes
            wake_source_id, _ = await web.archive(inp["wake_url"], text, self.ctx.agent)
        hours = float(inp.get("wake_after_hours") or 0)
        await memory.remember(f"Parked: {self.story['hypothesis']}. {inp['memo']}", agent_id=self.ctx.agent,
                              kind="kill_lesson")
        return await self._finish(
            "parked", inp["wake_condition"], status="dormant", resolution="unresolved", kill_memo=inp["memo"],
            wake_condition=inp["wake_condition"], wake_source_id=wake_source_id,
            wake_at=datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None,
        )

    async def kill(self, inp: dict) -> str:
        await memory.remember(f"Killed: {self.story['hypothesis']}. {inp['kill_memo']}", agent_id=self.ctx.agent,
                              kind="kill_lesson")
        return await self._finish("killed", inp["kill_memo"], status="killed", resolution="killed",
                                  kill_memo=inp["kill_memo"])

    # --- bookkeeping ---------------------------------------------------------------------

    async def write_evidence_plan(self, inp: dict) -> str:
        p = await db.pool()
        async with p.acquire() as conn, conn.transaction():
            await conn.execute("delete from evidence_plan where story_id = $1 and status = 'pending'", self.story["id"])
            start = await conn.fetchval("select coalesce(max(rank), 0) from evidence_plan where story_id = $1",
                                        self.story["id"])
            for i, item in enumerate(inp["items"], start=start + 1):
                await conn.execute(
                    "insert into evidence_plan (story_id, rank, description, why) values ($1,$2,$3,$4)",
                    self.story["id"], i, item["description"], item["why"],
                )
        return await self._plan_text()

    async def _plan_text(self) -> str:
        p = await db.pool()
        rows = await p.fetch("select rank, description, status from evidence_plan where story_id = $1 order by rank",
                             self.story["id"])
        return "\n".join(f"{r['rank']}. [{r['status']}] {r['description']}" for r in rows) or "(no plan yet)"

    async def update_plan_item(self, inp: dict) -> str:
        p = await db.pool()
        last_call = await p.fetchval(
            "select max(id) from tool_calls where story_id = $1 and billable", self.story["id"])
        res = await p.execute(
            "update evidence_plan set status = $3, checked_by = $4 where story_id = $1 and rank = $2",
            self.story["id"], int(inp["rank"]), inp["status"], last_call,
        )
        if res.endswith(" 0"):
            raise ValueError(f"no plan item with rank {inp['rank']}")
        return await self._plan_text()

    async def record_fact(self, inp: dict) -> str:
        p = await db.pool()
        existing = await p.fetch("select fact from facts where story_id = $1", self.story["id"])
        if any(_norm(r["fact"]) == _norm(inp["fact"]) for r in existing):
            return "Already recorded; not a new fact."
        if not web.span_in_source(inp["source_url"], inp["quoted_span"]):
            raise ValueError("quoted_span not found verbatim in the fetched text of that URL; fetch it and quote exactly")
        source_id = await _source_id(inp["source_url"])
        call_id = await p.fetchval(
            "select max(id) from tool_calls where story_id = $1 and source_id = $2", self.story["id"], source_id)
        await p.execute(
            """insert into facts (story_id, tool_call_id, fact, source_id, quoted_span, bearing)
               values ($1,$2,$3,$4,$5,$6)""",
            self.story["id"], call_id, inp["fact"], source_id, inp["quoted_span"], inp["bearing"],
        )
        return f"Recorded ({len(existing) + 1} facts so far)."

    async def request_extension(self, inp: dict) -> str:
        p = await db.pool()
        appeal_id = await p.fetchval(
            """insert into budget_appeals (story_id, extra_calls, next_checks, why_could_change)
               values ($1,$2,$3,$4) returning id""",
            self.story["id"], int(inp["extra_calls"]), inp["next_checks"], inp["why_could_change"],
        )
        await db.event(self.ctx.agent, "appeal", inp["next_checks"], story_id=self.story["id"])
        for _ in range(40):  # the managing editor is woken by NOTIFY; this usually returns in seconds
            await asyncio.sleep(3)
            await db.heartbeat(self.story["id"], self.worker)
            row = await p.fetchrow("select decision, decision_reason from budget_appeals where id = $1", appeal_id)
            if row["decision"]:
                return f"Extension {row['decision']}: {row['decision_reason']}"
        return "No decision yet. Plan to resolve with what you have."

    # --- stopping rules ------------------------------------------------------------------

    async def on_turn(self, ctx: AgentContext) -> str | None:
        b = await _budget_state(self.story["id"])
        new_facts, calls = await _yield_state(self.story["id"])
        plan = await _plan_state(self.story["id"])
        notes = [f"budget {b['used']}/{b['granted']} research calls, {b['elapsed_min']:.0f}/{b['limit_min']:.0f} min."]
        if not plan["exists"]:
            notes.append("Write the evidence plan before researching further.")
        if calls >= settings.yield_window and new_facts == 0:
            notes.append(f"Diminishing returns: your last {calls} page fetches produced no new facts.")
        if plan["top_all_checked"] and plan["top_none_moved"]:
            notes.append("Every top-ranked plan item is checked and none moved the hypothesis. Resolve now.")
        if b["exhausted"]:
            self.overrun_turns += 1
            notes.append("Budget exhausted. Research tools are closed. request_extension with specific next checks, "
                         "or resolve now with submit_draft, park, or kill.")
            if self.overrun_turns > 4:
                await self.park({"wake_condition": "budget exhausted without resolution",
                                 "memo": "Reporter ran out of budget and did not resolve; parked automatically."})
                ctx.done = True
        return " ".join(notes)

    def gate(self, tool: Tool) -> Tool:
        """Research tools refuse to run once the budget is exhausted."""
        inner = tool.handler

        async def handler(inp: dict):
            if (await _budget_state(self.story["id"]))["exhausted"]:
                raise RuntimeError("budget exhausted: request_extension or resolve")
            return await inner(inp)

        tool.handler = handler
        return tool

    # --- run -----------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        claim_schema = {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "One sentence of the story."},
                "source_url": {"type": "string"},
                "quoted_span": {"type": "string", "description": "Exact words from that page supporting the sentence."},
                "paragraph": {"type": "integer"},
            },
            "required": ["text", "source_url", "quoted_span"],
        }
        research = [self.gate(t) if t.billable else t for t in research_tools(self.ctx)]
        return [
            *research,
            Tool("write_evidence_plan", "Write (or extend) the ranked list of sources that could settle the hypothesis.",
                 {"items": {"type": "array", "items": {"type": "object", "properties": {
                     "description": {"type": "string"}, "why": {"type": "string"}},
                     "required": ["description", "why"]}}},
                 ["items"], self.write_evidence_plan, billable=False),
            Tool("update_plan_item", "Mark a plan item checked, with whether it moved the hypothesis.",
                 {"rank": {"type": "integer"},
                  "status": {"type": "string", "enum": ["checked_no_change", "checked_moved", "unreachable"]}},
                 ["rank", "status"], self.update_plan_item, billable=False),
            Tool("record_fact", "Record a new fact bearing on the hypothesis, from a page you fetched, with the "
                 "exact words that establish it. The quote is checked against the fetched text.",
                 {"fact": {"type": "string"}, "source_url": {"type": "string"},
                  "quoted_span": {"type": "string", "description": "Exact words from that page."},
                  "bearing": {"type": "string", "enum": ["supports", "contradicts", "context"]}},
                 ["fact", "source_url", "quoted_span", "bearing"], self.record_fact, billable=False),
            Tool("request_extension", "Ask the managing editor for more research calls. Say exactly what you would "
                 "check next and why it could change the outcome.",
                 {"extra_calls": {"type": "integer"}, "next_checks": {"type": "string"},
                  "why_could_change": {"type": "string"}},
                 ["extra_calls", "next_checks", "why_could_change"], self.request_extension, billable=False),
            Tool("submit_draft", "Submit the story for review. Every sentence is a claim with a verbatim quote.",
                 {"headline": {"type": "string"}, "claims": {"type": "array", "items": claim_schema}},
                 ["headline", "claims"], self.submit_draft, billable=False, terminal=True),
            Tool("park", "Park the story: no public evidence yet, or it's held by someone we can't reach.",
                 {"wake_condition": {"type": "string"},
                  "wake_url": {"type": "string", "description": "A page whose change should wake the story."},
                  "wake_after_hours": {"type": "number"},
                  "memo": {"type": "string", "description": "What was checked, what was found, what would settle it."}},
                 ["wake_condition", "memo"], self.park, billable=False, terminal=True),
            Tool("kill", "Kill the story: checked evidence contradicts the hypothesis.",
                 {"kill_memo": {"type": "string",
                                "description": "What was checked, what was found, what would change our mind."}},
                 ["kill_memo"], self.kill, billable=False, terminal=True),
        ]

    async def task(self) -> str:
        p = await db.pool()
        sid = self.story["id"]
        lead = await p.fetchrow(f"select {db.LEAD_COLS} from leads where id = $1", self.story["lead_id"])
        self.story = {**dict(self.story), "hypothesis": lead["hypothesis"]}
        facts = await p.fetch("""select f.fact, f.bearing, s.url from facts f left join sources s on s.id = f.source_id
                                 where story_id = $1 order by created_at""", sid)
        blocks = await p.fetch("""select role, reason from reviews where story_id = $1 and verdict = 'block'
                                  order by created_at""", sid)
        draft = await p.fetch("select text from claims where story_id = $1 order by position", sid)
        hints = await memory.recall(lead["hypothesis"])
        b = await _budget_state(sid)
        parts = [
            f"Hypothesis: {lead['hypothesis']}",
            f"Why now: {lead['why_now']}\nWho would know: {lead['who_would_know']}\n"
            f"Would settle it: {lead['would_settle_it']}",
            f"Scout's score: {lead['score']} ({lead['score_reason']})",
            f"Budget: {b['granted']} research calls ({b['used']} used), {b['limit_min']:.0f} minutes.",
            "Evidence plan so far:\n" + await self._plan_text(),
            "Facts so far:\n" + ("\n".join(f"- ({r['bearing']}) {r['fact']} [{r['url']}]" for r in facts) or "- none"),
            "Newsroom memory hints:\n" + ("\n".join(f"- {h}" for h in hints) or "- none"),
        ]
        if blocks:
            parts.append("The review panel blocked your previous draft. Address every point or park/kill:\n"
                         + "\n".join(f"- {r['role']}: {r['reason']}" for r in blocks)
                         + "\n\nPrevious draft:\n" + "\n".join(f"- {r['text']}" for r in draft))
        if facts or blocks:
            parts.append("This story is being resumed. Continue from the state above rather than starting over.")
        return "\n\n".join(parts)

    async def run(self) -> None:
        task = await self.task()
        b = await _budget_state(self.story["id"])
        await db.event(self.ctx.agent, "reporting", f"picked up: {self.story['hypothesis']}", story_id=self.story["id"])
        await run_agent(
            self.ctx, model=settings.models.reporter, system=SYSTEM, task=task, tools=self.tools(),
            max_turns=b["granted"] + 40, effort="high", on_turn=self.on_turn,
            heartbeat=lambda: db.heartbeat(self.story["id"], self.worker),
        )
        if not self.ctx.done and self.ctx.outcome != "lease_lost":
            await self.park({"wake_condition": "reporter stopped without resolving",
                             "memo": f"Reporter ended ({self.ctx.outcome or 'no tool call'}) without resolving."})


async def slot(n: int, waker: db.Waker) -> None:
    worker = f"{settings.worker_id}/{n}"
    while True:
        story = None
        try:
            story = None if await db.over_budget() else await db.claim_story(["assigned"], "reporting", worker)
            if story is None:
                await waker.wait(10)
                continue
            await StoryWork(story, worker).run()
        except Refused as e:
            log.warning("reporter refused: %s", e)
            if story:
                await db.release(story["id"], worker, status="dormant", resolution="unresolved",
                                 kill_memo=f"Model declined to continue: {e}")
        except Exception:  # noqa: BLE001 - the lease expires and another slot resumes the story
            log.exception("reporter slot %s failed", worker)
            await asyncio.sleep(5)


async def main() -> None:
    waker = await db.Waker("story_ready").start()
    log.info("reporter pool: %d concurrent slots", settings.reporter_concurrency)
    await asyncio.gather(*(slot(n, waker) for n in range(settings.reporter_concurrency)))

