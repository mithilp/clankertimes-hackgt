"""The council: skeptic, virality and novelty judge a finished draft. Instructions: agents/council/.

    review(draft)  - mechanical checks first (newsroom/article.py), then each judge independently.
    seeded()       - plant known errors in a good draft and measure what gets caught, and where.

A draft is {"article": {"headline", "paragraphs": [[{"text", "cite"}]]}, "sources": {id: {"title", "url", "text"}}},
the same shape article.check() takes.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import article, config, llm, playbooks, web

JUDGES = ("skeptic", "virality", "novelty")
DEFAULT_DRAFT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "council_draft.json"

OUTPUT = """
# Your output

Judge the draft below as the playbook above describes. Reply with JSON only:
{
  "verdict": "approve" | "revise",
  "problems": [{"sentence": "the exact sentence, or 'headline'", "issue": "what is wrong", "fix": "what would fix it"}],
  "notes": "anything else the reporter should know, briefly"
}
"approve" means you have no problems that should stop publication. Every problem must quote a sentence.

House rule: this newsroom is automated and cannot call or email anyone, so it never seeks comment. Don't ask
for a response to be sought. Do require the accountable party's own public words where the sources have them,
and a plain line saying the newsroom did not contact them.
"""


def load_draft(path: Path | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_DRAFT).read_text(encoding="utf-8"))


def render(draft: dict) -> str:
    a, sources = draft["article"], draft["sources"]
    body = "\n".join(
        f"- {s['text']}  [cites: {', '.join(s['cite'])}]" for p in a["paragraphs"] for s in p
    )
    listing = "\n\n".join(f"[{sid}] {s.get('title', '')} ({s.get('url', '')})\n{s.get('text', '')[:3000]}"
                          for sid, s in sources.items())
    return f"HEADLINE: {a['headline']}\n\nSENTENCES:\n{body}\n\nSOURCES:\n\n{listing}"


KEPT = """

THIS IS A REVISION, AND YOU APPROVED THE PREVIOUS DRAFT. Approve again unless the revision introduced a
problem that should stop publication (a new unsupported claim, a new misleading frame). Don't reopen points
you already accepted, and don't raise new polish: put that in "notes"."""

REVISION = """

THIS IS A REVISION. Your problems with the previous draft were:
{earlier}

First check whether each was fixed. Then raise a new problem only if it should stop publication: a claim
the sources don't support, an unfair or misleading framing, a buried or wrong lead, a missing response or
caveat a reader needs. Wording and polish you would still change go in "notes" with an "approve"."""


def judge(name: str, draft: dict, *, web_search: bool = True, earlier: dict | None = None) -> dict:
    extra = ""
    if earlier and earlier.get("verdict") == "approve":
        extra += KEPT
    elif earlier:
        listed = "\n".join(f"- \"{p.get('sentence', '')}\": {p.get('issue', '')}" for p in earlier.get("problems", [])) or "(none)"
        extra += REVISION.format(earlier=listed)
    if name == "novelty" and web_search:
        try:
            hits = web.search(draft["article"]["headline"], count=8)
            extra += "\n\nPRIOR COVERAGE SEARCH (top results for the headline):\n" + "\n".join(
                f"- {h['title']} ({h['url']}): {h.get('description', '')[:200]}" for h in hits)
        except Exception as e:  # noqa: BLE001 - the judge can still rule on the draft alone
            extra += f"\n\n(prior coverage search failed: {e}; judge from the draft alone and say so)"
    system = playbooks.load(f"council/{name}") + "\n" + OUTPUT
    reply = llm.ask_json(system, render(draft) + extra, model=config.load().smart_model, max_tokens=4000)
    verdict = str(reply.get("verdict", "")).lower()
    return {"judge": name, "verdict": verdict if verdict in ("approve", "revise") else "revise",
            "problems": [p for p in reply.get("problems", []) if isinstance(p, dict)],
            "notes": str(reply.get("notes", ""))}


def review(draft: dict, judges=JUDGES, *, web_search: bool = True, previous: dict | None = None) -> dict:
    """Mechanical checks first; judges only see drafts that pass them. The skeptic can block alone.
    previous: the last round's review, so each judge checks its own earlier problems instead of starting over."""
    mechanical = article.check(draft["article"], draft["sources"])
    if mechanical:
        return {"verdict": "revise", "stage": "mechanical", "mechanical": mechanical, "judges": []}
    before = {j["judge"]: j for j in (previous or {}).get("judges", [])}
    results = [judge(j, draft, web_search=web_search, earlier=before.get(j)) for j in judges]
    verdict = "approve" if all(r["verdict"] == "approve" for r in results) else "revise"
    return {"verdict": verdict, "stage": "council", "mechanical": [], "judges": results}


# --- seeded errors -----------------------------------------------------------------------------
# Each mutation plants one known mistake (agents/council/skeptic/examples/bad/seeded-errors.md) and
# returns (draft, the text of the planted sentence or "headline").


def _append(draft: dict, text: str, cite: list[str]) -> tuple[dict, str]:
    d = copy.deepcopy(draft)
    d["article"]["paragraphs"][-1].append({"text": text, "cite": cite})
    return d, text


def _headline(draft: dict, text: str) -> tuple[dict, str]:
    d = copy.deepcopy(draft)
    d["article"]["headline"] = text
    return d, "headline"


MUTATIONS = {
    "office_as_defendant": lambda d: _append(d, "The agency was charged with ignoring the steering complaints.", ["F2"]),
    "fabricated_quote": lambda d: _append(d, 'One owner wrote that the car "shut itself off at seventy miles an hour with no warning at all."', ["D"]),
    "stale_as_current": lambda d: _append(d, "NHTSA is currently reviewing 115 reports of lost steering control.", ["F2"]),
    "crime_word_on_complaints": lambda d: _append(d, "Owners describe a pattern that amounts to fraud by the manufacturer.", ["D"]),
    "leaked_template": lambda d: _append(d, "The [[MODEL_YEAR]] vehicles remain under review.", ["F2"]),
    "complaint_as_fact": lambda d: _append(d, "The 2023 Model 3's steering fails without warning.", ["D"]),
    "contradicts_record": lambda d: _append(d, "Tesla acknowledged that the power steering defect caused crashes.", ["F1"]),
    "intent_verb": lambda d: _append(d, "Tesla ignored the steering complaints for more than two years.", ["F2"]),
    "absence_as_fact": lambda d: _append(d, "No records exist showing Tesla fixed the problem.", ["F1"]),
    "headline_overreach": lambda d: _headline(d, "Tesla's Deadly Steering Scandal"),
}


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _points_at(problem: dict, planted: str) -> bool:
    said = str(problem.get("sentence", ""))
    if planted == "headline":
        return "headline" in said.lower()
    a, b = _words(said), _words(planted)
    return bool(a and b) and len(a & b) / len(b) >= 0.6


def seeded(draft: dict | None = None, judges=("skeptic",), mutations: list[str] | None = None, say=print) -> dict:
    """Plant each error, then record whether it was caught mechanically, by a judge, or not at all."""
    base = draft or load_draft()
    if problems := article.check(base["article"], base["sources"]):
        raise ValueError(f"the base draft must pass the mechanical checks first: {problems}")
    rows = []
    for name in mutations or list(MUTATIONS):
        planted_draft, planted = MUTATIONS[name](base)
        mechanical = article.check(planted_draft["article"], planted_draft["sources"])
        if mechanical:
            rows.append({"error": name, "caught_by": "mechanical", "detail": mechanical[0]})
            say(f"  {name:<26} caught by mechanical checks")
            continue
        caught_by, detail = None, ""
        for j in judges:
            result = judge(j, planted_draft, web_search=False)
            hit = next((p for p in result["problems"] if _points_at(p, planted)), None)
            if result["verdict"] == "revise" and hit:
                caught_by, detail = j, hit.get("issue", "")
                break
        rows.append({"error": name, "caught_by": caught_by or "MISSED", "detail": detail})
        say(f"  {name:<26} {'caught by ' + caught_by if caught_by else 'MISSED'}")
    caught = sum(r["caught_by"] != "MISSED" for r in rows)
    return {"caught": caught, "total": len(rows), "rate": caught / len(rows) if rows else 0.0, "rows": rows}
