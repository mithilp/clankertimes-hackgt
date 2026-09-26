# AI newsroom — HackGT 13

Agent newsroom that works hypothesis-first: tipsters find stories and propose hypotheses, a reporter splits each one into sub-claims, scouts research the sub-claims in parallel with open-ended tools (web search, fetch, a real browser), the reporter writes from their quoted facts, a review panel checks it, and a managing editor publishes. Demo is a real four-hour unattended run.

Design: [ARCHITECTURE.md](ARCHITECTURE.md) · Build order and schedule: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) · Schema: [db/schema.sql](db/schema.sql)

## Layout

| Path | What |
|---|---|
| `newsroom/tipster.py` | One loop per beat, all beats in parallel; finds stories and raises scored hypotheses |
| `newsroom/editor.py` | Managing editor (singleton): triage, dedupe, promotion, scouts' budget appeals, publishing, waking parked stories |
| `newsroom/reporter.py` | Plans a hypothesis into sub-claims; later rolls up the verdicts and writes, kills, follows up or parks |
| `newsroom/scout.py` | Pool of scouts; each researches one sub-claim and returns a verdict with quoted facts |
| `newsroom/reviewer.py` | Verifier, skeptic, fairness, run in parallel per story |
| `newsroom/scoring.py` | Lead scoring: model-rated rubric + measured novelty and timeliness |
| `newsroom/llm.py` | Shared agent loop; logs every tool call's reason |
| `newsroom/db.py` | Leases, singleton locks, LISTEN/NOTIFY wake-ups |
| `newsroom/memory.py` | mem0 shared memory + local embeddings |
| `config/beats.json` | The beats scouts cover |

## Run it on the box

On a Linux VM with Docker installed (2+ vCPU, 4+ GB RAM):

```bash
cp .env.example .env   # add ANTHROPIC_API_KEY and BRAVE_API_KEY
docker compose up -d --build
sudo systemctl enable docker   # so the newsroom comes back after a reboot
```

Watch it:

```bash
docker compose logs -f editor reporter scout
docker compose exec db psql -U newsroom -c "select * from counters"
docker compose exec db psql -U newsroom -c "select created_at, agent, action, reason from agent_events order by id desc limit 30"
```

Scale scouts, the widest fan-out: `docker compose up -d --scale scout=3`, or set `SCOUT_CONCURRENCY`. How many stories run at once is capped by `MAX_ACTIVE_STORIES`.

## Run it locally without Docker

Needs Python 3.12 and a Postgres with pgvector.

```bash
pip install -r requirements.txt && playwright install chromium
export DATABASE_URL=postgresql://newsroom:newsroom@localhost:5432/newsroom
python -m newsroom all
```
