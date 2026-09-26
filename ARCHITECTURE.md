# Architecture — AI newsroom (HackGT 13)

An agent newsroom that runs on a loop, finds its own stories, reports them with open-ended tools, and publishes. The demo is a real run: the system has been going for four hours unattended, and here is what it found, what it killed, and what it published.

It works the way an investigative desk does: **start from a hypothesis, break it into the parts that must be true, send people after each part at once, then decide from what came back.**

---

## 1. The loop

```
TIPSTERS (one per beat, every ~10-15 min, raise a hypothesis the moment they find one)
   look around the beat -> write a falsifiable hypothesis -> score it
        |  INSERT lead -> pg_notify('new_lead')   (milliseconds, not a poll cycle)
        v
MANAGING EDITOR  triage: dedupe (fingerprint + embedding), promote or hold
        |  promoted -> story
        v
REPORTER: PLAN   break the hypothesis into sub-claims, each one checkable on its own
   ├─ A (core)  "Resolution 26-R-3539 exists and calls for a performance audit"
   ├─ B (core)  "The Urban League received $500K tied to the Housing Help Center"
   ├─ C (core)  "The agreements set no placement targets or reporting duties"
   └─ D         "Recipients filed outcome reports"
        |  sub-claims go out in parallel, one scout each (leased)
        v
SCOUTS   each researches ONE sub-claim with open tools and returns
         a verdict (supported / contradicted / not found / held by an office)
         + facts with verbatim quotes + the trail of what it checked
        |  last sub-claim resolved -> story moves to drafting automatically
        v
REPORTER: WRITE  roll the verdicts up (code decides, see §4):
   any core part contradicted       -> KILL, with a memo naming the part that failed
   every core part supported        -> DRAFT, every sentence citing a scout's fact
   core parts unresolved            -> one round of follow-up sub-claims, else PARK
        |
        v
REVIEW PANEL  verifier / skeptic / fairness. Any block -> back to the writer once
        |  all approve
        v
MANAGING EDITOR  deterministic publish gate, then publish and write the timeline
        |
        v
DASHBOARD  live feed, lead queue with scores, each story's hypothesis tree lighting
           up as scouts report, kill memos, reviewer blocks, counters

SHARED MEMORY (mem0) — beat notes, kill-memo lessons, source reliability,
                       editorial precedent. Read/written by every agent.
```

## 2. Principle: no source whitelist

We do not hand agents a list of approved websites. They get instructions about what good reporting requires and open access: internet and a real browser. The agent decides what this particular question needs to check, whether that's a county permit portal, a Reddit thread, a company's own site, or a filing.

**The judgment about where to look is the product.** Anyone can wire up five fixed APIs.

Consequence: we log the agent's stated *reason* for every tool call, not just the call. The persuasive artifact is the trail — "checked X, found nothing, went to Y, then pulled Z" — because without it a judge sees a black box with an article at the end.

Phone and email are out of scope. Every story must be settleable from public records and published material.

## 3. Tipsters

Each tipster owns a beat and loops. Per cycle it looks around, then raises any candidate hypothesis with a score and a justification, immediately rather than at the end of the cycle. Leads below the threshold stay visible in the queue (good demo material: the system visibly making choices).

A lead is not a topic. It must be a **falsifiable hypothesis** with the fields below, or it isn't a lead yet:

```
hypothesis:       "The county paid Vendor X $2.1M with no competitive bid"
why_now:          what surfaced it
who_would_know:   purchasing office, losing bidders
would_settle_it:  the contract file, bid tabulation, commission minutes
sources_seen:     urls already looked at
score:            0-1, computed from components (below)
```

Why this matters: a topic can never be exhausted, so a reporter handed a topic either quits early or digs forever. A hypothesis has three terminal states — **confirmed**, **killed**, **unresolved** — and it can be broken into parts that each have the same three states.

Beat notes live in shared memory (mem0) so tipsters don't rediscover the same thing, and hypotheses are fingerprinted in Postgres so killed ones don't come back.

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
| `novelty` — not already covered, not a near-duplicate of a past lead | code: prior-coverage search hits + max embedding similarity to existing leads | three outlets ran it yesterday | no coverage, nothing similar in `leads` |
| `tip_strength` — quality of what surfaced it | model, informed by mem0 source-reliability notes | anonymous forum post | official record showing an anomaly |
| `timeliness` — how recent `why_now` is | code: age of the triggering item, decaying over ~60 days (local government moves in weeks and months) | a year old | this week |

**Combine** with a weighted geometric mean so a near-zero anywhere drags the whole lead down:

```
score = impact^0.30 · settleability^0.30 · novelty^0.20 · tip_strength^0.10 · timeliness^0.10
```

Settleability is weighted as heavily as impact on purpose: a huge story we can't resolve in four hours burns budget and produces nothing to show.

**Promotion** is threshold + capacity: a lead is promoted when `score >= threshold` *and* the newsroom is under `MAX_ACTIVE_STORIES`; otherwise the highest-scoring waiting lead goes next. The initial threshold (~0.55) comes from a trial run. Components are stored per lead (`score_components`) so after a trial run we can see which component actually predicted published-vs-killed and re-weight.

## 4. Reporting: plan, scout, write

### Plan

When a story is promoted, the reporter's first job is to break the hypothesis into **3–6 sub-claims**. Each sub-claim is one checkable statement with:

```
claim:          "Resolution 26-R-3539 calls for a performance audit of the Housing Help Center"
core:           true if the hypothesis falls without it
where_to_look:  council legislation system, the resolution text and its exhibits
would_confirm:  the resolution text naming the Housing Help Center and an audit
would_refute:   no such resolution, or one about something else
```

Core sub-claims are the parts the hypothesis cannot survive without. Supporting sub-claims add context (prior coverage, the official response, what happens next) and make the story worth reading, but don't decide it.

### Scouts

Each sub-claim is researched by one scout, and all of a story's sub-claims go out at once. A scout gets the hypothesis for context, but its job is only its own question. It uses open tools, records every fact it finds with a verbatim quote, and finishes with one verdict:

| Verdict | Meaning | Requirement |
|---|---|---|
| `supported` | Evidence establishes the sub-claim | at least one recorded fact that supports it |
| `contradicted` | Evidence shows it is false | at least one recorded fact that contradicts it |
| `not_found` | Checked the places that should have it; nothing | a finding saying where it looked |
| `gated` | The evidence exists but is held by an office or person | a finding naming the records and who holds them |

Small, single-question tasks are faster, parallel, and work well even on cheap models. They also make coverage honest: a sub-claim can't be marked resolved without its own evidence.

**When a scout stops.** Each sub-claim has a small budget of research calls (default 10), and the marginal-yield rule applies per sub-claim: if the last N page fetches (default 5; searches don't count) produced no new facts, it's time to give a verdict. When the budget runs out the scout can ask the managing editor for more, stating exactly what it would check next and why that could change the verdict. Dedication where it's earned, rabbit holes cut off. (Judges love watching an agent ask for more time and get told no.)

### Write

When the last sub-claim resolves, the story moves to drafting and a reporter picks it up. **Code, not the model, applies the roll-up:**

| Sub-claim verdicts | Outcome |
|---|---|
| Any core sub-claim contradicted | **Kill.** The memo says which part failed and why. Sometimes the contradiction is itself the story |
| Every core sub-claim supported | **Draft** |
| Some core sub-claims unresolved, follow-up round unused | Send up to 3 **follow-up sub-claims** aimed at the unresolved parts, then roll up again |
| Still unresolved after the follow-up round | **Park** with a wake condition naming the exact records needed |

A follow-up that is supported or contradicted settles the core part it targets.

**Drafting is built from facts only.** The writer is given the scouts' facts, numbered, and every sentence it writes must cite one of them; the sentence's source and quoted span come from that fact. It cannot cite anything a scout didn't fetch and record. The draft follows a news structure: lede, why it matters, the key facts and numbers, background and prior coverage, the official response on record, what happens next.

### Three kinds of "nothing," which need different responses

| Situation | Response |
|---|---|
| No public evidence exists yet | **Park it**, don't kill it. Set a wake condition: a filing appears, a meeting agenda posts, a watched page changes |
| Evidence exists but is gated behind a person or an office | **Park it** with a note naming who holds the evidence. We don't contact people this weekend |
| Evidence checked and contradicts the hypothesis | **Kill it** |

### Kill memo

Every dead story gets one paragraph: what was checked, what was found, which part of the hypothesis failed, what would change our mind. It feeds shared memory so tipsters don't re-raise it, and showing five killed stories with reasoning is what separates this from every "AI writes articles" project in the room.

## 5. Editors

### Managing editor (one)

Coordination must have a single owner; several editors deciding in parallel means races and double-assigned stories.

- Wakes on `new_lead` notifications. Dedupes by exact fingerprint; embedding similarity above the cutoff only flags a possible duplicate, and one model call decides whether it's the same hypothesis. Then promotes or holds.
- Grants or denies scouts' budget appeals.
- Wakes parked stories when their condition fires: their unresolved sub-claims go back out to the scouts.
- Publishes through a deterministic gate: every claim marked verified by the verifier and every quote still found verbatim in its archived source. Reviewers mark claims; code decides. Writes the story's agent timeline.

Mostly code plus one model call per decision, not a full agent.

### Review panel (three, at publish time)

Reviewers differ by **job**, not by political persona. Persona prompts on one model produce correlated, performative disagreement; distinct jobs produce real catches.

- **Verifier** — does each claim's quoted span actually support the claim, including every name, number and qualifier? Sets `claims.verified`.
- **Skeptic** — tries to kill the story: alternative explanations, missing context, weaker reading of the same records.
- **Fairness** — who is affected, is their side present in the record, is the named-person rule followed.

Run reviewers on different models where possible for genuine diversity.

**Rule:** any reviewer can block with a written reason. The story goes back to the writer once; a second block parks it, with the reviewers' reasons as the memo. The verifier's overall verdict is overridden by code: any claim it marks unsupported, or fails to check, is a block. `REVIEWERS=verifier` runs the MVP with the verifier alone.

**Guardrail:** no allegations about named private individuals. Named officials and organizations are allowed, phrased strictly as what the records show. Four hours of unattended publishing with real names is the one way this becomes a problem instead of a win.

## 6. Data model

Full DDL: [`db/schema.sql`](db/schema.sql).

- `sources` — every URL touched; url hash for cross-agent dedupe, archive snapshot, content hash for wake conditions
- `leads` — hypothesis fields, `score` + `score_components`, `fingerprint` (unique), `embedding` (pgvector), status
- `lead_sources` — which sources surfaced a lead
- `stories` — one per lead (`lead_id unique`), status, resolution, kill memo, wake condition, review round, follow-up round
- `sub_claims` — the parts of a hypothesis: claim, core flag, where to look, what would confirm/refute, verdict, finding, per-sub-claim budget, lease
- `tool_calls` — every call with its **reason** (the trail), tied to its story and sub-claim
- `facts` — facts with verbatim quotes, tied to the sub-claim and tool call that produced them
- `claims` — published sentences, each citing one fact's source and quoted span, verified flag
- `budget_appeals` — scouts' requests for more calls on a sub-claim, and the editor's ruling
- `reviews` — panel verdicts per round and role
- `agent_events` — curated dashboard feed
- `counters` — view for the dashboard

Story statuses: `assigned → planning → researching → drafting → writing → in_review → approved → published`, or `killed` / `dormant` (parked).

Coordination state lives in Postgres because it needs exact, immediate answers. Workers claim stories and sub-claims with a lease (`FOR UPDATE SKIP LOCKED`, `lease_owner`, `lease_expires_at`), renewed by a background task for as long as the work runs, so a race has exactly one winner and a dead worker's work is resumed by another. Triggers fire `pg_notify` on new leads, new sub-claims, appeals and story status changes, so the next worker wakes within milliseconds; every worker also polls every 10–30s, so a missed notification costs seconds, not a stuck pipeline. Citations are checked mechanically: every fetched page's text is archived, and a fact is rejected unless its quoted span appears verbatim in the archived text of its source.

### Shared memory (mem0)

What goes in mem0: beat notes, kill-memo lessons, source reliability ("Fulton permit portal needs the browser"), editorial precedent. Scoped by `agent_id` for per-agent notes plus a shared newsroom namespace.

What does not: dedupe, locks, handoffs (mem0 is LLM-extracted and similarity-searched, so it's approximate and lags writes by seconds). Memory is also **never evidence**. Memories are stored as hints about where to look; every published claim still needs a source URL and quoted span.

## 7. Stack

| Layer | Choice |
|---|---|
| Workers | Python 3.12, asyncio. One package (`newsroom/`), one Docker image, role picked at start: `python -m newsroom tipster\|editor\|reporter\|scout\|reviewer` |
| Agent loop | Manual tool loop (`newsroom/llm.py`). Every tool requires a `reason`, which is logged before the tool runs |
| Models | Anthropic (default): Opus 5 writer and skeptic, Sonnet 5 scouts, tipsters, scorer, editor, verifier and fairness. Or Gemini (`LLM_PROVIDER=gemini`) for free-tier testing |
| State | Postgres 17 + pgvector, in the box (Docker) |
| Shared memory | mem0 open source, self-hosted: pgvector store in the same Postgres, local fastembed embeddings |
| Embeddings | fastembed `BAAI/bge-small-en-v1.5` (384 dims), run locally on CPU and baked into the image |
| Web search | Brave Search API (web + news) |
| Browser | Playwright Chromium, running locally in the image |
| Archive | Extracted page text on a Docker volume; quoted spans are checked against it |
| Site + dashboard | Next.js, polling `agent_events` and the `counters` view (not built yet) |
| Runtime | One VM running `docker compose up -d`. Everything restarts on crash and on reboot |

"Offline" here means unattended, with nobody's laptop involved. The box still needs outbound internet for the model API, search, and the sites scouts visit.

### Processes and parallelism

| Role | Parallel? | How |
|---|---|---|
| Tipsters | Yes, every beat at once | One asyncio task per beat. Each beat holds an advisory lock, so a second replica is a hot standby per beat, never a duplicate |
| Managing editor | No, one on purpose | Advisory-lock singleton. A second replica waits and takes over within ~15s if the first dies |
| Reporters | Yes | `replicas × REPORTER_CONCURRENCY` slots, each planning or writing one story under a lease |
| Scouts | Yes, the widest fan-out | `replicas × SCOUT_CONCURRENCY` slots, each researching one sub-claim under a lease. One story's sub-claims are researched at the same time |
| Reviewers | Yes, two levels | `replicas × REVIEWER_CONCURRENCY` stories at once; the reviewers of one story run concurrently |

The number of stories in flight is capped by the editor (`MAX_ACTIVE_STORIES`), not by how many worker slots exist. A global `SPEND_CAP_USD`, checked against `model_usage`, stops promotions, tipsters and new research once reached.

## 8. Demo

Run for four hours untouched before judging. On screen:

1. **Live activity feed** — agents working, with their stated reasons.
2. **Lead queue** — scores and their components, what got promoted, what didn't.
3. **Hypothesis trees** — each story's sub-claims turning green (supported), red (contradicted) or grey (not found / gated) as scouts report, with the evidence behind each.
4. **Published stories** — every sentence's citation clickable to the source.
5. **Kill memos** — what it refused to report, and which part of the hypothesis failed.
6. **Reviewer blocks** — the skeptic stopping a draft, with its reason.
7. **Counters** — sources checked, leads raised, sub-claims resolved, stories published, killed, parked, reviewer blocks, appeals denied.

Freeze code before the run starts. Keep an earlier run's database as a backup demo. Set a hard spend cap across model, search, and browser APIs.

## 9. Legal and safety notes for the weekend

- No contacting people: no email, no phone.
- No CAPTCHA bypassing. Use official APIs and bulk data first.
- No allegations about named private individuals. Officials and organizations: records-only phrasing.

## 10. Open questions

- Tipster cadence and how many beats at once.
- Score threshold and component weights, which we'll only learn from a trial run.
- How many sub-claims a hypothesis needs, and how big each scout's budget should be.
- Which "public discourse" surfaces are reachable cheaply enough to be useful in 24 hours.
- Embedding similarity cutoff for near-duplicate leads.
