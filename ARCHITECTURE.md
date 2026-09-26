# Architecture — AI newsroom (HackGT 13)

An agent newsroom that runs on a loop, finds its own stories, reports them with open-ended tools, and publishes. The demo is a real run: the system has been going for four hours unattended, and here is what it found, what it killed, and what it published.

---

## 1. The loop

```
SCOUTS (several, each with a beat, fire every ~10-15 min)
   look for something worth reporting -> score it -> write a lead
        |  score >= threshold
        v
HANDOFF  (lead becomes a story, assigned to one reporter)
        |
        v
REPORTER (one per story, open-ended tools, runs until resolved or out of budget)
   - what's already reported, and by whom
   - what is NOT reported  <- the gap list drives everything after this
   - goes wherever the story needs: web, browser, public discourse, official
     records, filings, company sites, and email or phone calls to real people
   - drafts with a citation on every claim
        |
        v
EDITOR   dedupe, sanity check, publish, write the timeline
        |
        v
DASHBOARD  live activity feed, lead queue with scores, handoffs, kill memos,
           published stories, counters   <- this is what we show the judges
```

## 2. Principle: no source whitelist

We do not hand agents a list of approved websites. They get instructions about what good reporting requires and open access: internet, a real browser, email, and phone. The agent decides what this particular story needs to check, whether that's a county permit portal, a Reddit thread, a company's own site, a filing, or a phone call to the owner.

**The judgment about where to look is the product.** Anyone can wire up five fixed APIs.

Consequence: we log the agent's stated *reason* for every tool call, not just the call. The persuasive artifact is the trail — "checked X, found nothing, went to Y, then emailed Z" — because without it a judge sees a black box with an article at the end.

## 3. Scouts

Each scout owns a beat and loops. Per cycle it looks around, then writes any candidate lead with a score and a justification. Leads below the threshold stay visible in the queue (good demo material: the system visibly making choices).

A lead is not a topic. It must be a **falsifiable hypothesis** with the fields below, or it isn't a lead yet:

```
hypothesis:       "The county paid Vendor X $2.1M with no competitive bid"
why_now:          what surfaced it
who_would_know:   purchasing office, losing bidders
would_settle_it:  the contract file, bid tabulation, commission minutes
sources_seen:     urls already looked at
score:            0-1 with reasoning
```

Why this matters: a topic can never be exhausted, so a reporter handed a topic either quits early or digs forever. A hypothesis has three terminal states — **confirmed**, **killed**, **unresolved**.

Scouts keep beat notes so they don't rediscover the same thing, and hypotheses are fingerprinted so killed ones don't come back.

## 4. Reporter

Runs until the hypothesis resolves or budget runs out. Sequence:

1. **Evidence plan first.** Before reporting, it writes the ranked list of sources that *could* settle the hypothesis. This is what "done" gets measured against.
2. **Prior coverage.** What's already been reported and by whom. Cited, credited, never rewritten as ours.
3. **The gap.** What nobody has established. Everything after this points at the gap.
4. **Go get it.** Open tools. Records and datasets, the browser for things a fetch can't read, public discourse for leads, and email or a phone call when the information is held by a person.
5. **Draft.** Every claim carries the source it came from and the exact span it came from.

### When to stop, and when to keep digging

The hard part. Three mechanisms, all cheap to build:

**a. Plan coverage, not effort.** Stop when every high-value item in the evidence plan has been checked and none of them moved the hypothesis. That's the difference between "I got tired" and "I looked at the things that would have answered this."

**b. Marginal yield.** Log every *new* fact that bears on the hypothesis, tied to the tool call that produced it. If the last N sources produced zero new facts, we're in diminishing returns. This is the empirical version of "have I looked hard enough," it beats a timer, and it graphs well on the dashboard.

**c. Budget with an appeal.** Each story gets a tool-call and wall-clock budget sized by the scout's confidence. When it runs out the reporter can request an extension, but must state what specifically it would check next and why that could change the outcome. The editor grants or denies. Dedication where it's earned, rabbit holes cut off. (Judges love watching an agent ask for more time and get told no.)

### Three kinds of "nothing," which need different responses

| Situation | Response |
|---|---|
| No public evidence exists | **Park it**, don't kill it. Set a wake condition: a filing appears, a meeting happens, a records request comes due, someone replies |
| Evidence exists but is gated behind a person or an office | Not a search problem. Switch to a records request, an email, or a phone call |
| Evidence checked and contradicts the hypothesis | **Kill it.** Sometimes the contradiction is itself the story |

### Kill memo

Every dead story gets one paragraph: what was checked, what was found, what would change its mind. Cheap to write, feeds the beat notes so scouts don't re-raise it, and showing five killed stories with reasoning is what separates this from every "AI writes articles" project in the room.

## 5. Editor

- Blocks two reporters working the same hypothesis (will otherwise happen in the first hour, since scouts overlap).
- Grants or denies budget appeals.
- Publishes, and writes the story's agent timeline.
- Guardrail: unconfirmed allegations about named people get phrased as what the records show, plus a line stating we have requested comment. Four hours of unattended publishing with real names is the one way this becomes a problem instead of a win.

## 6. Data model

- `leads` — the hypothesis fields above, plus status
- `sources_seen` — url hash, first seen (dedupe across scouts)
- `stories` — lead_id, status (assigned / reporting / published / killed / dormant), wake_condition, headline, body
- `claims` — story_id, text, source_url, quoted_span
- `facts` — story_id, fact, from_tool_call (drives marginal yield)
- `agent_events` — agent, action, **reason**, detail, timestamp (drives the dashboard)
- `beat_notes` — per-scout memory
- `budgets` — story_id, granted, used, appeals

## 7. Stack

| Layer | Choice |
|---|---|
| Agent harness | Pi (`pi.dev`), driven in RPC/SDK mode. Minimal, model-agnostic, bash-first, exportable session tree |
| Models | Opus 5 reporter, Sonnet 5 / Haiku 4.5 scouts |
| Orchestrator | Node/TS worker, queue + interval loop, spawns agent sessions |
| State | Postgres (Neon) |
| Web search | Exa or Tavily or Brave Search API |
| Browser | Browserbase or Steel (hosted headless Chrome), or Playwright locally |
| Email | AgentMail (agent inboxes + webhooks so replies wake an agent) |
| Phone | Vapi or Bland |
| Archive | R2 or Vercel Blob for PDFs and screenshots the reporter relied on |
| Site + dashboard | Next.js on Vercel, live feed polling `agent_events` |
| Runtime | Always-on box (Railway / Fly / Hetzner). The loop must survive laptops closing |

## 8. Demo

Run for four hours untouched before judging. On screen:

1. **Live activity feed** — agents working, with their stated reasons.
2. **Lead queue** — scores, what got promoted, what didn't.
3. **Published stories** — every sentence's citation clickable to the source.
4. **Kill memos** — what it refused to report and why.
5. **Counters** — sources checked, leads raised, stories published, stories killed, calls and emails sent.

Freeze code before the run starts. Keep an earlier run's database as a backup demo.

## 9. Legal and safety notes for the weekend

- Phone calls: recording consent is all-party in California and several other states, one-party in Georgia. The agent says it's an AI reporter from the outset and asks before recording.
- Email and calls identify the agent as AI, every time.
- No CAPTCHA bypassing. Use official APIs and bulk data first.
- Named-person allegations: records-only phrasing, comment requested, and log the request.

## 10. Open questions

- Scout cadence and how many beats at once.
- Score threshold for handoff, which we'll only learn from a trial run.
- Which "public discourse" surfaces are reachable cheaply enough to be useful in 24 hours.
- How the reporter decides between an email and a phone call.
- Whether the editor is a separate agent or part of the reporter.
