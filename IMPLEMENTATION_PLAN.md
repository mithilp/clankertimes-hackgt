# HackGT 13 — AI Newsroom Implementation Plan

This plan turns the architecture in `ARCHITECTURE.md` into an execution-first build order for the hackathon.

The core system flow is:

```text
SCOUT
  ↓
MANAGING EDITOR
  ↓
REPORTER
  ↓
REVIEW PANEL
  ↓
PUBLISH / KILL / PARK
  ↓
DASHBOARD
```

The goal is to build the **smallest complete newsroom loop first**, then add the more ambitious features.

**Implementation notes.** The workers are Python (`newsroom/`), run as `python -m newsroom <role>`, against Postgres + pgvector running in Docker on the VM (`docker compose up`). The dashboard is Next.js and talks to the same database. Most later-stage features already exist in code; this plan decides the order in which they are **proven**, and config switches let the early milestones run with them turned down (one beat, one reporter slot, verifier only).

**Safety from the first run.** Three things are not enhancements and are on from the first reporter run: a hard spend cap (`SPEND_CAP_USD`), the named-private-person gate, and the mechanical quote check (Step 7).

---

## 1. Build Order

Implement in this order:

```text
1. Database schema
2. Shared event logger
3. One scout
4. Managing editor
5. One reporter
6. One verifier
7. Publishing pipeline
8. Dashboard
9. Multiple scouts
10. Full review panel
11. mem0
12. pgvector dedupe
13. Budget appeals
14. Wake conditions
```

Do **not** start with full multi-agent orchestration.

The first major milestone is:

> One scout discovers one lead, one reporter investigates it, one reviewer verifies it, and the story appears on the dashboard with citations.

If that works, the project works.

---

# 2. Step 1 — Database

Implement the database first because almost every component depends on it.

The full architecture calls for:

```text
sources
leads
lead_sources
stories
evidence_plan
tool_calls
facts
claims
budgets
budget_appeals
reviews
agent_events
```

For the first version, implement only:

```text
sources
leads
stories
evidence_plan
tool_calls
facts
claims
reviews
agent_events
```

Delay:

```text
budgets
budget_appeals
embeddings
mem0
wake conditions
```

## `leads`

Minimum fields:

```text
id
hypothesis
why_now
who_would_know
would_settle_it
score
score_components
fingerprint
status
created_at
```

Use statuses:

```text
new              (candidate, not yet triaged)
below_threshold  (held)
duplicate
promoted         (assigned: a story row exists)
```

## `stories`

Minimum fields:

```text
id
lead_id UNIQUE
status
resolution
headline
body
kill_memo
created_at
updated_at
```

Story statuses:

```text
assigned
reporting
in_review
approved     (passed review, waiting for the deterministic publish gate)
published
killed
dormant      (parked)
```

The `UNIQUE (lead_id)` constraint prevents duplicate stories from being created for the same lead.

Reporters should claim work atomically:

```sql
UPDATE stories
SET status = 'reporting'
WHERE id = $1
  AND status = 'assigned'
RETURNING *;
```

This guarantees that two reporters cannot claim the same story.

In the code this is a lease (`db.claim_story`): the claim also sets `lease_owner` and `lease_expires_at`, so a story whose reporter crashes is picked up again by another reporter.

## Acceptance Test

Before building agents, you should be able to manually move a lead through:

```text
create lead
→ assign lead
→ create story
→ claim story
→ move story to review
→ publish story
```

---

# 3. Step 2 — Event Logging

Build event logging immediately.

Every important action should write to:

```text
agent_events
```

Example:

```json
{
  "agent": "scout-local-gov",
  "action": "search",
  "reason": "Looking for recently posted procurement awards",
  "detail": "Searching county purchasing portal"
}
```

Create one shared helper:

```python
await db.event(agent, action, reason, story_id=..., lead_id=..., detail=...)
```

Every agent and tool wrapper should call it.

The dashboard should show this event trail so the judges can see not just the result, but how the system got there.

## Acceptance Test

Run:

```python
await db.event("test", "hello", "checking the event log")
```

and confirm the row appears in Postgres.

---

# 4. Step 3 — Build One Scout

Start with **one beat only**.

Possible first beat:

```text
Atlanta / Georgia public records
```

The scout loop:

```text
SEARCH
 ↓
READ promising source
 ↓
IDENTIFY anomaly or change
 ↓
FORM falsifiable hypothesis
 ↓
SCORE components
 ↓
INSERT lead
```

Every lead must include:

```text
hypothesis
why_now
who_would_know
would_settle_it
sources_seen
score
```

Hard gates, checked before scoring (a failed gate scores 0 and is never promoted):

```text
fields are specific, not a topic
the settling evidence is public (we never contact people)
no allegation of wrongdoing against a named private individual
```

Do not allow vague topics such as:

```text
"Investigate MARTA spending."
```

Require falsifiable hypotheses such as:

```text
"MARTA awarded Contract X for $Y under condition Z."
```

A lead must be capable of reaching one of three states:

```text
confirmed
killed
unresolved
```

## Scout Scoring

Use these components:

```text
impact
settleability
novelty
tip_strength
timeliness
```

For the MVP, the model can score most components while novelty can initially be approximated with deterministic logic.

Use the architecture's weighted geometric mean:

```text
score =
impact^0.30
× settleability^0.30
× novelty^0.20
× tip_strength^0.10
× timeliness^0.10
```

## Acceptance Test

Running:

```bash
python -m newsroom scout
```

with a single beat in `config/beats.json` should insert at least one valid lead into Postgres.

## Quality Test

Inserting a lead is not the bar; the leads have to be worth reporting. Run one scout for two or three cycles on a beat someone on the team knows, then rate 10 leads by hand:

```text
specific and falsifiable?
settleable from public records within hours?
not already covered by local outlets?
```

If fewer than about half pass, fix the scout prompt before building on top of it. Every later stage inherits lead quality.

---

# 5. Step 4 — Managing Editor

Do **not** make the managing editor a fully autonomous agent.

Keep most of it deterministic.

Flow:

```text
new lead
   ↓
validate required fields
   ↓
check hard gates
   ↓
check duplicate
   ↓
check score threshold
   ↓
reporter available?
   ↓
assign / hold
```

Pseudo-code:

```ts
async function triageLead(lead) {
  if (!passesHardGates(lead)) {
    return rejectLead(lead);
  }

  if (await isDuplicate(lead)) {
    return holdLead(lead);
  }

  if (lead.score < THRESHOLD) {
    return holdLead(lead);
  }

  if (!reporterAvailable()) {
    return holdLead(lead);
  }

  return assignLead(lead);
}
```

Initial threshold:

```text
0.55
```

For the MVP, use normalized hypothesis fingerprints for dedupe.

Add vector similarity later.

## Acceptance Test

Insert three leads:

- one below threshold
- one above threshold
- one duplicate

Confirm they are routed correctly.

---

# 6. Step 5 — Reporter

The reporter is the core of the project.

Do not simply prompt it with:

> Research this story.

Give it a structured loop:

```text
LOAD hypothesis
     ↓
CREATE evidence plan
     ↓
CHECK prior coverage
     ↓
IDENTIFY gap
     ↓
CHOOSE next source
     ↓
CALL tool
     ↓
EXTRACT facts
     ↓
UPDATE evidence plan
     ↓
repeat
     ↓
CONFIRM / KILL / PARK
```

## Evidence Plan

Before searching, require the reporter to create a ranked evidence plan.

Example:

```json
[
  {
    "source": "county contract database",
    "priority": 1,
    "reason": "Would establish contract amount",
    "checked": false
  },
  {
    "source": "commission meeting minutes",
    "priority": 2,
    "reason": "Could establish approval process",
    "checked": false
  }
]
```

Store these rows in:

```text
evidence_plan
```

This gives the reporter a concrete definition of what "done" means.

## Acceptance Test

Given a manual lead, the reporter should create an evidence plan before making any search/browser call.

---

# 7. Step 6 — Tool Wrapper

Every external tool call should go through one shared wrapper:

```ts
executeTool({
  agent,
  tool,
  reason,
  args
})
```

The wrapper should:

```text
1. log the reason
2. execute the tool
3. save the source
4. save result metadata
5. return the result
```

Do not allow agents to bypass this wrapper.

This guarantees that the investigation trail remains visible and auditable.

---

# 8. Step 7 — Facts and Citations

Every relevant discovery becomes a structured fact.

Example:

```json
{
  "fact": "The contract value was $2.1 million.",
  "source_url": "...",
  "quoted_span": "...",
  "tool_call_id": 183
}
```

The flow should be:

```text
sources
  ↓
facts
  ↓
claims
  ↓
article
```

Do not let the reporter draft directly from memory.

Every factual claim should remain traceable to source evidence.

## Mechanical Quote Check

Every page a tool fetches is archived as extracted text. A fact or claim is rejected by code unless its `quoted_span` appears verbatim (ignoring whitespace) in the archived text of its source. This is cheap, deterministic, and a much stronger guarantee than an LLM reviewer alone; it is on from the first run.

---

# 9. Step 8 — Reporter Stopping Rules

Implement two stopping rules first.

## Rule A — Evidence Coverage

Stop when:

```text
all high-priority evidence_plan items have been checked
```

## Rule B — Diminishing Returns

Track:

```text
new facts per source
```

If:

```text
last 5 page fetches (fetch_url / browse) → 0 relevant new facts
```

stop. Searches don't count: records work often needs a search, a fetch and a browser visit before anything turns up.

Do **not** build budget appeals yet, but do have a **hard cap** from the first run: a per-story research-call budget and the global `SPEND_CAP_USD`. Appeals come later; the cap does not.

---

# 10. Step 9 — Story Resolution

The reporter must output exactly one:

```text
CONFIRMED
KILLED
PARKED
```

## Confirmed

Enough public evidence supports the hypothesis.

## Killed

Evidence contradicts the hypothesis.

Generate a:

```text
kill_memo
```

with:

```text
what was checked
what was found
why the hypothesis failed
what evidence could change the conclusion
```

## Parked

The evidence is not currently available or accessible.

Parked is distinct from killed.

---

# 11. Step 10 — Draft Generation

Only confirmed stories proceed to drafting.

Generate:

```text
headline
body
claims[]
```

Every factual claim should include:

```text
text
source URL
quoted span
```

Do not rely on model memory as evidence.

---

# 12. Step 11 — One Reviewer

Start with the **Verifier** only.

Input:

```text
claim
quoted source text
```

The verifier also enforces the named-person rule from the first run: block any claim alleging wrongdoing by a named private individual, and any claim about an official or organization that isn't attributed to the record.

Output:

```json
{
  "claim_id": 88,
  "verdict": "approve",
  "reason": "Quoted text directly supports claim."
}
```

or:

```json
{
  "claim_id": 88,
  "verdict": "block",
  "reason": "Source says proposed, but claim says approved."
}
```

If blocked:

```text
reporter revises once
```

If blocked again:

```text
park the story
```

## Acceptance Test

Create one intentionally unsupported claim and confirm the verifier blocks it. Create one claim naming a private individual and confirm it is blocked too.

Run the MVP with the verifier alone: `REVIEWERS=verifier`.

---

# 13. Step 12 — Publish

Publication should be deterministic code.

Example:

```python
if story.status == "approved" and all_claims_verified(story) and all_quotes_found(story):
    publish(story)
```

Do not let the LLM decide whether the story passed review. Reviewers only mark claims; the managing editor's publish gate is code.

---

# 14. Step 13 — Dashboard

Build the demo around the system that already works.

**Start this at hour 0, not hour 12.** It is what the judges see. Build it against seeded rows (the schema, `agent_events` and the `counters` view already exist), then switch to live data once the pipeline runs.

## Screen 1 — Newsroom Overview

Show counters such as:

```text
12 leads found
4 promoted
2 reporting
1 published
1 killed
43 sources checked
```

## Screen 2 — Lead Queue

Show:

```text
Hypothesis
Score
Impact
Settleability
Novelty
Status
```

## Screen 3 — Investigation Timeline

This is one of the most important screens.

Example:

```text
10:31 Scout found procurement record

10:32 Lead score: 0.72

10:32 Managing editor promoted lead

10:33 Reporter created evidence plan

10:34 Searching procurement portal
      Reason: verify bidding process

10:36 Found contract award

10:37 New fact recorded

10:41 Reporter searched meeting minutes

10:47 Story confirmed

10:49 Verifier blocked claim #3

10:51 Reporter revised claim

10:52 Story published
```

This makes the autonomy visible.

## Screen 4 — Article

Each sentence should expose:

```text
Source
Quoted evidence
Verified ✓
```

## Screen 5 — Killed Stories

Show the kill memo prominently.

---

# 15. Step 14 — Add Multiple Scouts

Once one scout works reliably, add several beats.

Example:

```text
Scout 1 — local government
Scout 2 — transportation
Scout 3 — business/regulatory
Scout 4 — Georgia Tech / education
```

All scouts should write into the same:

```text
leads
```

table.

Do not create separate orchestration systems for each scout.

---

# 16. Step 15 — Postgres Notifications

Add:

```sql
NOTIFY new_lead;
```

and:

```text
LISTEN new_lead
```

The flow becomes:

```text
Scout INSERT
   ↓
Postgres NOTIFY
   ↓
Managing editor wakes
   ↓
triage immediately
```

Treat notifications as an optimization.

Use:

```text
LISTEN/NOTIFY = responsiveness
Postgres state = source of truth
2-second polling = fallback
```

The worker should query pending work after reconnecting or restarting.

---

# 17. Step 16 — Deduplication

Implement in two stages.

## Stage 1

```text
exact normalized fingerprint
```

If fingerprints match:

```text
duplicate
```

## Stage 2

Add:

```text
pgvector similarity
```

Flow:

```text
fingerprint identical
→ duplicate

otherwise

embedding similarity > cutoff
→ ask editor whether it is the same hypothesis
```

Do not let vector similarity automatically mark or delete leads; above the cutoff, a model call decides whether the two are the same hypothesis.

---

# 18. Step 17 — Full Review Panel

Once the verifier works, add:

```text
Verifier
Skeptic
Fairness
```

Their jobs must remain distinct.

Suggested output:

```json
{
  "reviewer": "skeptic",
  "verdict": "block",
  "reason": "...",
  "affected_claims": [4, 6],
  "required_fix": "..."
}
```

Run all three concurrently.

Any reviewer can block.

---

# 19. Step 18 — mem0

Add shared memory only after the core system works.

Put into mem0:

```text
beat notes
kill-memo lessons
source reliability
editorial precedent
```

Do **not** put into mem0:

```text
locks
handoffs
dedupe
story state
claim evidence
```

Use Postgres for exact coordination.

Use mem0 only for approximate retrieval of useful newsroom context.

Memory must never count as publishable evidence.

---

# 20. Step 19 — Budget System

Add:

```text
budget
used
remaining
```

Track:

```text
model calls
search calls
browser calls
wall-clock time
```

When the reporter runs out:

```text
Reporter:
"I want 5 more calls because I still need X,
which could determine Y."

        ↓

Managing editor:
APPROVE / DENY
```

Show appeals and decisions on the dashboard.

---

# 21. Step 20 — Wake Conditions

Add parked-story wake conditions last.

Examples:

```text
URL changes
new filing appears
meeting agenda posts
```

Store (as implemented):

```text
wake_condition    human-readable condition
wake_source_id    page to re-hash (sources.content_hash holds the last hash)
wake_at           time-based wake
```

The worker periodically checks these conditions.

When triggered:

```text
parked
  ↓
assigned
  ↓
reporting
```

---

# 22. Team Split

For a 4-person team:

| Person | Owns |
|---|---|
| 1 | Postgres, orchestration, state machine |
| 2 | Scout and reporter agent logic |
| 3 | Search/browser tooling and reviewer agents |
| 4 | Next.js dashboard and article UI |

Integrate through the database rather than creating tight dependencies between teammates' code.

---

# 23. Hackathon Execution Schedule

The unattended run needs **four hours before judging**, so the code freezes at about **hour 19**, not hour 24. Anything not working by then is cut, not rushed. Hours are from the start of hacking; shift them if judging isn't at hour 24.

The dashboard (person 4) runs in parallel the whole time, against seeded data until the pipeline produces real rows.

## Hours 0–2

```text
repo, VM, Postgres (docker compose), API keys
schema
event logging
dashboard skeleton on seeded data (parallel)
```

## Hours 2–5

```text
one scout, one beat
lead insertion + hard gates
scout quality test (rate 10 leads by hand)
editor triage
story claiming
```

## Hours 5–9

```text
reporter (one slot)
search, fetch, browser
evidence plan
facts + mechanical quote check
spend cap on
```

### Hour 9 Checkpoint

You should have:

```text
Scout → Reporter → Result
```

If not, stop adding features.

## Hours 9–12

```text
verifier (REVIEWERS=verifier)
drafting
deterministic publish gate
```

### Hour 12 Checkpoint

Target:

```text
Scout
↓
Editor
↓
Reporter
↓
Verifier
↓
Published Story
```

## Hours 12–14 — Trial Run

Run the MVP unattended for 1–2 hours with 2–3 beats. Watch it. Write down:

```text
lead scores vs. which leads were actually good (tune PROMOTE_THRESHOLD)
reporters stopping too early or too late (tune stopping rules)
what broke
```

The trial's findings are the priority list for the next five hours, ahead of anything below.

## Hours 14–19

In priority order; drop from the bottom:

```text
fixes from the trial run
dashboard: timeline, lead queue, story page, kill memos
multiple scouts
full review panel (skeptic, fairness)
dedupe (pgvector + editor confirmation)
notifications (LISTEN/NOTIFY)
mem0
budget appeals
wake conditions
```

## Hour 19 — Freeze

```text
freeze code
snapshot the trial-run database as the backup demo
start the unattended run
```

Do not introduce new architecture after this point.

## Hours 19–24

```text
the run
watch it, fix only crashes (restart, don't redeploy features)
prepare the demo walkthrough
```

---

# 24. Critical-Path Checklist

- [ ] Postgres schema exists
- [ ] API keys set on the box (Anthropic, Brave)
- [ ] Spend cap set
- [ ] Events can be logged
- [ ] Scout creates a valid hypothesis
- [ ] Scout passes the quality test (at least half of 10 leads usable)
- [ ] Named-private-person gate rejects a test lead
- [ ] Lead is scored
- [ ] Editor promotes or holds it
- [ ] Reporter atomically claims it
- [ ] Reporter writes evidence plan
- [ ] Reporter searches the web
- [ ] Tool reasons are logged
- [ ] Facts are stored
- [ ] Claims include sources and quoted spans
- [ ] Quote check rejects a paraphrased span
- [ ] Reporter confirms, kills, or parks
- [ ] Verifier checks claims
- [ ] Story publishes through the deterministic gate
- [ ] Trial run done, threshold tuned
- [ ] Dashboard displays investigation timeline
- [ ] Kill memo displays
- [ ] Multiple scouts work
- [ ] Reviewer panel works
- [ ] Deduplication works
- [ ] mem0 works
- [ ] Budget appeals work
- [ ] Wake conditions work

The critical breakpoint is:

```text
Scout → Editor → Reporter → Verifier → Published Story
```

Everything after that is enhancement.

If this path works with a visible event trail and clickable evidence, the core demo is complete.
