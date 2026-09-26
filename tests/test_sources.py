"""The coded sources (OSHA, FAERS, CAERS) and Bluesky: parsing, privacy, and claims without model tokens."""

import csv
import io
import zipfile

import pytest

from newsroom import count, db, llm
from newsroom.sources import bluesky, caers, faers, osha

OSHA_COLUMNS = ["ID", "UPA", "EventDate", "Employer", "Address1", "Address2", "City", "State", "Zip", "Latitude",
                "Longitude", "Primary NAICS", "Hospitalized", "Amputation", "Loss of Eye", "Inspection",
                "Final Narrative", "Nature", "NatureTitle", "Part of Body", "Part of Body Title", "Event", "EventTitle",
                "Source", "SourceTitle", "Secondary Source", "Secondary Source Title", "FederalState"]


def osha_zip(path, rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=OSHA_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: "" for c in OSHA_COLUMNS} | row)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("January2015toNovember2025.csv", out.getvalue())
    return path


def osha_row(id, employer, amputation="0.00", event="Caught, entangled in running powered equipment  normal operation"):
    return {"ID": id, "EventDate": "11/30/2025", "Employer": employer, "Address1": "12 Mill Rd", "City": "Springfield",
            "Zip": "12345", "Latitude": "40.1", "State": "TEXAS", "Hospitalized": "1.00", "Amputation": amputation,
            "Final Narrative": "An employee's finger was caught in a baler.", "EventTitle": event}


def test_osha_reports_become_coded_claims_per_employer(tmp_path):
    rows = [osha_row("1", "U.S. Postal Service", amputation="1.00"), osha_row("2", "USPS"),
            osha_row("3", "United States Postal Service, Inc."), osha_row("4", "Acme Plastics LLC")]
    reports = {r["id"]: r for r in osha.parse(osha_zip(tmp_path / "sir.zip", rows))}

    assert {r["product"] for r in reports.values()} == {"U.S. Postal Service", "Acme Plastics LLC"}
    assert reports["osha:1"]["claim"] == "amputation: caught, entangled in running powered equipment - normal operation"
    assert reports["osha:2"]["claim"].startswith("hospitalization: ")
    assert reports["osha:1"]["received"] == "2025-11-30"
    stored = repr(reports)
    for private in ("12 Mill Rd", "Springfield", "12345", "40.1"):
        assert private not in stored


def test_faers_report_becomes_one_coded_claim_per_reaction():
    report = {"safetyreportid": "100", "receivedate": "20260119", "serious": "1", "seriousnessdeath": "2",
              "patient": {"drug": [{"drugcharacterization": "2", "medicinalproduct": "ASPIRIN"},
                                   {"drugcharacterization": "1", "medicinalproduct": "LUCENTIS",
                                    "openfda": {"brand_name": ["Lucentis"], "manufacturer_name": ["Genentech, Inc."]}}],
                          "reaction": [{"reactionmeddrapt": "Endophthalmitis"}, {"reactionmeddrapt": "Sudden visual loss"}]}}
    complaints = faers.to_complaints(report)

    assert [c["claim"] for c in complaints] == ["endophthalmitis", "sudden visual loss"]
    assert {c["product"] for c in complaints} == {"LUCENTIS"}          # the suspect drug, not the concomitant one
    assert {c["company"] for c in complaints} == {"Genentech, Inc."}
    assert {c["fields"]["person"] for c in complaints} == {"faers:100"}
    assert all(c["severe"] for c in complaints)


def test_caers_report_becomes_one_coded_claim_per_reaction():
    report = {"report_number": "2025-CFS-1", "date_created": "20250701", "outcomes": ["Hospitalization"],
              "products": [{"role": "CONCOMITANT", "name_brand": "Other"}, {"role": "SUSPECT", "name_brand": "Nutritears"}],
              "reactions": ["Epilepsy", "Seizure"]}
    complaints = caers.to_complaints(report)
    assert [(c["product"], c["claim"], c["severe"]) for c in complaints] == [("NUTRITEARS", "epilepsy", True),
                                                                            ("NUTRITEARS", "seizure", True)]


def test_coded_claims_are_grouped_without_the_model(conn, monkeypatch):
    def no_model(*args, **kwargs):
        raise AssertionError("coded claims must not call the model")
    monkeypatch.setattr(llm, "ask_json", no_model)
    reports = [{"safetyreportid": str(i), "receivedate": "20260110", "serious": "1",
                "patient": {"drug": [{"drugcharacterization": "1", "medicinalproduct": "DRUGX"}],
                            "reaction": [{"reactionmeddrapt": "Pancreatitis"}]}} for i in range(5)]
    # Report 0 arrives twice (a duplicate version): it must still count once.
    db.save_complaints(conn, [c for r in reports + reports[:1] for c in faers.to_complaints(r)])
    count.count(conn)
    [group] = count.top(conn, 5)
    assert (group["product"], group["label"], group["total"]) == ("DRUGX", "pancreatitis", 5)


def test_one_person_counts_once_in_a_group():
    marks = count.distinct([{"id": "a", "text": "", "person": "p1"}, {"id": "b", "text": "", "person": "p1"},
                            {"id": "c", "text": "", "person": "p2"}])
    assert [m["counted"] for m in marks] == [True, False, True]
    assert marks[1]["duplicate_of"] == "a"


def post(n, did="did:plc:alice", text="My 2023 Hyundai Tucson stalled on the freeway today, second time."):
    return {"uri": f"at://{did}/app.bsky.feed.post/rkey{n}", "author": {"did": did, "handle": "alice.bsky.social"},
            "record": {"text": text, "createdAt": "2026-09-20T12:00:00Z"}, "indexedAt": "2026-09-20T12:00:01Z"}


@pytest.fixture
def fake_bluesky(monkeypatch):
    posts = [post(1), post(2, did="did:plc:bob"), post(3, did="did:plc:carol", text="lol what a week")]
    monkeypatch.setattr(bluesky, "search", lambda query, since, limit: iter(posts))

    def reader(system, user, **kwargs):
        ids = [line.strip("[]") for line in user.splitlines() if line.startswith("[bluesky:")]
        return {"posts": [{"id": ids[0], "product": "2023 Hyundai Tucson", "company": "Hyundai",
                           "claim": "engine stalls on freeway", "severe": False},
                          {"id": ids[1], "product": "2023 Hyundai Tucson", "company": "Hyundai",
                           "claim": "engine stalls on freeway", "severe": False},
                          {"id": ids[2], "product": None, "company": None, "claim": None}]}
    monkeypatch.setattr(llm, "ask_json", reader)


def test_bluesky_keeps_product_problems_and_never_the_poster(conn, fake_bluesky):
    from datetime import date
    read, stored = bluesky.ingest(conn, ["stalled"], since=date(2026, 9, 1), limit=100)
    assert (read, stored) == (3, 2)

    rows = conn.execute("select c.id, c.product, c.fields, k.claim, k.coded from complaints c "
                        "join claims k on k.complaint_id = c.id order by c.id").fetchall()
    assert {r["product"] for r in rows} == {"2023 HYUNDAI TUCSON"}
    assert all(r["coded"] == 0 for r in rows)  # model-written: grouped by the model like NHTSA claims
    everything = repr([tuple(r) for r in conn.execute("select * from complaints")])
    for private in ("alice", "did:plc:", "bob"):
        assert private not in everything

    # Searching again stores nothing new.
    assert bluesky.ingest(conn, ["stalled"], since=date(2026, 9, 1), limit=100) == (0, 0)
