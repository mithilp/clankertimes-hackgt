"""Official-record lookups, with the APIs replaced by recorded responses."""

import json
import zipfile

import httpx

from newsroom import db, llm, records, scout, web
from newsroom.sources import nhtsa
from tests.conftest import complaint


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


RECALL_API = {"Count": 1, "results": [{
    "NHTSACampaignNumber": "14V047000", "ReportReceivedDate": "07/02/2014", "Manufacturer": "General Motors LLC",
    "Component": "ELECTRICAL SYSTEM:IGNITION", "Summary": "The ignition switch may move out of the run position.",
    "Consequence": "The engine may shut off.", "Remedy": "Dealers will replace the ignition switch."}]}


def test_nhtsa_recalls_become_quotable_records(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: calls.append(kw["params"]) or FakeResponse(RECALL_API))
    [record] = records.nhtsa_recalls("CHEVROLET", "COBALT", "2006")

    assert calls == [{"make": "CHEVROLET", "model": "COBALT", "modelYear": "2006"}]
    assert record["url"] == "https://www.nhtsa.gov/recalls?nhtsaId=14V047000"
    assert record["source_type"] == "government_record"
    assert "The ignition switch may move out of the run position." in record["text"]
    assert records.nhtsa_recalls("CHEVROLET", "COBALT", "9999") == []  # unknown year: no lookup


def test_nhtsa_investigations_are_loaded_once_and_found_by_vehicle(conn, tmp_path):
    rows = ["PE14001\tCHEVROLET\tCOBALT\t2006\tELECTRICAL\tGeneral Motors\t20140210\t20140301\t14V047000\tStalling\tEngine stalls.",
            "PE14001\tCHEVROLET\tCOBALT\t2007\tELECTRICAL\tGeneral Motors\t20140210\t20140301\t14V047000\tStalling\tEngine stalls.",
            "PE20002\tFORD\tFOCUS\t2014\tPOWER TRAIN\tFord\t20200101\t\t\tShudder\tTransmission shudders."]
    path = tmp_path / "inv.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("FLAT_INV.txt", "\r\n".join(rows) + "\r\n")

    assert nhtsa.save_investigations(conn, path) == (2, 3)
    [found] = records.nhtsa_investigations(conn, "Chevrolet", "Cobalt", "2006")
    assert found["title"] == "NHTSA investigation PE14001: Stalling"
    assert "Led to recall 14V047000" in found["text"] and "Engine stalls." in found["text"]


def test_a_failing_lookup_is_skipped(conn, monkeypatch):
    db.save_complaints(conn, [complaint("nhtsa:1")])
    conn.execute("insert into claim_groups (as_of, source, product, company, label, total, last_90, severe, members)"
                 " values ('2026-09-01', 'nhtsa', '2006 CHEVROLET COBALT', 'General Motors LLC', 'stalls', 1, 1, 0, ?)",
                 (json.dumps([{"id": "nhtsa:1", "counted": True, "duplicate_of": None}]),))
    conn.execute("insert into stories (created, source, product, company, label, counts, status)"
                 " values ('now', 'nhtsa', '2006 CHEVROLET COBALT', 'General Motors LLC', 'stalls',"
                 " '{\"group_id\": 1}', 'reporting')")
    story = conn.execute("select * from stories").fetchone()

    def down(url, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", down)
    assert records.for_story(conn, story) == []


def test_scouts_read_official_records_first_and_keep_their_type(conn, monkeypatch):
    record = {"url": "https://www.nhtsa.gov/recalls?nhtsaId=14V047000", "title": "NHTSA recall 14V047000",
              "text": "GM is recalling 2006 Chevrolet Cobalt vehicles because the ignition switch may move.",
              "source_type": "government_record"}
    web.remember(record["url"], record["text"])
    chosen = []

    def fake_llm(system, user, **kwargs):
        if system is scout.PLAN_SYSTEM:
            return {"queries": []}
        if system is scout.CHOOSE_SYSTEM:
            chosen.append(user)
            return {"urls": [record["url"]]}
        return {"relevant": True, "source_type": "other", "quotes": [
            {"quote": "GM is recalling 2006 Chevrolet Cobalt vehicles", "finding": "supports"}]}
    monkeypatch.setattr(llm, "ask_json", fake_llm)

    [finding] = scout.research("GM recalled the Cobalt.", "context", budget=10, official=[record])
    assert finding["source_type"] == "government_record"  # the lookup's type, not the model's "other"
    assert "[official record]" in chosen[0]
