# Running the Council on its own

Three judges — **skeptic**, **virality**, **novelty** — review a finished draft independently. Mechanical checks (`newsroom/article.py`) run first; a draft that fails them never reaches the judges.

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

**Review a draft with all three judges** (defaults to the fixture draft `tests/fixtures/council_draft.json`):

```bash
.venv/bin/python -m newsroom try council
```

**Your own draft:**

```bash
.venv/bin/python -m newsroom try council --draft path/to/draft.json
```

A draft is `{"article": {"headline", "paragraphs": [[{"text", "cite": ["D", "F1"]}]]}, "sources": {"D": {"title", "url", "text"}}}`, the same shape `article.check()` takes. See the fixture for a full example.

**One judge only:**

```bash
.venv/bin/python -m newsroom try council --judges skeptic
```

**Skip the novelty judge's prior-coverage web search:** add `--no-web`.

## Measuring the council: seeded errors

Plants 10 known mistakes into the fixture draft, one at a time, and reports what caught each one:

```bash
.venv/bin/python -m newsroom try council --seeded
```

```
  office_as_defendant        caught by mechanical checks
  complaint_as_fact          caught by skeptic
  ...
caught 10/10 (100%): 5 by mechanical checks, 5 by judges
```

A judge only gets credit when its objection points at the sentence that was planted, so a judge that objects to everything doesn't score well by accident. The errors live in `MUTATIONS` in `newsroom/council.py` and are described in [skeptic/examples/bad/seeded-errors.md](skeptic/examples/bad/seeded-errors.md). Add harder ones as you find real failures; 10/10 on a small, known set is a floor, not a grade.

## What you'll see from a review

Each judge prints `APPROVE` or `REVISE`, and for each problem: the sentence, what's wrong, and the fix. The combined verdict is `APPROVE` only if every judge approves. Saved to `runs/council/<timestamp>/review.json`.

The fixture draft intentionally still has one real flaw for the judges to find: its owner complaints (Autopilot/FSD) and its recall and investigation (power-steering assist) are different defects.
