"""FDA MAUDE medical device reports, through openFDA.

MAUDE receives several hundred thousand reports a month, so reports are fetched for a date range and,
usually, one device type (FDA product code). The reports are free text, so a model reads them.
"""

from collections.abc import Iterator
from datetime import date

from . import openfda


def fetch(since: date, until: date, *, product_code: str | None = None, limit: int = 5000) -> Iterator[dict]:
    search = f"date_received:[{since:%Y%m%d} TO {until:%Y%m%d}]"
    if product_code:
        search += f" AND device.device_report_product_code:{product_code}"
    for record in openfda.query("device/event", search, limit=limit):
        yield to_complaint(record)


def to_complaint(record: dict) -> dict:
    device = (record.get("device") or [{}])[0]
    code = device.get("device_report_product_code") or "?"
    brand = (device.get("brand_name") or device.get("generic_name") or "unknown device").strip()
    event_type = record.get("event_type") or ""
    descriptions = [t.get("text", "").strip() for t in record.get("mdr_text") or []
                    if t.get("text_type_code") == "Description of Event or Problem"]
    return {
        "id": f"maude:{record.get('mdr_report_key')}",
        "source": "maude",
        "received": openfda.ymd(record.get("date_received")),
        "product": f"{brand} [{code}]",
        "company": device.get("manufacturer_d_name") or record.get("manufacturer_name"),
        "severe": event_type in ("Death", "Injury"),
        "text": "\n".join(d for d in descriptions if d),
        "fields": {
            "mdr_report_key": record.get("mdr_report_key"),
            "report_number": record.get("report_number"),
            "event_type": event_type,
            "product_code": code,
            "generic_name": device.get("generic_name"),
            "report_source": record.get("report_source_code"),
            "product_problems": record.get("product_problems") or [],
        },
    }
