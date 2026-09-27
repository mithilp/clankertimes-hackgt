"""The Reporter: owns one McLovin hypothesis from start to finish. Instructions: agents/reporter/.

    recall      - the desk's memory: past investigations of this hypothesis, or similar ones, and their memos.
    frame       - restate the hypothesis and break it into its elements: every word is something to research.
                  Set the minimum and maximum story. Suggest searches for the coverage scout.
    coverage    - the reporter never searches: a coverage scout finds prior reporting and reports back.
    plan        - credit prior coverage and find the gap. Then size the investigation: elements that one
                  record would settle go to one scout, elements needing different records get separate scouts,
                  and at least one scout rules out an innocent explanation. Code checks the plan: every
                  load-bearing element covered, every record McLovin named either assigned or deferred with a
                  reason. The reporter repairs a plan that fails, and code fills whatever is still missing.
    assign      - each scout gets a structured assignment: records to try in order (McLovin's first), the
                  agencies' own terms, what would support and what would contradict, and a budget by priority.
    rounds      - scouts go out in parallel. After each round the reporter directs the next: redirect a scout
                  (only by naming a new specific check), correct a wrong detail, drop a side question, grant a
                  new sub-hypothesis (only with named records), or stop.
    new         - the newsroom exists to publish what no outlet has: the coverage scout marks which elements are
                  already published, the plan must aim at least one scout at something unpublished (a new record,
                  number, connection, contradiction or development), and a story whose whole finding is already
                  out there is spiked before any budget is spent.
    verdict     - code decides from sources that count as proof: kill, write the maximum or minimum story, or
                  park. Writing also needs a NEW finding: a sub-hypothesis aimed at the gap, supported by a primary
                  record (government or court) that prior coverage didn't cite. The model can only downgrade.
    draft       - a cited article that must pass newsroom/article.py.
    council     - the skeptic, virality and novelty judges must all approve. If any flags the draft, it comes
                  back to the reporter, who either fixes it (and can send a scout for a missing fact) or spikes it
                  as not worth publishing.

Every stage is recorded to the desk (newsroom/desk.py: an Astra DB collection when configured), to SQLite
(`newsroom show <id>`), and to runs/reporter/<ts>/result.json.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import article, config, council, db, llm, playbooks, scout, scout_agent, web
from .bossman import run_dir
from .desk import TERMINAL, get_desk
from .signals import now
from .mclovin import hypothesis_id

MAX_SUBS = 8                 # sub-hypotheses one story may carry, counting ones granted along the way
MAX_ROUNDS = 3               # scout rounds before the reporter must decide
COUNCIL_EXTRA_SUBS = 8       # scouts the council can still ask for once the plan is full
EXTRA_REVISIONS = 2          # more rounds, one at a time, while two of three judges approve
MAX_REVISIONS = 2            # council rounds after the first draft
MAX_SOURCES = 30             # findings handed to the writer
SCOUT_WORKERS = 4
GATE_JUDGES = ("skeptic", "virality", "novelty")   # all must approve
PRIMARY = {"government_record", "court_record"}      # a new finding must rest on a record, not on someone's reporting
NOVELTY = ("record", "number", "connection", "contradiction", "time")
PRIORITY_BUDGET = {"high": 1.5, "medium": 1.0, "low": 0.6}
MIN_BUDGET = 3

GENERIC_RECORDS = {"public records", "records", "documents", "data", "news reports", "online sources",
                   "government records", "the web", "news", "court records", "search", "google"}

FRAME = """
# Your job right now: frame the story

You were just handed the hypothesis below. Before you send anyone anywhere:
1. Restate it precisely.
2. Break it into its elements: the separate facts that must each be true for it to hold. Every word is
   something to research ("NHTSA has an open investigation into steering loss on the 2023 Model 3" is:
   an NHTSA investigation exists; it concerns steering loss; it covers the 2023 Model 3; it is still open).
   For each element say whether the minimum story needs it ("minimum") or only the maximum story, and
   whether McLovin's evidence ALREADY establishes it with a record that counts (rarely: social posts and
   complaints never do).
3. Set the minimum story (worth publishing even if the full hypothesis fails) and the maximum story. This
   newsroom only publishes what no outlet has: the minimum story must contain something that isn't already
   in published reporting (a record nobody pulled, a number nobody computed, a connection nobody drew, an
   official claim contradicted by the record, or a development since the last coverage). Restating what's
   already reported is not a story.
4. Suggest 1-3 short searches a scout could use to find prior coverage of this exact story.

If the desk's memory shows this or a very similar hypothesis was already killed or parked, say what is
different now in "since_last_time", or say it isn't worth reopening.

Reply with JSON only:
{"restated": "...", "context": "a few words naming the product, company, agency or institution",
 "elements": [{"id": "E1", "claim": "...", "needed_for": "minimum|maximum", "established": false, "how": "..."}],
 "minimum_story": "...", "maximum_story": "...", "coverage_searches": ["..."], "since_last_time": ""}
"""

PLAN = """
# Your job right now: size the investigation and assign the scouts

You have the framed hypothesis and its elements, the records McLovin named (M1, M2, ...), what the
newsroom's database holds, and the coverage scout's report on prior reporting.

1. Credit the prior coverage that exists (by URL) and say what nobody has established: the gap.
2. Name the innocent explanations that would make the story wrong or unfair.
3. Decide how many scouts this story needs, and why. The rule:
   - elements that ONE record would settle together go to ONE scout (one scout reads the recall once);
   - elements that need DIFFERENT records get separate scouts;
   - elements already established need no scout;
   - at least one scout rules out an innocent explanation ("kind": "alternative"), phrased so the story
     needs it TRUE ("The stalling is not explained by the 2025 remote-start recall");
   - aim at the gap, not at re-proving what is already reported;
   - every scout costs budget. Never more than {max_subs}. Usually 3-5.
4. Write each scout's assignment:
   - "statement": one plain sentence that could be true or false, checkable on the public web, about one thing.
   - "covers": the element ids it settles.
   - "needed_for": "minimum" if the minimum story fails without it, else "maximum".
   - "priority": "high" (the story stands or falls on it), "medium" or "low". High gets more budget.
   - "records_first": the specific records or systems to try, in order. Use McLovin's by id ("M1") where they
     fit. Name systems, not categories: "NHTSA recalls API for 2023 Tesla Model 3", not "government records".
   - "search_terms": the words the records themselves use (agencies often use different terms than posts do).
   - "supports_if" and "contradicts_if": the concrete finding that would settle it each way.
5. Every McLovin record must be used in some scout's records_first, or listed in "deferred" with the reason.
6. This newsroom publishes only what no outlet has. The coverage scout marked which elements are already
   reported. Mark each assignment that goes after something unpublished with "new": true and its "novelty":
   "record" (a record nobody pulled), "number" (a count or rate nobody computed), "connection" (two facts
   nobody linked), "contradiction" (an official claim the record contradicts) or "time" (what changed since
   the coverage). At least one "new" assignment must be one the minimum story needs, and it must be settled
   by a primary record (a government or court record), because a news report means it's already published.
   If the story is already fully reported and you can see no unpublished angle, say so with "no_new_angle":
   true and why, instead of assigning scouts: the reporter will spike it and spend nothing.

Reply with JSON only:
{{"prior_coverage": [{{"url": "...", "what_it_established": "..."}}], "gap": "...", "alternatives": ["..."],
 "evidence_plan": [{{"source": "...", "why": "...", "value": "high|medium|low"}}],
 "sizing": {{"count": 3, "why": "why this many scouts and not more or fewer"}},
 "assignments": [{{"statement": "...", "covers": ["E1"], "needed_for": "minimum|maximum", "kind": "claim|alternative",
                  "priority": "high|medium|low", "records_first": ["M1", "..."], "search_terms": ["..."],
                  "supports_if": "...", "contradicts_if": "...", "new": true, "novelty": "record"}}],
 "deferred": [{{"record": "M3", "why": "..."}}], "no_new_angle": false, "no_new_angle_why": ""}}
"""

DIRECT = """
# Your job right now: read what the scouts brought back and direct the next round

Your scouts did the legwork. For each sub-hypothesis you see its assignment, the scout's report (its
verdict, what it didn't find, dead ends, what it suggests checking next, and any new hypotheses it
proposes), every call it made, and the quotes it brought back (checked word for word against the pages).
A scout's verdict is its opinion; the proof status below is what counts. "Proof status" is the code's reading: only
government records, court records and news outlets' own reporting count.

For each sub-hypothesis decide one action:
- "done": settled, or nothing more worth checking. Say why.
- "redirect": send its scout out again. You must name "next_check": the specific record, system or term it
  has not tried, and why it could change the answer. "Keep looking" is refused. You may replace its
  records_first and search_terms, and raise or lower its priority.
- "correct": a detail was wrong (a date, a number, a model year) but the substance holds. Give the
  corrected statement. It keeps its evidence. A wrong detail is not a kill.
- "drop": it turned out not to matter to the story. You cannot drop one the minimum story needs.

You may grant new sub-hypotheses the evidence points to, including ones your scouts propose, but only
with the specific records that would settle them. If sub-hypotheses keep coming back contradicted, ask whether the angle itself is wrong: a
different angle is a new story, so propose it as a spin-off for McLovin rather than drifting into it.

This newsroom publishes only what no outlet has: the story needs at least one NEW sub-hypothesis (marked
[NEW]) supported by a primary record (government or court). If none is supported yet, spend the next round
there: redirect a NEW scout to the records that could establish it, or grant one.

Stop ("stop": true) when the plan is covered, the last round produced nothing new, or nothing left could
change the verdict.

Reply with JSON only:
{"assessments": [{"id": "H1", "action": "done|redirect|correct|drop", "why": "...", "next_check": "...",
                  "records_first": ["..."], "search_terms": ["..."], "priority": "high|medium|low",
                  "corrected_statement": "..."}],
 "new_sub_hypotheses": [{"statement": "...", "records": ["the specific records that would settle it"],
                         "supports_if": "...", "contradicts_if": "..."}],
 "spinoffs": [{"hypothesis": "...", "why": "..."}],
 "stop": false, "stop_reason": "plan covered | no new yield | nothing left could change the verdict"}
"""

PIVOT = """
# Your job right now: is there a different, true story in what the scouts established?

The story as planned won't run: {why}
But the scouts established things with primary records that no outlet has published. Reporters follow the
evidence: the story is what the records show, not what we expected them to show.

Using ONLY sub-hypotheses whose proof status is supported or disputed, and their verified quotes, is there a
story that:
  (a) leads with at least one sub-hypothesis marked NEW (listed at the top as the NEW FINDING),
  (b) a general reader would care about: a number nobody computed, two records that disagree, a promise
      against what happened, an official status that isn't true in practice, money moving somewhere unexpected,
  (c) states plainly what contradicted the original premise, when a reader would otherwise be misled.
Say no if the only new material is trivia, a restatement of a press release, or a document merely existing.

Reply with JSON only:
{{"pivot": true, "hypothesis": "one sentence the records support", "minimum_story": "what the article
establishes, in two sentences", "rests_on": ["H2"], "why": "why a reader would care"}}
or {{"pivot": false, "why": "..."}}
"""

MEMO = """
# Your job right now: the verdict memo

The newsroom's rules have already turned the evidence into a verdict, shown below. You may DOWNGRADE it
(write -> park, or write -> kill, or park -> kill), never upgrade it. Downgrade a "write" if an innocent
explanation was never ruled out, if the supporting quotes are about a different problem than the story,
or if the story would mislead readers about what the evidence shows.

Then write the memo that keeps the newsroom from chasing this twice:
- what was checked (systems, searches, records), what was found, and what would change the verdict;
- for a park: the wake condition (a filing appears, a meeting happens, a records request comes due) and
  who holds the evidence if a person or office does;
- "No record found" is a statement about a search, never about the world. Scope it.

Finally, narrow the hypothesis: rewrite McLovin's hypothesis as exactly what the evidence now supports
(or, for a kill, what it shows instead). This is what the story will claim, and no more.

Reply with JSON only:
{"downgrade_to": null, "downgrade_why": "", "narrowed_hypothesis": "...",
 "memo": {"checked": "...", "found": "...", "would_change_it": "...", "wake_condition": "...", "held_by": "..."},
 "headline_idea": "one line, only if the verdict is write"}
"""

WRITE = """
# Your job right now: write the article

Write a short investigative news article (350-600 words) using only the sources given.

Sources:
- "D" is what the newsroom saw online: the posts and pages that raised the question. They are the claim
  itself, never proof of it. Attribute them ("posts on Reddit said..."), and do not quote them.
- "F1", "F2", ... are the scouts' findings, each a quote checked against its page, with its source type.

Rules:
- This newsroom is automated and does not contact anyone. Give the accountable party's own public words where
  the sources have them (statements, filings, testimony), and end with one plain sentence saying the Clanker
  Times did not contact them for this story. Never write that comment was sought or declined.
- Write the story the evidence supports: the maximum story only if the verdict says so, otherwise the
  minimum story. Sub-hypotheses marked unsupported are unknowns: say what is not established, or leave them out.
- Lead with the NEW finding: what this reporting established that no outlet had published (it's given
  below). Then credit, by outlet, what earlier coverage already reported. Be specific: the record, the
  number, the agency, the date.
- Every sentence cites at least one source id.
- Match the wording to the evidence. Strongest first:
    two independent records agree -> state it plainly;
    one record -> "records show ..." or "according to <the record>";
    an official finding that is not final -> "alleged", and never upgrade a charge to a conviction;
    an act established but not intent -> drop the intent verb ("did not follow the rule", not "ignored" it);
    a pattern but not a cause -> state the pattern and say the cause is not established.
- Use the exact term a record uses, never a heavier synonym ("errors" is not "fraud").
- An agency or office charges, inspects or fines. It is never the accused in its own record.
- Give numbers, not adjectives. Date-scope data ("through <date>"), never "currently" or "to date".
- Credit prior coverage by outlet when a source is another outlet's reporting.
- Say which innocent explanations were ruled out, and how. If a source contradicts part of the story, say so.
- Include the accountable party's public response if a source has one. Never write that it "did not
  respond" or "declined to comment": nobody asked it. Never claim something didn't happen unless a source says so.
- Put direct quotes in double quotation marks, copied exactly from a source's text.
- Name no private individuals.

Write numbers the way a newspaper does in your own sentences: "5.17%", "$3.2 million", "two years". Quoted
text stays exactly as the source wrote it.

The headline is what makes someone read the story: say what happened and why it matters to a reader, in
plain words (who failed, who paid, what went wrong, what regulators did or didn't do), in one grammatical
sentence of no more than about 16 words. Don't lead the headline with a technical figure (a ratio, a
docket number, a statute), and use no jargon a general reader wouldn't know ("covenant", "leverage ratio",
"arrears", "preemption"): say what it means instead. The specific numbers go in the dek. Good: "A Philadelphia bank failed more than
two years after regulators ordered it to fix its capital". Bad: "Bank reported a 5.17 percent leverage
ratio at the December 2024 deadline its consent order set at 9 percent".

Also write a "kicker" (two to four words naming the subject, e.g. "Vehicle safety") and a "dek": one or two
sentences under the headline with the specific finding and its numbers, as strong as the evidence and no
stronger. Then three short lines for the box at the top of the story:
  "found": what this reporting established that no outlet had published, in one or two plain sentences.
  "prior": who reported what before (by outlet), in one sentence; "" if nobody had.
  "why_it_matters": one plain sentence on who is affected and how (customers, taxpayers, investors, students).

Reply with JSON only: {"kicker": "...", "headline": "...", "dek": "...", "found": "...", "prior": "...", "why_it_matters": "...",
 "paragraphs": [[{"text": "One sentence.", "cite": ["F1"]}]]}
"""

TRIAGE = """
# Your job right now: the council sent your draft back

The skeptic (is it true, fair and defensible?), the virality judge (will people care and share it?) and
the novelty judge (is it something no outlet has published?) must all approve before anything is
published. At least one of them flagged this draft. Decide:

- "fix": the problems can be fixed with the evidence you have, or with one or two specific checks a scout
  could make. Wording, sourcing, framing, a missing caveat, a buried lead, a dull headline: all fixable.
- "spike": the story isn't worth publishing in any honest form. Spike when the judges show that the only
  interesting version overstates the evidence, that it adds nothing to what is already reported, or that
  nobody affected would care even when told well. Don't spike over something a revision could fix.

If a fix needs a public record or number your sources don't have (a filed figure, a dataset value, a
document's text), list it under "research" as a statement a scout can check, with the records to try.
Scouts go out before you revise.

Reply with JSON only: {"decision": "fix|spike", "why": "...", "fixes": ["what you will change"],
 "research": [{"statement": "...", "records": ["..."]}]}
"""

REVISE = """
# Your job right now: revise the draft after the council's review

Fix every problem the skeptic, the virality judge and the novelty judge raised: any of them can stop
publication. If novelty says it's already reported, lead with what is new and credit the rest by outlet;
if nothing is new, say so in "changes" and leave the draft as is. Never make it more shareable by
overstating the evidence: the skeptic outranks virality. You may cut sentences. You may not add a claim no
source supports.

If a problem can only be fixed with a fact you don't have, list it under "needs_research" as a plain
statement a scout could check, with the records to try. When a judge names a specific public number or
record the story needs (a filed figure, a dataset value, a document's text), ask for it here: don't soften
the story to avoid it. Otherwise leave that list empty and cut or soften the claim.

The writing rules below still apply.

Reply with JSON only:
{"kicker": "...", "headline": "...", "dek": "...", "found": "...", "prior": "...", "why_it_matters": "...",
 "paragraphs": [[{"text": "...", "cite": ["F1"]}]],
 "changes": ["what you changed and why"],
 "needs_research": [{"statement": "...", "records": ["..."]}]}
"""


# --- the input -----------------------------------------------------------------------------------

def normalize(hypothesis: dict | str) -> dict:
    """A McLovin hypothesis record, or a plain string, as a full record. McLovin's records get ids M1, M2..."""
    h = {"hypothesis": hypothesis} if isinstance(hypothesis, str) else dict(hypothesis)
    h["hypothesis"] = str(h.get("hypothesis", "")).strip()
    if not h["hypothesis"]:
        raise ValueError("the hypothesis is empty")
    h["id"] = h.get("id") or hypothesis_id(h["hypothesis"])
    for key in ("who_would_know", "would_settle_it"):
        h[key] = [str(x).strip() for x in h.get(key) or [] if str(x).strip()]
    for key in ("why_now", "accountable_party"):
        h[key] = str(h.get(key) or "").strip()
    h["evidence_so_far"] = dict(h.get("evidence_so_far") or {})
    h["evidence_so_far"].setdefault("signal_ids", [])
    h["records"] = [{"id": f"M{n}", "record": r} for n, r in enumerate(h["would_settle_it"], 1)]
    return h


def brief(h: dict) -> str:
    lines = [f"Hypothesis (from McLovin): {h['hypothesis']}"]
    if h["why_now"]:
        lines.append(f"Why now: {h['why_now']}")
    if h["accountable_party"]:
        lines.append(f"Accountable party: {h['accountable_party']}")
    if h["who_would_know"]:
        lines.append(f"Who would know: {'; '.join(h['who_would_know'])}")
    if h["records"]:
        lines.append("Records McLovin says would settle it:\n" + "\n".join(f"  {r['id']}. {r['record']}" for r in h["records"]))
    ev = h["evidence_so_far"]
    if ev.get("signal_ids"):
        lines.append(f"Evidence so far: {len(ev['signal_ids'])} signals from {ev.get('distinct_origins', '?')} distinct origins")
    return "\n".join(lines)


# --- the newsroom's DB ---------------------------------------------------------------------------

def signals_for(h: dict, store=None) -> tuple[list, list[dict]]:
    """The signals McLovin cited (the reporter's input, not a search), and the pages behind them as the
    scouts' first leads. Finding other related signals is scout work (db_signals). A store that can't be
    reached is skipped, never fatal."""
    if not h["evidence_so_far"]["signal_ids"]:
        return [], []
    if store is None:
        try:
            from .signals import get_store
            store = get_store()
        except Exception:  # noqa: BLE001 - a missing or misconfigured store only means fewer leads
            return [], []
    found, seen = [], set()
    try:
        for sid in h["evidence_so_far"]["signal_ids"]:
            if (s := store.get(sid)) and s.id not in seen:
                seen.add(s.id)
                found.append(s)
    except Exception:  # noqa: BLE001
        pass
    leads, urls = [], set()
    for s in found:
        for src in s.sources:
            url = str(src.get("url", ""))
            if url.startswith("http") and url not in urls:
                urls.add(url)
                # The page's own title, not the signal's summary: a lead's title becomes its source title.
                leads.append({"url": url, "title": str(src.get("title") or "").strip() or web.host(url),
                              "description": f"{s.summary[:120]}: {s.checkable_claim}"})
    return found, leads


def db_listing(signals: list) -> str:
    if not signals:
        return "(nothing in the signals store about this)"
    return "\n".join(f"- {s.summary}\n  claim: {s.checkable_claim}\n  records trail: {'; '.join(s.records_trail) or '?'}"
                     f"\n  seen on: {', '.join(s.source_types)}; origin {s.origin}" for s in signals)


# --- evidence ------------------------------------------------------------------------------------

def proof(findings: list[dict], finding: str) -> list[dict]:
    return [f for f in findings if f["finding"] == finding and f["source_type"] in scout.COUNTS_AS_PROOF]


def status(findings: list[dict]) -> str:
    """supported | contradicted | disputed | open, from sources that count as proof only."""
    sup, con = proof(findings, "supports"), proof(findings, "contradicts")
    if sup and con:
        return "disputed"      # the article reports both sides; not a kill
    return "supported" if sup else "contradicted" if con else "open"


def new_findings(subs: list[dict], coverage_urls=()) -> list[tuple[dict, dict]]:
    """(sub-hypothesis, quote) pairs that establish something unpublished: a sub-hypothesis aimed at the gap,
    supported by a primary record that prior coverage didn't already cite."""
    seen = set(coverage_urls)
    return [(s, f) for s in subs if s.get("new") and not s.get("dropped") for f in s["findings"]
            if f["finding"] == "supports" and f["source_type"] in PRIMARY and f["url"] not in seen]


def verdict(subs: list[dict], *, coverage_urls=(), require_new: bool = True) -> tuple[str, str, str]:
    """(verdict, which story, why). Kill if the minimum story is contradicted; write the maximum story if
    everything holds, the minimum story if its part holds; park otherwise. With require_new, a write also
    needs a new finding (new_findings): a story made only of what's already published is parked."""
    v, story, why = _verdict(subs)
    if v == "write" and require_new and not new_findings(subs, coverage_urls):
        return "park", "", ("nothing new is established yet: every supported finding is already published or rests "
                            "on news reports, and this newsroom publishes only what no outlet has")
    return v, story, why


def _verdict(subs: list[dict]) -> tuple[str, str, str]:
    live = [s for s in subs if not s.get("dropped")]
    if not live:
        return "park", "", "no sub-hypotheses left"
    minimum = [s for s in live if s["needed_for"] == "minimum"] or live
    for s in minimum:
        if status(s["findings"]) == "contradicted":
            against = proof(s["findings"], "contradicts")[0]
            return "kill", "", f'{s["id"]} is contradicted by {against["url"]}: "{against["quote"]}"'
    holds = {s["id"]: status(s["findings"]) in ("supported", "disputed") for s in live}
    if all(holds.values()):
        return "write", "maximum", "every sub-hypothesis is supported by a source that counts"
    if all(holds[s["id"]] for s in minimum):
        missing = [s["id"] for s in live if not holds[s["id"]]]
        return "write", "minimum", "the minimum story holds; not established: " + ", ".join(missing)
    missing = [s["id"] for s in minimum if not holds[s["id"]]]
    return "park", "", "not yet supported by a source that counts: " + ", ".join(missing)


def _dedupe(findings: list[dict]) -> list[dict]:
    seen, out = set(), []
    for f in findings:
        key = (f["url"], f["quote"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _is_generic(records: list[str]) -> bool:
    named = [r.strip().lower().rstrip(".") for r in records if r.strip()]
    return not named or all(r in GENERIC_RECORDS for r in named)


def _strs(value) -> list[str]:
    return [str(x).strip() for x in value if str(x).strip()] if isinstance(value, list) else []


COVERAGE_QUERIES = """
# Your job right now: find earlier reporting

Write three short news searches (3 to 7 words each, no quotes, no operators) that would find earlier
reporting on this story if it exists: the institution plus the event, the way a headline would say it.

Reply with JSON only: {"queries": ["...", "...", "..."]}
"""


NARRATE = """You write the "How this story came together" timeline under a news article, for a general reader.
Each numbered step below is one moment in the reporting, with who did it. Rewrite each step's text as one or two
short, plain sentences a curious non-expert follows. Keep every fact, add none. Drop docket numbers, statute
sections, report codes, file names and internal labels unless the reader needs them. Refer to agents plainly
("a research agent", "another research agent"); never "scout 3" or "H2". Make the steps read as a sequence:
"A research agent confirmed...", "Another research agent then found...". A hunch step starts "An agent had a
hunch that".
Keep each step under 45 words. Leave out steps about the mechanics of the reporting itself (duplicate links,
which reporter wrote an article, company descriptions everyone knows): return "" for those research steps.
Keep the records' own terms for legal actions ("consent order", "civil money penalty", "lawsuit"), with a few
plain words of explanation if needed; never swap in a milder or different word ("warning" is not a consent
order). Say only what each research agent confirmed: leave out what it couldn't find or verify. If a research
agent's step is mostly about what it couldn't find, return "" as its text and it will be left out.

Reply with JSON only: {"steps": [{"i": 0, "text": "..."}]}
"""


def _ts(at) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(_iso(at)).timestamp()
    except ValueError:
        return 0.0


def whole_sentences(text: str, limit: int) -> str:
    """Text cut to at most `limit` characters without cutting a sentence (or, failing that, a word) in half."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end > 0:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…"


class CoverageUnknown(RuntimeError):
    """The coverage scout couldn't run a single search, so whether the story is already reported is unknown."""


def coverage_checked(trail: list[dict]) -> bool:
    """True when at least one search for prior coverage returned results. A search that never ran, failed,
    or came back empty doesn't count: an empty result is more often a bad query (a whole sentence, say)
    than proof that nobody reported the story, and publishing an already-reported story is the worse error."""
    return any(t.get("tool") in ("news_search", "web_search") and re.match(r"[1-9]\d* ", str(t.get("result", "")))
               for t in trail)


# How the timeline names who did what.
DESKS = {"atlanta": "Atlanta desk", "georgia-tech": "Georgia Tech desk", "tech": "Technology desk",
         "us-politics": "Politics desk", "product-safety": "Product safety desk", "ai-industry": "AI desk",
         "higher-ed": "Higher education desk"}
JUDGES = {"skeptic": "Skeptic", "novelty": "Novelty judge", "virality": "Virality judge"}


def _iso(at) -> str:
    """Any stored timestamp ("...Z", "+00:00", with or without microseconds) as UTC ISO, to the second."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return str(at)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def require_new() -> bool:
    """NEWSROOM_REQUIRE_NEW=0 turns off the new-finding rule (for testing the rest of the pipeline only)."""
    import os
    return os.getenv("NEWSROOM_REQUIRE_NEW", "1") != "0"


def budget_for(base: int, priority: str) -> int:
    return max(MIN_BUDGET, round(base * PRIORITY_BUDGET.get(priority, 1.0)))


def assignment_text(sub: dict) -> str:
    """The scout's assignment as the direction its searches follow. The structured version is sub["assignment"]."""
    a = sub["assignment"]
    parts = []
    if a.get("next_check"):
        parts.append(f"Check this next: {a['next_check']}.")
    if a.get("records_first"):
        parts.append("Try these records first, in order: " + "; ".join(f"{n}. {r}" for n, r in enumerate(a["records_first"], 1)) + ".")
    if a.get("search_terms"):
        parts.append("The records' own terms to search: " + ", ".join(a["search_terms"]) + ".")
    if a.get("supports_if"):
        parts.append(f"It is supported if: {a['supports_if']}.")
    if a.get("contradicts_if"):
        parts.append(f"It is contradicted if: {a['contradicts_if']}. Search for that as hard as for support.")
    return " ".join(parts)


# --- the investigation ---------------------------------------------------------------------------

class Investigation:
    def __init__(self, h: dict, *, budget: int, rounds: int, store=None, desk=None, say=print) -> None:
        self.h, self.budget, self.max_rounds, self.say = h, budget, rounds, say
        self.store, self.desk = store, desk
        self.system = playbooks.load("reporter")
        self.smart = config.load().smart_model
        self.subs: list[dict] = []
        self.rounds: list[dict] = []
        self.spinoffs: list[dict] = []
        self.declined: list[dict] = []
        self.frame: dict = {}
        self.plan: dict = {}
        self.sizing: dict = {}
        self.memory: list[dict] = []
        self.coverage: dict = {"findings": [], "trail": [], "report": {}}
        self.coverage_urls: set[str] = set()
        self.already_reported = ""
        self.signals: list = []
        self.leads: list[dict] = []
        self.record_id = ""
        self._desk_warned = False

    def ask(self, task: str, user: str, *, max_tokens: int = 4000, thinking: bool = False) -> dict:
        return llm.ask_json(self.system + "\n" + task, user, model=self.smart, max_tokens=max_tokens, thinking=thinking)

    # 0. the desk's memory
    def recall(self) -> None:
        if self.desk is None:
            return
        try:
            past = {r["_id"]: (r, 1.0) for r in self.desk.for_hypothesis(self.h["id"])}
            for r, score in self.desk.similar(f"{self.h['hypothesis']}\n{self.h['accountable_party']}", k=3):
                past.setdefault(r["_id"], (r, score))
        except Exception as e:  # noqa: BLE001 - memory is a help, not a requirement
            self.say(f"desk memory unavailable: {e}")
            return
        for r, score in past.values():
            if r["_id"] == self.record_id or r.get("status") not in TERMINAL:
                continue
            memo = (r.get("verdict") or {}).get("memo") or {}
            self.memory.append({"id": r["_id"], "hypothesis": r.get("hypothesis", ""), "status": r.get("status"),
                                "why": (r.get("final") or {}).get("note", ""), "found": memo.get("found", ""),
                                "would_change_it": memo.get("would_change_it", ""), "score": round(float(score), 3)})
        if self.memory:
            self.say(f"desk memory: {len(self.memory)} related past investigation(s)")

    def memory_listing(self) -> str:
        if not self.memory:
            return "(the desk has no related past investigations)"
        return "\n".join(f"- [{m['status']}] {m['hypothesis']}\n  why: {m['why']}\n  found: {m['found']}\n"
                         f"  would change it: {m['would_change_it']}" for m in self.memory)

    # 1. frame
    def start(self) -> None:
        self.started_at = now()
        self.signals, self.leads = signals_for(self.h, self.store)
        self.say(f"newsroom DB: {len(self.signals)} related signals, {len(self.leads)} pages as leads")
        self.recall()
        self.frame = self.ask(FRAME, f"{brief(self.h)}\n\nWhat the newsroom's DB holds:\n{db_listing(self.signals)}\n\n"
                                     f"The desk's memory:\n{self.memory_listing()}", max_tokens=4000)
        self.frame["elements"] = self._elements(self.frame.get("elements"))
        self.say(f"minimum story: {self.frame.get('minimum_story', '')}\nmaximum story: {self.frame.get('maximum_story', '')}")
        self.say(f"{len(self.frame['elements'])} elements: " + "; ".join(f"{e['id']} {e['claim']}" for e in self.frame["elements"]))
        self.record("planning")

        # Prior coverage is legwork too: a coverage scout finds it, and the reporter reads its report.
        self.say("coverage scout out: what has already been reported?")
        task = {"id": "coverage", "mode": "coverage", "statement": self.frame.get("restated") or self.h["hypothesis"],
                "searches": self.coverage_queries(),
                "elements": [{"id": e["id"], "claim": e["claim"]} for e in self.frame["elements"]]}
        try:
            self.coverage = scout_agent.run(task, budget=max(MIN_BUDGET, self.budget // 2), context=self._context(),
                                            leads=self.leads, say=self.say)
        except web.SearchError:
            raise
        except Exception as e:  # noqa: BLE001 - no coverage report means the plan says so, not a failed story
            self.coverage = {"findings": [], "trail": [], "report": {"coverage": [], "summary": f"coverage scout failed: {e}"}}
        if not coverage_checked(self.coverage.get("trail", [])):
            # Web search is often paused (DuckDuckGo bot checks). Google News doesn't depend on it: send the
            # coverage scout out once more with news search, reading and the newsroom's DB only.
            # The scout can't be trusted to pick the search (it may spend its budget re-reading leads), so the
            # reporter runs the news searches itself, then sends the scout to read and judge what they found.
            from .gather import news_search
            queries = task["searches"]
            ran, hits = [], []
            for q in queries:
                try:
                    found = news_search(q if "when:" in q else f"{q} when:365d", limit=8)
                    ran.append({"tool": "news_search", "arg": q, "why": "prior coverage (run by the reporter)",
                                "result": f"{len(found)} news results"})
                    hits += [{"url": c["url"], "title": c["title"], "description": c["snippet"]} for c in found]
                except Exception as e:  # noqa: BLE001 - recorded; coverage stays unknown if every one fails
                    ran.append({"tool": "news_search", "arg": q, "why": "prior coverage (run by the reporter)",
                                "result": f"failed: {type(e).__name__}: {e}"[:200]})
            self.say(f"  coverage: web search down; the reporter ran {len(queries)} news search(es) itself, "
                     f"{len(hits)} results for the scout to read")
            first = self.coverage
            again = {"findings": [], "trail": [], "report": first.get("report", {})}
            if hits:
                # The pages behind McLovin's signals go first: when one is a news story, it is prior coverage
                # by definition, and a fresh news search often doesn't surface it.
                seen = {lead["url"] for lead in self.leads}
                try:
                    again = scout_agent.run({**task, "tools": ["read", "news_search"]},
                                            budget=max(MIN_BUDGET, self.budget // 2), context=self._context(),
                                            leads=self.leads + [h for h in hits if h["url"] not in seen], say=self.say)
                except web.SearchError:
                    raise
                except Exception as e:  # noqa: BLE001 - the searches ran; the scout's reading just failed
                    self.say(f"  coverage scout failed reading: {type(e).__name__}: {e}")
            self.coverage = {**again, "trail": first.get("trail", []) + ran + again.get("trail", []),
                             "findings": first.get("findings", []) + again.get("findings", [])}
        if not coverage_checked(self.coverage.get("trail", [])):
            # "Nothing found" only means something if we could look. Never publish a story whose prior
            # coverage nobody could check: stop, and let the pipeline retry it when search is back.
            raise CoverageUnknown("couldn't check prior coverage: every search for it failed. "
                                  "Not writing a story that may already be reported; it will be retried.")
        self.coverage_at = now()
        cov = self.coverage["report"]
        found = cov.get("coverage", [])
        self.coverage_urls = {c["url"] for c in found} | {f["url"] for f in self.coverage.get("findings", [])}
        reported = set(cov.get("reported_elements", []))
        for e in self.frame["elements"]:
            e["reported"] = e["id"] in reported
        self.say(f"  coverage scout: {cov.get('status', 'unknown')}; {len(found)} prior report(s)"
                 + (f"; already published: {', '.join(sorted(reported))}" if reported else "") + f". {cov.get('summary', '')}")
        self._plan()

    def coverage_queries(self) -> list[str]:
        """Short searches for earlier reporting: the plan's, or three written for the purpose. Never the whole
        hypothesis as one query, which news search answers with nothing."""
        queries = [q for q in _strs(self.frame.get("coverage_searches")) if 0 < len(q.split()) <= 10][:3]
        if not queries:
            reply = self.ask(COVERAGE_QUERIES, brief(self.h), max_tokens=400)
            queries = [q for q in _strs(reply.get("queries")) if 0 < len(q.split()) <= 10][:3]
        return queries or [" ".join(self.h["hypothesis"].split()[:8])]

    def _context(self) -> str:
        return str(self.frame.get("context") or self.h["accountable_party"] or "")

    @staticmethod
    def _elements(raw) -> list[dict]:
        out = []
        for n, e in enumerate([e for e in raw or [] if isinstance(e, dict) and str(e.get("claim", "")).strip()], 1):
            out.append({"id": f"E{n}", "claim": str(e["claim"]).strip(),
                        "needed_for": "minimum" if e.get("needed_for") == "minimum" else "maximum",
                        "established": e.get("established") is True, "how": str(e.get("how", ""))})
        return out

    # 2. plan and size
    def _plan(self) -> None:
        cov = self.coverage.get("report", {}) if isinstance(self.coverage, dict) else {}
        listing = ("\n".join(f"- {c.get('outlet', '')} {c.get('date', '')} {c['url']}: {c.get('what_it_established', '')}"
                             for c in cov.get("coverage", [])) or "(the coverage scout found no prior reporting)")
        listing += (f"\nWhat it says nobody established: {cov.get('gap', '')}" if cov.get("gap") else "") \
            + (f"\nIt searched: {'; '.join(cov.get('searched', []))}" if cov.get("searched") else "")
        elements = "\n".join(f"{e['id']} [{e['needed_for']}{', established: ' + e['how'] if e['established'] else ''}"
                             f"{', ALREADY PUBLISHED' if e.get('reported') else ''}] {e['claim']}"
                             for e in self.frame["elements"]) or "(none listed)"
        prompt = (f"{brief(self.h)}\n\nRestated: {self.frame.get('restated', '')}\nElements:\n{elements}\n"
                  f"Minimum story: {self.frame.get('minimum_story', '')}\nMaximum story: {self.frame.get('maximum_story', '')}\n\n"
                  f"What the newsroom's DB holds:\n{db_listing(self.signals)}\n\nThe desk's memory:\n{self.memory_listing()}\n\n"
                  f"The coverage scout's report:\n\n{listing}")
        task = PLAN.format(max_subs=MAX_SUBS)
        self.plan = self.ask(task, prompt, max_tokens=6000, thinking=True)
        problems = self.plan_problems(self.plan)
        repairs = []
        if problems:
            self.say("plan needs work: " + "; ".join(problems))
            fixed = self.ask(task, prompt + "\n\nYOUR LAST PLAN:\n" + json.dumps(self.plan, ensure_ascii=False)
                             + "\n\nIt has these problems. Reply with the whole corrected plan:\n" + "\n".join(f"- {p}" for p in problems),
                             max_tokens=6000, thinking=True)
            if fixed.get("assignments"):
                self.plan = fixed
            repairs = [f"reporter repaired: {p}" for p in problems]
        if self.plan.get("no_new_angle") is True and require_new():
            self.already_reported = str(self.plan.get("no_new_angle_why") or "the story is already published, with no new angle")
            self.say(f"no new angle: {self.already_reported}")
            self.sizing = {"count": 0, "why": "no scouts: the story is already published", "repairs": repairs}
            return
        repairs += self._apply_plan()
        if not self.subs:
            raise RuntimeError("the reporter assigned no scouts")
        covered = {e for s in self.subs for e in s["covers"]}
        sizing = self.plan.get("sizing") if isinstance(self.plan.get("sizing"), dict) else {}
        self.sizing = {
            "count": len(self.subs), "why": str(sizing.get("why", "")),
            "elements": len(self.frame["elements"]),
            "load_bearing": sum(e["needed_for"] == "minimum" for e in self.frame["elements"]),
            "established": [e["id"] for e in self.frame["elements"] if e["established"]],
            "uncovered": [e["id"] for e in self.frame["elements"] if not e["established"] and e["id"] not in covered],
            "mclovin_records": len(self.h["records"]),
            "deferred": [d for d in self.plan.get("deferred", []) if isinstance(d, dict)],
            "alternatives_tested": sum(s["kind"] == "alternative" for s in self.subs),
            "new_scouts": [s["id"] for s in self.subs if s["new"]],
            "reported_elements": [e["id"] for e in self.frame["elements"] if e.get("reported")],
            "coverage_status": self.coverage.get("report", {}).get("status", "unknown"),
            "repairs": repairs,
        }
        if self.plan.get("gap"):
            self.say(f"gap: {self.plan['gap']}")
        self.say(f"\n{len(self.subs)} scouts: {self.sizing['why']}")
        for s in self.subs:
            self.say(f"  {s['id']} [{s['needed_for']}, {s['priority']}, budget {s['budget']}"
                     f"{', alternative' if s['kind'] == 'alternative' else ''}{', NEW ' + s['novelty'] if s['new'] else ''}] {s['statement']}"
                     f"\n      covers {', '.join(s['covers']) or '-'}; first: {'; '.join(s['assignment']['records_first'][:2])}")
        for r in repairs:
            self.say(f"  ({r})")

    def plan_problems(self, plan: dict) -> list[str]:
        """What code can check about a plan: coverage of the elements and of McLovin's records, an
        alternative tested, specific records, criteria both ways, and the size limit."""
        problems = []
        subs = [a for a in plan.get("assignments", []) if isinstance(a, dict) and str(a.get("statement", "")).strip()]
        if not subs:
            return ["no assignments"]
        if len(subs) > MAX_SUBS:
            problems.append(f"{len(subs)} scouts is more than the limit of {MAX_SUBS}: merge the ones one record settles")
        ids = {e["id"] for e in self.frame["elements"]}
        covered = {c for a in subs for c in _strs(a.get("covers"))}
        missing = [e["id"] for e in self.frame["elements"] if e["needed_for"] == "minimum" and not e["established"] and e["id"] not in covered]
        if missing:
            problems.append(f"load-bearing elements no scout covers: {', '.join(missing)}")
        if unknown := sorted(covered - ids):
            problems.append(f"assignments cover elements that don't exist: {', '.join(unknown)}")
        used = {m for a in subs for r in _strs(a.get("records_first")) for m in re.findall(r"\bM\d+\b", r)}
        deferred = {m for d in plan.get("deferred", []) if isinstance(d, dict) for m in re.findall(r"\bM\d+\b", str(d.get("record", "")))}
        if left := [r["id"] for r in self.h["records"] if r["id"] not in used | deferred]:
            problems.append(f"McLovin's records neither assigned nor deferred: {', '.join(left)}")
        if not any(a.get("kind") == "alternative" for a in subs):
            problems.append("no scout rules out an innocent explanation")
        if require_new() and not plan.get("no_new_angle") and not any(a.get("new") is True and a.get("needed_for") == "minimum" for a in subs):
            problems.append("no scout the minimum story needs goes after something no outlet has published "
                            "(mark it \"new\": true, settled by a government or court record)")
        for n, a in enumerate(subs, 1):
            if _is_generic([self._resolve(r) for r in _strs(a.get("records_first"))]):
                problems.append(f"assignment {n} names no specific records")
            if not str(a.get("supports_if", "")).strip() or not str(a.get("contradicts_if", "")).strip():
                problems.append(f"assignment {n} doesn't say what would support and what would contradict it")
        statements = [str(a["statement"]).strip().lower() for a in subs]
        if len(set(statements)) < len(statements):
            problems.append("two assignments have the same statement")
        return problems

    def _resolve(self, record: str) -> str:
        """"M2" -> McLovin's record 2, verbatim. Anything else stays as written."""
        by_id = {r["id"]: r["record"] for r in self.h["records"]}
        m = re.fullmatch(r"\s*(M\d+)\s*[:.)-]?\s*(.*)", record)
        return by_id[m[1]] if m and m[1] in by_id else record

    def _apply_plan(self) -> list[str]:
        """Turn the plan into sub-hypotheses, then fill in by code whatever the plan still misses."""
        notes = []
        planned = [a for a in self.plan.get("assignments", []) if isinstance(a, dict) and str(a.get("statement", "")).strip()]
        # Over the limit, keep what the minimum story needs and the high priorities; ids keep the plan's order.
        ranked = sorted(range(len(planned)), key=lambda i: (planned[i].get("needed_for") != "minimum",
                                                            {"high": 0, "medium": 1}.get(planned[i].get("priority"), 2), i))
        keep = set(ranked[:MAX_SUBS])
        for i, a in enumerate(planned):
            if i in keep:
                self._add_sub(a, origin="plan")
            else:
                notes.append(f"over the limit of {MAX_SUBS}, not assigned: {str(a['statement'])[:80]}")
        covered = {e for s in self.subs for e in s["covers"]}
        for e in self.frame["elements"]:
            if e["needed_for"] == "minimum" and not e["established"] and e["id"] not in covered and len(self.subs) < MAX_SUBS:
                sub = self._add_sub({"statement": e["claim"], "covers": [e["id"]], "needed_for": "minimum", "priority": "high",
                                     "records_first": [r["id"] for r in self.h["records"]]}, origin="desk")
                if sub:
                    notes.append(f"desk added {sub['id']} for uncovered element {e['id']}")
        used = {r for s in self.subs for r in s["assignment"]["records_first"]}
        deferred = {m for d in self.plan.get("deferred", []) if isinstance(d, dict) for m in re.findall(r"\bM\d+\b", str(d.get("record", "")))}
        target = next((s for s in self.subs if s["needed_for"] == "minimum"), self.subs[0] if self.subs else None)
        for r in self.h["records"]:
            if r["record"] not in used and r["id"] not in deferred and target:
                target["assignment"]["records_first"].append(r["record"])
                notes.append(f"desk gave McLovin's {r['id']} to {target['id']}")
        return notes

    def _add_sub(self, raw: dict, origin: str) -> dict | None:
        statement = str(raw.get("statement", "")).strip()
        cap = MAX_SUBS + (COUNCIL_EXTRA_SUBS if origin == "council" else 0)   # the council's asks come last and matter most
        if not statement or len(self.subs) >= cap:
            return None
        if any(statement.lower() == s["statement"].lower() for s in self.subs):
            return None
        priority = raw.get("priority") if raw.get("priority") in PRIORITY_BUDGET else "medium"
        records = [self._resolve(r) for r in _strs(raw.get("records_first") or raw.get("records"))]
        ids = {e["id"] for e in self.frame.get("elements", [])}
        sub = {"id": f"H{len(self.subs) + 1}", "statement": statement,
               "covers": [c for c in _strs(raw.get("covers")) if c in ids],
               "needed_for": "minimum" if raw.get("needed_for") == "minimum" else "maximum",
               "kind": "alternative" if raw.get("kind") == "alternative" else "claim",
               "new": raw.get("new") is True,
               "novelty": raw.get("novelty") if raw.get("novelty") in NOVELTY else ("" if raw.get("new") is not True else "record"),
               "priority": priority, "budget": budget_for(self.budget, priority), "origin": origin,
               "assignment": {"records_first": list(dict.fromkeys(records)), "search_terms": _strs(raw.get("search_terms")),
                              "supports_if": str(raw.get("supports_if", "")).strip(),
                              "contradicts_if": str(raw.get("contradicts_if", "")).strip(), "next_check": ""},
               "findings": [], "trail": [], "queries": [], "reports": [], "dispatches": 0, "open": True, "dropped": False,
               "history": []}
        self.subs.append(sub)
        return sub

    # 3. scouts
    def dispatch(self, subs: list[dict]) -> None:
        """Send one scout per sub-hypothesis, in parallel. The reporter only reads what they report."""
        def one(sub):
            try:
                task = {"id": sub["id"], "statement": sub["statement"], "assignment": sub["assignment"]}
                return scout_agent.run(task, budget=sub["budget"], context=self._context(),
                                       leads=self.leads if sub["dispatches"] == 0 else (),
                                       avoid=sub["queries"], say=self.say), None
            except Exception as e:  # noqa: BLE001 - one scout failing shouldn't sink the story
                return {"findings": [], "trail": [], "report": {}}, e

        with ThreadPoolExecutor(min(SCOUT_WORKERS, len(subs))) as pool:
            results = list(pool.map(one, subs))
        errors = [e for _, e in results if e]
        if errors and len(errors) == len(subs) and not any(r["findings"] for r, _ in results):
            raise errors[0]          # every scout failed: a config problem (no search key), not a finding
        for sub, (res, error) in zip(subs, results):
            before = len(sub["findings"])
            sub["findings"] = _dedupe(sub["findings"] + res["findings"])
            sub["trail"] += res["trail"]
            sub["queries"] += [f"{t['tool']}: {t['arg']}" for t in res["trail"] if t.get("tool") and t["tool"] != "read"]
            sub["dispatches"] += 1
            sub["open"] = False              # out again only if the reporter redirects it
            sub["last_new"] = len(sub["findings"]) - before
            report = dict(res.get("report") or {})
            if error:
                report = {"verdict": "unclear", "summary": f"the scout failed: {type(error).__name__}: {error}"[:300]}
            sub["reports"].append(report)
            sub["reported_at"] = now()
            if status(sub["findings"]) == "supported" and not sub.get("settled_at"):
                sub["settled_at"] = sub["reported_at"]      # when the claim was first confirmed, for the timeline
            tally = {k: sum(f["finding"] == k for f in sub["findings"]) for k in ("supports", "contradicts", "unclear")}
            self.say(f"  scout {sub['id']} reports {report.get('verdict', 'unclear')} ({report.get('confidence', '?')}): "
                     f"+{sub['last_new']} quotes {tally} -> {status(sub['findings'])}. {report.get('summary', '')[:160]}")

    def _status_listing(self) -> str:
        blocks = []
        for s in self.subs:
            if s["dropped"]:
                continue
            calls = [t for t in s["trail"] if t.get("tool")]
            checked = "; ".join(f"{t['tool']}({t['arg'][:70]})" for t in calls) or "(nothing yet)"
            quotes = "\n".join(f"    [{f['finding']}] {f['source_type']} {f['url']}\n      \"{f['quote'][:300]}\""
                               + (f"\n      note: {f['note'][:200]}" if f.get("note") else "")
                               for f in s["findings"][:10]) or "    (no verified quotes)"
            r = s["reports"][-1] if s["reports"] else {}
            report = (f"  scout's report: {r.get('verdict', '-')} ({r.get('confidence', '?')} confidence). {r.get('summary', '')}"
                      + (f"\n    not found: {'; '.join(r['not_found'])}" if r.get("not_found") else "")
                      + (f"\n    dead ends: {'; '.join(r['dead_ends'])}" if r.get("dead_ends") else "")
                      + (f"\n    it suggests checking next: {r['next_check']}" if r.get("next_check") else "")
                      + "".join(f"\n    it proposes: {p.get('hypothesis')} (records: {'; '.join(_strs(p.get('records')))})"
                                for p in r.get("proposals", []))
                      + (f"\n    note: {r['held_back']}" if r.get("held_back") else "")) if r else "  (no report yet)"
            blocks.append(f"{s['id']} [{s['needed_for']}, {s['kind']}, {s['priority']}{', NEW ' + s['novelty'] if s['new'] else ''}] {s['statement']}\n"
                          f"  proof status: {status(s['findings'])}; sent {s['dispatches']} time(s); "
                          f"new quotes last round: {s.get('last_new', 0)}\n"
                          f"  assignment: {assignment_text(s) or '(none)'}\n{report}\n  checked: {checked}\n{quotes}")
        novel = new_findings(self.subs, self.coverage_urls)
        head = (f"NEW FINDING so far: {novel[0][0]['id']} - \"{novel[0][1]['quote'][:200]}\" ({novel[0][1]['url']})" if novel
                else "NO NEW FINDING YET: nothing unpublished is supported by a primary record.")
        return head + "\n\n" + "\n\n".join(blocks)

    # 4. direct
    def direct(self, round_no: int) -> bool:
        """Read the round, set the next one. Returns True to keep going."""
        reply = self.ask(DIRECT, f"{brief(self.h)}\n\nGap: {self.plan.get('gap', '')}\n"
                                 f"Minimum story: {self.frame.get('minimum_story', '')}\n"
                                 f"Round {round_no} of at most {self.max_rounds}.\n\n{self._status_listing()}",
                         max_tokens=5000, thinking=True)
        by_id = {s["id"]: s for s in self.subs}
        record = {"round": round_no, "actions": [], "granted": [], "declined": [], "stop": bool(reply.get("stop")),
                  "stop_reason": str(reply.get("stop_reason", ""))}
        for a in reply.get("assessments", []):
            sub = by_id.get(str(a.get("id", "")).strip())
            if not sub or sub["dropped"] or not isinstance(a, dict):
                continue
            action, why = str(a.get("action", "")), str(a.get("why", ""))
            entry: dict = {"id": sub["id"], "action": action, "why": why}
            if action == "redirect":
                next_check = str(a.get("next_check", "")).strip()
                if not next_check or _is_generic([next_check]):
                    entry.update(action="done", why=f"redirect refused: it named no specific next check. {why}".strip())
                    sub["open"] = False
                else:
                    asg = sub["assignment"]
                    asg["next_check"] = next_check
                    if records := [self._resolve(r) for r in _strs(a.get("records_first"))]:
                        asg["records_first"] = list(dict.fromkeys(records))
                    if terms := _strs(a.get("search_terms")):
                        asg["search_terms"] = terms
                    if a.get("priority") in PRIORITY_BUDGET:
                        sub["priority"], sub["budget"] = a["priority"], budget_for(self.budget, a["priority"])
                    sub["open"] = True
                    entry["next_check"] = next_check
            elif action == "correct" and str(a.get("corrected_statement", "")).strip():
                sub["history"].append(sub["statement"])
                sub["statement"] = str(a["corrected_statement"]).strip()
                sub["open"] = False
                entry["statement"] = sub["statement"]
            elif action == "drop" and sub["needed_for"] != "minimum":
                sub["dropped"], sub["open"] = True, False
            else:
                if action == "drop":
                    entry["why"] = f"drop refused: the minimum story needs it. {why}".strip()
                entry["action"], sub["open"] = "done", False
            record["actions"].append(entry)
            self.say(f"  reporter -> {sub['id']}: {entry['action']}" + (f" ({entry['why']})" if entry["why"] else ""))
        for n in reply.get("new_sub_hypotheses", []):
            records = _strs(n.get("records")) if isinstance(n, dict) else []
            statement = str(n.get("statement", "")) if isinstance(n, dict) else str(n)
            if _is_generic(records):
                self.declined.append({"statement": statement, "why": "names no specific records that would settle it"})
                record["declined"].append(self.declined[-1])
                continue
            sub = self._add_sub({**n, "records_first": records, "needed_for": "maximum", "priority": "medium"}, origin=f"round {round_no}")
            if sub:
                record["granted"].append({"id": sub["id"], "statement": sub["statement"], "records": records})
                self.say(f"  reporter granted {sub['id']}: {sub['statement']}")
            else:
                self.declined.append({"statement": statement, "why": f"a duplicate, or over the limit of {MAX_SUBS}"})
                record["declined"].append(self.declined[-1])
        self.spinoffs += [{"hypothesis": str(s.get("hypothesis", "")), "why": str(s.get("why", ""))}
                          for s in reply.get("spinoffs", []) if isinstance(s, dict) and s.get("hypothesis")]
        self.rounds.append(record)
        return not record["stop"]

    def run_rounds(self) -> None:
        pending = list(self.subs)
        for round_no in range(1, self.max_rounds + 1):
            self.say(f"\nround {round_no}: {len(pending)} scouts out")
            self.record(f"scouting, round {round_no}")
            self.dispatch(pending)
            if round_no == self.max_rounds:
                self.rounds.append({"round": round_no, "actions": [], "granted": [], "declined": [],
                                    "stop": True, "stop_reason": "round limit"})
                break
            keep_going = self.direct(round_no)
            if round_no > 1 and all(s.get("last_new", 0) == 0 for s in pending):
                self.rounds[-1]["stop_reason"] = self.rounds[-1]["stop_reason"] or "no new yield"
                break
            pending = [s for s in self.subs if s["open"] and not s["dropped"]]
            if not keep_going or not pending:
                break

    # 5. verdict
    def decide(self) -> dict:
        v, story, why = verdict(self.subs, coverage_urls=self.coverage_urls, require_new=require_new())
        reply = self.ask(MEMO, f"{brief(self.h)}\n\nMinimum story: {self.frame.get('minimum_story', '')}\n"
                               f"Maximum story: {self.frame.get('maximum_story', '')}\n"
                               f"Innocent explanations named in the plan: {'; '.join(_strs(self.plan.get('alternatives'))) or '(none)'}\n\n"
                               f"VERDICT BY THE RULES: {v}{' (' + story + ' story)' if story else ''}: {why}\n\n{self._status_listing()}",
                         max_tokens=3000)
        order = {"write": 2, "park": 1, "kill": 0}
        down = str(reply.get("downgrade_to") or "").lower()
        downgraded = None
        if down in order and order[down] < order[v]:
            downgraded = {"from": v, "to": down, "why": str(reply.get("downgrade_why", ""))}
            v, story = down, ""
            why = f"downgraded by the reporter: {downgraded['why']}"
        novel = new_findings(self.subs, self.coverage_urls)
        pivot = None
        if v in ("kill", "park") and novel and not downgraded:
            pivot = self.pivot(why)
            if pivot:
                self.frame["pivot_story"] = pivot["minimum_story"]
                v, story, why = "write", "pivot", (f"the planned story failed ({why[:300]}); the records support a "
                                                  f"different one, resting on {', '.join(pivot['rests_on'])}: {pivot['why']}")
                novel = [n for n in novel if n[0]["id"] in pivot["rests_on"]] or novel
        new_finding = ({"id": novel[0][0]["id"], "statement": novel[0][0]["statement"], "novelty": novel[0][0]["novelty"],
                        "url": novel[0][1]["url"], "quote": novel[0][1]["quote"]} if novel else None)
        return {"verdict": v, "story": story, "why": why, "downgraded": downgraded, "new_finding": new_finding, "at": now(),
                "memo": reply.get("memo") if isinstance(reply.get("memo"), dict) else {},
                "narrowed_hypothesis": pivot["hypothesis"] if pivot else str(reply.get("narrowed_hypothesis") or "").strip(),
                "pivot": pivot,
                "headline_idea": str(reply.get("headline_idea") or "")}

    def pivot(self, why: str) -> dict | None:
        """A different story the records support, when the planned one was killed or parked. Checked in code:
        it must rest only on supported or disputed sub-hypotheses, at least one of them a new finding."""
        reply = self.ask(PIVOT.format(why=why), f"{brief(self.h)}\n\n{self._status_listing()}", max_tokens=2000)
        if reply.get("pivot") is not True or not str(reply.get("hypothesis", "")).strip():
            self.say(f"  no pivot: {str(reply.get('why', ''))[:200]}")
            return None
        holds = {s["id"] for s in self.subs if not s["dropped"] and status(s["findings"]) in ("supported", "disputed")}
        novel = {sub["id"] for sub, _ in new_findings(self.subs, self.coverage_urls)}
        rests = [r for r in _strs(reply.get("rests_on")) if r in holds]
        if not rests or not set(rests) & novel:
            self.say(f"  pivot rejected: it must rest on supported findings including a new one (said {reply.get('rests_on')})")
            return None
        self.say(f"  PIVOT: {reply['hypothesis']}")
        return {"hypothesis": str(reply["hypothesis"]).strip(), "minimum_story": str(reply.get("minimum_story", "")).strip(),
                "rests_on": rests, "why": str(reply.get("why", "")).strip()}

    # 6. draft
    def sources(self) -> dict[str, dict]:
        urls = []
        for s in self.signals:
            urls += [str(x.get("url")) for x in s.sources if str(x.get("url", "")).startswith("http")]
        # D carries URLs, never the model-written signal summaries: anything a model wrote is not a source.
        out = {"D": {"title": f"Posts and pages that raised the question ({len(set(urls))} seen by the newsroom)",
                     "url": urls[0] if urls else "", "text": "\n".join(dict.fromkeys(urls)), "source_type": "social"}}
        live = [f for s in self.subs if not s["dropped"] for f in s["findings"]]
        ranked = sorted(_dedupe(live), key=lambda f: (f["source_type"] not in scout.COUNTS_AS_PROOF, f["finding"] == "unclear"))
        for n, f in enumerate(ranked[:MAX_SOURCES], 1):
            out[f"F{n}"] = self._source(f)
        return out

    @staticmethod
    def _source(f: dict) -> dict:
        title = re.sub(r"^\[link on [^\]]*\]\s*", "", str(f.get("title") or "")).strip()
        return {"title": title or web.host(f["url"]), "url": f["url"], "text": f["quote"],
                "quote": f["quote"], "source_type": f["source_type"], "finding": f["finding"]}

    def _writer_input(self, decision: dict, sources: dict) -> str:
        which = {"maximum": self.frame.get("maximum_story"), "pivot": self.frame.get("pivot_story")}.get(
            decision["story"]) or self.frame.get("minimum_story")
        subs = "\n".join(f"- {s['id']} ({status(s['findings'])}): {s['statement']}" for s in self.subs if not s["dropped"])
        listing = "\n\n".join(f"[{sid}] {s['title']} ({s.get('source_type', '')}{', ' + s['finding'] if s.get('finding') else ''})\n"
                              f"{s['url']}\n{s['text'][:2000]}" for sid, s in sources.items())
        return (f"{brief(self.h)}\n\nVerdict: write the {decision['story']} story: {which}\n"
                f"The hypothesis, narrowed to what the evidence supports: {decision.get('narrowed_hypothesis') or which}\n"
                + (f"THE NEW FINDING (lead with it; no outlet has published it): {nf['statement']} - record: \"{nf['quote']}\" ({nf['url']})\n"
                   if (nf := decision.get("new_finding")) else "")
                + f"What prior coverage already reported (credit it by outlet): "
                  f"{json.dumps(self.coverage.get('report', {}).get('coverage', []), ensure_ascii=False)[:1500]}\n"
                f"Headline idea: {decision.get('headline_idea', '')}\n"
                f"Prior coverage: {json.dumps(self.plan.get('prior_coverage', []), ensure_ascii=False)}\n"
                f"Innocent explanations considered: {'; '.join(_strs(self.plan.get('alternatives')))}\n\n"
                f"Sub-hypotheses:\n{subs}\n\nSources:\n\n{listing}")

    def draft(self, decision: dict, sources: dict) -> tuple[dict | None, list[str]]:
        prompt = self._writer_input(decision, sources)
        problems: list[str] = []
        for _ in range(3):
            text = prompt if not problems else prompt + "\n\nYour last draft failed these checks; fix them:\n" + "\n".join(problems)
            d = self.ask(WRITE, text, max_tokens=6000)
            d = _draft(d)
            problems = article.check(d, sources)
            if not problems:
                return d, []
        return None, problems

    # 7. the council, and back to the reporter
    def review(self, decision: dict, first: dict, sources: dict, judges: tuple[str, ...],
               previous: dict | None = None) -> dict:
        d, history = first, []
        n, limit = 0, MAX_REVISIONS
        while n <= limit:
            result = council.review({"article": d, "sources": sources}, judges,
                                    previous=history[-1]["review"] if history else previous)
            entry = {"round": n + 1, "draft": d, "review": result, "at": now()}
            history.append(entry)
            flagged = [j["judge"] for j in result["judges"] if j["judge"] in GATE_JUDGES and j["verdict"] != "approve"]
            self.say(f"  council round {n + 1}: " + ", ".join(f"{j['judge']} {j['verdict']}" for j in result["judges"])
                     + (f" / mechanical: {result['mechanical'][:2]}" if result["mechanical"] else ""))
            self.record(f"council, round {n + 1}", council=history)
            if not result["mechanical"] and not flagged:
                return {"outcome": "approved", "draft": d, "history": history}
            approvals = sum(j["verdict"] == "approve" for j in result["judges"])
            if n == limit and approvals >= 2 and limit < MAX_REVISIONS + EXTRA_REVISIONS:
                limit += 1          # two of three judges are on board: one more round rather than hold it
                self.say(f"  {approvals} of 3 judges approve: one more revision round")
            if n == limit:
                break
            tri = self.triage(d, sources, result)
            entry["triage"] = tri
            self.say(f"  reporter: {tri['decision']} ({tri['why']})")
            if tri["decision"] == "spike":
                return {"outcome": "spiked", "draft": d, "history": history, "why": tri["why"]}
            researched = bool(tri.get("research")) and self.research(tri["research"], sources)
            revised = self.revise(decision, d, sources, result, plan=tri, researched=researched)
            if revised is None:
                break
            d = revised
            n += 1
        return {"outcome": "held", "draft": history[-1]["draft"], "history": history}

    @staticmethod
    def _council_notes(result: dict) -> str:
        notes = "\n".join(f"{j['judge']} ({j['verdict']}): " + "; ".join(
            f"\"{p.get('sentence', '')}\" - {p.get('issue', '')} (fix: {p.get('fix', '')})" for p in j["problems"])
            + (f" notes: {j['notes']}" if j["notes"] else "") for j in result["judges"])
        return notes + "".join(f"\nmechanical: {m}" for m in result["mechanical"])

    def triage(self, d: dict, sources: dict, result: dict) -> dict:
        if result["mechanical"] and not result["judges"]:
            return {"decision": "fix", "why": "mechanical checks failed", "fixes": result["mechanical"][:3]}
        reply = self.ask(TRIAGE, f"{brief(self.h)}\n\nDRAFT:\n{council.render({'article': d, 'sources': sources}).split('SOURCES:')[0]}"
                                 f"\nCOUNCIL:\n{self._council_notes(result)}", max_tokens=1500)
        decision = "spike" if reply.get("decision") == "spike" and str(reply.get("why", "")).strip() else "fix"
        research = [r for r in reply.get("research", []) or [] if isinstance(r, dict) and str(r.get("statement", "")).strip()]
        return {"decision": decision, "why": str(reply.get("why", "")), "fixes": _strs(reply.get("fixes")),
                "research": research[:2]}

    def research(self, asks: list[dict], sources: dict) -> bool:
        """Send scouts for facts the council needs, and add what they find to the sources. True if any went."""
        new = [self._add_sub({**r, "records_first": _strs(r.get("records")), "needed_for": "maximum",
                              "priority": "high"}, origin="council") for r in asks[:2]]
        new = [sub for sub in new if sub]
        if not new:
            self.say(f"  couldn't send scouts for the council ({len(self.subs)} sub-hypotheses already, or asked before)")
            return False
        self.say(f"  reporter sends {len(new)} scout(s) for the council: " + "; ".join(sub["statement"] for sub in new))
        self.dispatch(new)
        self._merge_sources(sources)
        return True

    def revise(self, decision: dict, d: dict, sources: dict, result: dict, plan: dict | None = None,
               researched: bool = False) -> dict | None:
        notes = self._council_notes(result)
        approved = [j["judge"] for j in result["judges"] if j["verdict"] == "approve"]
        if approved:
            notes += (f"\n\nALREADY APPROVED BY: {', '.join(approved)}. Keep what they approved: change only what the "
                      "other judges need, and don't rewrite parts nobody objected to.")
        if plan and (plan.get("why") or plan.get("fixes")):
            notes += (f"\n\nYOUR OWN PLAN FOR THIS REVISION: {plan.get('why', '')}"
                      + "".join(f"\n- {f}" for f in plan.get("fixes", [])))
        rules = WRITE.split("Rules:", 1)[1].rsplit("Reply with JSON", 1)[0]
        for attempt in range(3):      # research for the council doesn't use up a writing attempt
            reply = self.ask(REVISE + "\nRules:" + rules, f"{self._writer_input(decision, sources)}\n\nCURRENT DRAFT:\n"
                             f"{council.render({'article': d, 'sources': sources}).split('SOURCES:')[0]}\n\nCOUNCIL:\n{notes}",
                             max_tokens=6000)
            asks = [r for r in reply.get("needs_research", []) if isinstance(r, dict) and str(r.get("statement", "")).strip()]
            if asks and not researched:
                researched = True
                if self.research(asks, sources):
                    continue
            revised = _draft(reply, previous=d)
            problems = article.check(revised, sources)
            if not problems:
                return revised
            self.say(f"  revision failed its checks: {'; '.join(problems[:3])}")
            notes += "\n" + "\n".join(f"mechanical: {p}" for p in problems)
        return None

    def _merge_sources(self, sources: dict) -> None:
        """Add findings not yet in the source list, under fresh ids, so earlier citations stay valid."""
        have = {(s["url"], s.get("quote")) for s in sources.values()}
        n = max((int(k[1:]) for k in sources if re.fullmatch(r"F\d+", k)), default=0)
        for s in self.subs:
            for f in s["findings"]:
                if (f["url"], f["quote"]) not in have:
                    n += 1
                    have.add((f["url"], f["quote"]))
                    sources[f"F{n}"] = self._source(f)

    def site_fields(self, decision: dict, reviewed: dict) -> dict:
        """What the website needs beside the article: the beats it belongs to, and how it came together."""
        beats = sorted({b for s in self.signals for b in getattr(s, "beats", []) or []})
        return {"beats": beats, "timeline": self.timeline(decision, reviewed)}

    def timeline(self, decision: dict, reviewed: dict) -> list[dict]:
        """How the story came together, for the site, oldest first, in plain words a reader follows: what the
        news desk noticed, the hunch, what each research agent confirmed, the reporter's call, the council's
        sign-off. Dead ends, unsettled checks, contradicted claims and the editors' back-and-forth stay out."""
        steps: list[dict] = []

        def add(at, who: str, text: str, result: str = "") -> None:
            if at:
                steps.append({"at": _iso(at), "who": who, "text": text.strip(), "result": result})

        seen = [s for s in self.signals if getattr(s, "first_seen", "")]
        if seen:
            # The signal this story grew from: the one that shares the most with the story as published, not
            # just the earliest (a cluster of signals can start with a different company).
            about = " ".join([decision.get("narrowed_hypothesis", ""), self.h.get("accountable_party", ""),
                              str((reviewed.get("draft") or {}).get("headline", ""))]).lower()
            words = {w for w in re.findall(r"[a-z][a-z-]{3,}", about)}
            first = max(seen, key=lambda s: (sum(w in s.summary.lower() for w in words), -_ts(s.first_seen)))
            desk = next((DESKS[b] for b in getattr(first, "beats", []) or [] if b in DESKS), "The news desk")
            add(first.first_seen, desk, f"Noticed: {first.summary}")
        add(self.h.get("mclovin", {}).get("created") or getattr(self, "started_at", ""), "Hunch",
            f"An agent had a hunch that {self.h['hypothesis'][0].lower()}{self.h['hypothesis'][1:]}")
        cov = self.coverage.get("report", {}) if isinstance(self.coverage, dict) else {}
        if cov.get("coverage"):
            n = len(cov["coverage"])
            add(getattr(self, "coverage_at", ""), "Research agent",
                f"Checked what other outlets had already reported and found {n} earlier stor{'ies' if n != 1 else 'y'}; "
                f"what they left open: {self.plan.get('gap', '')}")
        for sub in [s for s in self.subs if not s["dropped"] and status(s["findings"]) in ("supported", "disputed")]:
            summary = (sub["reports"][-1] if sub["reports"] else {}).get("summary") or sub["statement"]
            add(sub.get("settled_at") or sub.get("reported_at"), "Research agent", summary, "confirmed")
        if decision.get("pivot"):
            add(decision.get("at"), "Reporter", f"Changed course: the records pointed to a different story than the one "
                                                f"proposed. {decision['pivot']['hypothesis']}")
        else:
            add(decision.get("at"), "Reporter", decision.get("why", ""))
        rounds = reviewed.get("history") or []
        if reviewed.get("outcome") == "approved" and rounds:
            add(rounds[-1].get("at"), "Council", "The council of models and agents signed off on the article.", "approved")
            add(now(), "Published", "", "published")
        steps = sorted(steps, key=lambda x: x["at"])
        return self.narrate(steps)

    def narrate(self, steps: list[dict]) -> list[dict]:
        """Rewrite each step for a general reader. The facts stay; jargon, docket numbers and agent numbering go."""
        todo = [(i, s) for i, s in enumerate(steps) if s["text"] and s["who"] not in ("Council", "Published")]
        if todo:
            listing = "\n".join(f"{i}. [{s['who']}] {s['text']}" for i, s in todo)
            try:
                reply = llm.ask_json(NARRATE, listing, model=config.load().fast_model, max_tokens=3000)
                for item in reply.get("steps", []) if isinstance(reply.get("steps"), list) else []:
                    i, text = item.get("i"), str(item.get("text") or "").strip()
                    if isinstance(i, int) and 0 <= i < len(steps) and "text" in item:
                        if text or steps[i]["who"] == "Research agent":     # only a research step may be dropped
                            steps[i]["text"] = text
            except Exception as e:  # noqa: BLE001 - the plain version is still correct, just wordier
                self.say(f"  timeline narration failed ({type(e).__name__}); keeping the plain steps")
        steps = [s for s in steps if s["text"] or s["who"] in ("Council", "Published")]
        for s in steps:
            s["text"] = whole_sentences(s["text"], 360)
        return steps

    # recording
    def to_dict(self) -> dict:
        return {"hypothesis": self.h, "frame": self.frame, "plan": self.plan, "sizing": self.sizing,
                "coverage": self.coverage.get("report", {}), "already_reported": self.already_reported,
                "memory": self.memory, "signals": [s.to_dict() for s in self.signals], "leads": self.leads,
                "sub_hypotheses": [{**s, "status": status(s["findings"])} for s in self.subs],
                "rounds": self.rounds, "spinoffs": self.spinoffs, "declined": self.declined}

    def record(self, stage: str, **extra) -> None:
        """Write the investigation's current state to the desk. Compact: no page text, capped quotes."""
        if self.desk is None:
            return
        subs = [{k: s[k] for k in ("id", "statement", "covers", "needed_for", "kind", "priority", "budget", "origin",
                                   "assignment", "dispatches", "dropped", "history")}
                | {"status": status(s["findings"]),
                   "searches": sum(t.get("tool", "read") != "read" for t in s["trail"]),
                   "reads": sum(t.get("tool") == "read" for t in s["trail"]),
                   "report": s["reports"][-1] if s["reports"] else {},
                   "trail": [{k: str(t.get(k, ""))[:240] for k in ("tool", "arg", "why", "result")} for t in s["trail"][-20:]],
                   "findings": [{"url": f["url"], "source_type": f["source_type"], "finding": f["finding"],
                                 "quote": f["quote"][:400]} for f in s["findings"][:12]]} for s in self.subs]
        council_rounds = [{"round": c["round"], "triage": c.get("triage"),
                           "judges": [{"judge": j["judge"], "verdict": j["verdict"], "notes": j["notes"][:500],
                                       "problems": [{"sentence": str(p.get("sentence", ""))[:300], "issue": str(p.get("issue", ""))[:300]}
                                                    for p in j["problems"][:6]]} for j in c["review"]["judges"]]}
                          for c in extra.get("council") or []]
        final = extra.get("final") or {}
        rec = {"_id": self.record_id, "hypothesis_id": self.h["id"], "hypothesis": self.h["hypothesis"],
               "accountable_party": self.h["accountable_party"], "signal_ids": self.h["evidence_so_far"]["signal_ids"],
               "mclovin_records": self.h["records"], "status": final.get("status", "reporting"), "stage": stage,
               "story_id": extra.get("story_id"), "run_dir": extra.get("run_dir"),
               "frame": {k: self.frame.get(k) for k in ("restated", "context", "minimum_story", "maximum_story", "since_last_time")},
               "elements": self.frame.get("elements", []), "gap": self.plan.get("gap", ""),
               "prior_coverage": self.plan.get("prior_coverage", []), "alternatives": _strs(self.plan.get("alternatives")),
               "sizing": self.sizing, "sub_hypotheses": subs, "rounds": self.rounds, "verdict": extra.get("verdict") or {},
               "council": council_rounds, "spinoffs": self.spinoffs, "memory": [m["id"] for m in self.memory], "final": final}
        if prior := getattr(self, "_last_record", None):
            for key in ("story_id", "run_dir", "verdict", "council"):
                if not rec[key] and prior.get(key):
                    rec[key] = prior[key]
        self._last_record = rec
        try:
            self.record_id = self.desk.save(rec)
        except Exception as e:  # noqa: BLE001 - recording must never stop the reporting
            if not self._desk_warned:
                self.say(f"desk: could not record ({type(e).__name__}: {e}); carrying on")
                self._desk_warned = True


def _draft(reply: dict, previous: dict | None = None) -> dict:
    """The parts of a model's draft the newsroom keeps. A revision that drops the kicker or dek keeps the old ones."""
    previous = previous or {}
    return {"kicker": str(reply.get("kicker") or previous.get("kicker") or "").strip(),
            "headline": str(reply.get("headline") or "").strip(),
            "dek": str(reply.get("dek") or previous.get("dek") or "").strip(),
            **{k: str(reply.get(k) if reply.get(k) is not None else previous.get(k) or "").strip()
               for k in ("found", "prior", "why_it_matters")},
            "paragraphs": reply.get("paragraphs") or []}


def hard_checks(result: dict) -> list[str]:
    """agents/reporter/rubric.md, the parts code can check."""
    problems = []
    if not result.get("plan", {}).get("evidence_plan"):
        problems.append("no evidence plan before scouts were sent")
    subs = [s for s in result.get("sub_hypotheses", []) if not s.get("dropped")]
    if not subs:
        problems.append("no sub-hypotheses")
    if any(s["dispatches"] == 0 for s in subs):
        problems.append("a sub-hypothesis was never assigned a scout")
    if not any(s["kind"] == "alternative" for s in subs):
        problems.append("no sub-hypothesis tests an innocent explanation")
    if (result.get("sizing") or {}).get("uncovered"):
        problems.append(f"elements no scout covered: {', '.join(result['sizing']['uncovered'])}")
    v = result.get("verdict", {})
    if (result.get("final") or {}).get("status") == "published" and not v.get("new_finding"):
        problems.append("published without a new finding")
    memo = v.get("memo") or {}
    if v.get("verdict") in ("kill", "park") and not all(memo.get(k) for k in ("checked", "found", "would_change_it")):
        problems.append("killed or parked without a full memo")
    return problems


# --- the entry point -----------------------------------------------------------------------------

def already_worked(conn, h: dict, desk=None) -> dict | None:
    """A finished investigation of this hypothesis, from SQLite or from the shared desk (another machine)."""
    for row in conn.execute("select id, status, note, counts from stories where source = 'mclovin' order by id desc"):
        if json.loads(row["counts"]).get("hypothesis_id") == h["id"] and row["status"] in TERMINAL and row["status"] != "failed":
            return {"where": f"story {row['id']}", "story_id": row["id"], "status": row["status"], "note": row["note"]}
    if desk is not None:
        try:
            for r in desk.for_hypothesis(h["id"]):
                if r.get("status") in TERMINAL and r.get("status") != "failed":
                    return {"where": f"desk record {r['_id']}", "story_id": r.get("story_id"), "status": r["status"],
                            "note": (r.get("final") or {}).get("note", "")}
        except Exception:  # noqa: BLE001
            pass
    return None


def investigate(hypothesis: dict | str, *, store=None, desk=None, budget: int = 12, rounds: int = MAX_ROUNDS,
                write: bool = True, judges: tuple[str, ...] = ("skeptic", "virality", "novelty"),
                force: bool = False, say=print) -> dict:
    """Work one hypothesis end to end. Returns the whole trail; also saved under runs/reporter/<ts>/."""
    h = normalize(hypothesis)
    if desk is None:
        try:
            desk = get_desk()
        except Exception as e:  # noqa: BLE001 - no desk means no shared record, not no reporting
            say(f"desk unavailable ({e}); recording to SQLite and the run folder only")
    out = run_dir("reporter")
    with db.session() as conn:
        if not force and (prior := already_worked(conn, h, desk)):
            say(f"already worked ({prior['where']}, {prior['status']}): {prior['note'] or ''}\n"
                "rerun with --force to work it again")
            return {"hypothesis": h, "skipped": True, "story_id": prior["story_id"], "status": prior["status"], "run_dir": str(out)}
        story_id = _open_story(conn, h)
        db.event(conn, "reporter", "assigned", h["hypothesis"], story_id=story_id, detail={"hypothesis_id": h["id"]})

    inv = Investigation(h, budget=budget, rounds=rounds, store=store, desk=desk, say=say)
    inv.record("assigned", story_id=story_id, run_dir=str(out))
    result: dict = {"story_id": story_id, "run_dir": str(out)}
    try:
        inv.start()
        if inv.already_reported:
            result.update(inv.to_dict(), verdict={"verdict": "spike", "story": "", "why": f"already reported: {inv.already_reported}",
                                                  "memo": {"checked": "prior coverage (coverage scout)",
                                                           "found": inv.already_reported,
                                                           "would_change_it": "a record or development the coverage hasn't reported"}},
                          final={"status": "spiked", "note": f"already reported, no new angle: {inv.already_reported}"})
            result["checks"] = hard_checks(result)
            say(f"\nSPIKED before any scouts: {inv.already_reported}")
            _finish(out, story_id, inv, result)
            return result
        inv.run_rounds()
        decision = inv.decide()
        result.update(inv.to_dict(), verdict=decision)
        inv.record("verdict", verdict=decision)
        say(f"\nVERDICT: {decision['verdict'].upper()}{' (' + decision['story'] + ' story)' if decision['story'] else ''}"
            f"  -  {decision['why']}")
        final = {"status": {"kill": "killed", "park": "parked"}.get(decision["verdict"], "drafted"), "note": decision["why"]}
        if decision["verdict"] == "write" and write:
            inv.record("drafting")
            sources = inv.sources()
            d, problems = inv.draft(decision, sources)
            if d is None:
                final = {"status": "failed", "note": "article failed its checks: " + "; ".join(problems[:5])}
            else:
                reviewed = inv.review(decision, d, sources, judges) if judges else {"outcome": "approved", "draft": d, "history": []}
                result["council"] = reviewed["history"]
                result["article"] = {"draft": reviewed["draft"], "sources": sources}
                if reviewed["outcome"] == "approved":
                    path = article.publish(story_id, reviewed["draft"], sources, config.load().published_dir,
                                           site=inv.site_fields(decision, reviewed))
                    final = {"status": "published", "note": decision["why"], "article": str(path),
                             "headline": reviewed["draft"].get("headline", "")}
                    say(f"published {path}")
                elif reviewed["outcome"] == "spiked":
                    final = {"status": "spiked", "note": f"spiked after the council's review: {reviewed['why']}"}
                    say(f"spiked: {reviewed['why']}")
                else:
                    final = {"status": "held", "note": "the skeptic or the virality judge did not approve after revisions"}
                    say("held: the skeptic or the virality judge did not approve after revisions (draft saved in the run folder)")
            result.update(inv.to_dict())      # a council scout may have added sub-hypotheses
        result["final"] = final
        result["checks"] = hard_checks(result)
    except Exception as error:
        result.update(inv.to_dict(), final={"status": "failed", "note": f"{type(error).__name__}: {error}"})
        _finish(out, story_id, inv, result)
        raise
    _finish(out, story_id, inv, result)
    say(f"saved {out / 'result.json'}  ·  full trail: python -m newsroom show {story_id}")
    return result


def _finish(out: Path, story_id: int, inv: Investigation, result: dict) -> None:
    inv.record("done", final=result["final"], verdict=result.get("verdict"), council=result.get("council"))
    result["desk_id"] = inv.record_id
    (out / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    _close_story(story_id, inv, result)


def _open_story(conn, h: dict) -> int:
    ev = h["evidence_so_far"]
    counts = {"hypothesis_id": h["id"], "signal_ids": ev.get("signal_ids", []), "distinct_origins": ev.get("distinct_origins")}
    cursor = conn.execute(
        "insert into stories (created, source, product, company, label, counts, status, note)"
        " values (?, 'mclovin', ?, ?, ?, ?, 'reporting', ?)",
        (db.now(), h["accountable_party"] or "(unknown)", h["accountable_party"], h["hypothesis"], json.dumps(counts), h["why_now"]))
    conn.commit()
    return cursor.lastrowid


def _close_story(story_id: int, inv: Investigation, result: dict) -> None:
    final = result.get("final", {})
    with db.session() as conn:
        conn.execute("update stories set status = ?, note = ?, angle = ?, article = ? where id = ?",
                     (final.get("status", "failed"), final.get("note"), inv.frame.get("restated"), final.get("article"), story_id))
        for n, sub in enumerate(inv.subs, 1):
            cursor = conn.execute("insert into hypotheses (story_id, n, statement) values (?, ?, ?)",
                                  (story_id, n, sub["statement"] + (" (dropped)" if sub["dropped"] else "")))
            conn.executemany("insert into findings (hypothesis_id, url, title, source_type, quote, finding, note)"
                             " values (?, ?, ?, ?, ?, ?, ?)",
                             [(cursor.lastrowid, f["url"], f["title"], f["source_type"], f["quote"], f["finding"], f["note"])
                              for f in sub["findings"]])
        if inv.sizing:
            db.event(conn, "reporter", "plan", f"{inv.sizing['count']} scouts: {inv.sizing['why']}", story_id=story_id,
                     detail={"gap": inv.plan.get("gap"), "sizing": inv.sizing, "evidence_plan": inv.plan.get("evidence_plan")})
        for r in inv.rounds:
            db.event(conn, "reporter", f"round {r['round']}", r.get("stop_reason") or "", story_id=story_id, detail=r)
        if v := result.get("verdict"):
            db.event(conn, "reporter", v["verdict"], v["why"], story_id=story_id, detail=v.get("memo"))
        for c in result.get("council", []):
            for j in c["review"]["judges"]:
                db.event(conn, f"council {j['judge']}", j["verdict"], j["notes"][:500], story_id=story_id, detail=j["problems"])
            if tri := c.get("triage"):
                db.event(conn, "reporter", tri["decision"], tri["why"], story_id=story_id)
        db.event(conn, "reporter", final.get("status", "failed"), final.get("note") or "", story_id=story_id)


def resume_council(path: str | Path, *, judges: tuple[str, ...] = ("skeptic", "virality", "novelty"),
                   desk=None, say=print) -> dict:
    """Pick a held story back up at the council, from its saved result.json: revise the draft against the
    council's last notes, review it again, and publish if approved. For drafts held because a revision
    failed its checks, without redoing the scouting."""
    from .signals import Signal
    path = Path(path)
    result = json.loads(path.read_text(encoding="utf-8"))
    if not result.get("article") or not result.get("council"):
        raise ValueError(f"{path} has no draft and council review to resume")
    inv = Investigation(result["hypothesis"], budget=10, rounds=MAX_ROUNDS, desk=desk if desk is not None else get_desk(), say=say)
    inv.frame, inv.plan, inv.sizing = result.get("frame") or {}, result.get("plan") or {}, result.get("sizing") or {}
    inv.coverage = {"report": result.get("coverage") or {}, "findings": [], "trail": []}
    inv.coverage_urls = {c["url"] for c in inv.coverage["report"].get("coverage", []) if c.get("url")}
    inv.subs = [{k: v for k, v in s.items() if k != "status"} for s in result.get("sub_hypotheses", [])]
    inv.signals = [Signal(**s) for s in result.get("signals", [])]
    inv.leads, inv.rounds = result.get("leads", []), result.get("rounds", [])
    inv.spinoffs, inv.declined, inv.memory = result.get("spinoffs", []), result.get("declined", []), []
    inv.record_id = result.get("desk_id", "")
    decision, sources = result["verdict"], result["article"]["sources"]
    last = result["council"][-1]
    say(f"resuming at the council: {result['article']['draft'].get('headline', '')}")
    plan = inv.triage(result["article"]["draft"], sources, last["review"])
    researched = bool(plan.get("research")) and inv.research(plan["research"], sources)
    revised = inv.revise(decision, result["article"]["draft"], sources, last["review"], plan=plan, researched=researched)
    if revised is None:
        final = {"status": "held", "note": "the revision still failed its checks"}
        result.update(final=final)
    else:
        reviewed = inv.review(decision, revised, sources, judges, previous=last["review"])
        result["council"] = result["council"] + reviewed["history"]
        result["article"] = {"draft": reviewed["draft"], "sources": sources}
        if reviewed["outcome"] == "approved":
            out = article.publish(result["story_id"], reviewed["draft"], sources, config.load().published_dir,
                                  site=inv.site_fields(decision, {**reviewed, "history": result["council"]}))
            final = {"status": "published", "note": decision.get("why", ""), "article": str(out),
                     "headline": reviewed["draft"].get("headline", "")}
            say(f"published {out}")
        elif reviewed["outcome"] == "spiked":
            final = {"status": "spiked", "note": f"spiked after the council's review: {reviewed['why']}"}
        else:
            final = {"status": "held", "note": "the skeptic or the virality judge did not approve after revisions"}
        result.update(inv.to_dict(), final=final)
    result["checks"] = hard_checks(result)
    _finish(path.parent, result["story_id"], inv, result)
    say(f"FINAL: {result['final']['status']} | {result['final'].get('headline') or result['final'].get('note', '')}")
    return result
