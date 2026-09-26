# AI newsroom — HackGT 13

An autonomous investigative newsroom. It reads public complaint and injury records (NHTSA, FDA, OSHA) and Bluesky posts, pulls out the claims, counts which ones keep coming up, and hands the biggest to a reporter, who sends a scout to test each hypothesis against official records and the web, and then kills, parks or writes the story.

Plan: [ARCHITECTURE.md](ARCHITECTURE.md) · Tests: [TEST_PLAN.md](TEST_PLAN.md)

## Setup

Needs Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # macOS/Linux: .venv/bin/pip
```

Add your keys to `.env` (see `.env.example` for the names): `DEEPSEEK_API_KEY` and `BRAVE_API_KEY`.

## Run it

```bash
# 1. Data. These spend no tokens:
python -m newsroom ingest-osha                     #    ~106k workplace severe injuries, 2015 on
python -m newsroom ingest-faers --since 2026-06-01 --until 2026-06-30   # drug adverse events
python -m newsroom ingest-caers --since 2025-01-01 --until 2025-12-31   # food/supplement/cosmetic events
python -m newsroom ingest-investigations           #    NHTSA defect investigations, for the scouts
python -m newsroom ingest-recalls                  #    NHTSA, FDA and CPSC recalls since 2025
# These are read by the model:
python -m newsroom ingest-nhtsa                    #    ~132k vehicle complaints, 2025-2026
python -m newsroom ingest-maude --since 2026-08-01 --product-code FRN  # device reports (FRN: infusion pumps)
python -m newsroom ingest-bluesky                  #    Bluesky posts about product problems, last 30 days

# The easy way: hunt depth-first. It reads one product at a time and investigates each lead as soon
# as it finds one, so tokens are spent only on what it reaches. Run it again to carry on.
python -m newsroom hunt --stories 1 --source nhtsa

# Or breadth-first, step by step:
python -m newsroom claims --limit 500              # 2. read a small batch first to check the cost
python -m newsroom claims                          #    then the rest
python -m newsroom count                           # 3. group the claims, count distinct people
python -m newsroom top --source osha               #    see the biggest claim groups (any source)
python -m newsroom run                             # 4-8. pick, hypotheses, scouts, verdict, article
python -m newsroom show 1                          #    the full trail of story 1
python -m newsroom usage                           #    DeepSeek tokens used so far

# Put what's moving in these records into the signals store, next to Bossman's (no tokens):
python -m newsroom record-signals --dry-run        #    build and check them; saved under runs/records/
python -m newsroom record-signals                  #    write them; rerun after each ingest
```

Published articles are written to `published/`. Everything else lives in `data/newsroom.db`.

## Tests

```bash
.venv/Scripts/python -m pytest
```

The previous beat-driven newsroom was removed; see git history (`122ea29`) for it.

## Agents

What each agent's job is and what doing it well means — playbooks, rubrics, good and bad examples, and evals — lives in [agents/](agents/). Start with [agents/README.md](agents/README.md).
