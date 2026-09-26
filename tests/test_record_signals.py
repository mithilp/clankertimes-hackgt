"""The records ingest: spikes in reports and official actions become signals, from SQLite, with no network."""

import io
import json
import zipfile
from datetime import date, timedelta

from newsroom import bossman, db, record_signals
from newsroom.signals import InMemorySignals, origin_key
from newsroom.sources import cpsc, fda_recalls, nhtsa

AS_OF = date(2026, 9, 23)


def day(days_before: int) -> str:
    return (AS_OF - timedelta(days=days_before)).isoformat()


def nhtsa_complaint(odino, received, components, *, model="MODEL 3", year="2024", severe=False,
                    text="My steering locked up. Call me at 404-555-0101 or owner@example.com."):
    return {"id": f"nhtsa:{odino}", "source": "nhtsa", "received": received, "product": f"{year} TESLA {model}",
            "company": "TESLA, INC.", "severe": severe, "text": text,
            "fields": {"odino": str(odino), "make": "TESLA", "model": model, "year": year, "components": components}}


def load_nhtsa(conn, *, recent_steering=6, baseline_steering=1):
    rows = [nhtsa_complaint(1, day(500), ["STEERING"])]    # old enough to give the dataset a year of history
    rows += [nhtsa_complaint(100 + n, day(10 + n), ["STEERING:ELECTRIC POWER ASSIST SYSTEM"], severe=n == 0)
             for n in range(recent_steering)]
    rows += [nhtsa_complaint(200 + n, day(200 + n), ["STEERING"]) for n in range(baseline_steering)]
    # Electrical: busy, but no busier than usual.
    rows += [nhtsa_complaint(300 + n, day(5 + n), ["ELECTRICAL SYSTEM"]) for n in range(5)]
    rows += [nhtsa_complaint(400 + n, day(100 + 15 * n), ["ELECTRICAL SYSTEM"]) for n in range(20)]
    rows.append(nhtsa_complaint(999, day(0), ["STEERING", "ELECTRICAL SYSTEM"]))   # counts in both groups
    db.save_complaints(conn, rows)


def signals_of(conn, dataset, **kw):
    return record_signals.build(conn, [dataset], **kw)[dataset]["signals"]


def test_a_spike_becomes_one_signal_with_exact_counts(conn):
    load_nhtsa(conn)
    [s] = signals_of(conn, "nhtsa_complaints")

    assert s.origin == "nhtsa_complaints: 2024 TESLA MODEL 3 / STEERING"
    assert s.spike["recent"] == 7 and s.spike["severe"] == 1        # 6 + the complaint listing two components
    assert "NHTSA received 7 owner complaints about 2024 TESLA MODEL 3 (STEERING) in the 90 days to 2026-09-23" in s.summary
    assert s.accountable_party == "TESLA, INC."
    assert s.last_seen == "2026-09-23T00:00:00+00:00"                # the data's date, not the run's
    assert s.source_types == ["gov", "nhtsa_complaints"]
    assert s.sources[0]["url"].startswith("https://api.nhtsa.gov/complaints/complaintsByVehicle?make=TESLA")
    assert not bossman.hard_checks(s)


def test_a_catch_all_component_is_not_a_problem(conn):
    load_nhtsa(conn)
    db.save_complaints(conn, [nhtsa_complaint(500 + n, day(4), ["UNKNOWN OR OTHER"]) for n in range(20)])
    assert [s.origin for s in signals_of(conn, "nhtsa_complaints")] == ["nhtsa_complaints: 2024 TESLA MODEL 3 / STEERING"]


def test_no_signal_without_enough_history(conn):
    db.save_complaints(conn, [nhtsa_complaint(n, day(n), ["STEERING"]) for n in range(10)])
    result = record_signals.build(conn, ["nhtsa_complaints"])["nhtsa_complaints"]
    assert result["signals"] == [] and "not enough history" in result["note"]


def test_the_cap_keeps_the_strongest(conn):
    load_nhtsa(conn)
    db.save_complaints(conn, [nhtsa_complaint(600 + n, day(3), ["AIR BAGS"], model="MODEL Y") for n in range(20)])
    [s] = signals_of(conn, "nhtsa_complaints", max_per_dataset=1)
    assert "AIR BAGS" in s.origin


def test_distinct_reports_are_counted_not_rows(conn):
    """FAERS stores one row per reaction; two rows of one report about the same reaction count once."""
    old = {"id": "faers:1:1", "source": "faers", "received": day(500), "product": "OZEMPIC", "company": "Novo Nordisk",
           "severe": True, "text": "", "claim": "nausea", "fields": {"person": "faers:1"}}
    rows = [old]
    for n in range(5):
        for copy in (1, 2):
            rows.append({**old, "id": f"faers:{10 + n}:{copy}", "received": day(n), "fields": {"person": f"faers:{10 + n}"}})
    db.save_complaints(conn, rows)
    [s] = signals_of(conn, "faers")
    assert s.spike["recent"] == 5
    assert s.origin == "faers: OZEMPIC / nausea"


def test_redacted_products_never_become_signals(conn):
    rows = [{"id": f"caers:{n}:1", "source": "caers", "received": day(n if n else 500), "product": "EXEMPTION 4",
             "company": None, "severe": False, "text": "", "claim": "vomiting", "fields": {"person": f"caers:{n}"}}
            for n in range(10)]
    db.save_complaints(conn, rows)
    assert signals_of(conn, "caers") == []


def test_rerunning_merges_into_the_same_signals(conn):
    load_nhtsa(conn)
    store = InMemorySignals()
    for s in signals_of(conn, "nhtsa_complaints"):
        assert store.add(s, near=False)[1]
    db.save_complaints(conn, [nhtsa_complaint(700 + n, day(1), ["STEERING"]) for n in range(3)])
    for s in signals_of(conn, "nhtsa_complaints"):
        assert not store.add(s, near=False)[1]
    [s] = store.recent("")
    assert s.spike["recent"] == 10                                    # the newest numbers
    assert len(s.sources) == 1


def test_two_problems_for_one_company_stay_separate(conn):
    """Templated texts about one company differ by a few words; they must not merge."""
    load_nhtsa(conn)
    db.save_complaints(conn, [nhtsa_complaint(800 + n, day(2), ["ELECTRICAL SYSTEM"], model="MODEL Y")
                              for n in range(8)] + [nhtsa_complaint(900, day(400), ["ELECTRICAL SYSTEM"], model="MODEL Y")])
    built = signals_of(conn, "nhtsa_complaints")
    assert len(built) == 2 and len({origin_key(s.origin) for s in built}) == 2
    store = InMemorySignals()
    assert all(store.add(s, near=False)[1] for s in built)
    assert len(store.recent("")) == 2


def test_no_narrative_or_contact_details_reach_a_signal(conn):
    load_nhtsa(conn)
    for s in signals_of(conn, "nhtsa_complaints"):
        stored = json.dumps(s.to_dict())
        for private in ("404-555-0101", "owner@example.com", "steering locked up"):
            assert private not in stored


def test_complaint_signal_names_the_investigation_and_recall_covering_it(conn):
    load_nhtsa(conn)
    conn.execute("insert into investigations values ('PE26012', 'TESLA, INC.', 'STEERING:COLUMN', '2026-08-01', null, null,"
                 " 'Loss of steering', 'summary')")
    conn.execute("insert into investigation_vehicles values ('PE26012', 'TESLA', 'MODEL 3', '2024')")
    db.save_recalls(conn, [nhtsa_recall_row("26V100000", "2026-09-01", components=["ELECTRICAL SYSTEM"])])
    [s] = signals_of(conn, "nhtsa_complaints")
    assert any("PE26012 (open)" in t for t in s.records_trail)
    assert not any("26V100000" in t for t in s.records_trail)          # a different component


def test_complaint_signal_says_when_nothing_covers_it(conn):
    load_nhtsa(conn)
    conn.execute("insert into investigations values ('PE20001', 'TESLA, INC.', 'AIR BAGS', '2020-01-01', '2021-01-01',"
                 " null, 's', 's')")
    db.save_recalls(conn, [nhtsa_recall_row("26V100000", "2026-09-01", components=["ELECTRICAL SYSTEM"])])
    [s] = signals_of(conn, "nhtsa_complaints")
    assert "No NHTSA recall or investigation on file covers this component" in s.why_interesting


def nhtsa_recall_row(campno, filed, *, components=("STEERING",), priority=0, units=100):
    return {"id": f"nhtsa_recalls:{campno}", "dataset": "nhtsa_recalls", "date": filed, "label": f"NHTSA recall {campno}",
            "product": "2024 TESLA MODEL 3", "company": "Tesla, Inc.", "problem": f"{components[0]}: it fails",
            "severity": "", "priority": priority, "units": units, "url": f"https://www.nhtsa.gov/recalls?nhtsaId={campno}",
            "fields": {"vehicles": [["TESLA", "MODEL 3", "2024"]], "components": list(components)}}


def test_recent_actions_become_signals_ranked_by_priority(conn):
    load_nhtsa(conn)
    db.save_recalls(conn, [nhtsa_recall_row("26V1", "2026-09-10", units=5000),
                           nhtsa_recall_row("26V2", "2026-09-01", priority=3, units=10),
                           nhtsa_recall_row("25V9", "2026-01-01")])                        # too old
    built = signals_of(conn, "nhtsa_recalls")
    assert [s.origin for s in built] == ["nhtsa_recalls:26V2", "nhtsa_recalls:26V1"]
    assert built[0].last_seen == "2026-09-01T00:00:00+00:00"
    # Steering complaints about the 2024 Model 3 received before Sept. 1: the old one and the baseline one.
    assert "holds 2 complaints about these vehicles and components received before" in built[0].why_interesting
    assert all(not bossman.hard_checks(s) for s in built)
    db.save_recalls(conn, [nhtsa_recall_row("26V4", "2026-09-05", components=["AIR BAGS"], priority=5)])
    assert "complaint file" not in signals_of(conn, "nhtsa_recalls")[0].why_interesting   # none: nothing said


def test_investigations_become_signals_with_distinct_origins(conn):
    conn.execute("insert into investigations values ('EA26001', 'Ford', 'BRAKES', '2026-09-01', null, null, 'Brake loss', 's')")
    conn.execute("insert into investigations values ('PE26002', 'Ford', 'AIR BAGS', '2026-08-01', '2026-09-15', '26V3', 'Air bag', 's')")
    conn.execute("insert into investigation_vehicles values ('EA26001', 'FORD', 'F-150', '2025')")
    built = signals_of(conn, "nhtsa_investigations")
    assert {s.origin for s in built} == {"nhtsa_investigations:EA26001", "nhtsa_investigations:PE26002"}
    assert len({origin_key(s.origin) for s in built}) == 2
    ea = next(s for s in built if "EA26001" in s.origin)
    assert "engineering analysis" in ea.why_interesting and "still open" in ea.why_interesting
    assert "2025 FORD F-150" in ea.summary
    pe = next(s for s in built if "PE26002" in s.origin)
    assert "NHTSA recall 26V3" in pe.records_trail


def test_missing_datasets_say_how_to_load_them(conn):
    result = record_signals.build(conn, ["osha_injuries", "cpsc_recalls"])
    assert "ingest-osha" in result["osha_injuries"]["note"]
    assert "ingest-recalls" in result["cpsc_recalls"]["note"]


def test_nhtsa_recall_file_rolls_rows_up_per_campaign(tmp_path):
    def line(model, year, comp, dnd="No"):
        p = [""] * 29
        p[1], p[2], p[3], p[4], p[6], p[7] = "26V534000", "TEREX", model, year, comp, "Terex USA"
        p[11], p[13], p[15], p[19], p[20], p[27], p[28] = "73", "ODI", "20260817", "The boom may crack.", "Crash risk.", dnd, "No"
        return "\t".join(p)
    old = [""] * 29
    old[1], old[15], old[4] = "19V1000", "20190101", "2019"
    path = tmp_path / "FLAT_RCL_POST_2010.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("FLAT_RCL_POST_2010.txt", "\n".join([
            line("C4000", "2021", "EQUIPMENT:OTHER"), line("C4000", "2022", "EQUIPMENT"),
            line("C5000", "2024", "STRUCTURE", dnd="Yes"), "\t".join(old)]))
    [r] = list(nhtsa.parse_recalls(path, since="2025-01-01"))

    assert r["id"] == "nhtsa_recalls:26V534000" and r["date"] == "2026-08-17"
    assert r["units"] == 73                                            # repeated per row, not summed
    assert r["priority"] == 3 and "do not drive" in r["severity"]
    assert r["product"] == "2021-2022 TEREX C4000, 2024 TEREX C5000"
    assert r["fields"]["components"] == ["EQUIPMENT", "STRUCTURE"]
    assert r["url"] == "https://www.nhtsa.gov/recalls?nhtsaId=26V534000"


def test_fda_recalls_roll_up_by_event():
    base = {"event_id": "99646", "recalling_firm": "Prince Bakery Inc", "classification": "Class II",
            "report_date": "20260909", "reason_for_recall": "Sesame is not declared.", "address_1": "2418 Belmont Ave"}
    rows = fda_recalls.to_recalls("food/enforcement", [
        {**base, "recall_number": "H-1305-2026", "product_description": "Italian bread"},
        {**base, "recall_number": "H-1306-2026", "product_description": "French bread"}])
    [r] = rows
    assert r["id"] == "fda_recalls:99646" and r["units"] == 2 and r["priority"] == 1
    assert r["product"] == "Italian bread and 1 more products" and r["date"] == "2026-09-09"
    assert "2418 Belmont" not in json.dumps(r)


def test_cpsc_recall_names_the_company_not_the_storefront():
    r = cpsc.to_recall({
        "RecallNumber": "26789", "RecallDate": "2026-09-24T00:00:00", "Title": "5Color Recalls Helmets",
        "URL": "https://cpsc.gov/Recalls/2026/5Color", "ConsumerContact": "call 833-382-6461 or FiveColorCS@163.com",
        "Products": [{"Name": "Bike Helmet Set", "NumberOfUnits": "About 1.2 million"}],
        "Injuries": [{"Name": "None reported"}], "Hazards": [{"Name": "The helmets can fail to protect."}],
        "Manufacturers": [], "Retailers": [{"Name": "Sold Online At:\nAmazon.com"}, {"Name": "Hengqin Co., dba. 5Color"}]})
    assert r["company"] == "Hengqin Co., dba. 5Color"
    assert r["units"] == 1_200_000 and r["priority"] == 0 and r["date"] == "2026-09-24"
    assert "833-382-6461" not in json.dumps(r)


def test_run_saves_a_dry_run_and_writes_on_a_real_one(conn, tmp_path):
    load_nhtsa(conn)
    report = record_signals.run(conn, None, ["nhtsa_complaints"], out_dir=tmp_path / "dry")
    assert report["nhtsa_complaints"]["built"] == 1 and report["nhtsa_complaints"]["created"] == 0
    assert (tmp_path / "dry" / "signals.json").exists()
    store = InMemorySignals()
    assert record_signals.run(conn, store, ["nhtsa_complaints"])["nhtsa_complaints"]["created"] == 1
    assert record_signals.run(conn, store, ["nhtsa_complaints"])["nhtsa_complaints"]["merged"] == 1
