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


RECALLS_URL = "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip"
# Column positions in the recall file (layout: https://static.nhtsa.gov/odi/ffdd/rcl/RCL.txt), field number minus one.
# (The RCL_FROM_<years>.zip files next to it are an index of recall documents, not the recalls.)
R_CAMPNO, R_MAKE, R_MODEL, R_YEAR, R_COMPNAME, R_MFGNAME, R_POTAFF, R_INFLUENCED_BY, R_RCDATE = 1, 2, 3, 4, 6, 7, 11, 13, 15
R_DEFECT, R_CONSEQUENCE, R_DO_NOT_DRIVE, R_PARK_OUTSIDE = 19, 20, 27, 28


def download_recalls(dest_dir: Path) -> Path:
    """NHTSA's recalls since 2010, one row per campaign, vehicle and component. Republished daily."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "FLAT_RCL_POST_2010.zip"
    with httpx.stream("GET", RECALLS_URL, timeout=600, follow_redirects=True) as response:
        response.raise_for_status()
        with open(path, "wb") as out:
            for chunk in response.iter_bytes():
                out.write(chunk)
    return path


def parse_recalls(zip_path: Path, since: str = "") -> Iterator[dict]:
    """One recall per campaign filed on or after `since` (YYYY-MM-DD), in the shape of db.save_recalls()."""
    campaigns: dict[str, list[list[str]]] = {}
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".txt"))
        with archive.open(name) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
                p = line.rstrip("\r\n").split("\t")
                if len(p) <= R_CONSEQUENCE or not p[R_CAMPNO].strip() or _date(p[R_RCDATE]) < since:
                    continue
                campaigns.setdefault(p[R_CAMPNO].strip(), []).append(p)
    for campno, rows in campaigns.items():
        first = rows[0]
        do_not_drive, park_outside = _any_yes(rows, R_DO_NOT_DRIVE), _any_yes(rows, R_PARK_OUTSIDE)
        influenced = first[R_INFLUENCED_BY].strip()
        units = _int(first[R_POTAFF])  # the same on every row of a campaign, so not summed
        vehicles = sorted({(r[R_MAKE].strip().upper(), r[R_MODEL].strip().upper(), r[R_YEAR].strip()) for r in rows})
        components = sorted({component_top(r[R_COMPNAME]) for r in rows} - {""})
        severity = [s for s, on in (("NHTSA says do not drive", do_not_drive), ("NHTSA says park outside", park_outside))
                    if on]
        if units:
            severity.append(f"{units:,} units potentially affected")
        if influenced == "ODI":
            severity.append("influenced by an NHTSA investigation")
        defect = " ".join(first[R_DEFECT].split())
        defect = defect if len(defect) <= 300 else defect[:300].rsplit(" ", 1)[0] + "..."
        yield {
            "id": f"nhtsa_recalls:{campno}",
            "dataset": "nhtsa_recalls",
            "date": _date(first[R_RCDATE]),
            "label": f"NHTSA recall {campno}",
            "product": vehicles_label(vehicles),
            "company": first[R_MFGNAME].strip() or None,
            "problem": f"{'; '.join(components) or 'unknown component'}: {defect}" if defect else "; ".join(components),
            "severity": "; ".join(severity),
            "priority": 3 if do_not_drive else 2 if park_outside else 1 if influenced == "ODI" else 0,
            "units": units,
            "url": f"https://www.nhtsa.gov/recalls?nhtsaId={campno}",
            "fields": {"vehicles": [list(v) for v in vehicles], "components": components,
                       "consequence": " ".join(first[R_CONSEQUENCE].split())},
        }


def _any_yes(rows: list[list[str]], col: int) -> bool:
    return any(len(r) > col and r[col].strip().upper() == "YES" for r in rows)


def component_top(component: str) -> str:
    """NHTSA's component codes are paths ("SERVICE BRAKES, HYDRAULIC:FOUNDATION COMPONENTS"); the top level
    is what complaints, recalls and investigations have in common."""
    return component.split(":", 1)[0].strip().upper()


def vehicles_label(vehicles, most: int = 3) -> str:
    """[(make, model, year), ...] -> "2023-2024 TESLA MODEL 3, 2024 TESLA MODEL Y and 2 more models"."""
    years: dict[tuple[str, str], list[int]] = {}
    for make, model, year in vehicles:
        years.setdefault((make, model), [])
        if year.isdigit() and year != "9999":
            years[(make, model)].append(int(year))
    names = []
    for (make, model), ys in years.items():
        span = "" if not ys else f"{min(ys)} " if min(ys) == max(ys) else f"{min(ys)}-{max(ys)} "
        names.append(f"{span}{make} {model}".strip())
    shown = ", ".join(names[:most])
    return shown + (f" and {len(names) - most} more models" if len(names) > most else "")


def _int(value: str) -> int:
    try:
        return int(value.strip() or 0)
    except ValueError:
        return 0


def _date(yyyymmdd: str) -> str:
    v = yyyymmdd.strip()
    return f"{v[0:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 else "1900-01-01"
