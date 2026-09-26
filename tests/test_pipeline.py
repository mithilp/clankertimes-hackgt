"""The pipeline: McLovin's results -> reporter -> scouts -> council -> published, with every outside call faked."""

import pytest

from newsroom import desk as desk_module
from newsroom import llm, pipeline
from newsroom.desk import InMemoryDesk
from tests.test_reporter_agent import FakeModel, fake_scouts


class Results:
    name = "fake mclov_results"

    def __init__(self, docs):
        self.docs = docs

    def results(self, limit=200):
        return self.docs[:limit]


def test_mclovin_documents_are_read_in_whatever_shape_they_were_stored():
    ours = {"_id": "abc", "hypothesis": "X did Y.", "why_now": "now", "accountable_party": "X",
            "would_settle_it": ["NHTSA recalls"], "evidence_so_far": {"signal_ids": ["s1"], "distinct_origins": 2}}
    h = pipeline.from_mclovin(ours)
    assert h["id"] == "mclov:abc" and h["would_settle_it"] == ["NHTSA recalls"]
    assert h["evidence_so_far"] == {"signal_ids": ["s1"], "distinct_origins": 2}

    other = {"_id": "d2", "claim": "County paid a vendor without a bid.", "entity": "Fulton County",
             "records": "county purchasing records", "signals": [{"id": "s9"}]}
    h = pipeline.from_mclovin(other)
    assert h["hypothesis"].startswith("County paid") and h["accountable_party"] == "Fulton County"
    assert h["would_settle_it"] == ["county purchasing records"] and h["evidence_so_far"]["signal_ids"] == ["s9"]


def test_results_mcLovin_flagged_or_not_ready_are_left_alone():
    assert pipeline.from_mclovin({"_id": "1", "hypothesis": "x", "problems": ["missing why_now"]}) is None
    assert pipeline.from_mclovin({"_id": "2", "hypothesis": "x", "status": "watching"}) is None
    assert pipeline.from_mclovin({"_id": "3", "summary": ""}) is None


def test_the_desk_decides_what_is_pending():
    d = InMemoryDesk()
    d.save({"hypothesis_id": "mclov:done", "status": "published"})
    d.save({"hypothesis_id": "mclov:crashed", "status": "failed"})
    src = Results([{"_id": i, "hypothesis": f"H {i}."} for i in ("done", "crashed", "new")])
    assert [h["id"] for h in pipeline.pending(src, d)] == ["mclov:crashed", "mclov:new"]


@pytest.fixture
def env(conn, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return conn


def test_a_pass_works_new_results_end_to_end_and_never_twice(env, monkeypatch):
    d = InMemoryDesk()
    monkeypatch.setattr(desk_module, "_desk", d)
    monkeypatch.setattr(pipeline, "get_desk", lambda: d)
    docs = [{"_id": "m1", "hypothesis": "NHTSA has an open investigation into loss of steering control on the 2023 Tesla Model 3.",
             "why_now": "posts", "accountable_party": "Tesla",
             "would_settle_it": ["NHTSA investigations file, 2023-2026", "NHTSA recalls for the 2023 Model 3"]}]
    monkeypatch.setattr(pipeline, "source", lambda kind="auto": Results(docs))
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch)

    first = pipeline.run(limit=3, say=lambda *_: None)
    again = pipeline.run(limit=3, say=lambda *_: None)

    assert [s["status"] for s in first] == ["published"] and again == []
    [record] = d.recent()
    assert record["hypothesis_id"] == "mclov:m1" and record["status"] == "published"


def test_mcLovin_results_marked_already_reported_or_not_hypotheses_are_skipped():
    base = {"hypothesis": "X did Y.", "accountable_party": "X", "kind": "hypothesis"}
    assert pipeline.from_mclovin({**base, "_id": "a", "outcome": "lead"})
    assert pipeline.from_mclovin({**base, "_id": "b", "outcome": "already_reported"}) is None
    assert pipeline.from_mclovin({"_id": "c", "kind": "no_connection", "reason": "same press release"}) is None
    assert pipeline.from_mclovin({"_id": "d", "kind": "run", "signals_read": 40}) is None


def test_pending_skips_finished_in_progress_and_repeatedly_failed_results():
    from newsroom.signals import now
    docs = [{"_id": n, "hypothesis": f"Claim {n} about X.", "kind": "hypothesis", "outcome": "lead"} for n in "abcd"]
    desk = InMemoryDesk()
    desk.save({"id": "r1", "hypothesis_id": "mclov:a", "status": "published"})
    desk.save({"id": "r2", "hypothesis_id": "mclov:b", "status": "reporting"})          # fresh: someone is on it
    for n in range(pipeline.MAX_FAILURES):
        desk.save({"id": f"f{n}", "hypothesis_id": "mclov:c", "status": "failed"})
    todo = pipeline.pending(Results(docs), desk)
    assert [h["id"] for h in todo] == ["mclov:d"]
    assert now()        # records got fresh updated_at stamps from the desk
