"""FDA CAERS adverse-event reports for foods, dietary supplements and cosmetics, through openFDA.

Like FAERS, reports are coded (product and reactions), so claims need no model tokens. One complaint is
stored per reaction, sharing the report as its "person".
"""

from collections.abc import Iterator
from datetime import date

from . import openfda

SERIOUS = {"Death", "Life Threatening", "Hospitalization", "Disability", "Required Intervention",
           "Other Serious or Important Medical Event", "Visited Emergency Room"}


def fetch(since: date, until: date, *, limit: int = 5000) -> Iterator[dict]:
    search = f"date_created:[{since:%Y%m%d} TO {until:%Y%m%d}]"
    for report in openfda.query("food/event", search, limit=limit):
        yield from to_complaints(report)


def to_complaints(report: dict) -> list[dict]:
    products = report.get("products") or []
    suspect = next((p for p in products if p.get("role") == "SUSPECT"), products[0] if products else {})
    name = (suspect.get("name_brand") or "unknown product").strip()
    number = report.get("report_number")
    outcomes = report.get("outcomes") or []
    reactions = sorted({r.strip() for r in report.get("reactions") or [] if r and r.strip()})
    return [{
        "id": f"caers:{number}:{n}",
        "source": "caers",
        "received": openfda.ymd(report.get("date_created")),
        "product": name.upper(),
        "company": None,  # CAERS doesn't name the maker
        "severe": any(o in SERIOUS for o in outcomes),
        "text": "",
        "claim": reaction.lower(),
        "fields": {"person": f"caers:{number}", "report": number, "industry": suspect.get("industry_name"),
                   "outcomes": outcomes},
    } for n, reaction in enumerate(reactions, 1)]
