"""FDA recalls of devices, drugs and foods, through openFDA, one per recall event.

One event (a firm's recall of a problem) usually lists many recall numbers, one per product or lot, so the
rows are rolled up by event. Firm addresses and contact names are never stored.
"""

import re
from collections.abc import Iterator
from datetime import date, timedelta

from . import openfda

# endpoint -> (field that dates a record's publication, field that names its event)
ENDPOINTS = {
    "device/recall": ("event_date_posted", "res_event_number"),
    "drug/enforcement": ("report_date", "event_id"),
    "food/enforcement": ("report_date", "event_id"),
}
CLASS_PRIORITY = {"Class I": 2, "Class II": 1}


def fetch(since: date, until: date) -> Iterator[dict]:
    """Recall events published between since and until, in the shape of db.save_recalls()."""
    for endpoint, (date_field, _) in ENDPOINTS.items():
        records = []
        start = since
        while start <= until:  # month by month: openFDA pages through at most 25,000 results per query
            end = min(start + timedelta(days=30), until)
            records += openfda.query(endpoint, f"{date_field}:[{start:%Y%m%d} TO {end:%Y%m%d}]", limit=25_000)
            start = end + timedelta(days=1)
        yield from to_recalls(endpoint, records)


def to_recalls(endpoint: str, records: list[dict]) -> list[dict]:
    date_field, event_field = ENDPOINTS[endpoint]
    events: dict[str, list[dict]] = {}
    for r in records:
        if event := str(r.get(event_field) or "").strip():
            events.setdefault(event, []).append(r)
    out = []
    for event, rows in events.items():
        first = rows[0]
        classification = next((r["classification"] for r in rows if r.get("classification")), "")
        numbers = sorted({r.get("product_res_number") or r.get("recall_number") or "" for r in rows} - {""})
        product = _first_line(first.get("product_description"), 120)
        if len(rows) > 1:
            product += f" and {len(rows) - 1} more products"
        if endpoint == "device/recall" and first.get("cfres_id"):
            url = f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfRES/res.cfm?id={first['cfres_id']}"
        else:
            url = f"https://api.fda.gov/{endpoint}.json?search={event_field}:{event}"
        severity = [classification] if classification else []
        severity.append(f"{len(numbers)} recall number{'s' if len(numbers) != 1 else ''}")
        out.append({
            "id": f"fda_recalls:{event}",
            "dataset": "fda_recalls",
            "date": max(_ymd(r.get(date_field)) for r in rows),
            "label": f"FDA {endpoint.split('/')[0]} recall event {event}" + (f" ({classification})" if classification else ""),
            "product": product or "unnamed product",
            "company": (first.get("recalling_firm") or "").strip() or None,
            "problem": _first_line(first.get("reason_for_recall"), 300) or "reason not given",
            "severity": "; ".join(severity),
            "priority": CLASS_PRIORITY.get(classification, 0),
            "units": len(numbers),
            "url": url,
            "fields": {"endpoint": endpoint, "recall_numbers": numbers,
                       "status": first.get("recall_status") or first.get("status")},
        })
    return out


def _first_line(text: str | None, limit: int) -> str:
    line = " ".join((text or "").strip().splitlines()[:1]).strip() if text else ""
    return line if len(line) <= limit else line[:limit].rsplit(" ", 1)[0] + "..."


def _ymd(value: str | None) -> str:
    """openFDA writes dates as 20260909 or 2026-09-09, depending on the endpoint."""
    digits = re.sub(r"\D", "", value or "")
    return openfda.ymd(digits[:8])
