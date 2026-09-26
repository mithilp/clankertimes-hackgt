"""OSHA Severe Injury Reports: workplace hospitalizations, amputations and eye losses, by employer.

OSHA publishes one CSV (inside a zip) covering 2015 onward, for federal-OSHA states only. Each report is
already coded (what happened, what injury), so claims come from the codes and no model tokens are spent.
The "product" of an OSHA complaint is the employer. Addresses and coordinates are never stored.
"""

import csv
import io
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

import httpx

PAGE_URL = "https://www.osha.gov/severe-injury-reports"
SITE = "https://www.osha.gov"
# OSHA's site refuses requests that don't look like a browser.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}

SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "lp", "llp", "pllc", "pc", "the"}
ALIASES = {  # the biggest employers, whose names are written many ways
    "usps": "us postal service", "united states postal service": "us postal service",
    "u s postal service": "us postal service", "us postal service": "us postal service",
    "ups": "united parcel service", "amazon com services": "amazon", "amazon com": "amazon",
}


def download(dest_dir: Path) -> Path:
    """Find the current data file on OSHA's page (its name changes with each update) and download it."""
    page = httpx.get(PAGE_URL, headers=HEADERS, timeout=60, follow_redirects=True)
    page.raise_for_status()
    match = re.search(r'href="(/sites/default/files/[^"]+\.zip)"', page.text)
    if not match:
        raise RuntimeError(f"no data file link found on {PAGE_URL}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / Path(match.group(1)).name
    with httpx.stream("GET", SITE + match.group(1), headers=HEADERS, timeout=600, follow_redirects=True) as response:
        response.raise_for_status()
        with open(path, "wb") as out:
            for chunk in response.iter_bytes():
                out.write(chunk)
    return path


def employer_key(name: str) -> str:
    """A matching key for employer names: "U.S. Postal Service" and "USPS" become the same key."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    key = " ".join(words)
    return ALIASES.get(key, key)


def claim_of(row: dict) -> str:
    injury = ("amputation" if _num(row.get("Amputation")) > 0
              else "loss of an eye" if _num(row.get("Loss of Eye")) > 0 else "hospitalization")
    event = re.sub(r"\s{2,}", " - ", (row.get("EventTitle") or "").strip()).lower()
    return f"{injury}: {event}" if event else injury


def parse(zip_path: Path) -> Iterator[dict]:
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        text = archive.read(name).decode("utf-8", errors="replace")
    rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get("ID") or "").strip() and (r.get("Employer") or "").strip()]

    # Show each employer under the spelling it's most often reported with.
    spellings: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        spellings[employer_key(row["Employer"])][row["Employer"].strip()] += 1
    display = {key: names.most_common(1)[0][0] for key, names in spellings.items()}

    for row in rows:
        employer = display[employer_key(row["Employer"])]
        yield {
            "id": f"osha:{row['ID'].strip()}",
            "source": "osha",
            "received": _date(row.get("EventDate", "")),
            "product": employer,
            "company": employer,
            "severe": True,  # every report here is a hospitalization, amputation or eye loss
            "text": (row.get("Final Narrative") or "").strip(),
            "claim": claim_of(row),
            "fields": {
                "person": f"osha:{row['ID'].strip()}",
                "state": (row.get("State") or "").strip(),
                "naics": (row.get("Primary NAICS") or "").strip(),
                "nature": (row.get("NatureTitle") or "").strip(),
                "body_part": (row.get("Part of Body Title") or "").strip(),
                "source": (row.get("SourceTitle") or "").strip(),
                "inspection": (row.get("Inspection") or "").strip(),
            },
        }


def _num(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _date(mdy: str) -> str:
    parts = mdy.strip().split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        month, day, year = parts
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return "1900-01-01"
