"""Steps 4-8: the reporter picks a claim, writes hypotheses, sends scouts, decides, and writes the article."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

from . import article, config, count, db, llm, records, scout, web

PICK_SYSTEM = """You are the editor of an investigative newsroom that finds stories in public complaint and injury records: vehicle complaints, medical device and drug reports, food and supplement reports, workplace injury reports, and social media posts.

Below are the problems reported most often, each about one product or one employer. Be selective: most are not worth an investigation. One is worth it only when all of these hold:
- people are hurt or put at risk (not just inconvenienced)
- a company or agency is responsible for fixing it
- the pattern is surprising. It is not an outcome you'd expect for the people involved (deaths or a worsening disease among patients on a cancer drug are expected), not a generic problem that affects every product of its kind ("drug ineffective", "off label use"), and not a well-known problem that has been widely reported for years.
Mark at most 5 groups as worth investigating.

Rank the groups worth investigating, most promising first, then list the others. Give every group a one-line reason. For each group worth investigating, also write "search": the short news search a reporter would type to check whether it has already been covered, in plain words (for example "UPS drivers heat illness").

Reply in JSON: {"ranked": [{"group": 3, "worth": true, "reason": "...", "search": "..."}, {"group": 1, "worth": false, "reason": "..."}]}"""

REPORTED_SYSTEM = """You check whether a product problem has already been reported.

Given a product, a problem and web search results, decide whether news outlets or regulators have already publicly reported this specific problem with this specific product: a news story about it, a recall for it, or a regulator's investigation of it. Coverage of the company in general, or of a different problem, doesn't count. Complaint sites (CarComplaints, CarProblemZoo, NHTSA complaint listings), forums, Reddit and social media don't count either: they are just more complaints. If the results don't clearly show it has been reported, answer false.

Reply in JSON: {"already_reported": true, "reason": "...", "url": "the result that shows it, or null"}"""

HYPOTHESES_SYSTEM = """You are an investigative reporter. The data below shows many people reporting the same problem with one product, or at one employer. The counts are already known from the data.

Write:
- "angle": the story this could be, in one sentence, stated as a possibility, not a conclusion
- "hypotheses": 3 to 5 plain statements that would all have to be true for the story to hold. Each names the product or employer (and the company, if known) so it can be checked on its own, is about the exact problem in the data rather than a broader category, and states exactly one thing: never join two claims with "or" or "and". Each must be checkable on the public web: recalls, regulator investigations or warnings, government records, court records, or news reporting. Don't restate the complaint counts, and don't assume anyone is guilty.

Reply in JSON: {"angle": "...", "hypotheses": ["...", "..."]}"""

WRITE_SYSTEM = """You write a short investigative news article (350-600 words) using only the sources given.

Sources:
- "D" is the complaint data: counts and example complaints.
- "F1", "F2", ... are findings from the web, each with a quote that has been checked against its page.

Rules:
- Every sentence cites at least one source id.
- Describe complaints as complaints ("owners reported..."), never as proven fact.
- State as fact only what a government record, court record or news report supports.
- Include the company's public response if a finding has one. Never write that the company "did not respond" or "declined to comment": nobody asked it.
- If a source contradicts part of the story, say so.
- Connect a source to the complaints only if it is about the same problem, not a different defect of the same product.
- Never state that something didn't happen (for example "the company has not commented") unless a source says so.
- Put direct quotes in double quotation marks, copied exactly from a source's text.
- Name no private individuals.

Reply in JSON: {"headline": "...", "paragraphs": [[{"text": "One sentence.", "cite": ["D", "F2"]}]]}"""

DATA_SOURCES = {
    "nhtsa": ("NHTSA vehicle complaints database", "https://www.nhtsa.gov/nhtsa-datasets-and-apis"),
    "maude": ("FDA MAUDE medical device reports", "https://open.fda.gov/apis/device/event/"),
    "faers": ("FDA FAERS drug adverse event reports", "https://open.fda.gov/apis/drug/event/"),
    "caers": ("FDA CAERS food, supplement and cosmetic adverse event reports", "https://open.fda.gov/apis/food/event/"),
    "osha": ("OSHA Severe Injury Reports", "https://www.osha.gov/severe-injury-reports"),
    "bluesky": ("Posts on Bluesky", "https://bsky.app/search"),
}


def run(conn: sqlite3.Connection, stories: int = 1, source: str | None = None) -> list[int]:
    """Pick up to `stories` claims from the latest count and report each one. Returns the story ids."""
    settings = config.load()
    groups = count.top(conn, settings.top_n, source=source, skip_reported=True)
    if not groups:
        db.event(conn, "reporter", "idle", "no claim groups to look at: run `count` first")
        return []
    story_ids = [open_story(conn, group, reason) for group, reason in pick(conn, groups, stories)]
    if story_ids:
        with ThreadPoolExecutor(min(settings.reporters, len(story_ids))) as pool:
            list(pool.map(report, story_ids))
    return story_ids


def rank(conn: sqlite3.Connection, groups: list[sqlite3.Row]) -> list[dict]:
    """The reporter's read of the top groups, best first: [{"group", "worth", "reason", "search"}]. One model call."""
    listing = []
    for n, g in enumerate(groups, 1):
        examples = "\n".join(f"   - {t[:300]}" for t in count.samples(conn, g))
        listing.append(f"{n}. {g['product']} ({g['company']}): {g['label']}\n"
                       f"   {g['total']} people, {g['last_90']} in the last 90 days, "
                       f"{g['severe']} reporting serious harm\n   Examples:\n{examples}")
    reply = llm.ask_json(PICK_SYSTEM, "\n\n".join(listing), model=config.load().smart_model, max_tokens=4000)
    leads, seen = [], set()
    for item in reply.get("ranked", []):
        n = item.get("group")
        if isinstance(n, int) and 1 <= n <= len(groups) and n not in seen:
            seen.add(n)
            g = groups[n - 1]
            leads.append({"group": g, "worth": item.get("worth") is True, "reason": str(item.get("reason", "")),
                          "search": str(item.get("search") or f"{g['product']} {g['label']}")})
    return leads


def pick(conn: sqlite3.Connection, groups: list[sqlite3.Row], wanted: int) -> list[tuple[sqlite3.Row, str]]:
    """Step 4: rank the top groups, then take the best ones that haven't already been reported."""
    chosen: list[tuple[sqlite3.Row, str]] = []
    leads = rank(conn, groups)
    for lead in leads:
        if len(chosen) >= wanted:
            break
        g, reason = lead["group"], lead["reason"]
        detail = {"product": g["product"], "label": g["label"], "total": g["total"]}
        if not lead["worth"]:
            db.event(conn, "reporter", "skip", reason, detail=detail)
            _reviewed(conn, g, "not_worth", reason)
            continue
        reported = already_reported(g, lead["search"])
        if reported.get("already_reported"):
            note = f"already reported: {reported.get('reason', '')}"
            db.event(conn, "reporter", "skip", note, detail={**detail, "search": lead["search"], "url": reported.get("url")})
            _reviewed(conn, g, "already_reported", note)
            continue
        db.event(conn, "reporter", "pick", reason, detail={**detail, "search": lead["search"]})
        _reviewed(conn, g, "picked", reason)
        chosen.append((g, reason))
    else:  # every ranked lead was judged; any group the reporter left out counts as not worth a story
        ranked = {(lead["group"]["product"], lead["group"]["label"]) for lead in leads}
        for g in groups:
            if (g["product"], g["label"]) not in ranked:
                _reviewed(conn, g, "not_worth", "left out of the reporter's ranking")
    return chosen


def _reviewed(conn: sqlite3.Connection, group: sqlite3.Row, outcome: str, reason: str) -> None:
    conn.execute("insert or replace into reviewed (source, product, label, outcome, reason, at) values (?, ?, ?, ?, ?, ?)",
                 (group["source"], group["product"], group["label"], outcome, reason, db.now()))
    conn.commit()


def already_reported(group: sqlite3.Row, query: str) -> dict:
    results = web.search(query, count=10)
    listing = "\n\n".join(f"{r['url']}\n{r['title']}\n{r['description']}" for r in results) or "(no results)"
    return llm.ask_json(REPORTED_SYSTEM,
                        f"Product: {group['product']} ({group['company']})\nProblem: {group['label']}\n\nSearch results:\n\n{listing}",
                        max_tokens=4000, thinking=True)


def open_story(conn: sqlite3.Connection, group: sqlite3.Row, reason: str) -> int:
    counts = {k: group[k] for k in ("total", "last_90", "severe", "as_of")} | {"group_id": group["id"]}
    cursor = conn.execute(
        "insert into stories (created, source, product, company, label, counts, status, note)"
        " values (?, ?, ?, ?, ?, ?, 'reporting', ?)",
        (db.now(), group["source"], group["product"], group["company"], group["label"], json.dumps(counts), reason),
    )
    conn.commit()
    return cursor.lastrowid


def report(story_id: int) -> None:
    """Steps 5-8 for one story. One reporter, one story; errors mark the story failed instead of stopping others."""
    with db.session() as conn:
        story = conn.execute("select * from stories where id = ?", (story_id,)).fetchone()
        try:
            _report(conn, story)
        except Exception as error:
            conn.execute("update stories set status = 'failed', note = ? where id = ?", (str(error), story_id))
            db.event(conn, "reporter", "failed", str(error), story_id=story_id)


def _report(conn: sqlite3.Connection, story: sqlite3.Row) -> None:
    settings = config.load()
    group = conn.execute("select * from claim_groups where id = ?", (json.loads(story["counts"])["group_id"],)).fetchone()
    examples = count.samples(conn, group, k=5)
    brief = _brief(story, examples)

    # Step 5: hypotheses.
    reply = llm.ask_json(HYPOTHESES_SYSTEM, brief, model=settings.smart_model, max_tokens=2000)
    hypotheses = [h.strip() for h in reply.get("hypotheses", []) if isinstance(h, str) and h.strip()][:5]
    if not hypotheses:
        raise RuntimeError("the reporter wrote no hypotheses")
    conn.execute("update stories set angle = ? where id = ?", (reply.get("angle"), story["id"]))
    hypothesis_ids = []
    for n, statement in enumerate(hypotheses, 1):
        cursor = conn.execute("insert into hypotheses (story_id, n, statement) values (?, ?, ?)", (story["id"], n, statement))
        hypothesis_ids.append(cursor.lastrowid)
    db.event(conn, "reporter", "hypotheses", reply.get("angle") or "", story_id=story["id"], detail=hypotheses)

    # Official records (recalls, investigations, lawsuits), looked up once and shared by the scouts.
    official = records.for_story(conn, story)
    for record in official:
        web.remember(record["url"], record["text"])
    db.event(conn, "reporter", "records", f"{len(official)} official records found", story_id=story["id"],
             detail=[r["title"] for r in official])

    # Step 6: one scout per hypothesis, all at once.
    context = f"{story['product']} ({story['company']}): {story['label']}"
    with ThreadPoolExecutor(len(hypotheses)) as pool:
        results = list(pool.map(lambda h: scout.research(h, context, settings.scout_budget, official), hypotheses))
    for n, (hypothesis_id, findings) in enumerate(zip(hypothesis_ids, results), 1):
        conn.executemany(
            "insert into findings (hypothesis_id, url, title, source_type, quote, finding, note)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            [(hypothesis_id, f["url"], f["title"], f["source_type"], f["quote"], f["finding"], f["note"]) for f in findings],
        )
        tally = {k: sum(1 for f in findings if f["finding"] == k) for k in ("supports", "contradicts", "unclear")}
        db.event(conn, f"scout H{n}", "report", f"{len(findings)} quotes", story_id=story["id"], detail=tally)

    # Step 7: verdict.
    verdict, note = decide(results)
    db.event(conn, "reporter", verdict, note, story_id=story["id"])
    if verdict != "write":
        conn.execute("update stories set status = ?, note = ? where id = ?",
                     ("killed" if verdict == "kill" else "parked", note, story["id"]))
        return

    # Step 8: write, check, publish.
    sources = _sources(story, examples, results)
    path = write(conn, story, brief, hypotheses, sources)
    if path:
        conn.execute("update stories set status = 'published', article = ? where id = ?", (str(path), story["id"]))
        db.event(conn, "reporter", "published", str(path), story_id=story["id"])


def decide(findings_per_hypothesis: list[list[dict]]) -> tuple[str, str]:
    """Kill if a hypothesis is clearly contradicted; write if every hypothesis is supported; else park.

    "Clearly contradicted" means a source that counts contradicts it and none supports it. One article
    disputing part of a hypothesis that official records confirm is not a kill: the article reports both.
    """
    if not findings_per_hypothesis:
        return "park", "no hypotheses"

    def counted(findings: list[dict], finding: str) -> list[dict]:
        return [f for f in findings if f["finding"] == finding and f["source_type"] in scout.COUNTS_AS_PROOF]

    for n, findings in enumerate(findings_per_hypothesis, 1):
        against = counted(findings, "contradicts")
        if against and not counted(findings, "supports"):
            return "kill", f'H{n} is contradicted by {against[0]["url"]}: "{against[0]["quote"]}"'
    missing = [n for n, findings in enumerate(findings_per_hypothesis, 1) if not counted(findings, "supports")]
    if missing:
        return "park", "not yet supported by a source that counts: " + ", ".join(f"H{n}" for n in missing)
    return "write", "every hypothesis is supported by a source that counts"


def write(conn: sqlite3.Connection, story: sqlite3.Row, brief: str, hypotheses: list[str], sources: dict[str, dict]):
    """Write the article, fixing it once if the checks fail. Returns the published path, or None."""
    listing = "\n\n".join(f"[{sid}] {s['title']} ({s.get('source_type', 'data')})\n{s['url']}\n{s['text'][:3000]}"
                          for sid, s in sources.items())
    prompt = f"{brief}\n\nConfirmed hypotheses:\n" + "\n".join(f"- {h}" for h in hypotheses) + f"\n\nSources:\n\n{listing}"
    problems: list[str] = []
    for _ in range(2):
        text = prompt if not problems else prompt + "\n\nYour last draft failed these checks; fix them:\n" + "\n".join(problems)
        draft = llm.ask_json(WRITE_SYSTEM, text, model=config.load().smart_model, max_tokens=6000)
        problems = article.check(draft, sources)
        if not problems:
            return article.publish(story["id"], draft, sources, config.load().published_dir)
    note = "article failed its checks: " + "; ".join(problems[:5])
    conn.execute("update stories set status = 'failed', note = ? where id = ?", (note, story["id"]))
    db.event(conn, "reporter", "failed", note, story_id=story["id"])
    return None


def _brief(story: sqlite3.Row, examples: list[str]) -> str:
    counts = json.loads(story["counts"])
    source_name = DATA_SOURCES.get(story["source"], ("complaint data", ""))[0]
    subject = "Employer" if story["source"] == "osha" else "Product"
    return (f"Data: {source_name}\n{subject}: {story['product']}\nCompany: {story['company']}\nProblem reported: {story['label']}\n"
            f"Distinct people reporting it: {counts['total']} ({counts['last_90']} in the 90 days before {counts['as_of']}; "
            f"{counts['severe']} reporting injury, fire or death)\n\nExample complaints:\n"
            + "\n".join(f"- {t[:600]}" for t in examples))


def _sources(story: sqlite3.Row, examples: list[str], results: list[list[dict]]) -> dict[str, dict]:
    counts = json.loads(story["counts"])
    name, url = DATA_SOURCES.get(story["source"], ("Complaint data", ""))
    if story["source"] == "bluesky":  # link the search, never individual posters
        url = f"https://bsky.app/search?q={quote_plus(story['product'] + ' ' + story['label'])}"
    sources = {"D": {
        "title": f"{name}: {counts['total']} people reporting \"{story['label']}\" for the {story['product']}, through {counts['as_of']}",
        "url": url,
        "text": "\n".join(examples),
    }}
    n = 0
    for findings in results:
        for f in findings:
            n += 1
            sources[f"F{n}"] = {"title": f["title"] or web.host(f["url"]), "url": f["url"], "text": f["quote"],
                                "quote": f["quote"], "source_type": f["source_type"]}
    return sources
