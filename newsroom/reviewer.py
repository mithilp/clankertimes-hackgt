"""Review panel: verifier, skeptic, fairness. Parallel across stories (lease-claimed slots) and
within a story (the three reviewers run concurrently). Any block sends the story back once;
a second block parks it. Which reviewers run is set by REVIEWERS (MVP: verifier only)."""

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel

from . import db, memory, web
from .config import settings
from .llm import QuotaExhausted, Refused, structured

log = logging.getLogger(__name__)


class ClaimCheck(BaseModel):
    position: int
    supported: bool
    problem: str


class Review(BaseModel):
    verdict: Literal["approve", "block"]
    reason: str
    claim_checks: list[ClaimCheck]


COMMON = """You are on the review panel of an automated local newsroom. You see a draft where every sentence is a
claim with the source excerpt it came from. Return verdict 'block' only for problems that should stop publication,
with a reason the reporter can act on. Your reason is shown publicly. List claim_checks only for claims you have an
opinion on (the verifier lists every claim)."""

ROLES = {
    "verifier": (settings.models.verifier, COMMON + """

Your job: does each quoted excerpt, read with its context, support its sentence exactly as written? Check every
name, title, number, date and qualifier in the sentence ("no", "only", "all", "first", "found"): each one must
appear in the quote or its context. A true-sounding detail that is not in the cited text makes the claim
unsupported, even if it is probably correct. Also mark unsupported any claim that alleges wrongdoing by a named
private individual, or that states something about an official or organization without attributing it to the
record. Block if any claim is unsupported, or if the headline says more than the claims do. List a claim_check
for every claim."""),
    "skeptic": (settings.models.skeptic, COMMON + """

Your job: try to kill the story. Look for innocent explanations the draft ignores, missing context (was this
normal practice? is there an exemption?), a weaker reading of the same records, or a headline the body doesn't earn.
Block if a reasonable reader would be misled."""),
    "fairness": (settings.models.fairness, COMMON + """

Your job: fairness and harm. Block if the draft alleges wrongdoing by a named private individual; if claims about
officials or organizations go beyond what the records show or aren't attributed to the record; if the people or
entity affected have a public response on record that the draft omits; or if the tone editorializes."""),
}


async def packet(story) -> str:
    p = await db.pool()
    lead = await p.fetchrow("select hypothesis from leads where id = $1", story["lead_id"])
    claims = await p.fetch(
        """select c.position, c.text, c.quoted_span, s.url from claims c join sources s on s.id = c.source_id
           where c.story_id = $1 order by c.position""", story["id"])
    facts = await p.fetch("select fact, bearing from facts where story_id = $1", story["id"])
    lines = [f"Hypothesis: {lead['hypothesis']}", f"Headline: {story['headline']}", "", "Claims:"]
    for c in claims:
        lines.append(f"[{c['position']}] {c['text']}\n    source: {c['url']}\n    quote: \"{c['quoted_span']}\"\n"
                     f"    context: ...{web.span_context(c['url'], c['quoted_span'])}...")
    contradicting = [f["fact"] for f in facts if f["bearing"] == "contradicts"]
    lines.append("\nFacts the scouts recorded that cut against the story:\n"
                 + ("\n".join(f"- {f}" for f in contradicting) or "- none"))
    return "\n".join(lines)


async def review_story(story, worker: str) -> None:
    sid, rnd = story["id"], story["review_round"]
    text = await packet(story)
    precedent = await memory.recall(f"review block {story['headline']}", k=3)
    if precedent:
        text += "\n\nPast editorial precedent:\n" + "\n".join(f"- {x}" for x in precedent)

    async def run(role: str) -> tuple[str, str, Review]:
        model, system = ROLES[role]
        return role, model, await structured(f"reviewer:{role}", model, system, text, Review, effort="high")

    results = await asyncio.gather(*(run(r) for r in settings.reviewers))
    p = await db.pool()
    n_claims = await p.fetchval("select count(*) from claims where story_id = $1", sid)
    for i, (role, model, r) in enumerate(results):
        if role != "verifier":
            continue
        checked = {c.position for c in r.claim_checks}
        unsupported = sorted(c.position for c in r.claim_checks if not c.supported)
        missing = n_claims - len(checked & set(range(n_claims)))
        if r.verdict == "approve" and (unsupported or missing):
            # Code, not the model's overall verdict, decides: any unsupported or unchecked claim is a block.
            reason = f"claims {unsupported} unsupported" if unsupported else f"{missing} claims not checked"
            results[i] = (role, model, Review(verdict="block", reason=f"{r.reason} ({reason})",
                                              claim_checks=r.claim_checks))
    async with p.acquire() as conn, conn.transaction():
        for role, model, r in results:
            await conn.execute(
                """insert into reviews (story_id, round, role, model, verdict, reason) values ($1,$2,$3,$4,$5,$6)
                   on conflict (story_id, round, role) do nothing""", sid, rnd, role, model, r.verdict, r.reason)
            if role == "verifier":
                for c in r.claim_checks:
                    await conn.execute("update claims set verified = $3 where story_id = $1 and position = $2",
                                       sid, c.position, c.supported)
    for role, _, r in results:
        await db.event(f"reviewer:{role}", "approved" if r.verdict == "approve" else "blocked", r.reason, story_id=sid)

    blocks = [(role, r) for role, _, r in results if r.verdict == "block"]
    if not blocks:
        await db.release(sid, worker, status="approved")
    elif rnd == 0:
        # One rework pass: the writer redrafts from the same facts with the reviewers' reasons.
        await db.release(sid, worker, status="drafting", review_round=1)
    else:
        memo = "Blocked twice by the review panel. " + " ".join(f"{role}: {r.reason}" for role, r in blocks)
        await db.release(sid, worker, status="dormant", resolution="unresolved", kill_memo=memo)
        await db.event("review-panel", "parked", memo, story_id=sid)
        await memory.remember(f"Parked at review: {story['headline']}. {memo}", agent_id="review-panel",
                              kind="editorial_precedent")


async def slot(n: int, waker: db.Waker) -> None:
    worker = f"{settings.worker_id}/review{n}"
    while True:
        story = None
        try:
            story = await db.claim_story(["in_review"], "in_review", worker)
            if story is None:
                await waker.wait(10)
                continue
            async with db.lease_keeper(story["id"], worker):
                await review_story(story, worker)
        except QuotaExhausted as e:
            log.warning("%s; handing the story back and pausing 15 min", e)
            if story:
                await db.release(story["id"], worker, status="in_review")
            await asyncio.sleep(900)
        except Refused as e:
            log.warning("reviewer refused: %s", e)
            if story:
                await db.release(story["id"], worker, status="dormant", resolution="unresolved",
                                 kill_memo=f"Review model declined: {e}")
        except Exception:  # noqa: BLE001 - lease expires; another slot retries the review
            log.exception("reviewer slot %s failed", worker)
            await asyncio.sleep(5)


async def main() -> None:
    waker = await db.Waker("story_review").start()
    log.info("review pool: %d concurrent stories x reviewers %s", settings.reviewer_concurrency,
             ",".join(settings.reviewers))
    await asyncio.gather(*(slot(n, waker) for n in range(settings.reviewer_concurrency)))
