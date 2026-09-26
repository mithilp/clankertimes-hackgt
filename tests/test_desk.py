"""The desk contract. Every implementation (in-memory, file, Astra DB) must pass these.

With NEWSROOM_TEST_ASTRA=1 (and Astra configured in .env) they also run against AstraDesk, in a throwaway
collection, exactly like tests/test_signals.py.
"""

import os
import uuid

import pytest

from newsroom.desk import FileDesk, InMemoryDesk

ASTRA = os.getenv("NEWSROOM_TEST_ASTRA") == "1"


@pytest.fixture(scope="session")
def astra_desk():
    from newsroom.desk_astra import AstraDesk
    desk = AstraDesk(f"desk_test_{uuid.uuid4().hex[:8]}")
    yield desk
    desk.database.drop_collection(desk.name)


@pytest.fixture(params=["memory", "file", pytest.param("astra", marks=pytest.mark.skipif(
    not ASTRA, reason="set NEWSROOM_TEST_ASTRA=1 to run against Astra DB"))])
def desk(request, tmp_path):
    if request.param == "memory":
        return InMemoryDesk()
    if request.param == "file":
        return FileDesk(tmp_path / "desk.json")
    astra = request.getfixturevalue("astra_desk")
    astra.collection.delete_many({})
    return astra


def record(**kw):
    return {"hypothesis_id": "h-1", "hypothesis": "NHTSA has an open investigation into 2023 Model 3 steering loss.",
            "accountable_party": "Tesla", "status": "reporting", "stage": "planning",
            "sub_hypotheses": [{"id": "H1", "statement": "s", "findings": [{"quote": "q"}]}], **kw}


def test_save_assigns_an_id_and_get_returns_the_record(desk):
    rid = desk.save(record())
    got = desk.get(rid)
    assert got["_id"] == rid and got["sub_hypotheses"][0]["findings"][0]["quote"] == "q"
    assert got["created_at"] and got["updated_at"]


def test_saving_again_replaces_the_record_in_place(desk):
    rid = desk.save(record())
    desk.save({**desk.get(rid), "status": "killed", "stage": "done"})
    assert len(desk.recent()) == 1 and desk.get(rid)["status"] == "killed"


def test_for_hypothesis_and_recent_filter(desk):
    a = desk.save(record(status="parked"))
    desk.save(record(hypothesis_id="h-2", hypothesis="County paid a vendor without a bid.", accountable_party="Fulton County"))
    assert [r["_id"] for r in desk.for_hypothesis("h-1")] == [a]
    assert [r["_id"] for r in desk.recent(status="parked")] == [a]


def test_similar_ranks_the_related_investigation_first(desk):
    steering = desk.save(record(status="killed"))
    desk.save(record(hypothesis_id="h-2", hypothesis="Fulton County paid a paving vendor $2.1M without a competitive bid.",
                     accountable_party="Fulton County"))
    hits = desk.similar("Is NHTSA investigating steering loss on the Tesla Model 3?", k=2)
    assert hits[0][0]["_id"] == steering and 0 < hits[0][1] <= 1


def test_the_file_desk_survives_a_restart(tmp_path):
    rid = FileDesk(tmp_path / "d.json").save(record())
    assert FileDesk(tmp_path / "d.json").get(rid)["hypothesis_id"] == "h-1"
