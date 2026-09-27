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


SGO_FILES = {"ADS": "https://static.nhtsa.gov/odi/ffdd/sgo-2021-01/SGO-2021-01_Incident_Reports_ADS.csv",
             "ADAS": "https://static.nhtsa.gov/odi/ffdd/sgo-2021-01/SGO-2021-01_Incident_Reports_ADAS.csv"}


def _sgo_rows(kind: str) -> tuple[list[dict], str]:
    """NHTSA's Standing General Order crash file (ADS = driverless, ADAS = Level 2 driver assist), cached for a
    day under data/sgo/. Returns (rows, the file's Last-Modified date)."""
    import csv
    import time
    from pathlib import Path
    path = Path("data/sgo") / f"{kind.lower()}.csv"
    meta = path.with_suffix(".modified")
    if not path.exists() or time.time() - path.stat().st_mtime > 86_400:
        path.parent.mkdir(parents=True, exist_ok=True)
        response = httpx.get(SGO_FILES[kind], headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
        response.raise_for_status()
        path.write_bytes(response.content)
        meta.write_text(response.headers.get("last-modified", ""), encoding="utf-8")
    if not meta.exists():
        meta.write_text(httpx.head(SGO_FILES[kind], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                        .headers.get("last-modified", ""), encoding="utf-8")
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(f)), meta.read_text(encoding="utf-8")


def nhtsa_sgo(operator: str = "", city: str = "", state: str = "", kind: str = "ADS") -> list[dict]:
    """Crashes reported to NHTSA under the Standing General Order, filtered by reporting or operating company,
    city and state. First row: counts computed from the file (by company, month and injury severity); then
    the newest individual reports, each with its narrative (which companies may withhold as confidential)."""
    kind = "ADAS" if kind.strip().upper() == "ADAS" else "ADS"
    rows, modified = _sgo_rows(kind)
    op, city, state = operator.strip().lower(), city.strip().lower(), state.strip().lower()
    hits = [r for r in rows
            if (not op or op in r.get("Reporting Entity", "").lower() or op in r.get("Operating Entity", "").lower())
            and (not city or r.get("City", "").strip().lower() == city)
            and (not state or r.get("State", "").strip().lower() == state)]
    url = SGO_FILES[kind]
    scope = ", ".join(x for x in (operator, city, state) if x.strip()) or "all"
    if not hits:
        return [{"url": f"{url}#none-{scope}", "title": f"NHTSA SGO {kind} crash file: no reports for {scope}",
                 "text": f"NHTSA's Standing General Order {kind} incident report file (last updated {modified}), "
                         f"{len(rows)} reports in total, contains no reports matching {scope}.",
                 "source_type": "government_record"}]
    from collections import Counter
    months = {m: n for n, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}

    def when(r: dict) -> tuple[int, int]:
        mon, _, year = r.get("Incident Date", "").partition("-")
        return (int(year) if year.isdigit() else 0, months.get(mon.upper(), 0))
    latest = {}
    for r in hits:                       # a crash can have several report versions; count each Report ID once
        if r.get("Report ID") and (r["Report ID"] not in latest or r.get("Report Version", "") > latest[r["Report ID"]].get("Report Version", "")):
            latest[r["Report ID"]] = r
    reports = sorted(latest.values(), key=when, reverse=True)
    by_company = Counter(r.get("Reporting Entity", "?") for r in reports)
    by_month = Counter(r.get("Incident Date", "?") for r in reports)
    by_injury = Counter(r.get("Highest Injury Severity Alleged", "?") for r in reports)
    summary = (f"Counts computed by the newsroom from NHTSA's Standing General Order {kind} incident report file "
               f"(last updated {modified}; {len(rows)} report rows in the file). Reports matching {scope}: {len(reports)} "
               f"distinct crash reports. By reporting company: {dict(by_company.most_common())}. By incident month: "
               f"{dict(sorted(by_month.items(), key=lambda kv: when({'Incident Date': kv[0]})))}. By highest injury "
               f"severity alleged: {dict(by_injury.most_common())}. Fatalities alleged: "
               f"{sum('fatal' in r.get('Highest Injury Severity Alleged', '').lower() for r in reports)}.")
    out = [{"url": f"{url}#summary-{scope.replace(' ', '_')}", "title": f"NHTSA SGO {kind} crash reports, {scope}: counts",
            "text": summary, "source_type": "government_record"}]
    for r in reports[:15]:
        narrative = r.get("Narrative", "").strip() or "(no narrative)"
        if r.get("Narrative - CBI?", "").strip().upper() == "Y":
            narrative = "[narrative withheld by the company as confidential business information]"
        out.append({"url": f"{url}#report-{r['Report ID']}",
                    "title": f"NHTSA SGO report {r['Report ID']}: {r.get('Reporting Entity', '')}, {r.get('Incident Date', '')}, "
                             f"{r.get('City', '')} {r.get('State', '')}",
                    "text": _text(f"NHTSA Standing General Order {kind} crash report {r['Report ID']} (version {r.get('Report Version')}, "
                                  f"{r.get('Report Type')} report, submitted {r.get('Report Submission Date')}). Reporting entity: "
                                  f"{r.get('Reporting Entity')}. Operator: {r.get('Operating Entity')}. Vehicle: {r.get('Model Year')} "
                                  f"{r.get('Make')} {r.get('Model')}, driver/operator type {r.get('Driver / Operator Type')}, "
                                  f"engagement {r.get('Engagement Status')}. Incident {r.get('Incident Date')} "
                                  f"{r.get('Incident Time (24:00)')}, {r.get('City')}, {r.get('State')}, {r.get('Roadway Type')}. "
                                  f"Crash with: {r.get('Crash With')}. Highest injury severity alleged: "
                                  f"{r.get('Highest Injury Severity Alleged')}. Speed before crash: {r.get('SV Precrash Speed (MPH)')} mph.",
                                  narrative),
                    "source_type": "government_record"})
    return out


FDIC_API = "https://api.fdic.gov/banks"
# Call Report fields, with what they mean. Ratios are percents; dollar amounts are in thousands.
FDIC_FIELDS = {"ASSET": "total assets ($ thousands)", "DEP": "total deposits ($ thousands)",
               "EQ": "total equity capital ($ thousands)", "NETINC": "net income, year to date ($ thousands)",
               "RBC1AAJ": "tier 1 leverage ratio (%)", "RBCRWAJ": "total risk-based capital ratio (%)",
               "NCLNLSR": "noncurrent loans as a share of loans (%)", "ROA": "return on assets (%)"}


def fdic_bank(bank: str) -> list[dict]:
    """A bank's FDIC record and its Call Report history: the institution (by FDIC certificate number or
    name) and, quarter by quarter, assets, deposits, capital ratios and income, newest first."""
    bank = bank.strip()
    params = ({"filters": f"CERT:{bank}"} if bank.isdigit() else {"search": f"NAME:{bank}"})
    found = httpx.get(f"{FDIC_API}/institutions", params={**params, "limit": 3,
                      "fields": "NAME,CITY,STALP,ACTIVE,CERT,ENDEFYMD,ESTYMD,ASSET"}, timeout=30)
    found.raise_for_status()
    out = []
    for inst in [d["data"] for d in found.json().get("data", [])][:2]:
        cert = inst["CERT"]
        fin = httpx.get(f"{FDIC_API}/financials", params={"filters": f"CERT:{cert}", "sort_by": "REPDTE",
                        "sort_order": "DESC", "limit": 12, "fields": "REPDTE," + ",".join(FDIC_FIELDS)}, timeout=30)
        fin.raise_for_status()
        quarters = []
        for q in (d["data"] for d in fin.json().get("data", [])):
            rep = str(q.get("REPDTE", ""))
            parts = [f"{label}: {q[k]:,.2f}" if isinstance(q.get(k), float) else f"{label}: {q.get(k)}"
                     for k, label in FDIC_FIELDS.items() if q.get(k) is not None]
            quarters.append(f"Quarter ending {rep[:4]}-{rep[4:6]}-{rep[6:]}: " + "; ".join(parts) + ".")
        status = "active" if inst.get("ACTIVE") == 1 else f"inactive since {inst.get('ENDEFYMD', '?')}"
        out.append({"url": f"https://banks.data.fdic.gov/bankfind-suite/bankfind/details/{cert}",
                    "title": f"FDIC BankFind: {inst['NAME']} ({inst.get('CITY')}, {inst.get('STALP')}), cert {cert}",
                    "text": _text(f"FDIC institution record: {inst['NAME']}, {inst.get('CITY')}, {inst.get('STALP')}, "
                                  f"FDIC certificate {cert}, established {inst.get('ESTYMD')}, {status}. "
                                  "Call Report financial data filed with the FDIC, newest quarter first:",
                                  "\n".join(quarters)),
                    "source_type": "government_record"})
    return out


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
