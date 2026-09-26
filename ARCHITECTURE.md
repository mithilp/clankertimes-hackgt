# Architecture — AI newsroom (HackGT 13)

An agent newsroom that runs on a loop, finds its own stories, reports them with open-ended tools, and publishes. The demo is a real run: the system has been going for four hours unattended, and here is what it found, what it killed, and what it published.

---

## 1. The loop

```
SCOUTS (several, each with a beat, fire every ~10-15 min, flash leads immediately)
   look for something worth reporting -> score it -> write a lead
        |  INSERT lead -> pg_notify('new_lead')   (milliseconds, not a poll cycle)
        v
MANAGING EDITOR  triage: dedupe (fingerprint + embedding), promote or hold
        |  promoted
        v
REPORTER (one per story, open-ended tools, runs until resolved or out of budget)
   - what's already reported, and by whom
   - what is NOT reported  <- the gap list drives everything after this
   - goes wherever the story needs: web, browser, public discourse, official
     records, filings, company sites
   - drafts with a citation on every claim
        |
        v
REVIEW PANEL  verifier / skeptic / fairness. Any block -> back to reporter once
        |  all approve
        v
MANAGING EDITOR  publish, write the timeline
        |
        v
DASHBOARD  live activity feed, lead queue with scores, handoffs, kill memos,
           reviewer blocks, published stories, counters   <- what we show judges

SHARED MEMORY (mem0) — beat notes, kill-memo lessons, source reliability,
                       editorial precedent. Read/written by every agent.
```

## 2. Principle: no source whitelist

We do not hand agents a list of approved websites. They get instructions about what good reporting requires and open access: internet and a real browser. The agent decides what this particular story needs to check, whether that's a county permit portal, a Reddit thread, a company's own site, or a filing.

**The judgment about where to look is the product.** Anyone can wire up five fixed APIs.

Consequence: we log the agent's stated *reason* for every tool call, not just the call. The persuasive artifact is the trail — "checked X, found nothing, went to Y, then pulled Z" — because without it a judge sees a black box with an article at the end.

Phone and email are out of scope. Every story must be settleable from public records and published material.

## 3. Scouts

Each scout owns a beat and loops. Per cycle it looks around, then writes any candidate lead with a score and a justification. Leads below the threshold stay visible in the queue (good demo material: the system visibly making choices). A scout that finds something strong mid-cycle writes it immediately (a flash lead) rather than waiting for the cycle to end.

A lead is not a topic. It must be a **falsifiable hypothesis** with the fields below, or it isn't a lead yet:

```
hypothesis:       "The county paid Vendor X $2.1M with no competitive bid"
why_now:          what surfaced it
who_would_know:   purchasing office, losing bidders
would_settle_it:  the contract file, bid tabulation, commission minutes
sources_seen:     urls already looked at
score:            0-1, computed from components (below)
```

Why this matters: a topic can never be exhausted, so a reporter handed a topic either quits early or digs forever. A hypothesis has three terminal states — **confirmed**, **killed**, **unresolved**.

Beat notes live in shared memory (mem0) so scouts don't rediscover the same thing, and hypotheses are fingerprinted in Postgres so killed ones don't come back.

### Scoring a lead

The model never emits a single holistic number; LLMs are badly calibrated at that. It rates components against anchored rubrics, each with a one-line reason, and code combines them. Parts that can be measured are measured, not asked.

**Hard gates** (fail any → not promoted, score 0, reason logged):
- A required field is missing or vague ("look into the budget").
- `would_settle_it` depends only on non-public evidence (a person, a phone call, a sealed record).
- The hypothesis alleges wrongdoing by a named private individual.

**Components** (each 0–1):

| Component | Who scores it | 0.2 looks like | 0.9 looks like |
|---|---|---|---|
| `impact` — public money, people affected, officials involved | model | a restaurant changed its hours | $2M no-bid contract, county commission |
| `settleability` — is the settling evidence public, specific, reachable in hours | model | "internal emails would show it" | named portal + document type + date range |
| `novelty` — not already covered, not a near-duplicate of a past lead | code: prior-coverage search hits + max embedding similarity to existing leads + mem0 kill lessons | three outlets ran it yesterday | no coverage, nothing similar in `leads` |
| `tip_strength` — quality of what surfaced it | model, informed by mem0 source-reliability notes | anonymous forum post | official record showing an anomaly |
| `timeliness` — how recent `why_now` is | code: age of the triggering item | months old | today |

**Combine** with a weighted geometric mean so a near-zero anywhere drags the whole lead down:

```
score = impact^0.30 · settleability^0.30 · novelty^0.20 · tip_strength^0.10 · timeliness^0.10
```

Settleability is weighted as heavily as impact on purpose: a huge story we can't resolve in four hours burns budget and produces nothing to show.

**Promotion** is threshold + capacity: a lead is promoted when `score >= threshold` *and* a reporter slot is free; otherwise the highest-scoring waiting lead goes next. The initial threshold (~0.55) comes from a trial run. Each rubric prompt carries three or four scored example leads as anchors, and components are stored per lead (`score_components`) so we can see after the trial run which component actually predicted published-vs-killed and re-weight.

The score also sizes the reporter's starting budget (§4c).

## 4. Reporter

Runs until the hypothesis resolves or budget runs out. Sequence:

1. **Evidence plan first.** Before reporting, it writes the ranked list of sources that *could* settle the hypothesis. This is what "done" gets measured against.
2. **Prior coverage.** What's already been reported and by whom. Cited, credited, never rewritten as ours.
3. **The gap.** What nobody has established. Everything after this points at the gap.
4. **Go get it.** Open tools. Records and datasets, the browser for things a fetch can't read, public discourse for leads. Shared memory for hints about where to look.
5. **Draft.** Every claim carries the source it came from and the exact span it came from.

### When to stop, and when to keep digging

The hard part. Three mechanisms, all cheap to build:

**a. Plan coverage, not effort.** Stop when every high-value item in the evidence plan has been checked and none of them moved the hypothesis. That's the difference between "I got tired" and "I looked at the things that would have answered this."

**b. Marginal yield.** Log every *new* fact that bears on the hypothesis, tied to the tool call that produced it. If the last N page fetches (default 5; searches don't count) produced zero new facts, we're in diminishing returns. Facts carry a verbatim quote, checked against the fetched page like claims are. This is the empirical version of "have I looked hard enough," it beats a timer, and it graphs well on the dashboard.

**c. Budget with an appeal.** Each story gets a tool-call and wall-clock budget sized by the lead's score. When it runs out the reporter can request an extension, but must state what specifically it would check next and why that could change the outcome. The managing editor grants or denies. Dedication where it's earned, rabbit holes cut off. (Judges love watching an agent ask for more time and get told no.)

### Three kinds of "nothing," which need different responses

| Situation | Response |
|---|---|
| No public evidence exists yet | **Park it**, don't kill it. Set a wake condition: a filing appears, a meeting agenda posts, a watched page changes |
| Evidence exists but is gated behind a person or an office | **Park it** with a note naming who holds the evidence. We don't contact people this weekend |
| Evidence checked and contradicts the hypothesis | **Kill it.** Sometimes the contradiction is itself the story |

### Kill memo

Every dead story gets one paragraph: what was checked, what was found, what would change its mind. Cheap to write, feeds shared memory so scouts don't re-raise it, and showing five killed stories with reasoning is what separates this from every "AI writes articles" project in the room.

## 5. Editors

### Managing editor (one)

Coordination must have a single owner; several editors deciding in parallel means races and double-assigned stories.

- Wakes on `new_lead` notifications. Dedupes by exact fingerprint; embedding similarity above the cutoff only flags a possible duplicate, and one model call decides whether it's the same hypothesis. Then promotes or holds.
- Blocks two reporters working the same hypothesis (enforced in the database too — see §6).
- Grants or denies budget appeals.
- Publishes through a deterministic gate: every claim marked verified by the verifier and every quote still found verbatim in its archived source. Reviewers mark claims; code decides. Writes the story's agent timeline.

Mostly code plus one model call per decision, not a full agent.

### Review panel (three, at publish time)

Reviewers differ by **job**, not by political persona. Persona prompts on one model produce correlated, performative disagreement; distinct jobs produce real catches.

- **Verifier** — does each claim's quoted span actually support the claim? Sets `claims.verified`.
- **Skeptic** — tries to kill the story: alternative explanations, missing context, weaker reading of the same records.
- **Fairness** — who is affected, is their side present in the record, is the named-person rule followed.

Run reviewers on different models where possible for genuine diversity.

**Rule:** any reviewer can block with a written reason. The story goes back to the reporter once, with a half budget top-up; a second block parks it, with the reviewers' reasons as the memo. The verifier's overall verdict is overridden by code: any claim it marks unsupported, or fails to check, is a block. `REVIEWERS=verifier` runs the MVP with the verifier alone.

**Guardrail:** no allegations about named private individuals. Named officials and organizations are allowed, phrased strictly as what the records show. Four hours of unattended publishing with real names is the one way this becomes a problem instead of a win.

## 6. Data model

Full DDL: [`db/schema.sql`](db/schema.sql).

- `sources` — every URL touched; url hash for cross-agent dedupe, archive snapshot, content hash for wake conditions
- `leads` — hypothesis fields, `score` + `score_components`, `fingerprint` (unique), `embedding` (pgvector), status
- `lead_sources` — which sources surfaced a lead
- `stories` — one per lead (`lead_id unique`), status, resolution, kill memo, wake condition, review round
- `evidence_plan` — ranked items and their checked status (stopping rule a)
- `tool_calls` — every call with its **reason** (the trail)
- `facts` — new facts tied to the tool call that produced them (stopping rule b)
- `claims` — published sentences, source, quoted span, verified flag
- `budgets`, `budget_appeals` — stopping rule c
- `reviews` — panel verdicts per round and role
- `agent_events` — curated dashboard feed
- `counters` — view for the dashboard

Coordination state lives in Postgres because it needs exact, immediate answers. Workers claim stories with a lease (`FOR UPDATE SKIP LOCKED`, `lease_owner`, `lease_expires_at`), so a race has exactly one winner and a dead worker's story is resumed by another. Triggers fire `pg_notify` on new leads, appeals and story status changes, so the next worker wakes within milliseconds; every worker also polls every 10–30s, so a missed notification costs seconds, not a stuck pipeline. Citations are checked mechanically: every fetched page's text is archived, and a draft is rejected unless each claim's quoted span appears verbatim in the archived text of its source.

### Shared memory (mem0)

What goes in mem0: beat notes, kill-memo lessons, source reliability ("Fulton permit portal needs the browser"), editorial precedent. Scoped by `agent_id` for per-agent notes plus a shared newsroom namespace.

What does not: dedupe, locks, handoffs (mem0 is LLM-extracted and similarity-searched, so it's approximate and lags writes by seconds). Memory is also **never evidence**. Memories are stored as hints about where to look; every published claim still needs a source URL and quoted span. Memory reads are logged as `tool_calls` (`tool = 'mem0.search'`) so the trail shows when an agent acted on something it remembered.

## 7. Stack

| Layer | Choice |
|---|---|
| Workers | Python 3.12, asyncio. One package (`newsroom/`), one Docker image, role picked at start: `python -m newsroom scout\|editor\|reporter\|reviewer` |
| Agent loop | Anthropic Python SDK, manual tool loop (`newsroom/llm.py`). Every tool requires a `reason`, which is logged before the tool runs |
| Models | Opus 5 reporter and skeptic (with server-side refusal fallback), Sonnet 5 scouts, scorer, editor, verifier and fairness reviewers, Haiku 4.5 for mem0's memory extraction |
| State | Postgres 17 + pgvector, in the box (Docker) |
| Shared memory | mem0 open source, self-hosted: pgvector store in the same Postgres, local fastembed embeddings |
| Embeddings | fastembed `BAAI/bge-small-en-v1.5` (384 dims), run locally on CPU and baked into the image |
| Web search | Brave Search API (web + news) |
| Browser | Playwright Chromium, running locally in the image |
| Archive | Extracted page text on a Docker volume; quoted spans are checked against it |
| Site + dashboard | Next.js, polling `agent_events` and the `counters` view (not built yet) |
| Runtime | One VM running `docker compose up -d`. Everything restarts on crash and on reboot |

"Offline" here means unattended, with nobody's laptop involved. The box still needs outbound internet for the Claude API, search, and the sites reporters visit.

### Processes and parallelism

| Role | Parallel? | How |
|---|---|---|
| Scouts | Yes, every beat at once | One asyncio task per beat. Each beat holds an advisory lock, so a second replica is a hot standby per beat, never a duplicate |
| Managing editor | No, one on purpose | Advisory-lock singleton. A second replica waits and takes over within ~15s if the first dies |
| Reporters | Yes | `replicas × REPORTER_CONCURRENCY` slots, each working one story under a lease. Parallel tool calls within one turn also run concurrently |
| Reviewers | Yes, two levels | `replicas × REVIEWER_CONCURRENCY` stories at once; the three reviewers of one story run concurrently |

The number of stories in flight is capped by the editor (`MAX_ACTIVE_STORIES`), not by how many reporter slots exist. A global `SPEND_CAP_USD`, checked against `model_usage`, stops promotions, scouting and new reporting once reached.

## 8. Demo

Run for four hours untouched before judging. On screen:

1. **Live activity feed** — agents working, with their stated reasons.
2. **Lead queue** — scores and their components, what got promoted, what didn't.
3. **Published stories** — every sentence's citation clickable to the source.
4. **Kill memos** — what it refused to report and why.
5. **Reviewer blocks** — the skeptic stopping a draft, with its reason.
6. **Counters** — sources checked, leads raised, stories published, killed, parked, reviewer blocks, appeals denied.

Freeze code before the run starts. Keep an earlier run's database as a backup demo. Set a hard spend cap across model, search, and browser APIs.

## 9. Legal and safety notes for the weekend

- No contacting people: no email, no phone.
- No CAPTCHA bypassing. Use official APIs and bulk data first.
- No allegations about named private individuals. Officials and organizations: records-only phrasing.

## 10. Open questions

- Scout cadence and how many beats at once.
- Score threshold and component weights, which we'll only learn from a trial run.
- Which "public discourse" surfaces are reachable cheaply enough to be useful in 24 hours.
- Embedding similarity cutoff for near-duplicate leads.
