"""Official records the scouts read first: recalls, investigations and lawsuits.

These are looked up directly from government and court databases by product, company and problem, with no
web search and no model tokens. Each record becomes a page the scouts can quote from. A lookup that fails
is skipped; it never stops a story.
"""

import json
import re
import sqlite3
from urllib.parse import quote_plus

import httpx

from . import config
from .sources import nhtsa, openfda

MAX_RECORDS = 10
TEXT_LIMIT = 6000
STOPWORDS = {"a", "an", "the", "of", "on", "in", "to", "and", "or", "while", "with", "when", "is", "are", "at", "from", "for"}
COMPANY_SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "lp", "the", "us"}


def for_story(conn: sqlite3.Connection, story: sqlite3.Row) -> list[dict]:
    """Official records relevant to a story: [{"url", "title", "text", "source_type"}]."""
    group_id = json.loads(story["counts"])["group_id"]
    members = json.loads(conn.execute("select members from claim_groups where id = ?", (group_id,)).fetchone()[0])
    first = conn.execute("select fields from complaints where id = ?", (members[0]["id"],)).fetchone()
    fields = json.loads(first["fields"]) if first else {}
    product, company, source = story["product"], story["company"], story["source"]

    lookups = []
    if source == "nhtsa":
        lookups += [lambda: nhtsa_recalls(fields.get("make", ""), fields.get("model", ""), fields.get("year", "")),
                    lambda: nhtsa_investigations(conn, fields.get("make", ""), fields.get("model", ""), fields.get("year", ""))]
    elif source == "maude":
        lookups.append(lambda: fda_records("device/recall", f"product_code:{fields.get('product_code', '')}",
                                           sort="event_date_initiated:desc"))
    elif source == "faers":
        lookups.append(lambda: fda_records("drug/enforcement", f'openfda.brand_name:"{product}"',
                                           sort="recall_initiation_date:desc"))
    elif source == "caers":
        lookups.append(lambda: fda_records("food/enforcement", f'product_description:"{product}"',
                                           sort="recall_initiation_date:desc"))
    elif source == "bluesky":
        lookups.append(lambda: cpsc_recalls(product))
    if company:
        lookups.append(lambda: lawsuits(company, story["label"]))

    records = []
    for lookup in lookups:
        try:
            records += lookup()
        except (httpx.HTTPError, ValueError, KeyError):
            continue
    return records[:MAX_RECORDS]


def nhtsa_recalls(make: str, model: str, year: str) -> list[dict]:
    if not (make and model and year.isdigit() and year != "9999"):  # NHTSA writes 9999 for "year unknown"
        return []
    response = httpx.get("https://api.nhtsa.gov/recalls/recallsByVehicle",
                         params={"make": make, "model": model, "modelYear": year}, timeout=60)
    response.raise_for_status()
    return [{
        "url": f"https://www.nhtsa.gov/recalls?nhtsaId={r['NHTSACampaignNumber']}",
        "title": f"NHTSA recall {r['NHTSACampaignNumber']}: {r.get('Component', '')}",
        "text": _text(f"NHTSA recall campaign {r['NHTSACampaignNumber']}, report received {r.get('ReportReceivedDate')}. "
                      f"Manufacturer: {r.get('Manufacturer')}. Component: {r.get('Component')}.",
                      r.get("Summary"), r.get("Consequence"), r.get("Remedy")),
        "source_type": "government_record",
    } for r in response.json().get("results", [])]


def nhtsa_investigations(conn: sqlite3.Connection, make: str, model: str, year: str) -> list[dict]:
    rows = conn.execute(
        """select i.* from investigations i join investigation_vehicles v on v.action = i.action
           where v.make = ? and v.model = ? and v.year = ? order by i.opened desc""",
        (make.upper(), model.upper(), year),
    ).fetchall()
    # NHTSA has no stable public page per investigation, so the link is the official data file, one anchor each.
    return [{
        "url": f"{nhtsa.INVESTIGATIONS_URL}#{r['action']}",
        "title": f"NHTSA investigation {r['action']}: {r['subject']}",
        "text": _text(f"NHTSA defect investigation {r['action']}, opened {r['opened']}, "
                      f"{'closed ' + r['closed'] if r['closed'] else 'still open'}. Manufacturer: {r['company']}. "
                      f"Component: {r['component']}." + (f" Led to recall {r['recall']}." if r['recall'] else ""),
                      r["subject"], r["summary"]),
        "source_type": "government_record",
    } for r in rows]


def fda_records(endpoint: str, search: str, sort: str | None = None) -> list[dict]:
    records = []
    for r in openfda.query(endpoint, search, limit=5, sort=sort):
        number = r.get("product_res_number") or r.get("recall_number") or ""
        url = (f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfRES/res.cfm?id={r['cfres_id']}" if r.get("cfres_id")
               else f"https://api.fda.gov/{endpoint}.json?search=recall_number:{number}")
        records.append({
            "url": url,
            "title": f"FDA recall {number}: {r.get('recalling_firm', '')}",
            "text": _text(f"FDA recall {number} by {r.get('recalling_firm')}, initiated "
                          f"{r.get('event_date_initiated') or r.get('recall_initiation_date')}, status {r.get('recall_status') or r.get('status')}"
                          + (f", {r['classification']}" if r.get("classification") else "") + ".",
                          r.get("product_description"), "Reason: " + (r.get("reason_for_recall") or ""),
                          r.get("root_cause_description")),
            "source_type": "government_record",
        })
    return records


def cpsc_recalls(product: str) -> list[dict]:
    name = " ".join(product.split()[:2])  # CPSC matches names loosely: "NINJA FOODI" finds Foodi recalls, longer names often miss
    response = httpx.get("https://www.saferproducts.gov/RestWebServices/Recall",
                         params={"format": "json", "ProductName": name}, timeout=60)
    response.raise_for_status()
    return [{
        "url": r.get("URL") or f"https://www.cpsc.gov/Recalls?search={quote_plus(name)}",
        "title": f"CPSC recall {r.get('RecallNumber')}: {r.get('Title', '')}",
        "text": _text(f"CPSC recall {r.get('RecallNumber')}, {str(r.get('RecallDate', ''))[:10]}. {r.get('Title', '')}",
                      r.get("Description"), " ".join(h.get("Name", "") for h in r.get("Hazards") or []),
                      " ".join(x.get("Name", "") for x in r.get("Remedies") or [])),
        "source_type": "government_record",
    } for r in response.json()[:5]]


def lawsuits(company: str, problem: str) -> list[dict]:
    """Federal court dockets naming the company and the problem, from CourtListener."""
    core = " ".join(w for w in re.sub(r"[^A-Za-z0-9 ]+", " ", company).split() if w.lower() not in COMPANY_SUFFIXES)
    keywords = [w for w in re.sub(r"[^a-z0-9 ]+", " ", problem.lower()).split() if w not in STOPWORDS][:4]
    if not core:
        return []
    headers = {}
    if token := config.load().courtlistener_token:
        headers["Authorization"] = f"Token {token}"
    response = httpx.get("https://www.courtlistener.com/api/rest/v4/search/",
                         params={"q": f'"{core}" {" ".join(keywords)}', "type": "r"}, headers=headers, timeout=60)
    response.raise_for_status()
    records = []
    for r in response.json().get("results", [])[:5]:
        snippets = " ".join((d.get("snippet") or "").strip() for d in r.get("recap_documents") or [])
        records.append({
            "url": "https://www.courtlistener.com" + r["docket_absolute_url"],
            "title": f"{r.get('caseName')} ({r.get('court')}, filed {r.get('dateFiled')})",
            "text": _text(f"Court case: {r.get('caseName')}. Court: {r.get('court')}. Docket {r.get('docketNumber')}, "
                          f"filed {r.get('dateFiled')}. Nature of suit: {r.get('suitNature')}.", snippets),
            "source_type": "court_record",
        })
    return records


# What an 8-K item number means: the items are how a company says what happened.
EIGHT_K_ITEMS = {"1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy or receivership",
                 "1.05": "cybersecurity incident", "2.01": "acquisition or sale completed", "2.03": "new debt",
                 "2.04": "debt accelerated", "2.05": "exit or layoff costs", "2.06": "material impairment",
                 "3.01": "delisting notice", "4.01": "auditor changed", "4.02": "past financials can't be relied on",
                 "5.02": "executive or director departure", "5.07": "shareholder vote", "8.01": "other event"}


def sec_filings(phrase: str, forms: str = "", since: str = "") -> list[dict]:
    """EDGAR full-text search (filings since 2001): the filings whose text contains the phrase, newest first.
    forms: comma-separated form types (8-K, 10-K, 4, S-1, DEF 14A, 13D ...). since: YYYY-MM-DD, default a
    year back. Returns [{url, title, date, form, filer}] pointing at the filing document itself."""
    from datetime import date, timedelta
    from .web import SEC_HEADERS
    phrase = phrase.strip()
    q = phrase if '"' in phrase or len(phrase.split()) == 1 else f'"{phrase}"'
    params = {"q": q, "dateRange": "custom", "startdt": since or (date.today() - timedelta(days=365)).isoformat(),
              "enddt": date.today().isoformat()}
    if forms:
        params["forms"] = ",".join(f.strip().upper() for f in forms.split(",") if f.strip())
    response = httpx.get("https://efts.sec.gov/LATEST/search-index", params=params, headers=SEC_HEADERS, timeout=30)
    response.raise_for_status()
    rows = []
    for hit in response.json().get("hits", {}).get("hits", []):
        src, (adsh, _, filename) = hit.get("_source", {}), hit.get("_id", "").partition(":")
        if not (src.get("ciks") and adsh and filename):
            continue
        cik = src["ciks"][0].lstrip("0")
        rows.append({"url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{filename}",
                     "title": f"{src.get('form', '')} {', '.join(src.get('display_names', []))[:120]}",
                     "date": src.get("file_date", ""), "form": src.get("form", ""),
                     "filer": ", ".join(src.get("display_names", [])),
                     "items": [EIGHT_K_ITEMS.get(i, i) for i in src.get("items") or []],
                     "place": ", ".join(src.get("biz_locations") or [])})
    return sorted(rows, key=lambda r: r["date"], reverse=True)


def _text(*parts: str | None) -> str:
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()[:TEXT_LIMIT]
