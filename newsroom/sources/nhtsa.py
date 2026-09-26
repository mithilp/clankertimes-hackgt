"""NHTSA vehicle complaints, from the bulk files NHTSA republishes daily.

The layout is described at https://static.nhtsa.gov/odi/ffdd/cmpl/CMPL.txt. One complaint (ODINO) is
listed once per affected component, so its rows are merged into one complaint.

Only the fields the newsroom needs are kept. The complainant's city, dealer details and the
vehicle operator's name are never stored.
"""

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx

BASE_URL = "https://static.nhtsa.gov/odi/ffdd/cmpl/"

# Column positions: the CMPL.txt field number minus one.
ODINO, MFR_NAME, MAKE, MODEL, YEAR, CRASH, FAILDATE, FIRE, INJURED, DEATHS, COMPDESC = 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
LDATE, CDESCR = 16, 19


def download(period: str, dest_dir: Path) -> Path:
    """Download a complaints file: a 5-year period such as "2025-2026", or "all" (every year since 1995)."""
    name = "FLAT_CMPL.zip" if period == "all" else f"COMPLAINTS_RECEIVED_{period}.zip"
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    with httpx.stream("GET", BASE_URL + name, timeout=600, follow_redirects=True) as response:
        response.raise_for_status()
        with open(path, "wb") as out:
            for chunk in response.iter_bytes():
                out.write(chunk)
    return path


def parse(zip_path: Path) -> Iterator[dict]:
    """Yield one complaint per ODINO, with its components merged."""
    merged: dict[str, dict] = {}
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".txt"))
        with archive.open(name) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) <= CDESCR or not parts[ODINO].strip():
                    continue
                complaint = _from_row(parts)
                existing = merged.get(complaint["id"])
                if existing is None:
                    merged[complaint["id"]] = complaint
                else:
                    _merge(existing, complaint)
    yield from merged.values()


def _from_row(p: list[str]) -> dict:
    year = p[YEAR].strip()
    make, model = p[MAKE].strip(), p[MODEL].strip()
    product = f"{year} {make} {model}" if year and year != "9999" else f"{make} {model} (year unknown)"
    injured, deaths = _int(p[INJURED]), _int(p[DEATHS])
    fire = p[FIRE].strip() == "Y"
    component = p[COMPDESC].strip()
    return {
        "id": f"nhtsa:{p[ODINO].strip()}",
        "source": "nhtsa",
        "received": _date(p[LDATE]),
        "product": product,
        "company": p[MFR_NAME].strip() or None,
        "severe": fire or injured > 0 or deaths > 0,
        "text": p[CDESCR].strip(),
        "fields": {
            "odino": p[ODINO].strip(),
            "make": make, "model": model, "year": year,
            "components": [component] if component else [],
            "crash": p[CRASH].strip() == "Y", "fire": fire,
            "injured": injured, "deaths": deaths,
            "failed": _date(p[FAILDATE]) if p[FAILDATE].strip() else None,
        },
    }


def _merge(into: dict, other: dict) -> None:
    for component in other["fields"]["components"]:
        if component not in into["fields"]["components"]:
            into["fields"]["components"].append(component)
    into["severe"] = into["severe"] or other["severe"]
    for key in ("injured", "deaths"):
        into["fields"][key] = max(into["fields"][key], other["fields"][key])
    if len(other["text"]) > len(into["text"]):
        into["text"] = other["text"]


INVESTIGATIONS_URL = "https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip"


def download_investigations(dest_dir: Path) -> Path:
    """NHTSA's defect investigations (layout: https://static.nhtsa.gov/odi/ffdd/inv/INV.txt)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "FLAT_INV.zip"
    response = httpx.get(INVESTIGATIONS_URL, timeout=600, follow_redirects=True)
    response.raise_for_status()
    path.write_bytes(response.content)
    return path


def save_investigations(conn, zip_path: Path) -> tuple[int, int]:
    """Load investigations: one row per investigation, plus the vehicles each covers. Returns both counts.

    The file repeats each investigation's summary once per vehicle, so it's stored once here.
    """
    investigations: dict[str, tuple] = {}
    vehicles: set[tuple[str, str, str, str]] = set()
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".txt"))
        with archive.open(name) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
                p = line.rstrip("\r\n").split("\t")
                if len(p) < 11 or not p[0].strip():
                    continue
                action = p[0].strip()
                investigations.setdefault(action, (action, p[5].strip(), p[4].strip(), _date(p[6]) if p[6].strip() else None,
                                                   _date(p[7]) if p[7].strip() else None, p[8].strip() or None,
                                                   p[9].strip(), p[10].strip()))
                vehicles.add((action, p[1].strip().upper(), p[2].strip().upper(), p[3].strip()))
    conn.executemany("insert or replace into investigations values (?, ?, ?, ?, ?, ?, ?, ?)", investigations.values())
    conn.executemany("insert or ignore into investigation_vehicles values (?, ?, ?, ?)", vehicles)
    conn.commit()
    return len(investigations), len(vehicles)


def _int(value: str) -> int:
    try:
        return int(value.strip() or 0)
    except ValueError:
        return 0


def _date(yyyymmdd: str) -> str:
    v = yyyymmdd.strip()
    return f"{v[0:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 else "1900-01-01"
