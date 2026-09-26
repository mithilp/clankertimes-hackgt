# Running a Scout on its own

A scout researches **one** hypothesis and brings back findings: each one a URL, the exact quote from that page, and whether it `supports`, `contradicts`, or is `unclear`.

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

## Command

```bash
.venv/bin/python -m newsroom try scout \
  --hypothesis "Tesla recalled 2023 Model 3 vehicles for loss of power steering assist" \
  --context "Tesla Model 3 power steering" \
  --budget 8
```

- `--hypothesis` — one plain statement that could be true or false.
- `--context` — a few words naming the product, company, or agency. It keeps searches on target.
- `--budget` — searches plus page reads (default 12).

## What you'll see

```
[supports] gov  https://...
   "the exact sentence from the page"
[contradicts] news  https://...
   "..."
6 findings: {'supports': 4, 'contradicts': 1, 'unclear': 1}
```

Saved to `runs/scout/<timestamp>/findings.json`.

## Judging a run

Use [rubric.md](rubric.md). Check three things: every quote actually appears on its page, official records were tried before news coverage, and it looked for evidence *against* the hypothesis, not just for it.
