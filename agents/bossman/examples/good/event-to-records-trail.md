# From the event to the records trail

*Constructed example to illustrate the pattern. Not a real event.*

**Signal:** A celebrity is reported killed in a helicopter crash. It's #1 trending on X, every outlet leads with it, and a Kalshi market on an unrelated event moves on the news.

**What Bossman records:**

| Field | Value |
|---|---|
| `summary` | A helicopter crash is being reported by multiple outlets, citing local authorities |
| `why_interesting` | Moving now (#1 trending, wall-to-wall coverage); accountable parties exist (operator, maintenance provider); records trail is almost certainly unpulled |
| `accountable_party` | The aircraft operator |
| `checkable_claim` | The operator's safety and maintenance record |
| `records_trail` | FAA registry entry for the tail number, operator certificate and enforcement history, airworthiness directives for the model, prior NTSB findings on the type |
| `origin` | The local authority's statement |

**Why it's good:** Bossman doesn't try to add another crash story. It records the event *and the layer below it*, which is where the newsroom can find something no one else has.
