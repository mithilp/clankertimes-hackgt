"""The reporter's whole chain, with DeepSeek and Brave replaced by fakes."""

import json
from pathlib import Path

import pytest

from newsroom import db, llm, records, reporter, scout, web
from tests.conftest import complaint

RECALL = {"url": "https://www.nhtsa.gov/recalls/x", "title": "Recall", "source_type": "government_record",
          "quote": "No recall has been issued for this problem.", "finding": "supports", "note": ""}


@pytest.fixture(autouse=True)
def no_record_lookups(monkeypatch):
    """Official-record lookups would reach real APIs; tests give the reporter none."""
    monkeypatch.setattr(records, "for_story", lambda conn, story: [])


@pytest.fixture
def one_group(conn):
    db.save_complaints(conn, [complaint(f"nhtsa:{i}", text=f"Complaint {i}: the engine shut off while driving.")
                              for i in range(3)])
    members = [{"id": f"nhtsa:{i}", "counted": True, "duplicate_of": None} for i in range(3)]
    conn.execute("insert into claim_groups (as_of, source, product, company, label, total, last_90, severe, members)"
                 " values ('2026-09-01', 'nhtsa', '2006 CHEVROLET COBALT', 'General Motors LLC',"
                 " 'engine shuts off while driving', 3, 2, 1, ?)", (json.dumps(members),))
    conn.commit()
    return conn


def fake_llm(system, user, **kwargs):
    if system is reporter.PICK_SYSTEM:
        return {"ranked": [{"group": 1, "worth": True, "reason": "stalling at speed puts drivers at risk"}]}
    if system is reporter.REPORTED_SYSTEM:
        return {"already_reported": False, "reason": "no coverage found", "url": None}
    if system is reporter.HYPOTHESES_SYSTEM:
        return {"angle": "Cobalts may be stalling without a recall.", "hypotheses": ["No recall covers the stalling."]}
    if system is reporter.WRITE_SYSTEM:
        return {"headline": "Cobalt owners report stalling", "paragraphs": [[
            {"text": "Three owners reported that their engine shut off while driving.", "cite": ["D"]},
            {"text": "NHTSA's records say \"No recall has been issued for this problem.\"", "cite": ["F1"]},
        ]]}
    raise AssertionError(f"unexpected prompt: {system[:40]}")


def test_a_supported_story_is_published(one_group, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    monkeypatch.setattr(web, "search", lambda q, count=10: [])
    monkeypatch.setattr(scout, "research", lambda h, context, budget, official=(): [RECALL])

    [story_id] = reporter.run(one_group, stories=1)

    story = one_group.execute("select * from stories where id = ?", (story_id,)).fetchone()
    assert story["status"] == "published", story["note"]
    text = Path(story["article"]).read_text(encoding="utf-8")
    assert "# Cobalt owners report stalling" in text and "No recall has been issued" in text
    actions = [e["action"] for e in one_group.execute("select action from events order by id")]
    assert actions == ["pick", "hypotheses", "records", "report", "write", "published"]


def test_a_contradicted_story_is_killed(one_group, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    monkeypatch.setattr(web, "search", lambda q, count=10: [])
    monkeypatch.setattr(scout, "research", lambda h, context, budget, official=(): [{**RECALL, "finding": "contradicts"}])

    [story_id] = reporter.run(one_group, stories=1)

    story = one_group.execute("select status, note, article from stories where id = ?", (story_id,)).fetchone()
    assert story["status"] == "killed" and story["article"] is None
    assert "H1 is contradicted" in story["note"]


def test_a_crashing_scout_fails_its_story_without_stopping_the_run(one_group, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    monkeypatch.setattr(web, "search", lambda q, count=10: [])

    def broken(h, context, budget, official=()):
        raise web.SearchError("BRAVE_API_KEY is not set")
    monkeypatch.setattr(scout, "research", broken)

    [story_id] = reporter.run(one_group, stories=1)
    story = one_group.execute("select status, note from stories where id = ?", (story_id,)).fetchone()
    assert story["status"] == "failed" and "BRAVE_API_KEY" in story["note"]
