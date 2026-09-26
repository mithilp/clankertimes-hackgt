# Running a Scout on its own

A scout settles **one** sub-hypothesis for the Reporter and reports back. The Reporter never searches: scouts do all the legwork. Code: [newsroom/scout_agent.py](../../newsroom/scout_agent.py).

```
assignment from the Reporter (statement · records to try first · the records' own terms ·
                              what would support it · what would contradict it · budget)
   │
   └─ loop, within budget: choose up to 3 calls, each with a one-line reason (published as the trail)
         the newsroom's DB   db_signals (Bossman's signals in Astra) · db_desk (quotes past investigations verified)
         official records   nhtsa_recalls · nhtsa_investigations · fda_recalls · cpsc_recalls · court_dockets · federal_register
         the web            news_search (Google News) · web_search (DuckDuckGo, then Bing; no API key) · read
                            read pulls exact quotes; each is checked word for word against the page
   │
   └─ report back: verdict (held to the evidence in code) · summary · what was searched · not found ·
                   dead ends · proposals (new hypotheses, with the records that would settle them) · next check
```

A scout can only report **supports** or **contradicts** if a verified quote from a source that counts (government record, court record, news reporting) says so. Otherwise its verdict is held back to **unclear**, with a note.

A **coverage scout** (`mode: "coverage"`) looks for prior reporting of the whole story, so the Reporter can credit it and aim at the gap. It may only report coverage it actually saw in its results.

## Setup

Needs a model (`DEEPSEEK_API_KEY`, or `NEWSROOM_LLM_PROVIDER=claude_code`) and a search backend:

- By default search needs no key: DuckDuckGo's lite and HTML pages over plain HTTP, then Bing in a headless browser (install once with `pip install playwright` and `python -m playwright install chromium`). Every newsroom process on the machine takes turns, one request per engine every 10 seconds. An engine that answers with a bot check is paused for everyone for ten minutes and never worked around; results unrelated to the query are thrown out.
- `NEWSROOM_SEARCH_BACKEND=brave` with `BRAVE_API_KEY` uses Brave's search API instead. It is never used as a silent fallback.

`nhtsa_investigations` reads NHTSA's investigations file from the local database: load it once with `python -m newsroom ingest-investigations`.

## Command

```bash
.venv/Scripts/python -m newsroom try scout \
  --hypothesis "NHTSA opened a defect investigation into loss of steering control covering 2023 Tesla Model 3 vehicles" \
  --context "Tesla Model 3 steering" --budget 8
```

You'll see each call and its result as it happens, then the findings and the report. Saved to `runs/scout/<timestamp>/findings.json`.

## Judging a run

Use [rubric.md](rubric.md). Check that every quote appears on its page, that official records were tried before news coverage, that the scout looked for evidence *against* the statement, and that "not found" names the system and terms searched.
