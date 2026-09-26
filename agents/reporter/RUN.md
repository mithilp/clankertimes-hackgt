# Running the Reporter on its own

The Reporter takes one hypothesis, breaks it into 3–5 sub-hypotheses that must all hold, sends a scout after each, and decides: **write**, **park**, or **kill**.

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

Scouts search the web, so also set **either** `BRAVE_API_KEY` **or** `NEWSROOM_SEARCH_BACKEND=browser` (needs `pip install playwright && playwright install chromium`).

## Commands

**A hypothesis McLovin wrote:**

```bash
.venv/bin/python -m newsroom try reporter --from-mclovin runs/mclovin/<timestamp>/hypotheses.json --pick 1
```

**Any hypothesis you type:**

```bash
.venv/bin/python -m newsroom try reporter --hypothesis "NHTSA has an open investigation into loss of steering on the 2023 Tesla Model 3"
```

`--budget N` sets searches plus page reads per scout (default 12). Lower it while testing; each scout's budget is where most of the time and cost goes.

**Rerun an existing story from the complaint pipeline** (the original records-driven flow, which also writes the article):

```bash
.venv/bin/python -m newsroom try reporter --story 3
.venv/bin/python -m newsroom show 3
```

## What you'll see

```
hypothesis: ...
minimum story: ...
maximum story: ...
  H1. ...   H2. ...   H3. ...
H1: {'supports': 2, 'contradicts': 0, 'unclear': 1}
   [supports] https://...  "the exact sentence from the page"
VERDICT: write | park | kill  —  why
```

Everything, including every scout finding, is saved to `runs/reporter/<timestamp>/result.json`.

## Judging a run

Use [rubric.md](rubric.md). Look at whether the sub-hypotheses were each checkable, whether scouts searched for contradicting evidence, and whether the verdict matches the findings. A `kill` backed by a real contradiction is a good result.

Note: the hypothesis path stops at the verdict. Writing the article from a McLovin hypothesis is the next piece of wiring; today only `--story` runs all the way to a published draft.
