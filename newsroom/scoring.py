"""Lead scoring (ARCHITECTURE.md §3). The model rates rubric components; code measures the rest and combines."""

import asyncio
import hashlib
import math
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from . import db, memory, web
from .config import settings
from .llm import structured

WEIGHTS = {"impact": 0.30, "settleability": 0.30, "novelty": 0.20, "tip_strength": 0.10, "timeliness": 0.10}


class Rated(BaseModel):
    value: float = Field(description="0 to 1")
    reason: str


class Rubric(BaseModel):
    fields_specific: bool = Field(description="All fields are concrete, not vague ('look into the budget' fails).")
    evidence_is_public: bool = Field(description="would_settle_it names evidence obtainable from public records or "
                                                 "published material, without contacting anyone.")
    names_private_individual: bool = Field(description="The hypothesis alleges wrongdoing by a named private "
                                                       "individual (not a public official or organization).")
    impact: Rated
    settleability: Rated
    tip_strength: Rated


RUBRIC_SYSTEM = """You score leads for an automated local newsroom. Rate each component 0-1 against these anchors.

impact: public money, number of people affected, whether officials are involved.
  0.1 a restaurant changed its hours | 0.5 a city department missed a reporting deadline | 0.9 $2M no-bid county contract
settleability: is the settling evidence public, specific, and reachable within a few hours of research?
  0.1 "internal emails would show it" | 0.5 "county records probably exist somewhere" |
  0.9 named portal + document type + date range ("bid tabulation for RFP 24-117 on the county procurement portal")
tip_strength: quality of what surfaced it.
  0.1 anonymous forum post | 0.5 a local blog citing an unnamed source | 0.9 an official record showing an anomaly

Anchored examples:
- "Atlanta paid Vendor X $2.1M for software without competitive bidding" / why_now: commission agenda item lists a
  sole-source justification / settle: contract file, sole-source memo, bid history on the procurement portal
  -> impact 0.85, settleability 0.8, tip_strength 0.85
- "Traffic got worse downtown" -> fields_specific false (a topic, not a falsifiable hypothesis)
- "Council member Y's aide took bribes" / settle: "the aide's bank records" -> evidence_is_public false
- "A Reddit user says a local landlord, John Doe, illegally evicts tenants" -> names_private_individual true

Be strict. Most leads should not score above 0.7 on everything."""


def _clamp(r: Rated) -> dict:
    return {"value": min(1.0, max(0.0, r.value)), "reason": r.reason}


def fingerprint(hypothesis: str) -> str:
    return hashlib.sha256(re.sub(r"[^a-z0-9]+", " ", hypothesis.lower()).strip().encode()).hexdigest()


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)


async def _novelty(hypothesis: str, emb: list[float]) -> dict:
    p = await db.pool()
    nearest = await p.fetchrow(
        "select id, hypothesis, 1 - (embedding <=> $1::vector) as sim from leads "
        "where embedding is not null order by embedding <=> $1::vector limit 1",
        db.vec(emb),
    )
    max_sim = float(nearest["sim"]) if nearest else 0.0
    lead_part = min(1.0, max(0.0, (0.95 - max_sim) / 0.45))  # sim <= 0.5 -> 1, >= 0.95 -> 0

    try:
        coverage = await web.search(hypothesis, news=True, freshness="pm", count=10)
    except Exception:  # noqa: BLE001 - search outage shouldn't block scoring
        coverage = []
    embs = await asyncio.gather(*(memory.embed(f"{c['title']}. {c['snippet']}") for c in coverage))
    covered = [c for c, e in zip(coverage, embs) if _cos(emb, e) > 0.75]
    coverage_part = [1.0, 0.6, 0.35][len(covered)] if len(covered) < 3 else 0.15

    return {
        "value": round(lead_part * coverage_part, 3),
        "reason": f"nearest past lead sim {max_sim:.2f}; {len(covered)} matching news items in past month",
        "covered_by": [c["url"] for c in covered][:5],
        "nearest_lead": str(nearest["id"]) if nearest else None,
    }


def _timeliness(why_now_at: datetime | None) -> dict:
    if why_now_at is None:
        return {"value": 0.5, "reason": "trigger date unknown"}
    age_days = max(0.0, (datetime.now(timezone.utc) - why_now_at).total_seconds() / 86400)
    return {"value": round(math.exp(-age_days / 10), 3), "reason": f"trigger is {age_days:.1f} days old"}


async def score_lead(agent: str, lead: dict, why_now_at: datetime | None) -> dict:
    """Returns score, components, reason, embedding, fingerprint."""
    lessons = await memory.recall(lead["hypothesis"], k=4)
    prompt = (
        f"Beat: {lead['beat']}\nHypothesis: {lead['hypothesis']}\nWhy now: {lead['why_now']}\n"
        f"Who would know: {lead['who_would_know']}\nWould settle it: {lead['would_settle_it']}\n"
        f"Sources seen: {', '.join(lead.get('sources_seen', [])) or 'none'}\n\n"
        f"Newsroom memory that may bear on tip strength:\n" + ("\n".join(f"- {m}" for m in lessons) or "- none")
    )
    emb = await memory.embed(lead["hypothesis"])
    rubric, novelty = await asyncio.gather(
        structured(agent, settings.models.scorer, RUBRIC_SYSTEM, prompt, Rubric),
        _novelty(lead["hypothesis"], emb),
    )
    components = {
        "gates": {
            "fields_specific": rubric.fields_specific,
            "evidence_is_public": rubric.evidence_is_public,
            "no_private_individual": not rubric.names_private_individual,
        },
        "impact": _clamp(rubric.impact),
        "settleability": _clamp(rubric.settleability),
        "tip_strength": _clamp(rubric.tip_strength),
        "novelty": novelty,
        "timeliness": _timeliness(why_now_at),
    }
    failed = [g for g, ok in components["gates"].items() if not ok]
    if failed:
        score, reason = 0.0, f"failed gate: {', '.join(failed)}"
    else:
        score = math.prod(max(components[k]["value"], 0.01) ** w for k, w in WEIGHTS.items())
        weakest = min(WEIGHTS, key=lambda k: components[k]["value"])
        reason = f"weakest component: {weakest} ({components[weakest]['reason']})"
    return {
        "score": round(score, 2),
        "components": components,
        "reason": reason,
        "embedding": emb,
        "fingerprint": fingerprint(lead["hypothesis"]),
    }
