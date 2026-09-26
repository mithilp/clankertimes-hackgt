"""Scouts: each researches ONE sub-claim of a hypothesis and returns a verdict.

A pool of slots per process, any number of processes. Each slot claims a pending sub-claim
under a lease, researches it with open tools, records facts with verbatim quotes, and
finishes with one verdict. When a story's last sub-claim resolves, the story moves to
drafting and a reporter picks it up.
"""

import asyncio
import logging
import re

from . import db, memory, web
from .config import settings
from .llm import AgentContext, QuotaExhausted, Refused, Tool, run_agent
from .tools import research_tools

log = logging.getLogger(__name__)

SYSTEM = """You are a scout for an automated local newsroom. You research ONE sub-claim of a larger hypothesis and
report a verdict on it. Other scouts are covering the other parts at the same time; stay on yours.

How to work:
- Start with memory_search for hints about where this kind of record lives.
- Go where the evidence should be. "Where to look" is a suggestion, not a limit.
- Prefer the primary record (the resolution, the contract, the audit, the filing) over coverage of it. Coverage is
  useful for finding the record and for what has already been reported.
- Every time a page tells you something that bears on your sub-claim, record_fact with the exact words from that
  page. Quotes are checked mechanically against what you fetched; paraphrases are rejected.
- Finish with report_finding and exactly one verdict:
    supported     a recorded fact establishes the sub-claim
    contradicted  a recorded fact shows it is false
    not_found     you checked where it should be and it isn't there (say where you looked)
    gated         the evidence exists but is held by an office or person (name the records and who holds them)

Rules:
- We do not contact people: no email, no calls, no records requests.
- Don't get past CAPTCHAs or bot checks. If a page is blocked, look for the same record elsewhere.
- Messages starting with [newsroom] are from the desk: your budget and whether you're still finding anything."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


async def _source_id(url: str):
    p = await db.pool()
    return await p.fetchval("select id from sources where url_hash = $1", db.url_hash(url))


async def advance_story(story_id) -> None:
    """Move the story to drafting once none of its sub-claims are still open. Idempotent."""
    p = await db.pool()
    moved = await p.execute(
        """update stories set status = 'drafting' where id = $1 and status = 'researching'
           and not exists (select 1 from sub_claims where story_id = $1 and status in ('pending', 'researching'))""",
        story_id,
    )
    if moved.endswith(" 1"):
        await db.event("assignment-desk", "all_sub_claims_back", "every sub-claim has a verdict; ready to write",
                       story_id=story_id)


class ScoutWork:
    def __init__(self, sub, worker: str, lease: dict):
        self.sub = sub
        self.worker = worker
        self.lease = lease
        self.ctx = AgentContext(agent=f"scout:{worker}", story_id=sub["story_id"], sub_claim_id=sub["id"])
        self.overrun_turns = 0

    async def _budget(self) -> tuple[int, int]:
        p = await db.pool()
        row = await p.fetchrow("select calls_used, calls_granted from sub_claims where id = $1", self.sub["id"])
        return row["calls_used"], row["calls_granted"]

    async def _still_held(self) -> bool:
        return self.lease["held"]

    # --- tools -----------------------------------------------------------------------------

    async def record_fact(self, inp: dict) -> str:
        p = await db.pool()
        existing = await p.fetch("select fact from facts where story_id = $1", self.sub["story_id"])
        if any(_norm(r["fact"]) == _norm(inp["fact"]) for r in existing):
            return "Already recorded on this story; not a new fact."
        if not web.span_in_source(inp["source_url"], inp["quoted_span"]):
            raise ValueError("quoted_span not found verbatim in the fetched text of that URL; fetch it and quote exactly")
        source_id = await _source_id(inp["source_url"])
        call_id = await p.fetchval(
            "select max(id) from tool_calls where sub_claim_id = $1 and source_id = $2", self.sub["id"], source_id)
        await p.execute(
            """insert into facts (story_id, sub_claim_id, tool_call_id, fact, source_id, quoted_span, bearing)
               values ($1,$2,$3,$4,$5,$6,$7)""",
            self.sub["story_id"], self.sub["id"], call_id, inp["fact"], source_id, inp["quoted_span"], inp["bearing"],
        )
        mine = await p.fetchval("select count(*) from facts where sub_claim_id = $1", self.sub["id"])
        return f"Recorded ({mine} facts on this sub-claim)."

    async def report_finding(self, inp: dict) -> str:
        verdict, finding = inp["verdict"], inp["finding"].strip()
        p = await db.pool()
        need = {"supported": "supports", "contradicted": "contradicts"}.get(verdict)
        if need and not await p.fetchval(
            "select 1 from facts where sub_claim_id = $1 and bearing = $2 limit 1", self.sub["id"], need
        ):
            raise ValueError(f"'{verdict}' needs at least one recorded fact with bearing '{need}'. "
                             "Record the evidence first, or choose not_found / gated.")
        return await self._finish(verdict, finding)

    async def _finish(self, verdict: str, finding: str) -> str:
        if not await db.release(self.sub["id"], self.worker, table="sub_claims",
                                status=verdict, finding=finding, scout=self.ctx.agent):
            raise RuntimeError("lost the lease on this sub-claim; another scout has it")
        await db.event(self.ctx.agent, "verdict", f"{verdict}: {self.sub['claim']} — {finding}",
                       story_id=self.sub["story_id"], sub_claim_id=self.sub["id"],
                       detail={"verdict": verdict, "core": self.sub["core"]})
        if verdict in ("not_found", "gated"):
            await memory.remember(f"Looking for: {self.sub['claim']}. {verdict}: {finding}",
                                  agent_id=self.ctx.agent, kind="source_reliability")
        return "Reported."

    async def request_extension(self, inp: dict) -> str:
        p = await db.pool()
        appeal_id = await p.fetchval(
            """insert into budget_appeals (story_id, sub_claim_id, extra_calls, next_checks, why_could_change)
               values ($1,$2,$3,$4,$5) returning id""",
            self.sub["story_id"], self.sub["id"], int(inp["extra_calls"]), inp["next_checks"], inp["why_could_change"],
        )
        await db.event(self.ctx.agent, "appeal", inp["next_checks"], story_id=self.sub["story_id"],
                       sub_claim_id=self.sub["id"])
        for _ in range(40):  # the managing editor is woken by NOTIFY; this usually returns in seconds
            await asyncio.sleep(3)
            row = await p.fetchrow("select decision, decision_reason from budget_appeals where id = $1", appeal_id)
            if row["decision"]:
                return f"Extension {row['decision']}: {row['decision_reason']}"
        return "No decision yet. Plan to give your verdict with what you have."

    def gate(self, tool: Tool) -> Tool:
        """Research tools refuse to run once this sub-claim's budget is spent."""
        inner = tool.handler

        async def handler(inp: dict):
            used, granted = await self._budget()
            if used >= granted:
                raise RuntimeError("budget spent: request_extension or report_finding")
            return await inner(inp)

        tool.handler = handler
        return tool

    def tools(self) -> list[Tool]:
        research = [self.gate(t) if t.billable else t for t in research_tools(self.ctx)]
        return [
            *research,
            Tool("record_fact", "Record a fact bearing on your sub-claim, from a page you fetched, with the exact "
                 "words that establish it. The quote is checked against the fetched text.",
                 {"fact": {"type": "string"}, "source_url": {"type": "string"},
                  "quoted_span": {"type": "string", "description": "Exact words from that page."},
                  "bearing": {"type": "string", "enum": ["supports", "contradicts", "context"]}},
                 ["fact", "source_url", "quoted_span", "bearing"], self.record_fact, billable=False),
            Tool("request_extension", "Ask the managing editor for more research calls on this sub-claim. Say exactly "
                 "what you would check next and why it could change the verdict.",
                 {"extra_calls": {"type": "integer"}, "next_checks": {"type": "string"},
                  "why_could_change": {"type": "string"}},
                 ["extra_calls", "next_checks", "why_could_change"], self.request_extension, billable=False),
            Tool("report_finding", "Finish with your verdict on this sub-claim.",
                 {"verdict": {"type": "string", "enum": ["supported", "contradicted", "not_found", "gated"]},
                  "finding": {"type": "string", "description": "Two or three sentences: what you found, or where "
                              "you looked and what's missing, or which records are held and by whom."}},
                 ["verdict", "finding"], self.report_finding, billable=False, terminal=True),
        ]

    # --- stopping rules ----------------------------------------------------------------------

    async def on_turn(self, ctx: AgentContext) -> str | None:
        used, granted = await self._budget()
        p = await db.pool()
        row = await p.fetchrow(
            """with recent as (select id from tool_calls where sub_claim_id = $1 and tool in ('fetch_url', 'browse')
                               order by id desc limit $2)
               select (select count(*) from recent) as fetches,
                      (select count(*) from facts where tool_call_id in (select id from recent)) as facts""",
            self.sub["id"], settings.yield_window,
        )
        notes = [f"budget {used}/{granted} research calls."]
        if row["fetches"] >= settings.yield_window and row["facts"] == 0:
            notes.append(f"Diminishing returns: your last {row['fetches']} page fetches produced no new facts. "
                         "Give your verdict unless you know exactly where else to look.")
        if used >= granted:
            self.overrun_turns += 1
            notes.append("Budget spent. Research tools are closed. request_extension with specific next checks, "
                         "or report_finding now.")
            if self.overrun_turns > 3:
                await self._finish("not_found", "Budget ran out before a verdict; recorded as not found.")
                ctx.done = True
        return " ".join(notes)

    # --- run -------------------------------------------------------------------------------

    async def task(self) -> str:
        p = await db.pool()
        story = await p.fetchrow(
            "select l.hypothesis from stories s join leads l on l.id = s.lead_id where s.id = $1", self.sub["story_id"])
        others = await p.fetch("select claim from sub_claims where story_id = $1 and id <> $2 order by position",
                               self.sub["story_id"], self.sub["id"])
        facts = await p.fetch("""select f.fact, s.url from facts f left join sources s on s.id = f.source_id
                                 where f.story_id = $1 order by f.created_at""", self.sub["story_id"])
        hints = await memory.recall(self.sub["claim"])
        return "\n\n".join([
            f"The hypothesis (context only): {story['hypothesis']}",
            f"YOUR SUB-CLAIM: {self.sub['claim']}",
            f"Where to look: {self.sub['where_to_look']}",
            f"Would confirm it: {self.sub['would_confirm']}\nWould refute it: {self.sub['would_refute']}",
            f"Budget: {self.sub['calls_granted']} research calls.",
            "Other scouts are covering:\n" + ("\n".join(f"- {r['claim']}" for r in others) or "- nothing else"),
            "Facts already recorded on this story (don't re-record them):\n"
            + ("\n".join(f"- {r['fact']} [{r['url']}]" for r in facts) or "- none yet"),
            "Newsroom memory hints:\n" + ("\n".join(f"- {h}" for h in hints) or "- none"),
        ])

    async def run(self) -> None:
        await db.event(self.ctx.agent, "scouting", self.sub["claim"], story_id=self.sub["story_id"],
                       sub_claim_id=self.sub["id"])
        await run_agent(
            self.ctx, model=settings.models.scout, system=SYSTEM, task=await self.task(), tools=self.tools(),
            max_turns=self.sub["calls_granted"] + 15, effort="medium", on_turn=self.on_turn,
            heartbeat=self._still_held,
        )
        if not self.ctx.done and self.lease["held"]:
            await self._finish("not_found", f"Scout stopped ({self.ctx.outcome or 'no verdict given'}) "
                                            "without a verdict; recorded as not found.")


async def slot(n: int, waker: db.Waker) -> None:
    worker = f"{settings.worker_id}/s{n}"
    while True:
        sub = None
        try:
            if await db.over_budget():
                await waker.wait(60)
                continue
            sub = await db.claim(["pending"], "researching", worker, table="sub_claims")
            if sub is None:
                await waker.wait(10)
                continue
            async with db.lease_keeper(sub["id"], worker, table="sub_claims") as lease:
                await ScoutWork(sub, worker, lease).run()
            await advance_story(sub["story_id"])
        except QuotaExhausted as e:
            log.warning("%s; handing the sub-claim back and pausing 15 min", e)
            if sub:
                await db.release(sub["id"], worker, table="sub_claims", status="pending")
            await asyncio.sleep(900)
        except Refused as e:
            log.warning("scout refused: %s", e)
            if sub:
                await db.release(sub["id"], worker, table="sub_claims", status="not_found",
                                 finding=f"Model declined to research this: {e}")
                await advance_story(sub["story_id"])
        except Exception:  # noqa: BLE001 - the lease expires and another slot retries the sub-claim
            log.exception("scout slot %s failed", worker)
            await asyncio.sleep(5)


async def main() -> None:
    waker = await db.Waker("sub_claim_ready").start()
    log.info("scout pool: %d concurrent sub-claims", settings.scout_concurrency)
    await asyncio.gather(*(slot(n, waker) for n in range(settings.scout_concurrency)))
