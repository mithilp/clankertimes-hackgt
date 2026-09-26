# Prompt: wire Astra DB in as the signals store

Paste everything below the line into your coding agent, from the repo root on `main`.

---

We're adding DataStax Astra DB to this repo as the newsroom's vector database. Read these first, then implement:

- `newsroom/signals.py` — the contract. `Signal` is the record, `SignalStore` is the interface, and `InMemorySignals` is the reference implementation that defines the intended behavior.
- `tests/test_signals.py` — the contract tests. Your implementation must pass them.
- `agents/bossman/playbook.md` and `agents/mclovin/playbook.md` — who writes signals and who reads them, and why each field exists.

## What Astra DB is for

**Signals only.** A signal is one item Bossman finds on the live web: something moving now, with an accountable party and a checkable claim underneath. Bossman writes signals with `add()`. McLovin reads them with `recent()` and `similar()`, and calls `mark_used()` or `mark_ignored()` as it works through them. That's the entire job of this database.

Bossman is the only writer, and it calls `add()` one signal at a time. Don't add locking or transactions for concurrent writers.

## What goes in

One document per signal, in a collection named `signals` (configurable), with every field on `Signal`:

- `summary`, `why_interesting`, `checkable_claim`, `accountable_party`, `origin`
- `records_trail` (list of strings), `sources` (list of `{url, seen_at}`), `spike` (dict), `source_types` (list)
- `first_seen`, `last_seen`, `status` (`new` / `used` / `ignored`), `used_by` (list of hypothesis ids), `ignored_reason`
- `Signal.id` as the document's `_id` (not a separate `id` field), so `get()`, `mark_used()` and `mark_ignored()` are primary-key lookups
- plus `origin_key` (from `signals.origin_key()`), stored so duplicate detection is a cheap exact lookup. `origin_key()` is already correct; don't change it.
- plus `party_key`: `accountable_party.strip().lower()`, so the same-party check can be a filter
- plus the vector: embed `Signal.text()` (summary + checkable claim + accountable party)

Timestamps are UTC ISO strings (`2026-09-26T10:00:00+00:00`). `get()`, `recent()` and `similar()` must return them exactly as they were stored, because the tests compare strings. If `$gte` filtering or sorting on a string field doesn't work in the Data API, store an extra `$date` copy for filtering and sorting, and keep the string for returning.

## What does NOT go in

- **No pipeline state.** Stories, hypotheses, scout findings, events, claim groups, and complaints all stay in SQLite (`newsroom/db.py`). Don't move or duplicate them.
- **No LLM cache or usage.** Those stay in SQLite; the cache is what makes reruns free and prompt tuning cheap.
- **No full page text.** Fetched pages stay in SQLite's `pages` table, where the quote checker reads them. A signal only carries source URLs; `web.remember(url, text)` stores the snapshot.
- **No personal data about posters in fields of their own.** No handles, display names, author profile URLs, or locations. If you need to count distinct people, store a one-way hash. (Same rule the Bluesky source already follows.) Post URLs in `origin` and `sources` are allowed, because they're how a reporter checks the claim, even though some contain the poster's handle. `origin_key()` already writes X posts without the handle (`x.com/i/status/<id>`).
- **No secrets.** Endpoint, token and keyspace come from `.env` only. Never commit them, never log them.

## Implementation

1. Create `newsroom/signals_astra.py` with `class AstraSignals` implementing every method on `SignalStore`, with the same behavior as `InMemorySignals`. Use the official Python client, `astrapy`, and add it to `requirements.txt`.
2. **Deduplication on `add()` is the important part.** A repost of the same thing must merge into the existing signal, not create a new one — McLovin counts distinct origins, and duplicates make one viral thread look like forty sources. But merging two *different* claims is worse: the second signal's summary, claim and origin are thrown away, and its sources end up backing a claim they don't make. Two passes, in this order:
   - **exact:** another signal with the same `origin_key`.
   - **near:** a vector search for the 5 closest signals, filtered to the same `party_key`. Merge into the first of them whose word overlap with the new signal is at or above `MERGE_SIMILARITY`, using `signals._overlap(existing.text(), new.text())`, exactly as `InMemorySignals` does.

   **Don't use the vector score as the merge test.** `NEWSROOM_SIGNAL_MERGE` (default 0.8) is a word-overlap threshold that means "near-identical text". Astra's cosine score is on a different scale, (1 + cosine) / 2, and embeddings put different claims about the same party close together. The Model 3 steering signal and the Cybertruck trim signal in the tests are an example. Vector search finds the candidates; word overlap decides.

   On a merge, do what `InMemorySignals.add()` does: append sources not already present (by URL), set `source_types` to the sorted union, set `last_seen` to the later of the two, and replace `spike` with the incoming one if it isn't empty. Return `(existing_id, False)`.
3. `similar()` uses Astra vector search, returns `(Signal, score)` with score in 0–1, best first, optionally filtered to `last_seen >= since`. Return Astra's score as it comes; don't try to make it match word overlap.
4. `recent()` filters on `last_seen` and optional `status`, newest first.
5. **Indexing:** create the collection with an indexing *allow* list of `last_seen` (and its `$date` copy, if you add one), `status`, `origin_key` and `party_key`. `_id` and the vector are always indexed. Don't index the long text fields; Astra limits indexed string length.
6. **Embeddings:** Astra's server-side vectorize with its built-in NVIDIA model, `nvidia/nv-embedqa-e5-v5` (1024 dimensions, cosine). Astra hosts it, so it needs no API key and costs nothing extra, but only databases in AWS `us-east-2` or Google Cloud `us-east1` have it. It reads at most 512 tokens, so cap the embedded text. Create the collection like this:

   ```python
   database.create_collection(
       name,
       definition=CollectionDefinition(
           vector=CollectionVectorOptions(
               dimension=1024,
               metric=VectorMetric.COSINE,
               service=VectorServiceOptions(provider="nvidia", model_name="nvidia/nv-embedqa-e5-v5"),
           ),
           indexing={"allow": [...]},
       ),
   )
   ```

   Record the model name and dimension once, as constants in `signals_astra.py`, because changing either later means re-embedding everything.
7. Config: read `ASTRA_DB_ID` and `ASTRA_DB_APPLICATION_TOKEN` (starts with `AstraCS:`). Optional: `ASTRA_DB_REGION` (otherwise look it up from the ID with `client.get_admin().database_info(id)`), `ASTRA_DB_API_ENDPOINT` (instead of ID + region), `ASTRA_DB_KEYSPACE`, and `NEWSROOM_SIGNALS_COLLECTION` (default `signals`). List them all, commented out, in `.env.example`, along with `NEWSROOM_TEST_ASTRA` (see Tests). Create the collection on first use if it doesn't exist.
8. `signals.get_store()` already handles the switch. It returns `InMemorySignals` when `NEWSROOM_SIGNALS=memory`. Otherwise it imports `AstraSignals`, and raises if neither `ASTRA_DB_ID` nor `ASTRA_DB_API_ENDPOINT` is set. Keep both paths working.

## Tests

- The existing suite must keep passing **without network access or Astra credentials**. Nothing outside `signals_astra.py` may require Astra.
- Run `tests/test_signals.py` against `AstraSignals` too, as an opt-in variant (parametrize the `store` fixture). Run it only when `NEWSROOM_TEST_ASTRA=1`. Don't gate on the Astra settings alone: `newsroom/config.py` loads `.env` when it's imported, so anyone with Astra configured would hit the network on every plain `pytest`.
- Create one throwaway collection per test session (e.g. `signals_test_<random>`). Empty it before each test, because the tests expect an empty store. Delete it at the end of the session, even when a test fails.
- Word-overlap similarity in the in-memory store and real embeddings won't score identically. If a test asserts an exact `similar()` score, loosen it to ranking behavior. **Don't loosen the merge tests**, especially `test_different_claim_same_party_stays_separate`. If one fails against Astra, the dedup logic is wrong.

## Done means

- `pytest` passes offline, and passes with `NEWSROOM_TEST_ASTRA=1` and credentials.
- Adding a repost of a signal (same origin, a different source URL) leaves one document with both sources.
- Adding a different claim about the same accountable party leaves two documents.
- A short note at the top of `signals_astra.py` says which embedding model and dimension we use, and why.

Don't change `newsroom/db.py`, `origin_key()`, the reporter, scouts, article checks, or anything under `agents/` as part of this.
