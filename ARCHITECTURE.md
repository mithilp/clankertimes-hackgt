# Architecture — AI Investigative Newsroom

The newsroom reads public complaint and injury records and Bluesky posts, pulls out the claims, counts which claims keep coming up, and hands the biggest ones to a reporter. The reporter writes the hypotheses that would have to be true, sends one scout per hypothesis to research them in official records and on the web, and then kills, parks or writes the story based on what the scouts find.

---

## The pipeline

```text
1. DATA         public complaint and injury records, plus Bluesky posts
       |
       v
2. CLAIMS       coded records: taken from their codes (no tokens)
                free text: a model reads each one and pulls out the claim
       |
       v
3. COUNT        same claim about the same product, counted by distinct people
       |
       v
4. PICK         the reporter looks at the most-reported claims and picks one
       |
       v
5. HYPOTHESES   the reporter writes what would have to be true
       |
       +-----------+-----------+
       v           v           v
6. SCOUTS       one per hypothesis: official records first (recalls, investigations,
       |        lawsuits), then the Brave Search API
       |        each reports: supports / contradicts / unclear, with quotes
       +-----------+-----------+
       v
7. VERDICT      contradicted -> KILL    not enough -> PARK    supported -> WRITE
       |
       v
8. ARTICLE      every sentence cites a source, then publish
```

---

## 1. Data

Every source is free, and none needs an API key.

| Source | What it has | Claims come from | Size |
|---|---|---|---|
| **NHTSA vehicle complaints** | Owner complaints: make, model, year, component, crash/fire/injury/death flags, the owner's own description | The model reads them | ~132,000 for 2025-2026 |
| **FDA MAUDE** (openFDA) | Medical device problem reports: device, manufacturer, death/injury/malfunction, description | The model reads them | Hundreds of thousands a month: fetched by date and device type |
| **FDA FAERS** (openFDA) | Drug adverse-event reports: suspect drug, maker, reactions in standard medical terms | Their codes: **no tokens** | ~100,000 a month; serious reports only by default |
| **FDA CAERS** (openFDA) | Adverse events from foods, supplements and cosmetics: product, reactions, outcomes | Their codes: **no tokens** | ~6,000 a year |
| **OSHA Severe Injury Reports** | Workplace hospitalizations, amputations and eye losses: employer, what happened, a description | Their codes: **no tokens** | ~106,000 since 2015 (federal-OSHA states only) |
| **Bluesky** | Public posts, searched for product problems ("caught fire", "recall", "stalled while driving"...) | The model reads them and keeps only posts reporting a problem with a specific product | Whatever the searches return |

Every complaint is stored with its source, ID, date, product (for OSHA, the employer), company, whether it reports serious harm, and its text. Personal details are never stored: no complainant cities, dealers or names from NHTSA, no addresses from OSHA, and no Bluesky handles (only a one-way hash of the account, to count distinct people).

In FAERS and CAERS, one report lists several reactions, so each reaction is stored as its own complaint, all tied to the same report so it's never counted twice.

Not included:
- **CFPB complaints:** they no longer publish complaint text.
- **CPSC SaferProducts incident reports:** these need a free API key. They're easy to add once we register.
- **NASA ASRS aviation reports:** these can only be exported by hand from a web page.

---

### The same records, as signals

`python -m newsroom record-signals` (`newsroom/record_signals.py`) is the signals store's second writer, beside Bossman. It reads these tables and writes two kinds of signal, each built one way for every dataset, with no model calls:

- **A spike in reports:** for NHTSA complaints, OSHA injuries, FAERS, CAERS and MAUDE, one signal per product and problem whose distinct reports in the dataset's latest 90 days are at least 5 and at least twice the usual 90-day level of the year before. The problem is the source's own code: NHTSA's component, OSHA's injury and event, the FDA reaction or device problem.
- **An official action:** one signal per NHTSA recall (`FLAT_RCL_POST_2010.zip`), NHTSA investigation, FDA recall event (openFDA `device/recall`, `drug/enforcement`, `food/enforcement`) or CPSC recall (saferproducts.gov) dated in the dataset's latest 90 days. `ingest-recalls` loads the three recall sources into the `recalls` table.

At most 50 per dataset per run, strongest first. Individual reports stay in SQLite, and no signal carries a report's narrative. A complaint spike lists the NHTSA recalls and investigations that cover its vehicle and component, or says none do; a recall says how many complaints preceded it. Origins are exact keys (`nhtsa_complaints: 2024 TESLA MODEL 3 / STEERING`), so a rerun merges into the same signals and refreshes their numbers, and these signals are added with `add(near=False)`: templated texts about one company overlap by more than `MERGE_SIMILARITY`, and must not merge.

---

## 2. Claims

**Coded sources spend no tokens.** FAERS and CAERS reactions and OSHA's injury codes already say what happened, so the claim is taken straight from the codes: "pancreatitis", or "amputation: caught in running powered equipment".

**Free-text sources are read by a model.** For NHTSA and MAUDE, product, company and severity come straight from the data, and a model reads each complaint and writes down its claim as one plain sentence:

```text
product      "2006 CHEVROLET COBALT"            from the data
company      "General Motors LLC"               from the data
claim        "engine shuts off while driving"   written by the model
```

Complaints are read in batches of 25 from one product at a time, so the same problem gets the same wording. Products with fewer than 10 complaints are skipped: they can't produce a big claim group, and skipping them saves most of the cost.

Bluesky posts are read as they're collected: the model finds the product, the maker and the claim, and posts that don't report a problem with a specific product are dropped. Every post read is remembered, so none is paid for twice.

---

## 3. Count

Complaints that make the same claim about the same product are grouped together. "Engine shut off while driving" and "car died on the highway" are the same claim.

Each group is counted by **distinct people**, so repeats don't inflate it:

- the same complaint listed under several parts counts once
- the same report, or the same Bluesky account, counts once
- copy-pasted text counts once, even with small edits. Only texts of 12 words or more are compared: short ones like "brakes failed" match by chance.

Coded claims are grouped by their exact label, with no model call. Free-text claims are grouped by a model, since "engine shut off while driving" and "car died on the highway" are worded differently.

(NHTSA only publishes the first 11 characters of a VIN, which identify the model and factory, not the car, so the VIN can't be used to spot repeats.)

The output is a list of claim groups, sorted by count, each showing its total count and its count in the last 90 days. The total catches problems that build slowly over years; the last-90-days count catches ones that suddenly spike.

---

## 4. Pick

The reporter looks at the top 20 claim groups and picks the one most worth a story:

- people are hurt or at risk
- a company or agency is responsible for fixing it
- one Brave search shows it hasn't already been reported

Every pick and every skip is logged with a one-line reason.

---

## 5. Hypotheses

One reporter works on one story at a time. The reporter writes 3-5 hypotheses: plain statements that would all have to be true for the story to hold. Each must be checkable on the public web. The complaint counts are already known from the data, so they are never hypotheses.

Example, for pump complaints:

```text
H1  The pump has not been recalled for delivering the whole bag at once.
H2  The FDA has been told about the problem, through reports or an inspection.
H3  The manufacturer has acknowledged the problem somewhere public.
```

---

## 6. Scouts

Each hypothesis gets one scout, with a budget of about 30 searches and page reads.

**Official records first.** Before the scouts start, the reporter looks up official records for the story directly from government and court databases. These lookups need no web search and no tokens:

| Record | Looked up by | Used for |
|---|---|---|
| NHTSA recalls | make, model, year | vehicle stories |
| NHTSA defect investigations | make, model, year (from NHTSA's investigations file, loaded once) | vehicle stories |
| FDA device recalls | FDA product code | medical device stories |
| FDA drug and food enforcement (recalls) | product name | drug, food and supplement stories |
| CPSC recalls | product name | Bluesky stories about consumer products |
| Federal lawsuits (CourtListener) | company name and the problem | every story with a company |

Each scout sees these records alongside its search results and reads the ones that bear on its hypothesis. Then it researches with the **Brave Search API** and reads the pages it finds.

The scout reports back a list of sources:

```text
url
quote        the exact sentence from the page
finding      supports / contradicts / unclear
```

Every quote is checked in code against the page text.

What counts as proof:

- **Counts:** government records, court records, and news outlets' own reporting.
- **Doesn't count:** a company denial doesn't prove a claim false, and complaints or social posts don't prove it true. They are the claim itself.
- A lawsuit shows that someone **alleged** something. It can prove that a lawsuit was filed, not that the allegation is true.

---

## 7. Verdict

The reporter reads the scouts' findings and decides:

```text
a hypothesis is clearly contradicted by a source that counts   -> KILL, with a short note why
every hypothesis is supported by a source that counts          -> WRITE
anything else                                                  -> PARK, and try again later
```

A wrong detail isn't a kill. If a source shows the date was June 5, not June 3, the reporter corrects the hypothesis and carries on.

---

## 8. Article

The reporter writes the story:

- what the complaints show, described as complaints ("owners report..."), never as proven fact
- what the scouts confirmed, with sources
- the company's side, if it has said anything publicly
- what is still unknown

Before publishing, code checks that every sentence cites a source and every quote appears in its source. No private individual is named. Then it publishes.

---

## Hunt mode: depth-first

Reading every complaint before looking for stories wastes tokens on products that never produce one. The **hunt** works depth-first instead:

```text
next product (most serious complaints first, taking turns between sources)
    -> read only its complaints (steps 2-3)
    -> any claim reported by 10+ people that the reporter hasn't judged yet?
         no  -> next product
         yes -> reporter judges it (step 4)
                  lead -> investigate it right away (steps 5-8), then continue
                  no   -> next product
stop after N investigations, or M products
```

- Tokens are spent only on the products the hunt reaches.
- Every product scanned and every claim the reporter judged is remembered, so the next hunt carries on where the last one stopped.
- A product is scanned again only if it gains new complaints.
- Unidentified products (NHTSA's "UNKNOWN" make) are skipped.

The breadth-first commands (`claims`, `count`, `leads`, `run`) still work for looking at everything at once.

---

## Demo

The dashboard shows the whole trail for a story: complaints read, claims found, the counts, why the reporter picked this one (and skipped others), the hypotheses, what each scout found, the verdict, and the article. Kills and parks are shown too.

---

## Settings

| Setting | Value |
|---|---|
| Reporters | 3, each on one story at a time |
| Claim groups the reporter looks at | Top 20 |
| Hypotheses per story | 3-5 |
| Scout budget | About 30 searches and page reads |
| Minimum complaints for a product to be read | 10 |
| Minimum people behind a claim for the hunt to call it a lead | 10 |

All of these can be changed in `.env` (see `.env.example`).

---

## Tech

- **Python 3.12**, run from the command line (`python -m newsroom ...`)
- **SQLite** for everything: complaints, claims, groups, stories, and the log of every decision
- **DeepSeek**, through its OpenAI-compatible API: `deepseek-flash` for bulk work (reading complaints, grouping claims, scouts reading pages) and `deepseek-v4-pro` for the reporter's judgment and writing. Replies are cached, so rerunning a step costs nothing.
- **Brave Search API** for the scouts and the "already reported?" check
- **Free public APIs and files** for everything else: NHTSA, openFDA, OSHA, CPSC, CourtListener and Bluesky. None needs a key. An openFDA key only raises its page size and rate limit, and a CourtListener token only raises its rate limit.

---

## Not in v1

Kept out on purpose, to keep v1 simple. Each can be added later if we need it.

- CPSC SaferProducts incident reports (need a free API key), NASA ASRS (no API)
- automatic re-checking of parked stories
- statistics that compare products against similar products
