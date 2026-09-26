# AI newsroom — HackGT 13

Agent newsroom: scouts loop for stories, reporters develop them with open-ended tools (web search, fetch, a real browser), a review panel checks them, and a managing editor publishes. Demo is a real four-hour unattended run.

Plan: [ARCHITECTURE.md](ARCHITECTURE.md) · Schema: [db/schema.sql](db/schema.sql)

## Layout

| Path | What |
|---|---|
| `newsroom/scout.py` | One loop per beat, all beats in parallel; raises and scores leads |
| `newsroom/editor.py` | Managing editor (singleton): triage, dedupe, promotion, budget appeals, publishing, waking parked stories |
| `newsroom/reporter.py` | Pool of reporter slots; evidence plan, stopping rules, draft / park / kill |
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
docker compose logs -f editor reporter
docker compose exec db psql -U newsroom -c "select * from counters"
docker compose exec db psql -U newsroom -c "select created_at, agent, action, reason from agent_events order by id desc limit 30"
```

Scale reporters: `docker compose up -d --scale reporter=3`, or set `REPORTER_CONCURRENCY`. How many stories run at once is capped by `MAX_ACTIVE_STORIES`.

## Run it locally without Docker

Needs Python 3.12 and a Postgres with pgvector.

```bash
pip install -r requirements.txt && playwright install chromium
export DATABASE_URL=postgresql://newsroom:newsroom@localhost:5432/newsroom
python -m newsroom all
```
