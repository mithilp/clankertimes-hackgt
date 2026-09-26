# Running Bossman on its own

Bossman pulls what's live right now from free public sources, decides which items are worth keeping, and writes them to the signals store as **signals**.

## Setup (once)

```bash
cd clankertimes-hackgt
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in what you use
```

Pick a model provider in `.env` or your shell:

| Provider | Set | Pays with |
|---|---|---|
| DeepSeek (default) | `DEEPSEEK_API_KEY=...` | DeepSeek credits |
| Claude Code | `NEWSROOM_LLM_PROVIDER=claude_code` | your Claude Pro/Max plan (needs `claude` installed and logged in) |

To keep experiments out of the real database, prefix commands with `NEWSROOM_DB=/tmp/scratch.db`.

Every run prints which provider and models it used, and saves its inputs and outputs under `runs/<agent>/<timestamp>/` (git-ignored).

## Commands

**One pass, all sources:**

```bash
.venv/bin/python -m newsroom try bossman
```

**Only some sources** (faster and cheaper while tuning):

```bash
.venv/bin/python -m newsroom try bossman --sources google_trends,reddit,gov
```

Sources: `google_trends`, `google_news`, `bluesky`, `reddit`, `polymarket`, `gov` (Federal Register), `hacker_news`. None needs an API key.

**Focus on a beat.** A beat is a file in [beats/](beats/) that says what this Bossman watches, what "interesting" means there, what to skip, and where to look:

```bash
.venv/bin/python -m newsroom try bossman --beat georgia-tech
```

With a beat, the fixed national feeds are replaced by a small loop. It plans 8–14 calls from the beat (news searches, subreddits, RSS feeds, Federal Register phrase searches, web searches), runs them, and judges what came back. Then it looks back at which calls were dead ends, runs up to 4 follow-ups, and merges signals that describe the same event. The run folder also gets `plan.json`, `trace.json` (every call and what it yielded) and `reflection.json` (dead ends, follow-ups, and suggested edits to the beat file). `--sources` adds national feeds on top of the plan.

To start a new beat, copy `beats/georgia-tech.md` and rewrite it. Read the `beat_notes` in `reflection.json` after each pass. They are Bossman's suggested edits to the beat, and about one in eight is wrong, so check them before applying.

**Gather without judging** (no model calls, just see what's out there):

```bash
.venv/bin/python -m newsroom try bossman --gather-only
```

**Replay: rerun the judge on a saved gather.** This is how you tune the playbook. The live web changes every minute, so compare two versions of `playbook.md` on the *same* candidates:

```bash
.venv/bin/python -m newsroom try bossman --replay runs/bossman/<timestamp>/candidates.json
```

Unchanged prompts come from the LLM cache for free; only what you changed costs anything.

**Keep it running**, one pass every 15 minutes:

```bash
.venv/bin/python -m newsroom try bossman --loop 15
```

## What you'll see

```
50 candidates -> 9 new signals, 0 merged into existing, 41 skipped, 0 failed checks
  + Pennsylvania's measles outbreak has grown to 890 cases ...
  = (a repeat of something already stored, merged into it)
  ✗ (a signal that failed a hard check, with why; not stored)
```

In `runs/bossman/<timestamp>/`:

| File | What |
|---|---|
| `candidates.json` | Everything gathered, exactly as seen |
| `decisions.json` | What the model decided, including every skip and its reason |
| `report.json` | What was stored, merged, or rejected |
| `plan.json`, `trace.json`, `reflection.json` | With `--beat` only: the plan, each call's yield, and the look back |

## Where signals go

`NEWSROOM_SIGNALS` picks the store: `astra` (Astra DB, the default once `ASTRA_DB_ID` or `ASTRA_DB_API_ENDPOINT` is set), `file` (`runs/signals.json`, the default otherwise), or `memory`. Delete `runs/signals.json` to start fresh.

## Judging a run

Read `decisions.json` against [rubric.md](rubric.md). The two questions that matter most: did it skip the right things, and does each signal's `records_trail` point one layer below the headline?

## Adding a source

Write a function in `newsroom/gather.py` that returns candidates in the shape at the top of that file, and add it to `GATHERERS`.
