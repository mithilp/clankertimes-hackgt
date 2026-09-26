"""Scouts: one loop per beat, all beats in parallel.

Each beat is guarded by an advisory lock, so running extra scout replicas is safe: they
become hot standbys for any beat whose owner dies. Leads are written the moment they're
found (flash leads) and the insert trigger wakes the managing editor via NOTIFY.
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone

from . import db
from .config import settings
from .llm import AgentContext, Refused, Tool, run_agent
from .scoring import fingerprint, score_lead
from .tools import research_tools

log = logging.getLogger(__name__)

SYSTEM = """You are a scout for an automated local newsroom. Your job is to find leads on your beat and raise them.

A lead is a falsifiable hypothesis, not a topic. "Atlanta's software contracts" is a topic. "Atlanta paid Vendor X
$2.1M for permitting software without a competitive bid" is a hypothesis: it can be confirmed or killed.

How to work:
- Start with memory_search for your beat's notes so you don't rediscover old ground.
- Look wherever this beat's news actually surfaces: agendas and minutes, procurement and permit portals, court and
  agency filings, budgets, inspection data, press releases, local forums, local coverage. There is no approved list.
- When something looks like a story, raise_lead immediately; don't batch leads to the end.
- Every lead must be settleable from public records or published material. We do not email or call anyone.
- Never raise allegations of wrongdoing against a named private individual. Officials, agencies and companies are fine.
- Skip anything already well covered by local outlets unless you have a genuinely new angle.
- Before you finish, write one memory_note (kind beat_note) about what you checked this cycle and what to try next.

Finish with a two-sentence summary of the cycle."""


def load_beats() -> list[dict]:
    return json.loads(settings.beats_file.read_text(encoding="utf-8"))


def raise_lead_tool(ctx: AgentContext, beat: dict) -> Tool:
    async def handler(inp: dict) -> str:
        lead = {
            "beat": beat["id"],
            "hypothesis": inp["hypothesis"].strip(),
            "why_now": inp["why_now"].strip(),
            "who_would_know": inp["who_would_know"].strip(),
            "would_settle_it": inp["would_settle_it"].strip(),
            "sources_seen": inp.get("sources_seen", []),
        }
        p = await db.pool()
        fp = fingerprint(lead["hypothesis"])
        if await p.fetchval("select 1 from leads where fingerprint = $1", fp):
            return "Already raised (same hypothesis). Look for something else."

        why_now_at = None
        if inp.get("why_now_date"):
            try:
                why_now_at = datetime.fromisoformat(inp["why_now_date"]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        s = await score_lead(ctx.agent, lead, why_now_at)
        lead_id = await p.fetchval(
            """insert into leads (scout, beat, hypothesis, why_now, why_now_at, who_would_know, would_settle_it,
                                  score, score_components, score_reason, fingerprint, embedding)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::vector)
               on conflict (fingerprint) do nothing returning id""",
            ctx.agent, lead["beat"], lead["hypothesis"], lead["why_now"], why_now_at, lead["who_would_know"],
            lead["would_settle_it"], s["score"], s["components"], s["reason"], s["fingerprint"], db.vec(s["embedding"]),
        )
        if lead_id is None:
            return "Another scout raised this at the same moment. Look for something else."
        for url in lead["sources_seen"]:
            source_id, _ = await db.upsert_source(url, ctx.agent)
            await p.execute("insert into lead_sources values ($1,$2) on conflict do nothing", lead_id, source_id)
        await db.event(ctx.agent, "lead_raised", f"score {s['score']:.2f}: {s['reason']}",
                       lead_id=lead_id, detail={"hypothesis": lead["hypothesis"], "components": s["components"]})
        return f"Raised. Score {s['score']:.2f} ({s['reason']}). The managing editor decides whether to promote it."

    return Tool(
        "raise_lead",
        "Raise a lead the moment you find one. It is scored and sent to the managing editor immediately.",
        {
            "hypothesis": {"type": "string", "description": "One falsifiable sentence."},
            "why_now": {"type": "string", "description": "What surfaced it."},
            "why_now_date": {"type": "string", "description": "ISO date of the triggering item, if known."},
            "who_would_know": {"type": "string"},
            "would_settle_it": {"type": "string", "description": "Specific public records or documents."},
            "sources_seen": {"type": "array", "items": {"type": "string"}, "description": "URLs you looked at."},
        },
        ["hypothesis", "why_now", "who_would_know", "would_settle_it"],
        handler,
        billable=False,
    )


async def cycle(beat: dict) -> None:
    ctx = AgentContext(agent=f"scout:{beat['id']}")
    p = await db.pool()
    recent = await p.fetch(
        "select hypothesis, status, score from leads where beat = $1 order by created_at desc limit 15", beat["id"]
    )
    recent_txt = "\n".join(f"- ({r['status']}, {r['score']}) {r['hypothesis']}" for r in recent) or "- none yet"
    task = (
        f"Beat: {beat['name']}\n{beat['description']}\n\n"
        f"Places people often start on this beat (not a limit): {', '.join(beat.get('starting_points', []))}\n\n"
        f"Leads already raised on this beat (don't repeat them):\n{recent_txt}\n\n"
        f"Current time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Start the cycle."
    )
    await db.event(ctx.agent, "cycle_start", f"scanning {beat['name']}")
    summary = await run_agent(
        ctx, model=settings.models.scout, system=SYSTEM, task=task,
        tools=[*research_tools(ctx), raise_lead_tool(ctx, beat)],
        max_turns=settings.scout_max_calls, effort="medium",
    )
    await db.event(ctx.agent, "cycle_end", summary[:400] or "cycle finished")


async def beat_loop(beat: dict) -> None:
    async with db.singleton(f"scout:{beat['id']}"):
        while True:
            if await db.over_budget():
                log.warning("spend cap reached; scout %s idle", beat["id"])
            else:
                try:
                    await cycle(beat)
                except Refused as e:
                    log.warning("scout refused: %s", e)
                except Exception:  # noqa: BLE001 - one bad cycle must not kill a 4-hour run
                    log.exception("scout cycle failed for %s", beat["id"])
            # Jitter so beats don't fire in lockstep and spike rate limits.
            await asyncio.sleep(settings.scout_interval_s * random.uniform(0.8, 1.2))


async def main() -> None:
    beats = load_beats()
    log.info("scouting %d beats in parallel", len(beats))
    await asyncio.gather(*(beat_loop(b) for b in beats))
