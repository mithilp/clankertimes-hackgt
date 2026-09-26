# Prompt: wire Astra DB in as the signals store

Paste everything below the line into your coding agent, from the repo root on the `claude-backend` branch.

---

We're adding DataStax Astra DB to this repo as the newsroom's vector database. Read these first, then implement:

- `newsroom/signals.py` — the contract. `Signal` is the record, `SignalStore` is the interface, and `InMemorySignals` is the reference implementation that defines the intended behavior.
- `tests/test_signals.py` — the contract tests. Your implementation must pass them.
- `agents/bossman/playbook.md` and `agents/mclovin/playbook.md` — who writes signals and who reads them, and why each field exists.

## What Astra DB is for

**Signals only.** A signal is one item Bossman finds on the live web: something moving now, with an accountable party and a checkable claim underneath. Bossman writes signals with `add()`. McLovin reads them with `recent()` and `similar()`, and calls `mark_used()` when a signal feeds a hypothesis. That's the entire job of this database.

## What goes in

One document per signal, in a collection named `signals` (configurable), with every field on `Signal`:

- `summary`, `why_interesting`, `checkable_claim`, `accountable_party`, `origin`
- `records_trail` (list of strings), `sources` (list of `{url, seen_at}`), `spike` (dict), `source_types` (list)
- `id`, `first_seen`, `last_seen`, `status` (`new` / `used` / `ignored`), `used_by` (list of hypothesis ids), `ignored_reason`
- plus `origin_key` (from `signals.origin_key()`), stored so duplicate detection is a cheap exact lookup
- plus the vector: embed `Signal.text()` (summary + checkable claim + accountable party)

## What does NOT go in

- **No pipeline state.** Stories, hypotheses, scout findings, verdicts, events, claim groups, and complaints all stay in SQLite (`newsroom/db.py`). Don't move or duplicate them.
- **No LLM cache or usage.** Those stay in SQLite; the cache is what makes reruns free and prompt tuning cheap.
- **No full page text.** Fetched pages stay in SQLite's `pages` table, where the quote checker reads them. A signal only carries source URLs; `web.remember(url, text)` stores the snapshot.
- **No personal data about posters.** No handles, display names, profile URLs as authors, or locations. If you need to count distinct people, store a one-way hash. (Same rule the Bluesky source already follows.)
- **No secrets.** Endpoint, token and keyspace come from `.env` only. Never commit them, never log them.

## Implementation

1. Create `newsroom/signals_astra.py` with `class AstraSignals` implementing every method on `SignalStore`, with the same behavior as `InMemorySignals`. Use the official Python client, `astrapy`, and add it to `requirements.txt`.
2. **Deduplication on `add()` is the important part.** A repost of the same thing must merge into the existing signal, not create a new one — McLovin counts distinct origins, and duplicates make one viral thread look like forty sources. Two passes:
   - exact: another signal with the same `origin_key`;
   - near: a vector search for the closest existing signal, where similarity ≥ `NEWSROOM_SIGNAL_MERGE` (default 0.8, cosine) **and** the same `accountable_party` (case-insensitive).

   On a merge: append sources not already present (by URL), union `source_types`, move `last_seen` forward, replace `spike` with the newest one. Return `(existing_id, False)`.
3. `similar()` uses Astra vector search, returns `(Signal, score)` with score in 0–1, best first, optionally filtered to `last_seen >= since`.
4. `recent()` filters on `last_seen` and optional `status`, newest first. Index `last_seen`, `status` and `origin_key`. Don't index long text fields; Astra limits indexed string length.
5. **Embeddings:** use Astra's server-side vectorize if an embedding provider is available on our database, otherwise embed client-side. Either way, record the embedding model name and dimension in one place in config, since changing either later means re-embedding everything.
6. Config: read `ASTRA_DB_API_ENDPOINT`, `ASTRA_DB_APPLICATION_TOKEN`, `ASTRA_DB_KEYSPACE` (optional) and `NEWSROOM_SIGNALS_COLLECTION` (default `signals`). Add them, commented, to `.env.example`. Create the collection on first use if it doesn't exist.
7. `signals.get_store()` already imports `AstraSignals` when `ASTRA_DB_API_ENDPOINT` is set, and uses `InMemorySignals` when `NEWSROOM_SIGNALS=memory`. Keep both paths working.

## Tests

- The existing suite must keep passing **without network access or Astra credentials**. Nothing outside `signals_astra.py` may require Astra.
- Run `tests/test_signals.py` against `AstraSignals` too: add an opt-in variant that runs only when `ASTRA_DB_API_ENDPOINT` is set, against a throwaway collection (e.g. `signals_test_<random>`) that the test deletes afterward.
- Word-overlap similarity in the in-memory store and real embeddings won't score identically. If a contract test depends on exact scores, loosen the test to ranking behavior rather than bending the Astra implementation to match.

## Done means

- `pytest` passes offline, and the Astra variant passes with credentials.
- Adding the same signal twice (same origin) leaves one document with both sources.
- A short note at the top of `signals_astra.py` says which embedding model and dimension we use, and why.

Don't change `newsroom/db.py`, the reporter, scouts, article checks, or anything under `agents/` as part of this.
