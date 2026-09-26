"""The records ingest: what is moving in the official safety datasets, written to the signals store.

Bossman finds signals on the live web; this finds them in the government records the newsroom already
loads into SQLite, so both meet in one store and McLovin can connect them. Plain code, no model calls.

Two kinds of signal, each built one way for every dataset:
  * a spike in reports: for NHTSA complaints, OSHA severe injuries, FDA FAERS, CAERS and MAUDE, one signal
    per (product, problem) whose distinct reports in the latest 90 days jump above its own baseline;
  * an official action: one signal per NHTSA recall, NHTSA investigation, FDA recall event or CPSC recall
    dated in the latest 90 days.

Individual reports never become signals and never leave SQLite. Signals carry coded fields and agency text
only, never a report's narrative. Origins are exact keys ("nhtsa_complaints: 2024 TESLA MODEL 3 / STEERING"),
so a rerun merges into the same signals, and the store is asked to match on origin only (add(near=False)):
templated texts about one company differ by a few words, and word overlap would merge different claims.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from . import bossman
from .signals import Signal, SignalStore, now
from .sources import nhtsa

WINDOW = 90            # days: "recent" is the 90 days ending at the dataset's newest report
BASELINE = 360         # days before that window; its count / (BASELINE / WINDOW) is the usual 90-day level
MIN_RECENT = 5         # a spike needs at least this many distinct reports in the recent window
SPIKE_RATIO = 2.0      # ...and at least this multiple of the baseline (a baseline under 1 counts as 1)
MAX_PER_DATASET = 50   # signals per dataset per run, strongest first

# FDA's redaction for confidential product names, and other names that name nothing.
NO_PRODUCT = {"EXEMPTION 4", "UNKNOWN", "UNKNOWN PRODUCT", "UNKNOWN DRUG", "UNKNOWN DEVICE", "NONE", ""}
# Problem codes that say nothing about what went wrong (NHTSA's catch-all component; MAUDE's).
NO_PROBLEM = {"unknown or other", "adverse event without identified device or use problem", "appropriate term/code not available",
              "insufficient information", "no known device problem", "unknown (for use when the device problem is not known)"}


@dataclass(frozen=True)
class Reports:
    """A report dataset in the complaints table. Only `problems` and `url` differ between datasets."""
    slug: str
    agency: str
    noun: str                 # what one report is, plural
    database: str
    severe: str               # what the source's "severe" flag means
    caveat: str
    problems: Callable[[dict, str | None], list[str]]    # (fields, coded claim) -> the problems it reports
    url: Callable[[str, str, dict], str]                 # (product, problem, fields) -> a page a reporter can open


def _nhtsa_problems(fields, claim):
    return sorted(c for c in {nhtsa.component_top(c) for c in fields.get("components", [])} if c.lower() not in NO_PROBLEM | {""})


def _nhtsa_url(product, problem, f):
    query = urlencode({"make": f.get("make", ""), "model": f.get("model", ""), "modelYear": f.get("year", "")})
    return f"https://api.nhtsa.gov/complaints/complaintsByVehicle?{query}"


def _openfda_url(endpoint, *terms):
    return f"https://api.fda.gov/{endpoint}.json?" + urlencode({"search": " AND ".join(terms), "limit": 100})


REPORTS = {
    "nhtsa": Reports(
        "nhtsa_complaints", "NHTSA", "owner complaints", "vehicle complaint database",
        "a crash, fire, injury or death",
        "Complaints are owners' unverified reports; a jump can follow news coverage or a recall notice "
        "rather than a new defect.",
        _nhtsa_problems, _nhtsa_url),
    "osha": Reports(
        "osha_injuries", "OSHA", "severe injury reports", "Severe Injury Reports",
        "a hospitalization, amputation or eye loss",
        "Employers file these themselves, and the file covers federal-OSHA states only (not state-plan "
        "states such as California or Washington), so a missing employer proves nothing.",
        lambda fields, claim: [claim] if claim else [],
        lambda product, problem, f: "https://www.osha.gov/severe-injury-reports"),
    "faers": Reports(
        "faers", "FDA", "serious adverse-event reports", "FAERS database", "a serious outcome",
        "A report doesn't establish that the drug caused the reaction, and there is no denominator; spikes "
        "often follow litigation, news or a manufacturer filing a batch of literature cases.",
        lambda fields, claim: [claim] if claim else [],
        lambda product, problem, f: _openfda_url("drug/event", f'patient.drug.openfda.brand_name:"{product}"',
                                                 f'patient.reaction.reactionmeddrapt:"{problem}"')),
    "caers": Reports(
        "caers", "FDA", "adverse-event reports", "CAERS database (foods, supplements, cosmetics)", "a serious outcome",
        "A report doesn't establish that the product caused the reaction, and there is no denominator.",
        lambda fields, claim: [claim] if claim else [],
        lambda product, problem, f: _openfda_url("food/event", f'products.name_brand:"{product}"',
                                                 f'reactions:"{problem}"')),
    "maude": Reports(
        "maude", "FDA", "device reports", "MAUDE database", "a death or injury",
        "Most MAUDE reports are filed by manufacturers, one report can summarize many events, and a jump can "
        "be a filing backlog rather than new events.",
        lambda fields, claim: [p for p in fields.get("product_problems") or [] if p.strip().lower() not in NO_PROBLEM],
        lambda product, problem, f: _openfda_url("device/event",
                                                 f'device.device_report_product_code:"{f.get("product_code", "")}"',
                                                 f'product_problems:"{problem}"')),
}

ACTIONS = {  # official-action datasets: where they are stored, and what one is
    "nhtsa_recalls": "A recall filed with NHTSA",
    "nhtsa_investigations": "An NHTSA defect investigation",
    "fda_recalls": "An FDA recall",
    "cpsc_recalls": "A CPSC recall",
}
DATASETS = [r.slug for r in REPORTS.values()] + list(ACTIONS)

INVESTIGATION_TYPES = {"PE": "preliminary evaluation", "EA": "engineering analysis", "RQ": "recall query",
                       "DP": "defect petition", "AQ": "audit query"}
INVESTIGATIONS_URL = nhtsa.INVESTIGATIONS_URL


@dataclass
class _Group:
    recent: set = field(default_factory=set)
    baseline: set = field(default_factory=set)
    severe: set = field(default_factory=set)
    companies: Counter = field(default_factory=Counter)
    last: str = ""
    fields: dict = field(default_factory=dict)


# --- building ---------------------------------------------------------------------------------------

def build(conn: sqlite3.Connection, datasets: list[str] | None = None, *,
          max_per_dataset: int = MAX_PER_DATASET) -> dict[str, dict]:
    """Signals for each dataset: {slug: {"signals": [...], "note": "why there are none", "as_of": ...}}."""
    links = _Links(conn)
    out = {}
    for slug in datasets or DATASETS:
        source = next((s for s, r in REPORTS.items() if r.slug == slug), None)
        if source:
            out[slug] = report_signals(conn, source, links, max_per_dataset)
        elif slug in ACTIONS:
            out[slug] = action_signals(conn, slug, links, max_per_dataset)
        else:
            raise ValueError(f"unknown dataset {slug!r}; known: {', '.join(DATASETS)}")
    return out


def report_signals(conn: sqlite3.Connection, source: str, links: _Links, max_signals: int) -> dict:
    spec = REPORTS[source]
    first, last = conn.execute("select min(received), max(received) from complaints where source = ? and received > '1901'",
                               (source,)).fetchone()
    if last is None:
        return {"signals": [], "note": f"no {source} reports in SQLite: run ingest-{source}", "as_of": None}
    as_of = date.fromisoformat(last)
    recent_start = (as_of - timedelta(days=WINDOW)).isoformat()      # recent: after this, up to as_of
    baseline_start = (as_of - timedelta(days=WINDOW + BASELINE)).isoformat()
    if first > baseline_start:
        return {"signals": [], "as_of": last,
                "note": f"not enough history: {source} reports in SQLite start {first}; a baseline needs them from "
                        f"{baseline_start} (ingest a longer range)"}
    rows = conn.execute(
        """select c.id, c.received, c.product, c.company, c.severe, c.fields, k.claim
           from complaints c left join claims k on k.complaint_id = c.id and k.coded = 1
           where c.source = ? and c.received > ?""", (source, baseline_start)).fetchall()
    groups: dict[tuple[str, str], _Group] = defaultdict(_Group)
    for row in rows:
        if row["product"].strip().upper() in NO_PRODUCT:
            continue
        fields = json.loads(row["fields"])
        report = fields.get("person") or row["id"]     # FAERS and CAERS store one row per reaction
        for problem in spec.problems(fields, row["claim"]):
            g = groups[(row["product"], problem)]
            if row["received"] > recent_start:
                g.recent.add(report)
                g.last = max(g.last, row["received"])
                if row["severe"]:
                    g.severe.add(report)
            else:
                g.baseline.add(report)
            if row["company"]:
                g.companies[row["company"]] += 1
            g.fields = g.fields or fields
    spikes = []
    for (product, problem), g in groups.items():
        usual = len(g.baseline) / (BASELINE / WINDOW)
        if len(g.recent) >= MIN_RECENT and len(g.recent) >= SPIKE_RATIO * max(usual, 1):
            spikes.append((len(g.recent) - usual, len(g.severe), product, problem, g))
    spikes.sort(key=lambda s: (s[0], s[1]), reverse=True)
    signals = [_spike_signal(spec, product, problem, g, as_of, links) for _, _, product, problem, g in spikes[:max_signals]]
    return {"signals": signals, "as_of": last,
            "note": f"{len(groups)} groups, {len(spikes)} spiking" + (f", kept the top {max_signals}" if len(spikes) > max_signals else "")}


def _spike_signal(spec: Reports, product: str, problem: str, g: _Group, as_of: date, links: _Links) -> Signal:
    n, before = len(g.recent), len(g.baseline)
    usual = before / (BASELINE / WINDOW)
    first_day = (as_of - timedelta(days=WINDOW - 1)).isoformat()
    level = f"about {usual:.0f} per 90 days in the year before" if before else "none in the year before"
    company = g.companies.most_common(1)[0][0] if g.companies else ""
    url = spec.url(product, problem, g.fields)
    trail, coverage = [f"{spec.agency} {spec.database}: {product}, {problem} ({url})"], ""
    if spec.slug == "nhtsa_complaints":
        found, coverage = links.nhtsa_coverage(g.fields, problem)
        trail += found
    ratio = f"{n / max(usual, 1):.1f} times its usual level" if before else "with none in the year before"
    return Signal(
        summary=f"{spec.agency} received {n} {spec.noun} about {product} ({problem}) in the 90 days to "
                f"{as_of.isoformat()}, against {level}.",
        why_interesting=f"A jump against its own baseline: {n} reports in the latest 90 days, {ratio}. "
                        f"{len(g.severe)} of the {n} report {spec.severe}. {spec.caveat}{coverage}",
        checkable_claim=f"{spec.agency}'s {spec.database} holds {n} {spec.noun} about {product} ({problem}) received "
                        f"{first_day} to {as_of.isoformat()}, and {before} in the {BASELINE} days before that.",
        origin=f"{spec.slug}: {product} / {problem}",
        accountable_party=company,
        records_trail=trail,
        sources=[{"url": url, "seen_at": now(), "dataset": spec.slug, "as_of": as_of.isoformat()}],
        spike={"kind": "count_vs_baseline",
               "value": f"{n} in the 90 days to {as_of.isoformat()}, against {level}",
               "recent": n, "baseline_per_90_days": round(usual, 1), "severe": len(g.severe),
               "as_of": as_of.isoformat()},
        source_types=["gov", spec.slug],
        last_seen=f"{g.last}T00:00:00+00:00",
    )


def action_signals(conn: sqlite3.Connection, slug: str, links: _Links, max_signals: int) -> dict:
    actions = _investigations(conn) if slug == "nhtsa_investigations" else [
        {**dict(r), "fields": json.loads(r["fields"])}
        for r in conn.execute("select * from recalls where dataset = ?", (slug,))]
    actions = [a for a in actions if a["date"] > "1901"]
    if not actions:
        command = "ingest-investigations" if slug == "nhtsa_investigations" else "ingest-recalls"
        return {"signals": [], "note": f"no {slug} in SQLite: run {command}", "as_of": None}
    as_of = max(a["date"] for a in actions)
    since = (date.fromisoformat(as_of) - timedelta(days=WINDOW)).isoformat()
    recent = sorted((a for a in actions if a["date"] > since),
                    key=lambda a: (a["priority"], a["units"], a["date"]), reverse=True)
    signals = [_action_signal(slug, a, as_of, links) for a in recent[:max_signals]]
    return {"signals": signals, "as_of": as_of,
            "note": f"{len(recent)} dated in the 90 days to {as_of}"
                    + (f", kept the top {max_signals}" if len(recent) > max_signals else "")}


def _action_signal(slug: str, a: dict, as_of: str, links: _Links) -> Signal:
    company = a.get("company") or ""
    problem = a["problem"].rstrip(".")
    trail, note = [f"{a['label']} ({a['url']})"], ""
    if slug == "nhtsa_recalls":
        note = links.complaints_before(a)
    elif slug == "nhtsa_investigations" and a["fields"].get("recall"):
        trail.append(f"NHTSA recall {a['fields']['recall']}")
    return Signal(
        summary=f"{a['label']}, {a['date']}: {company or 'no company named'}, covering {a['product']}. {problem}.",
        why_interesting=f"{ACTIONS[slug]}, dated in the 90 days to {as_of}"
                        + (f": {a['severity']}." if a.get("severity") else ".") + note,
        checkable_claim=f"{a['label']}, dated {a['date']}, names {company or 'no company'} and covers {a['product']} "
                        f"for this problem: {problem}.",
        origin=a["id"],
        accountable_party=company,
        records_trail=trail,
        sources=[{"url": a["url"], "seen_at": now(), "dataset": slug, "as_of": as_of}],
        spike={"kind": "official_action",
               "value": f"{a['label']}, {a['date']}" + (f"; {a['severity']}" if a.get("severity") else ""),
               "date": a["date"], "priority": a["priority"], "units": a["units"]},
        source_types=["gov", slug],
        last_seen=f"{a['date']}T00:00:00+00:00",
    )


def _investigations(conn: sqlite3.Connection) -> list[dict]:
    """NHTSA investigations in the recalls table's shape, dated by their latest event (opened or closed)."""
    vehicles: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for v in conn.execute("select action, make, model, year from investigation_vehicles"):
        vehicles[v["action"]].append((v["make"], v["model"], v["year"]))
    out = []
    for r in conn.execute("select * from investigations"):
        kind = INVESTIGATION_TYPES.get(r["action"][:2].upper(), "investigation")
        opened, closed = r["opened"] or "", r["closed"] or ""
        state = f"closed {closed}" if closed else "still open"
        out.append({
            "id": f"nhtsa_investigations:{r['action']}",
            "date": max(opened, closed),
            "label": f"NHTSA investigation {r['action']}",
            "product": nhtsa.vehicles_label(sorted(vehicles[r["action"]])) or "vehicles not listed",
            "company": r["company"],
            "problem": " ".join(f"{nhtsa.component_top(r['component'] or '')}: {r['subject'] or ''}".split()).strip(": "),
            "severity": f"{kind}, opened {opened}, {state}" + (f", led to recall {r['recall']}" if r["recall"] else ""),
            "priority": 2 if kind in ("engineering analysis", "recall query") else 1 if kind != "audit query" else 0,
            "units": len(vehicles[r["action"]]),
            "url": INVESTIGATIONS_URL,
            "fields": {"recall": r["recall"]},
        })
    return out


class _Links:
    """What code can say about how the NHTSA datasets relate: which recalls and investigations cover a
    complaint group, and how many complaints preceded a recall. Loaded on first use."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._coverage: dict | None = None
        self._complaints: dict | None = None
        self._loaded: list[int] | None = None

    def nhtsa_coverage(self, fields: dict, component: str) -> tuple[list[str], str]:
        if self._coverage is None:
            self._coverage = defaultdict(list)
            for r in self.conn.execute(
                    """select v.make, v.model, v.year, i.action, i.component, i.closed
                       from investigation_vehicles v join investigations i on i.action = v.action"""):
                state = f"closed {r['closed']}" if r["closed"] else "open"
                self._coverage[(r["make"], r["model"], r["year"], nhtsa.component_top(r["component"] or ""))].append(
                    f"NHTSA investigation {r['action']} ({state})")
            for r in self.conn.execute("select label, date, fields from recalls where dataset = 'nhtsa_recalls'"):
                f = json.loads(r["fields"])
                for make, model, year in f.get("vehicles", []):
                    for c in f.get("components", []):
                        self._coverage[(make, model, year, c)].append(f"{r['label']} (filed {r['date']})")
        if self._loaded is None:
            self._loaded = [self.conn.execute(q).fetchone()[0] for q in (
                "select count(*) from investigations", "select count(*) from recalls where dataset = 'nhtsa_recalls'")]
        loaded = self._loaded
        key = (fields.get("make", "").upper(), fields.get("model", "").upper(), fields.get("year", ""), component)
        found = sorted(set(self._coverage.get(key, [])))
        if found:
            return found, ""
        if not all(loaded):
            return [], (" Whether a recall or investigation covers it is unchecked: NHTSA's "
                        f"{'investigations' if not loaded[0] else 'recalls'} aren't loaded.")
        return [], " No NHTSA recall or investigation on file covers this component for this model-year."

    def complaints_before(self, recall: dict) -> str:
        if self._complaints is None:
            self._complaints = defaultdict(list)
            for r in self.conn.execute("select received, fields from complaints where source = 'nhtsa'"):
                f = json.loads(r["fields"])
                for c in f.get("components", []):
                    self._complaints[(f.get("make", "").upper(), f.get("model", "").upper(), f.get("year", ""),
                                      nhtsa.component_top(c))].append(r["received"])
        f = recall["fields"]
        n = sum(1 for make, model, year in f.get("vehicles", []) for c in f.get("components", [])
                for received in self._complaints.get((make, model, year, c), []) if received < recall["date"])
        # None proves little: the loaded file may start after the problem began, and equipment isn't in it.
        return (f" NHTSA's complaint file holds {n} complaints about these vehicles and components received before "
                "it was filed." if n else "")


# --- writing ----------------------------------------------------------------------------------------

def run(conn: sqlite3.Connection, store: SignalStore | None, datasets: list[str] | None = None, *,
        max_per_dataset: int = MAX_PER_DATASET, out_dir: Path | None = None) -> dict:
    """Build, check and (unless store is None: a dry run) add the signals. Saves them to out_dir."""
    built = build(conn, datasets, max_per_dataset=max_per_dataset)
    report = {}
    for slug, result in built.items():
        entry = {"as_of": result["as_of"], "note": result["note"], "built": len(result["signals"]),
                 "created": 0, "merged": 0, "failed_checks": []}
        for signal in result["signals"]:
            if problems := bossman.hard_checks(signal):
                entry["failed_checks"].append({"summary": signal.summary, "problems": problems})
                continue
            if store is not None:
                _, created = store.add(signal, near=False)
                entry["created" if created else "merged"] += 1
        report[slug] = entry
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "signals.json").write_text(
            json.dumps({slug: [s.to_dict() for s in r["signals"]] for slug, r in built.items()}, indent=1,
                       ensure_ascii=False), encoding="utf-8")
        (out_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report
