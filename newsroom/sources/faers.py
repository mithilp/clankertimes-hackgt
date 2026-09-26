"""FDA FAERS drug adverse-event reports, through openFDA.

Reports are coded: each names the suspect drug and lists the reactions in standard medical terms, so the
claims come straight from the codes and no model tokens are spent. One complaint is stored per reaction;
they share the report as their "person", so a report counts once in each reaction's group.
"""

from collections.abc import Iterator
from datetime import date

from . import openfda


def fetch(since: date, until: date, *, drug: str | None = None, serious_only: bool = True,
          limit: int = 5000) -> Iterator[dict]:
    search = f"receivedate:[{since:%Y%m%d} TO {until:%Y%m%d}]"
    if serious_only:
        search += " AND serious:1"
    if drug:
        search += f' AND patient.drug.openfda.brand_name:"{drug}"'
    for report in openfda.query("drug/event", search, limit=limit):
        yield from to_complaints(report)


def to_complaints(report: dict) -> list[dict]:
    patient = report.get("patient") or {}
    drugs = patient.get("drug") or []
    suspect = next((d for d in drugs if d.get("drugcharacterization") == "1"), drugs[0] if drugs else {})
    labels = suspect.get("openfda") or {}
    name = ((labels.get("brand_name") or [None])[0] or suspect.get("medicinalproduct") or "unknown drug").strip()
    report_id = report.get("safetyreportid")
    reactions = sorted({(r.get("reactionmeddrapt") or "").strip() for r in patient.get("reaction") or []} - {""})
    return [{
        "id": f"faers:{report_id}:{n}",
        "source": "faers",
        "received": openfda.ymd(report.get("receivedate")),
        "product": name.upper(),
        "company": (labels.get("manufacturer_name") or [None])[0],
        "severe": report.get("serious") == "1",
        "text": "",
        "claim": reaction.lower(),
        "fields": {"person": f"faers:{report_id}", "report": report_id, "reactions": reactions,
                   "death": report.get("seriousnessdeath") == "1"},
    } for n, reaction in enumerate(reactions, 1)]
