"""Scouts: settle one sub-hypothesis for the Reporter, and report back. Instructions: agents/scout/.

The Reporter never searches. It writes an assignment (the statement, the records to try first, the records'
own terms, what would support it and what would contradict it) and a scout does the legwork:

    loop, within its budget:
        choose up to 3 calls, each with a one-line reason (the reasons are the published trail)
            the newsroom's DB:   db_signals (Bossman's signals in Astra), db_desk (quotes past investigations verified)
            official records:    nhtsa_recalls, nhtsa_investigations, fda_recalls, cpsc_recalls, court_dockets,
                                 federal_register
            the web:             news_search (Google News), web_search (a real browser, or Brave), read
        run them in parallel; `read` pulls exact quotes, each checked word for word against the page
    report back: verdict, what was searched (so "nothing found" is scoped to a search), dead ends,
                 new hypotheses worth the reporter's budget, and what to check next

The verdict is held to the evidence in code: a scout can only say "supports" or "contradicts" when a source
that counts as proof (government record, court record, news reporting) says so in a verified quote.

A coverage scout (mode="coverage") looks for prior reporting of the whole story instead, so the reporter can
credit it and aim at the gap.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import config, llm, playbooks, records, web
from .scout import COUNTS_AS_PROOF, read as read_page

MAX_CALLS_PER_STEP = 3
MAX_RESULTS_SHOWN = 6
TRAIL_CHARS = 14_000

TOOLS = {
    "db_signals": ("query", "the newsroom's signals DB (Astra): what Bossman saw online about this, with source URLs. Leads, never proof."),
    "db_desk": ("query", "the reporter's desk: quotes past investigations verified, with their pages. Read the page to use one."),
    "nhtsa_recalls": ("MAKE | MODEL | YEAR", "official NHTSA recalls for one vehicle, e.g. TESLA | MODEL 3 | 2023"),
    "nhtsa_investigations": ("MAKE | MODEL | YEAR", "NHTSA defect investigations covering one vehicle"),
    "fda_recalls": ("term", "FDA recall and enforcement records (devices, drugs, food) whose product or reason mentions the term"),
    "cpsc_recalls": ("product name", "CPSC consumer product recalls; use the product's short name"),
    "court_dockets": ("company | problem words", "federal court dockets (CourtListener) naming the company and the problem"),
    "federal_register": ("exact phrase", "Federal Register rules, proposed rules, notices and orders containing the phrase"),
    "news_search": ("query", "Google News, newest first; add when:365d to reach back a year. Best for what outlets reported"),
    "web_search": ("query", "general web search in a real browser; short queries work best, site:agency.gov helps"),
    "read": ("URL or result ref like R4", "read one page or record and pull the exact quotes that bear on your statement"),
}
COVERAGE_TOOLS = ("db_signals", "news_search", "web_search", "read")

STEP = """
# Your job right now: take the next step

You are a scout with the assignment below. Choose your next 1-{max_calls} calls. Before each, say in one
sentence WHY you are making it: those reasons are published as the trail of the work.

Work the way the playbook above says: official records first, then the agency's own pages, then news, and
social posts only for leads. Follow the reporter's direction. Search for what would CONTRADICT the statement
as hard as for what would support it. Use the records' own words: agencies rarely use the words posts use.
Never repeat a call you already made. Read the most promising results rather than searching forever: a
search result is not evidence until you read the page.

Tools:
{tools}

When a source that counts has settled the statement either way (or nothing left in your budget could), reply
{{"finish": true}}. Otherwise reply with JSON only:
{{"calls": [{{"tool": "...", "arg": "...", "why": "..."}}]}}
"""

REPORT = """
# Your job right now: report back to your reporter

You are finished. Write the report your reporter will read to decide what happens next. Only the verified
quotes listed count as evidence; your trail shows what you checked.

- "verdict": "supports", "contradicts" or "unclear" for the statement, judged only by sources that count
  (government records, court records, news outlets' own reporting). Complaints, posts and company denials
  never settle it.
- "summary": two or three plain sentences: what you found and how sure you are.
- "searched": what you checked, as "system: terms", so "nothing found" is a statement about a search.
- "not_found": records you looked for and did not find, scoped the same way.
- "dead_ends": calls that led nowhere, and why (blocked, wrong terms, off topic).
- "proposals": new hypotheses the evidence points to, each with the specific records that would settle it.
  Don't chase them yourself; the reporter decides.
- "next_check": if unsettled, the one specific thing most likely to settle it.

Reply with JSON only:
{"verdict": "...", "confidence": "high|medium|low", "summary": "...", "searched": ["..."], "not_found": ["..."],
 "dead_ends": ["..."], "proposals": [{"hypothesis": "...", "records": ["..."], "why": "..."}], "next_check": "..."}
"""

COVERAGE_REPORT = """
# Your job right now: report the prior coverage you found

You searched for earlier reporting of this story. List only coverage you actually saw in your results, by
URL, and say what each piece established. Coverage of the company in general or of a different problem
doesn't count; complaint sites, forums and social posts aren't coverage.

Then, for the story's elements listed in your assignment, say which ones published reporting has ALREADY
established ("reported_elements"), and the overall status:
  "new"               no outlet has published the core finding
  "partly_reported"   some elements are out there; others nobody has established
  "already_reported"  the whole story is already published
A search that found nothing is not proof nothing exists; say what you searched.

Reply with JSON only:
{"coverage": [{"url": "...", "outlet": "...", "date": "...", "what_it_established": "..."}],
 "reported_elements": ["E1"], "status": "new|partly_reported|already_reported",
 "gap": "what none of it established", "searched": ["system: terms"], "summary": "..."}
"""


def _pipe(arg: str, n: int) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s*\|\s*", arg)]
    return (parts + [""] * n)[:n]


class Scout:
    def __init__(self, task: dict, *, budget: int, context: str = "", leads=(), avoid=(), say=None) -> None:
        self.task, self.budget, self.context = task, max(1, int(budget)), context
        self.mode = task.get("mode", "verify")
        self.statement = task["statement"]
        self.say = say or (lambda *_: None)
        self.used = 0
        self.trail: list[dict] = []
        self.findings: list[dict] = []
        self.hits: dict[str, dict] = {}          # ref -> {"url", "title", "snippet", "source_type"?}
        self.by_url: dict[str, str] = {}
        self.known_type: dict[str, str] = {}
        self.read_urls: set[str] = set()
        self.done_calls: set[tuple[str, str]] = set()
        self.avoid = list(avoid)
        for lead in leads:
            self._hit(lead.get("url", ""), f"[lead from the newsroom DB] {lead.get('title', '')}", lead.get("description", ""))
        self.system = playbooks.load("scout", examples=False)
        self.fast = config.load().fast_model

    # --- results --------------------------------------------------------------------------------
    def _hit(self, url: str, title: str, snippet: str = "", source_type: str | None = None) -> str | None:
        if not str(url).startswith("http"):
            return None
        if url in self.by_url:
            return self.by_url[url]
        ref = f"R{len(self.hits) + 1}"
        self.hits[ref] = {"url": url, "title": title, "snippet": snippet}
        self.by_url[url] = ref
        if source_type:
            self.known_type[url] = source_type
        return ref

    def _official(self, rows: list[dict]) -> list[str]:
        """Official records become pages the scout can read and quote, with their source type fixed."""
        refs = []
        for r in rows:
            web.remember(r["url"], r["text"])
            if ref := self._hit(r["url"], f"[official record] {r['title']}", r["text"][:200], r["source_type"]):
                refs.append(ref)
        return refs

    # --- tools ----------------------------------------------------------------------------------
    def call(self, tool: str, arg: str) -> str:
        """Run one call. Returns a short text result for the trail."""
        if tool == "read":
            return self._read(arg)
        if tool == "db_signals":
            from .signals import get_store
            refs = []
            for s, score in get_store().similar(arg, k=6):
                for src in s.sources[:2]:
                    if ref := self._hit(src.get("url", ""), f"[signal] {s.summary[:140]}", s.checkable_claim[:160]):
                        refs.append(ref)
            return self._listing(refs, "signals")
        if tool == "db_desk":
            from .desk import get_desk
            refs = []
            for rec, score in get_desk().similar(arg, k=3):
                for sub in rec.get("sub_hypotheses", []):
                    for f in sub.get("findings", [])[:3]:
                        if f.get("source_type") in COUNTS_AS_PROOF and (ref := self._hit(
                                f["url"], f"[verified before: {f['finding']}] {sub.get('statement', '')[:100]}", f["quote"][:200])):
                            refs.append(ref)
            return self._listing(refs, "desk quotes")
        if tool == "nhtsa_recalls":
            make, model, year = _pipe(arg, 3)
            return self._listing(self._official(records.nhtsa_recalls(make, model, year)), "recalls")
        if tool == "nhtsa_investigations":
            from . import db
            make, model, year = _pipe(arg, 3)
            with db.session() as conn:
                rows = records.nhtsa_investigations(conn, make, model, year)
            note = "" if rows else " (the local investigations file may not be loaded: `python -m newsroom ingest-investigations`)"
            return self._listing(self._official(rows), "investigations") + note
        if tool == "fda_recalls":
            term = arg.replace('"', "").strip()
            rows = []
            for endpoint in ("device/recall", "drug/enforcement", "food/enforcement"):
                try:
                    rows += records.fda_records(endpoint, f'reason_for_recall:"{term}" product_description:"{term}"')
                except Exception:  # noqa: BLE001 - openFDA 404s and odd queries are "nothing here"
                    continue
            return self._listing(self._official(rows[:8]), "FDA records")
        if tool == "cpsc_recalls":
            return self._listing(self._official(records.cpsc_recalls(arg)), "CPSC recalls")
        if tool == "court_dockets":
            company, problem = _pipe(arg, 2)
            return self._listing(self._official(records.lawsuits(company, problem)), "dockets")
        if tool == "federal_register":
            from .gather import federal_register_search
            refs = [self._hit(c["url"], f"[Federal Register] {c['title']}", c["snippet"], "government_record")
                    for c in federal_register_search(arg, limit=8)]
            return self._listing([r for r in refs if r], "Federal Register documents")
        if tool == "news_search":
            from .gather import news_search
            refs = [self._hit(c["url"], f"{c['title']} ({c['snippet']})", c["spike"].get("value", ""))
                    for c in news_search(arg if "when:" in arg else f"{arg} when:365d", limit=8)]
            return self._listing([r for r in refs if r], "news results")
        if tool == "web_search":
            refs = [self._hit(r["url"], r["title"], r.get("description", "")) for r in web.search(arg, count=8)]
            return self._listing([r for r in refs if r], "results")
        return f"unknown tool {tool!r}"

    def _listing(self, refs: list[str], what: str) -> str:
        refs = list(dict.fromkeys(r for r in refs if r))
        if not refs:
            return f"0 {what}"
        shown = [f"{r} {self.hits[r]['title'][:110]} ({web.host(self.hits[r]['url'])})"
                 + (f": {self.hits[r]['snippet'][:120]}" if self.hits[r]["snippet"] else "") for r in refs[:MAX_RESULTS_SHOWN]]
        more = f" (+{len(refs) - MAX_RESULTS_SHOWN} more)" if len(refs) > MAX_RESULTS_SHOWN else ""
        return f"{len(refs)} {what}{more}:\n      " + "\n      ".join(shown)

    def _read(self, arg: str) -> str:
        ref = arg.strip().strip("[]")
        hit = self.hits.get(ref) or (self.hits.get(self.by_url[arg.strip()]) if arg.strip() in self.by_url else None)
        url = hit["url"] if hit else arg.strip()
        if not url.startswith("http"):
            return f"can't read {arg!r}: give a URL or a result ref"
        if url in self.read_urls:
            return "already read"
        self.read_urls.add(url)
        text = web.fetch_text(url)
        if not text:
            return "couldn't fetch it (blocked, gone, or not text); try another source"
        title = (hit or {}).get("title", "")
        for prefix in ("[official record] ", "[Federal Register] ", "[lead from the newsroom DB] ", "[signal] "):
            title = title.removeprefix(prefix)
        found = read_page(self.statement, url, title, text, source_type=self.known_type.get(url), context=self.context)
        self.findings.extend(found)
        if not found:
            return "nothing on this page bears on the statement"
        return f"{len(found)} verified quote(s):\n      " + "\n      ".join(
            f"[{f['finding']}] {f['source_type']}: \"{f['quote'][:220]}\"" for f in found)

    # --- the loop -------------------------------------------------------------------------------
    def _tools_text(self) -> str:
        names = COVERAGE_TOOLS if self.mode == "coverage" else TOOLS
        return "\n".join(f"- {n}({TOOLS[n][0]}): {TOOLS[n][1]}" for n in names)

    def _assignment(self) -> str:
        if self.mode == "coverage":
            elements = "\n".join(f"  {e['id']}. {e['claim']}" for e in self.task.get("elements", []))
            return (f"Find prior reporting of this story: {self.statement}\nContext: {self.context}\n"
                    f"Its elements:\n{elements or '  (none listed)'}\n"
                    "Search for the specific finding, not just the topic: the record, the number, the vehicle or "
                    "product and the agency together. A topic search finds a thousand articles; a finding search "
                    "tells you whether this finding is already published.\n"
                    f"Searches the reporter suggests: {'; '.join(self.task.get('searches', [])) or '(none)'}")
        a = self.task.get("assignment") or {}
        lines = [f"Your statement ({self.task.get('id', 'H?')}): {self.statement}", f"Story context: {self.context}"]
        if a.get("next_check"):
            lines.append(f"The reporter redirected you. Check this next: {a['next_check']}")
        if a.get("records_first"):
            lines.append("Records to try first, in order:\n" + "\n".join(f"  {n}. {r}" for n, r in enumerate(a["records_first"], 1)))
        if a.get("search_terms"):
            lines.append(f"The records' own terms: {', '.join(a['search_terms'])}")
        if a.get("supports_if"):
            lines.append(f"It is SUPPORTED if: {a['supports_if']}")
        if a.get("contradicts_if"):
            lines.append(f"It is CONTRADICTED if: {a['contradicts_if']}")
        if self.avoid:
            lines.append("Already searched on an earlier assignment (don't repeat): " + "; ".join(self.avoid[:12]))
        return "\n".join(lines)

    def _state(self) -> str:
        steps = []
        for n, t in enumerate(self.trail, 1):
            steps.append(f"[{n}] {t['tool']}({t['arg']!r}) - why: {t['why']}\n    -> {t['result']}")
        text = "\n".join(steps)
        if len(text) > TRAIL_CHARS:           # keep the newest steps in full; older ones as one line each
            head = [f"[{n}] {t['tool']}({t['arg']!r}) -> {t['result'].splitlines()[0][:100]}" for n, t in enumerate(self.trail, 1)]
            text = "\n".join(head[:-4]) + "\n" + "\n".join(steps[-4:])
        unread = [f"{r} {h['title'][:90]} ({web.host(h['url'])})" for r, h in self.hits.items() if h["url"] not in self.read_urls][:15]
        settled = any(f["source_type"] in COUNTS_AS_PROOF and f["finding"] in ("supports", "contradicts") for f in self.findings)
        return (f"{self._assignment()}\n\nBudget: {self.used} of {self.budget} calls used.\n"
                + ("A source that counts has spoken on the statement. Finish, unless one more call could find a contradiction.\n" if settled else "")
                + f"\nYour trail so far:\n{text or '(nothing yet)'}\n\nResults not yet read:\n" + ("\n".join(unread) or "(none)"))

    def run(self) -> dict:
        system = self.system + "\n" + STEP.format(max_calls=MAX_CALLS_PER_STEP, tools=self._tools_text())
        allowed = set(COVERAGE_TOOLS if self.mode == "coverage" else TOOLS)
        idle = 0
        while self.used < self.budget and idle < 2:
            reply = llm.ask_json(system, self._state(), model=self.fast, max_tokens=1500)
            if reply.get("finish"):
                break
            calls = []
            for c in reply.get("calls", []) if isinstance(reply.get("calls"), list) else []:
                tool, arg = str(c.get("tool", "")).strip(), str(c.get("arg", "")).strip()
                if tool in allowed and arg and (tool, arg) not in self.done_calls and (tool, arg) not in [(x[0], x[1]) for x in calls]:
                    calls.append((tool, arg, str(c.get("why", "")).strip()))
            calls = calls[:min(MAX_CALLS_PER_STEP, self.budget - self.used)]
            if not calls:
                idle += 1             # a reply with nothing new to do: give it one more chance, then stop
                continue
            idle = 0
            with ThreadPoolExecutor(len(calls)) as pool:
                results = list(pool.map(lambda c: self._safe(c[0], c[1]), calls))
            for (tool, arg, why), result in zip(calls, results):
                self.used += 1
                self.done_calls.add((tool, arg))
                self.trail.append({"tool": tool, "arg": arg, "why": why, "result": result})
                self.say(f"    {self.task.get('id', 'scout')} {tool}({arg[:60]!r}): {result.splitlines()[0][:90]}")
        return self.report()

    def _safe(self, tool: str, arg: str) -> str:
        try:
            return self.call(tool, arg)
        except web.SearchError:
            raise
        except Exception as e:  # noqa: BLE001 - a failed call is a result: the scout goes elsewhere
            return f"failed: {type(e).__name__}: {str(e)[:160]}"

    def report(self) -> dict:
        evidence = "\n".join(f"- [{f['finding']}] {f['source_type']} {f['url']}\n  \"{f['quote'][:300]}\"" for f in self.findings) or "(no verified quotes)"
        state = f"{self._assignment()}\n\nYour trail:\n" + "\n".join(
            f"[{n}] {t['tool']}({t['arg']!r}) -> {t['result'].splitlines()[0][:140]}" for n, t in enumerate(self.trail, 1)) \
            + f"\n\nVerified quotes:\n{evidence}"
        if self.mode == "coverage":
            reply = llm.ask_json(self.system + "\n" + COVERAGE_REPORT, state, model=self.fast, max_tokens=2000)
            seen = {h["url"] for h in self.hits.values()}
            coverage = [c for c in reply.get("coverage", []) if isinstance(c, dict) and c.get("url") in seen]
            return {"findings": self.findings, "trail": self.trail, "used": self.used,
                    "report": {"coverage": coverage, "gap": str(reply.get("gap", "")), "searched": _strs(reply.get("searched")),
                               "reported_elements": _strs(reply.get("reported_elements")),
                               "status": reply.get("status") if reply.get("status") in ("new", "partly_reported", "already_reported") else "unknown",
                               "summary": str(reply.get("summary", "")),
                               "dropped_unseen": len(reply.get("coverage", []) or []) - len(coverage)}}
        reply = llm.ask_json(self.system + "\n" + REPORT, state, model=self.fast, max_tokens=2000)
        verdict = str(reply.get("verdict", "unclear"))
        held = held_to_evidence(verdict, self.findings)
        report = {"verdict": held, "confidence": str(reply.get("confidence", "low")), "summary": str(reply.get("summary", "")),
                  "searched": _strs(reply.get("searched")), "not_found": _strs(reply.get("not_found")),
                  "dead_ends": _strs(reply.get("dead_ends")), "next_check": str(reply.get("next_check", "")),
                  "proposals": [p for p in reply.get("proposals", []) if isinstance(p, dict) and str(p.get("hypothesis", "")).strip()]}
        if held != verdict:
            report["held_back"] = f"the scout said {verdict!r}, but no verified quote from a source that counts says so"
        return {"findings": self.findings, "trail": self.trail, "used": self.used, "report": report}


def held_to_evidence(verdict: str, findings: list[dict]) -> str:
    """A scout's verdict, only as strong as its verified quotes from sources that count."""
    if verdict not in ("supports", "contradicts"):
        return "unclear"
    backed = any(f["finding"] == verdict and f["source_type"] in COUNTS_AS_PROOF for f in findings)
    return verdict if backed else "unclear"


def _strs(value) -> list[str]:
    return [str(x).strip() for x in value if str(x).strip()] if isinstance(value, list) else []


def run(task: dict, *, budget: int, context: str = "", leads=(), avoid=(), say=None) -> dict:
    """Send one scout. task: {"id", "statement", "assignment": {...}} or {"mode": "coverage", "statement", "searches"}.
    Returns {"findings", "trail", "used", "report"}."""
    return Scout(task, budget=budget, context=context, leads=leads, avoid=avoid, say=say).run()


def summary_line(result: dict) -> str:
    r = result.get("report", {})
    return json.dumps({k: r.get(k) for k in ("verdict", "confidence", "summary")}, ensure_ascii=False)
