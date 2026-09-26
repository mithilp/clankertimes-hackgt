"""CPSC product recalls, from saferproducts.gov's public recall API. No key needed.

Consumer contact lines (phone numbers, emails) and images are never stored.
"""

import re
from datetime import date

import httpx

RECALLS_URL = "https://www.saferproducts.gov/RestWebServices/Recall"


def fetch(since: date) -> list[dict]:
    """Recalls announced on or after `since`, in the shape of db.save_recalls()."""
    response = httpx.get(RECALLS_URL, params={"format": "json", "RecallDateStart": since.isoformat()}, timeout=120)
    response.raise_for_status()
    return [to_recall(r) for r in response.json() if r.get("RecallNumber")]


def to_recall(r: dict) -> dict:
    number = str(r["RecallNumber"]).strip()
    names = [p.get("Name", "").strip() for p in r.get("Products") or [] if p.get("Name", "").strip()]
    units_text = next((p.get("NumberOfUnits", "").strip() for p in r.get("Products") or [] if p.get("NumberOfUnits")), "")
    injuries = " ".join(i.get("Name", "").strip() for i in r.get("Injuries") or []).strip()
    hazard = " ".join(h.get("Name", "").strip() for h in r.get("Hazards") or []).strip()
    severity = [f"injuries: {injuries}" if injuries else "", f"units: {units_text}" if units_text else ""]
    reported = injuries and not injuries.lower().startswith("none")
    return {
        "id": f"cpsc_recalls:{number}",
        "dataset": "cpsc_recalls",
        "date": str(r.get("RecallDate") or "")[:10] or "1900-01-01",
        "label": f"CPSC recall {number}",
        "product": "; ".join(names[:3]) or r.get("Title", "").strip() or "unnamed product",
        "company": company(r),
        "problem": _short(hazard or r.get("Title", ""), 300),
        "severity": "; ".join(s for s in severity if s),
        "priority": 2 if re.search(r"\bdeath|\bdied|\bfatal", injuries, re.I) else 1 if reported else 0,
        "units": units(units_text),
        "url": r.get("URL") or f"https://www.cpsc.gov/Recalls?search={number}",
        "fields": {"title": r.get("Title", "").strip()},
    }


def company(r: dict) -> str | None:
    """Whoever is responsible: the manufacturer, else the importer or distributor, else the seller CPSC names.
    Retailer entries that only say where it was sold ("Sold Online At: ...") are not a company."""
    for key in ("Manufacturers", "Importers", "Distributors", "Retailers"):
        for entry in r.get(key) or []:
            name = " ".join((entry.get("Name") or "").split())
            if name and not name.lower().startswith("sold "):
                return name
    return None


def units(text: str) -> int:
    """"About 324" -> 324; "About 1.2 million" -> 1200000."""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(million|thousand)?", text or "", re.I)
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"million": 1_000_000, "thousand": 1_000}.get((m.group(2) or "").lower(), 1))


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."
