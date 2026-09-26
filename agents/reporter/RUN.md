# Running the Reporter on its own

The Reporter owns one McLovin hypothesis from start to finish. Code: [newsroom/reporter_agent.py](../../newsroom/reporter_agent.py).

```
McLovin hypothesis  (+ the signals behind it, from Astra)
   │
   ├─ recall     the desk's memory: past investigations of this or similar hypotheses, and their memos
   ├─ (the Reporter never searches or fetches: every search, record lookup and page read is a scout's)
   ├─ frame      restate it · break it into elements (every word is something to research)
   │             · minimum and maximum story · prior-coverage searches
   ├─ coverage   a coverage scout finds prior reporting and marks which elements are already published
   ├─ new        the newsroom publishes only what no outlet has: at least one scout the minimum story needs
   │             goes after something unpublished (a new record, number, connection, contradiction or
   │             development). If the whole story is already out there, it's SPIKED before any scout goes out
   ├─ size       elements one record settles share a scout; different records get separate scouts;
   │             at least one scout rules out an innocent explanation. Code checks the plan: every
   │             load-bearing element covered, every record McLovin named assigned or deferred with a
   │             reason. The reporter repairs a failing plan; code fills anything still missing.
   ├─ assign     each scout gets: records to try in order (McLovin's first) · the records' own terms ·
   │             what would support it · what would contradict it · a budget weighted by priority
   │
   ├─ rounds (up to 3)
   │     scouts go out in parallel (newsroom DB leads + web)
   │     reporter directs each one: done · redirect (must name a specific next check) · correct · drop
   │     grants a new sub-hypothesis only with named records; spots new angles as spin-offs for McLovin
   │
   │     each scout reports back: verdict (held to the evidence) · summary · not found · dead ends · proposals
   ├─ verdict    by code, from sources that count as proof (government, court, news reporting):
   │               minimum story contradicted → KILL
   │               everything supported → WRITE (maximum story)
   │               the minimum story's part supported → WRITE (minimum story)
   │               otherwise → PARK
   │             and WRITE also needs a new finding: a NEW sub-hypothesis supported by a government or court
   │             record that prior coverage didn't cite. Without one → PARK, however well the rest holds
   │             the model may only downgrade it, writes the memo, and narrows the hypothesis to what the evidence supports
   ├─ draft      every sentence cited, quotes verbatim; must pass newsroom/article.py
   │
   └─ Skeptic + Virality + Novelty (all three are gates)
         all approve → PUBLISHED (Markdown, plus the `articles` collection the website reads, with the checks and the council's verdicts)
         any flags  → back to the Reporter: FIX (revise; may send a scout for a missing fact)
                                              or SPIKE (not worth publishing in any honest form)
         still flagged after 2 revisions → HELD
```

**See it:** the [Reporter Desk](https://claude.ai/artifact/FdH6rhwQZZ2iZGfTfYXYQp) page opens any `result.json` and shows the sizing, the coverage grids, every assignment, each round's decisions, the verdict and the council. (It's private to Srikar until shared from the page's Share menu.)

## Setup (once)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # macOS/Linux: .venv/bin/pip
cp .env.example .env                              # then fill in the keys you use
```

Needs a model (`DEEPSEEK_API_KEY`, or `NEWSROOM_LLM_PROVIDER=claude_code`) and a search backend (`BRAVE_API_KEY`, or `NEWSROOM_SEARCH_BACKEND=browser`). With Astra configured (`ASTRA_DB_ID` + `ASTRA_DB_APPLICATION_TOKEN`), the desk is the `reporter_desk` collection; without it, `runs/desk.json`. To keep experiments out of the real database, prefix commands with `NEWSROOM_DB=/tmp/scratch.db NEWSROOM_DESK=memory`.

## Commands

```bash
# From a McLovin run
.venv/Scripts/python -m newsroom try reporter --from-mclovin latest --pick 1
.venv/Scripts/python -m newsroom try reporter --from-mclovin runs/mclovin/<timestamp>/hypotheses.json --all

# Before McLovin is wired up: the sample handoff (same shape)
.venv/Scripts/python -m newsroom try reporter --from-mclovin tests/fixtures/mclovin_hypotheses.json --pick 1 --budget 6

# Any hypothesis you type
.venv/Scripts/python -m newsroom try reporter --hypothesis "NHTSA has an open investigation into loss of steering on the 2023 Tesla Model 3"

# What's on the desk (from any machine sharing the Astra DB)
.venv/Scripts/python -m newsroom try desk
.venv/Scripts/python -m newsroom try desk --status parked
.venv/Scripts/python -m newsroom try desk --show <desk id> > record.json
```

| Flag | Default | What it does |
|---|---|---|
| `--budget N` | 12 | Base searches plus page reads per scout, per assignment. High priority gets 1.5×, low 0.6×. Use 4–6 while testing |
| `--rounds N` | 3 | Scout rounds before the verdict |
| `--no-write` | off | Stop at the verdict |
| `--judges a,b` | `skeptic,virality,novelty` | Council judges; `--judges ""` skips the council |
| `NEWSROOM_REQUIRE_NEW=0` | on | Environment setting: turns off the new-finding rule, only for testing the rest of the pipeline |
| `--force` | off | Work a hypothesis again that was already finished (here or on the shared desk) |

## From code (McLovin, an orchestrator)

```python
from newsroom import reporter_agent
result = reporter_agent.investigate(hypothesis_dict_or_string, budget=12)
result["final"]["status"]     # published | held | spiked | killed | parked | failed
result["spinoffs"]            # new angles the reporter spotted but didn't chase: hand these back to McLovin
```

It reads from a McLovin hypothesis: `hypothesis` (required), `id`, `why_now`, `accountable_party`, `who_would_know`, `would_settle_it` (these become M1, M2, …) and `evidence_so_far.signal_ids`.

## The scout contract

Each sub-hypothesis carries a structured `assignment`, which is what a scout is given:

```json
{"records_first": ["NHTSA defect investigations file (INV_*), 2023-2026, for 2023 Tesla Model 3", "..."],
 "search_terms": ["engineering analysis", "steering assist"],
 "supports_if": "an investigation number whose vehicle list includes MY2023 Model 3",
 "contradicts_if": "the only steering investigation excludes 2023",
 "next_check": "set on a redirect: the one specific thing to check next"}
```

plus `budget`, `priority`, the signals' pages as `leads` on the first dispatch, and the queries already run (`avoid`). Today `scout.research()` receives it as a written direction (`reporter_agent.assignment_text()`). A rebuilt scout can take the structured version directly. It must return findings as `{url, title, source_type, quote, finding, note}`, with quotes checked against the page.

## What gets recorded

| Where | What |
|---|---|
| `runs/reporter/<ts>/result.json` | Everything: frame, elements, plan, sizing, each sub-hypothesis with its assignment, trail and findings, rounds, verdict and memo, council rounds and triage, the final draft and sources |
| The desk (`reporter_desk` in Astra) | One compact record per investigation, updated at every stage (`status`, `stage`), vector-searchable. No page text |
| SQLite | The story, sub-hypotheses, findings and events: `python -m newsroom show <story id>` |

## Judging a run

Use [rubric.md](rubric.md). `result["checks"]` flags what code can check: an evidence plan was written, every sub-hypothesis got a scout, an innocent explanation was tested, no element was left uncovered, and killed or parked stories have a full memo. The judgment calls are yours: did the sizing reason hold up? Did redirects name new records? Does the verdict match the quotes? Was a spike deserved?
