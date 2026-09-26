"""Step 6: a scout researches one hypothesis and reports what the sources say.

In its first round, the scout also considers the official records looked up for the story (recalls,
investigations, lawsuits). Each round, it plans a few Brave searches, picks the most promising results,
reads them, and pulls out exact quotes. Quotes are checked in code against the page text; a quote that
isn't on the page is dropped. A scout stops when a source that counts as proof settles the hypothesis,
or when its budget runs out.
"""

from . import llm, web
from .text import contains_quote

ROUNDS = 2
QUERIES_PER_ROUND = 3
PAGES_PER_ROUND = 5
MAX_PAGE_CHARS = 30_000

SOURCE_TYPES = {"government_record", "court_record", "news_report", "company_statement", "complaint", "social", "other"}
COUNTS_AS_PROOF = {"government_record", "court_record", "news_report"}
FINDINGS = {"supports", "contradicts", "unclear"}
SOCIAL_HOSTS = ("reddit.com", "x.com", "twitter.com", "facebook.com", "tiktok.com", "instagram.com",
                "youtube.com", "quora.com", "threads.net", "bsky.app")

PLAN_SYSTEM = """You plan web searches to test one hypothesis about a product safety problem.

Write 3 short search-engine queries. Include at least one that would find evidence for the hypothesis and one that would find evidence against it (for example, a recall or fix that would make it false). Prefer queries that surface government records, court records and news reporting.

Reply in JSON: {"queries": ["...", "...", "..."]}"""

CHOOSE_SYSTEM = """Pick the search results most likely to settle a hypothesis, best first. Prefer government records, court records and news reporting over blogs, forums and complaint sites. Pick at most 5.

Reply in JSON: {"urls": ["...", "..."]}"""

READ_SYSTEM = """You check one hypothesis about a specific story against one web page.

1. Decide whether the page says anything about the hypothesis for this story's product, company or employer, and for the same specific problem. General background, definitions, statistics about other products, pages about other companies, and pages about a different problem with the same product (for example a power-steering hardware recall when the story is about driver-assistance software) are not relevant. If the page isn't relevant, reply {"relevant": false}.
2. Classify the page as one source_type:
   government_record: a government agency's record, data or statement (recalls, investigations, safety notices)
   court_record: court filings, rulings or dockets
   news_report: a news outlet's own reporting
   company_statement: the company's own site, press release or statement
   complaint: consumer complaints or reviews, including complaint databases and complaint sites
   social: social media or forums
   other: anything else
3. Copy up to 3 quotes that bear on the hypothesis, each exactly as written on the page (one or two sentences, word for word). For each, say whether it supports, contradicts, or is unclear about the hypothesis. A quote supports or contradicts only if it is about this story's product, company or employer; otherwise leave it out.
   Use "contradicts" only when the page shows the substance of the hypothesis is false. A wrong detail, such as a date that is off by a few days, is not a contradiction: mark it "supports" or "unclear" and explain in the note.
   A lawsuit or complaint only shows that someone alleged something: it supports a hypothesis that the allegation was made, not that the allegation is true.

Before anything else, write "page_problem": the specific problem the page is about, in a few words (for example "power steering assist circuit board failure" or "FSD software running red lights"), and "same_problem": whether that is the same problem as the hypothesis. If it isn't, the page is not relevant.

Reply in JSON: {"page_problem": "...", "same_problem": true, "relevant": true, "source_type": "...", "quotes": [{"quote": "...", "finding": "supports", "note": "..."}]}"""


def research(hypothesis: str, context: str, budget: int, official: list[dict] = (), *,
             guidance: str = "", leads: list[dict] = (), avoid: list[str] = (), trail: list | None = None) -> list[dict]:
    """Research one hypothesis. Returns findings: url, title, source_type, quote, finding, note.

    official: records already looked up for the story ({"url", "title", "text", "source_type"}); their
    text must already be in the page cache (web.remember), and their source type is taken as given.
    guidance: the reporter's direction for this scout (where to look, what would settle it).
    leads: pages worth a look from the newsroom's own DB ({"url", "title", "description"}), e.g. the
        sources behind a Bossman signal. Read like search results; their source type is judged normally.
    avoid: queries already run for this hypothesis on an earlier assignment, so they aren't repeated.
    trail: if given, every search and page read is appended to it, so the reporter can see what was checked.
    """
    used = 0
    tried: list[str] = list(avoid)
    seen: set[str] = set()
    findings: list[dict] = []
    known_type = {r["url"]: r["source_type"] for r in official}
    for round_no in range(ROUNDS):
        results = []
        if round_no == 0:
            for record in official:
                seen.add(record["url"])
                results.append({"url": record["url"], "title": f"[official record] {record['title']}",
                                "description": record["text"][:300]})
            for lead in leads:
                if lead.get("url") and lead["url"] not in seen:
                    seen.add(lead["url"])
                    results.append({"url": lead["url"], "title": f"[newsroom lead] {lead.get('title', '')}",
                                    "description": str(lead.get("description", ""))[:300]})
        for query in plan(hypothesis, context, tried, findings, guidance)[:QUERIES_PER_ROUND]:
            if used >= budget:
                break
            tried.append(query)
            used += 1
            hits = web.search(query)
            if trail is not None:
                trail.append({"search": query, "results": len(hits)})
            for result in hits:
                if result["url"] not in seen:
                    seen.add(result["url"])
                    results.append(result)
        for url in choose(hypothesis, results)[:PAGES_PER_ROUND]:
            if used >= budget:
                break
            used += 1
            text = web.fetch_text(url)
            found = []
            if text:
                title = next((r["title"] for r in results if r["url"] == url), "")
                title = title.removeprefix("[official record] ").removeprefix("[newsroom lead] ")
                found = read(hypothesis, url, title, text, source_type=known_type.get(url), context=context)
                findings.extend(found)
            if trail is not None:
                trail.append({"read": url, "quotes": len(found), "fetched": bool(text)})
        if settled(findings) or used >= budget:
            break
    return findings


def settled(findings: list[dict]) -> bool:
    return any(f["source_type"] in COUNTS_AS_PROOF and f["finding"] in ("supports", "contradicts") for f in findings)


def plan(hypothesis: str, context: str, tried: list[str], findings: list[dict], guidance: str = "") -> list[str]:
    prompt = f"Story context: {context}\n\nHypothesis: {hypothesis}"
    if guidance:
        prompt += f"\n\nYour reporter's direction (follow it): {guidance}"
    if tried:
        prompt += "\n\nAlready searched (write different queries):\n" + "\n".join(f"- {q}" for q in tried)
    if findings:
        prompt += "\n\nFound so far:\n" + "\n".join(f"- {f['source_type']}, {f['finding']}: {f['quote'][:200]}" for f in findings)
    reply = llm.ask_json(PLAN_SYSTEM, prompt, max_tokens=500)
    return [q.strip() for q in reply.get("queries", []) if isinstance(q, str) and q.strip()]


def choose(hypothesis: str, results: list[dict]) -> list[str]:
    if not results:
        return []
    listing = "\n\n".join(f"{r['url']}\n{r['title']}\n{r['description']}" for r in results)
    reply = llm.ask_json(CHOOSE_SYSTEM, f"Hypothesis: {hypothesis}\n\nSearch results:\n\n{listing}", max_tokens=800)
    known = {r["url"] for r in results}
    return [u for u in reply.get("urls", []) if u in known]


def read(hypothesis: str, url: str, title: str, text: str, source_type: str | None = None, context: str = "") -> list[dict]:
    """Quotes from one page that bear on the hypothesis. source_type, if given, overrides the model's."""
    # Thinking mode: without it the model confuses different problems with the same product (tested on a
    # power-steering recall vs. a driver-assistance story); it costs ~800 more output tokens per page.
    reply = llm.ask_json(READ_SYSTEM, f"Story: {context}\nHypothesis: {hypothesis}\n\nPage: {title}\n{url}\n\n"
                                      f"Page text:\n{text[:MAX_PAGE_CHARS]}", max_tokens=8000, thinking=True)
    if not reply.get("relevant") or reply.get("same_problem") is False:
        return []
    source_type = source_type or source_type_of(url, reply.get("source_type"))
    findings = []
    for item in reply.get("quotes", [])[:3]:
        quote = str(item.get("quote", "")).strip()
        finding = item.get("finding")
        if finding in FINDINGS and contains_quote(text, quote):
            findings.append({"url": url, "title": title, "source_type": source_type, "quote": quote,
                             "finding": finding, "note": str(item.get("note") or "")})
    return findings


def source_type_of(url: str, claimed: str | None) -> str:
    """The model's classification, except that social media is always social media."""
    host = web.host(url)
    if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
        return "social"
    return claimed if claimed in SOURCE_TYPES else "other"
