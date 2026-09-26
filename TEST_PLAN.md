# Test Plan

One test per step of the pipeline in [ARCHITECTURE.md](ARCHITECTURE.md), plus one full run. The **Status** column says whether it runs automatically (`pytest`, no API keys needed) or still needs real API calls or human labels.

| Step | Test | Passes when | Status |
|---|---|---|---|
| **1. Data** | Parse NHTSA rows: one complaint listed under two parts, a year of 9999, personal fields | Parts merge into one complaint; no city, dealer or operator name is stored | Automated (`test_nhtsa.py`) |
| **1. Data, coded sources** | OSHA: employer spellings merge, claims come from the codes, no address is stored. FAERS and CAERS: one claim per reaction, the suspect product is used | As described | Automated (`test_sources.py`) |
| **1. Data, Bluesky** | Posts that report a product problem are kept and others dropped; no handle or account ID is stored; a second run reads nothing twice | As described | Automated (`test_sources.py`) |
| **2. Claims** | Complaints are read one product at a time; products under the minimum are skipped; skipped complaints are retried | As described | Automated (`test_count.py`) |
| **2. Claims, quality** | Label 100 real complaints by hand (the claim), then compare with the model | The model gets the claim right at least 90 times out of 100 | Needs DeepSeek + labels |
| **3. Count** | The same text twice, a copied template with small edits, short identical texts, two people describing the same problem, 10 different people | Every count comes out exactly right | Automated (`test_count.py`) |
| **3. Count, coded claims** | Coded claims are grouped with no model call; the same report arriving twice counts once; one person counts once per group | As described | Automated (`test_sources.py`) |
| **3. Count, planted pattern** | Hide 30 complaints about a made-up product among 400 unrelated ones | The made-up claim comes out on top | Automated (`test_count.py`) |
| **3. Count, real history** | Run on NHTSA data only up to 2013 | GM Cobalt stalling (recalled February 2014) shows up near the top for 2005-07 cars | Needs DeepSeek + the older NHTSA file |
| **4. Pick** | Label 20 claim groups by hand: worth a story or not | The reporter agrees at least 16 times out of 20 | Needs DeepSeek + labels |
| **6. Scouts** | Quotes not on the page are dropped; social media is always "social"; the budget is respected; a scout stops once a source that counts settles it | As described | Automated (`test_scout.py`) |
| **6. Official records** | NHTSA recalls become quotable records; unknown years are skipped; investigations load once and are found by vehicle; a failing lookup is skipped; scouts see official records first and keep their source type | As described | Automated (`test_records.py`) |
| **6. Scouts, quality** | 20 hypotheses with known answers, e.g. "Philips recalled CPAP machines in June 2021" (true) and "GM never recalled the Cobalt" (false) | The scout gets supports / contradicts right at least 18 times out of 20 | Needs DeepSeek + Brave |
| **7. Verdict** | 10 made-up sets of scout findings | The right call every time | Automated (`test_verdict.py`) |
| **8. Article** | Uncited sentences, unknown sources, invented quotes, quotes attributed to the wrong source | Each is caught; a clean article passes | Automated (`test_article.py`) |
| **8. Article, quality** | A person reads 5 articles | Accurate and fair | Needs a full run |
| **Hunt (depth-first)** | Three products: the most serious has no lead, the second does, the third is never reached. Then a second hunt, and a product that gains complaints | The hunt stops at the lead without reading the third product; the next hunt starts at the third; a product with new complaints is scanned again | Automated (`test_hunt.py`) |
| **LLM client** | Empty replies are retried, replies are cached, JSON mode is on, token usage is recorded | As described | Automated (`test_llm.py`) |
| **End to end** | One full run on real data | It publishes at least one article, or its log explains why not | Needs DeepSeek + Brave |
