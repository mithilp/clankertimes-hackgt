"""Reporters: plan a hypothesis into sub-claims, then write once scouts have returned verdicts.

Two jobs, both short and leased:
- PLAN  (story 'assigned' -> 'planning' -> 'researching'): split the hypothesis into 3-6
  sub-claims for scouts to research in parallel.
- WRITE (story 'drafting' -> 'writing' -> ...): roll up the verdicts in code, then draft
  from the scouts' facts, kill, send one round of follow-ups, or park.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from . import db, memory, web
from .config import settings
from .llm import QuotaExhausted, Refused, structured

log = logging.getLogger(__name__)


# --- roll-up (pure, deterministic) ---------------------------------------------------------


def rollup(subs: list[dict]) -> tuple[str, list[dict]]:
    """Decide from verdicts alone. Returns (outcome, sub-claims that decided it).

    outcome: 'kill' if any core part is contradicted, 'draft' if every core part is supported,
    otherwise 'unresolved'. A follow-up (targets=<position>) that is supported or contradicted
    settles the core sub-claim it targets.
    """
    core = [s for s in subs if s["core"] and s.get("targets") is None]
    effective = {}
    for c in core:
        verdicts = [c["status"]] + [s["status"] for s in subs if s.get("targets") == c["position"]]
        if "contradicted" in verdicts:
            effective[c["position"]] = "contradicted"
        elif "supported" in verdicts:
            effective[c["position"]] = "supported"
        else:
            effective[c["position"]] = "unresolved"
    contradicted = [c for c in core if effective[c["position"]] == "contradicted"]
    if contradicted:
        return "kill", contradicted
    unresolved = [c for c in core if effective[c["position"]] == "unresolved"]
    if unresolved:
        return "unresolved", unresolved
    return "draft", []


# --- structured outputs --------------------------------------------------------------------


class SubClaimSpec(BaseModel):
    claim: str = Field(description="One checkable statement of fact.")
    core: bool = Field(description="True if the hypothesis cannot survive without it.")
    where_to_look: str
    would_confirm: str
    would_refute: str


class Plan(BaseModel):
    sub_claims: list[SubClaimSpec]


class FollowUp(BaseModel):
    targets: int = Field(description="Position number of the unresolved core sub-claim this tries to settle.")
    claim: str
    where_to_look: str
    would_confirm: str
    would_refute: str


class FollowUps(BaseModel):
    follow_ups: list[FollowUp] = Field(description="Empty if no other public route could settle these parts.")


class Memo(BaseModel):
    memo: str = Field(description="One paragraph: what was checked, what was found, which part failed or is "
                                  "missing, what evidence would change the conclusion.")
    wake_condition: str = Field(description="For parked stories: the concrete event or record that should reopen it.")
    wake_url: str = Field(description="A public page whose change should reopen it, or empty.")


class Sentence(BaseModel):
    text: str
    fact: int = Field(description="Number of the fact (F#) this sentence states.")
    paragraph: int


class Draft(BaseModel):
    headline: str
    sentences: list[Sentence]


PLAN_SYSTEM = """You are the assigning editor of an automated local newsroom. Break a hypothesis into the sub-claims
that must be checked, so that scouts can research them in parallel.

- 3 to 6 sub-claims. Each is ONE checkable statement of fact that one researcher can settle from public records or
  published material in about ten searches and page fetches.
- core=true for the parts the hypothesis cannot survive without (usually 2 to 4). If a core part is false, the
  story dies.
- Add supporting sub-claims (core=false) a reader will need: what has already been reported and by whom, the
  official response on record, what happens next.
- where_to_look: the specific kinds of records or sites. would_confirm / would_refute: the evidence that settles it.
- Don't split into trivia and don't bundle two facts into one sub-claim.
- We never contact people, so every sub-claim must be answerable from public material."""

FOLLOW_UP_SYSTEM = """You are the assigning editor. Some core parts of a hypothesis came back unresolved. Propose
follow-up sub-claims that could settle them by a DIFFERENT public route than the one already tried: another record
type, another agency, an archived copy, a meeting video or minutes, coverage that cites the record. Only propose a
follow-up if it has a real chance. Return an empty list if no public route is left."""

MEMO_SYSTEM = """You write the newsroom's internal memo for a story that won't be published. One paragraph, plain
language: what was checked, what was found, which part of the hypothesis failed (killed) or could not be settled
(parked), and what evidence would change the conclusion. For parked stories, give a concrete wake condition."""

DRAFT_SYSTEM = """You write a news story for an automated local newsroom, using ONLY the numbered facts provided.

- Every sentence cites exactly one fact number, and says only what that fact's quote supports. Do not add names,
  titles, numbers, dates or qualifiers that are not in the cited quote.
- Attribute to the record ("according to the city's contract file", "the audit found").
- Structure: a lede with the central finding; why it matters; the key facts and numbers; background and prior
  coverage; the official response on record, if any; what happens next, if known.
- As many sentences as the facts support, typically 6 to 15. You may cite a fact more than once.
- Neutral tone. No allegations of wrongdoing against named private individuals.
- The headline must be supported by the facts."""


# --- work ----------------------------------------------------------------------------------


async def _lead(story) -> dict:
    p = await db.pool()
    return dict(await p.fetchrow(f"select {db.LEAD_COLS} from leads where id = $1", story["lead_id"]))


async def plan(story, worker: str) -> None:
    lead = await _lead(story)
    agent = f"reporter:{worker}"
    p = await db.pool()
    if await p.fetchval("select count(*) from sub_claims where story_id = $1", story["id"]):
        # A previous planner wrote the sub-claims and died before handing the story on.
        await db.release(story["id"], worker, status="researching")
        return
    hints = await memory.recall(lead["hypothesis"])
    prompt = (
        f"Hypothesis: {lead['hypothesis']}\nWhy now: {lead['why_now']}\n"
        f"Who would know: {lead['who_would_know']}\nWould settle it: {lead['would_settle_it']}\n\n"
        "Newsroom memory:\n" + ("\n".join(f"- {h}" for h in hints) or "- none")
    )
    result = await structured(agent, settings.models.reporter, PLAN_SYSTEM, prompt, Plan, effort="high")
    specs = result.sub_claims[: settings.max_sub_claims]
    if not specs:
        raise ValueError("planner returned no sub-claims")
    if not any(s.core for s in specs):
        specs[0].core = True
    p = await db.pool()
    async with p.acquire() as conn, conn.transaction():
        for i, s in enumerate(specs, start=1):
            await conn.execute(
                """insert into sub_claims (story_id, position, claim, core, where_to_look, would_confirm,
                                           would_refute, calls_granted)
                   values ($1,$2,$3,$4,$5,$6,$7,$8)""",
                story["id"], i, s.claim, s.core, s.where_to_look, s.would_confirm, s.would_refute,
                settings.sub_claim_calls,
            )
    if not await db.release(story["id"], worker, status="researching"):
        raise RuntimeError("lost the lease while planning")
    await db.event(agent, "planned", f"{len(specs)} sub-claims ({sum(s.core for s in specs)} core) sent to scouts",
                   story_id=story["id"], lead_id=story["lead_id"],
                   detail=[{"claim": s.claim, "core": s.core} for s in specs])


async def _subs(story_id) -> list[dict]:
    p = await db.pool()
    return [dict(r) for r in await p.fetch(
        "select id, position, round, targets, claim, core, status, finding from sub_claims "
        "where story_id = $1 order by position", story_id)]


def _verdict_lines(subs: list[dict]) -> str:
    def kind(s: dict) -> str:
        if s["targets"]:
            return f"follow-up of {s['targets']}"
        return "core" if s["core"] else "supporting"

    return "\n".join(f"{s['position']}. [{kind(s)}] {s['status']}: {s['claim']} — {s['finding'] or ''}" for s in subs)


async def write(story, worker: str) -> None:
    agent = f"reporter:{worker}"
    lead = await _lead(story)
    subs = await _subs(story["id"])
    outcome, deciding = rollup(subs)
    await db.event(agent, "rollup",
                   f"{outcome}: " + ("; ".join(s["claim"] for s in deciding) or "all core parts supported"),
                   story_id=story["id"])

    if outcome == "kill":
        await _close(story, worker, agent, lead, subs, killed=True)
    elif outcome == "unresolved" and story["follow_up_round"] == 0 and settings.max_follow_ups > 0:
        if not await _follow_up(story, worker, agent, lead, subs, deciding):
            await _close(story, worker, agent, lead, subs, killed=False)
    elif outcome == "unresolved":
        await _close(story, worker, agent, lead, subs, killed=False)
    else:
        await _draft(story, worker, agent, lead)


async def _follow_up(story, worker, agent, lead, subs, unresolved) -> bool:
    prompt = (f"Hypothesis: {lead['hypothesis']}\n\nVerdicts so far:\n{_verdict_lines(subs)}\n\n"
              "Unresolved core parts: " + ", ".join(str(s["position"]) for s in unresolved))
    result = await structured(agent, settings.models.reporter, FOLLOW_UP_SYSTEM, prompt, FollowUps, effort="high")
    valid = {s["position"] for s in unresolved}
    follow_ups = [f for f in result.follow_ups if f.targets in valid][: settings.max_follow_ups]
    if not follow_ups:
        return False
    p = await db.pool()
    start = max(s["position"] for s in subs)
    async with p.acquire() as conn, conn.transaction():
        for i, f in enumerate(follow_ups, start=start + 1):
            await conn.execute(
                """insert into sub_claims (story_id, position, round, targets, claim, core, where_to_look,
                                           would_confirm, would_refute, calls_granted)
                   values ($1,$2,1,$3,$4,false,$5,$6,$7,$8)""",
                story["id"], i, f.targets, f.claim, f.where_to_look, f.would_confirm, f.would_refute,
                settings.sub_claim_calls,
            )
    if not await db.release(story["id"], worker, status="researching", follow_up_round=1):
        raise RuntimeError("lost the lease while sending follow-ups")
    await db.event(agent, "follow_ups", f"{len(follow_ups)} follow-up sub-claims sent to scouts",
                   story_id=story["id"], detail=[f.model_dump() for f in follow_ups])
    return True


async def _close(story, worker, agent, lead, subs, *, killed: bool) -> None:
    kind = "KILLED" if killed else "PARKED"
    prompt = f"Hypothesis: {lead['hypothesis']}\nOutcome: {kind}\n\nSub-claim verdicts:\n{_verdict_lines(subs)}"
    m = await structured(agent, settings.models.reporter, MEMO_SYSTEM, prompt, Memo, effort="medium")
    if killed:
        ok = await db.release(story["id"], worker, status="killed", resolution="killed", kill_memo=m.memo)
        action = "killed"
    else:
        wake_source_id = None
        if m.wake_url.startswith("http"):
            try:  # baseline hash; the managing editor re-hashes it to detect changes
                wake_source_id, _ = await web.archive(m.wake_url, await web.fetch(m.wake_url), agent)
            except Exception as e:  # noqa: BLE001 - a bad wake URL shouldn't block parking
                log.info("wake url %s unusable: %s", m.wake_url, e)
        ok = await db.release(story["id"], worker, status="dormant", resolution="unresolved", kill_memo=m.memo,
                              wake_condition=m.wake_condition, wake_source_id=wake_source_id,
                              wake_at=datetime.now(timezone.utc) + timedelta(hours=12))
        action = "parked"
    if not ok:
        raise RuntimeError("lost the lease while closing the story")
    await db.event(agent, action, m.memo, story_id=story["id"], lead_id=story["lead_id"])
    await memory.remember(f"{kind}: {lead['hypothesis']}. {m.memo}", agent_id=agent, kind="kill_lesson")


async def _draft(story, worker, agent, lead) -> None:
    p = await db.pool()
    facts = [dict(r) for r in await p.fetch(
        """select f.id, f.fact, f.quoted_span, f.bearing, f.source_id, s.url, sc.claim
           from facts f join sources s on s.id = f.source_id left join sub_claims sc on sc.id = f.sub_claim_id
           where f.story_id = $1 order by sc.position, f.created_at""", story["id"])]
    if not facts:
        raise ValueError("no facts to draft from")
    fact_lines = "\n".join(
        f"F{i}. ({f['bearing']}; re: {f['claim']}) {f['fact']}\n    quote: \"{f['quoted_span']}\"\n    source: {f['url']}"
        for i, f in enumerate(facts, start=1))
    blocks = await p.fetch("select role, reason from reviews where story_id = $1 and verdict = 'block' "
                           "order by created_at", story["id"])
    prev = await p.fetch("select text from claims where story_id = $1 order by position", story["id"])
    prompt = f"Hypothesis (confirmed by the facts below): {lead['hypothesis']}\n\nFacts:\n{fact_lines}"
    if blocks:
        prompt += ("\n\nThe review panel blocked the previous draft. Fix every point, dropping any sentence the facts "
                   "don't support:\n" + "\n".join(f"- {b['role']}: {b['reason']}" for b in blocks)
                   + "\n\nPrevious draft:\n" + "\n".join(f"- {r['text']}" for r in prev))

    draft, error = None, ""
    for _ in range(2):  # one retry with the validation error
        d = await structured(agent, settings.models.reporter, DRAFT_SYSTEM, prompt + error, Draft, effort="high")
        bad = [s.fact for s in d.sentences if not 1 <= s.fact <= len(facts)]
        if d.sentences and not bad:
            draft = d
            break
        error = f"\n\nYour last draft cited facts that don't exist: {bad}. Cite only F1 to F{len(facts)}."
    if draft is None:
        raise ValueError("draft cited nonexistent facts twice")

    async with p.acquire() as conn, conn.transaction():
        await conn.execute("delete from claims where story_id = $1", story["id"])
        for i, s in enumerate(draft.sentences):
            f = facts[s.fact - 1]
            await conn.execute(
                """insert into claims (story_id, position, paragraph, text, fact_id, source_id, quoted_span)
                   values ($1,$2,$3,$4,$5,$6,$7)""",
                story["id"], i, s.paragraph, s.text, f["id"], f["source_id"], f["quoted_span"],
            )
    paragraphs: dict[int, list[str]] = {}
    for s in draft.sentences:
        paragraphs.setdefault(s.paragraph, []).append(s.text)
    body = "\n\n".join(" ".join(paragraphs[k]) for k in sorted(paragraphs))
    if not await db.release(story["id"], worker, status="in_review", resolution="confirmed",
                            headline=draft.headline, body=body):
        raise RuntimeError("lost the lease while drafting")
    await db.event(agent, "draft_submitted", f"{draft.headline} ({len(draft.sentences)} sentences from "
                   f"{len({s.fact for s in draft.sentences})} facts)", story_id=story["id"], lead_id=story["lead_id"])


# --- loop ----------------------------------------------------------------------------------


async def slot(n: int, waker: db.Waker) -> None:
    worker = f"{settings.worker_id}/r{n}"
    while True:
        story = None
        try:
            if await db.over_budget():
                await waker.wait(60)
                continue
            story, job = await db.claim_story(["drafting"], "writing", worker), write
            if story is None:
                story, job = await db.claim_story(["assigned"], "planning", worker), plan
            if story is None:
                await waker.wait(10)
                continue
            async with db.lease_keeper(story["id"], worker):
                await job(story, worker)
        except QuotaExhausted as e:
            log.warning("%s; handing the story back and pausing 15 min", e)
            if story:
                back = "drafting" if story["status"] == "writing" else "assigned"
                await db.release(story["id"], worker, status=back)
            await asyncio.sleep(900)
        except Refused as e:
            log.warning("reporter refused: %s", e)
            if story:
                await db.release(story["id"], worker, status="dormant", resolution="unresolved",
                                 kill_memo=f"Model declined to continue: {e}")
        except Exception:  # noqa: BLE001 - the lease expires and another slot retries
            log.exception("reporter slot %s failed", worker)
            await asyncio.sleep(5)


async def main() -> None:
    waker = await db.Waker("story_ready").start()
    log.info("reporter pool: %d concurrent slots", settings.reporter_concurrency)
    await asyncio.gather(*(slot(n, waker) for n in range(settings.reporter_concurrency)))
