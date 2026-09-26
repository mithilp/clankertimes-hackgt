# Running McLovin on its own

McLovin reads signals, looks for patterns, and writes **falsifiable hypotheses** a reporter could settle. Signals that aren't ready go on a watch list with the reason.

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

**Read recent signals from the store** (run Bossman first so there's something to read):

```bash
.venv/bin/python -m newsroom try mclovin
```

**Look further back** (default is the last 24 hours):

```bash
.venv/bin/python -m newsroom try mclovin --hours 72
```

**Replay a saved set of signals** instead of the live store, to compare playbook versions on identical input:

```bash
.venv/bin/python -m newsroom try mclovin --input runs/mclovin/<timestamp>/input_signals.json
```

By default, signals that feed a hypothesis are marked `used` so the next pass skips them. Add `--no-mark` to leave them alone while you experiment.

## What you'll see

```
1. <one-sentence hypothesis>
   accountable: <who>  ·  1 distinct origins across 1 sources  ·  ok
   settle with: <specific record types and systems>

   watching <signal id>: <what's missing before it can become a hypothesis>
```

`distinct origins` is counted **by code** from the signals, not by the model, so one viral thread can never read as many sources. `ok` means the hypothesis passed the hard checks; otherwise you'll see `✗` and why.

In `runs/mclovin/<timestamp>/`: `input_signals.json` (exactly what it read) and `hypotheses.json` (everything it produced, including the watch list).

## Judging a run

Use [rubric.md](rubric.md). The fastest check: could someone start pulling the records in `would_settle_it` right now, without asking what they are?

## Next step

Hand a hypothesis to the reporter:

```bash
.venv/bin/python -m newsroom try reporter --from-mclovin runs/mclovin/<timestamp>/hypotheses.json --pick 1
```
