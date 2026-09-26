"""The signals-store contract. Any implementation (in-memory, Astra DB) must pass these.

With NEWSROOM_TEST_ASTRA=1 (and Astra configured in .env) they also run against AstraSignals, in a
throwaway collection. Not on the endpoint alone: newsroom/config.py loads .env, so anyone with Astra
configured would otherwise reach the network on every plain `pytest`.
"""

import os
import uuid

import pytest

from newsroom.signals import FileSignals, InMemorySignals, Signal, origin_key

ASTRA = os.getenv("NEWSROOM_TEST_ASTRA") == "1"


@pytest.fixture(scope="session")
def astra_store():
    from newsroom.signals_astra import AstraSignals
    store = AstraSignals(f"signals_test_{uuid.uuid4().hex[:8]}")
    yield store
    store.database.drop_collection(store.name)


@pytest.fixture(params=["memory", "file", pytest.param("astra", marks=pytest.mark.skipif(
    not ASTRA, reason="set NEWSROOM_TEST_ASTRA=1 to run against Astra DB"))])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemorySignals()
    if request.param == "file":
        return FileSignals(tmp_path / "signals.json")
    astra = request.getfixturevalue("astra_store")
    astra.collection.delete_many({})    # every test starts from an empty store
    return astra


def signal(**kw) -> Signal:
    base = dict(
        summary="Owners report 2023 Model 3 steering loss; NHTSA widens its investigation",
        why_interesting="spike in complaints; open federal investigation",
        checkable_claim="NHTSA upgraded its steering investigation to an engineering analysis",
        origin="https://www.reddit.com/r/TeslaModel3/comments/abc123/steering_failed/",
        accountable_party="Tesla",
        sources=[{"url": "https://www.reddit.com/r/TeslaModel3/comments/abc123/steering_failed/",
                  "seen_at": "2026-09-26T10:00:00+00:00"}],
        source_types=["reddit"],
        last_seen="2026-09-26T10:00:00+00:00",
    )
    return Signal(**{**base, **kw})


def test_a_merge_keeps_every_beat_that_found_it(store):
    first, _ = store.add(signal(beats=["national"]))
    store.add(signal(beats=["georgia-tech"], last_seen="2026-09-26T11:00:00+00:00"))
    assert store.get(first).beats == ["georgia-tech", "national"]


def test_add_creates_and_get_returns_it(store):
    sid, created = store.add(signal())
    assert created and store.get(sid).accountable_party == "Tesla"


def test_same_origin_merges_instead_of_duplicating(store):
    first, _ = store.add(signal())
    repost = signal(
        origin="https://reddit.com/r/TeslaModel3/comments/abc123/steering_failed?utm_source=x",
        sources=[{"url": "https://x.com/someone/status/1", "seen_at": "2026-09-26T11:00:00+00:00"}],
        source_types=["x"], last_seen="2026-09-26T11:00:00+00:00",
    )
    second, created = store.add(repost)
    assert second == first and not created
    merged = store.get(first)
    assert len(merged.sources) == 2 and merged.source_types == ["reddit", "x"]
    assert merged.last_seen == "2026-09-26T11:00:00+00:00"


def test_different_claim_same_party_stays_separate(store):
    store.add(signal())
    _, created = store.add(signal(
        summary="Tesla recalls Cybertruck over trim panel that can detach",
        checkable_claim="NHTSA recall for Cybertruck exterior trim",
        origin="https://www.nhtsa.gov/recalls?nhtsaId=25V000",
    ))
    assert created


def test_recent_filters_by_time_and_status(store):
    old, _ = store.add(signal(origin="a", last_seen="2026-09-20T00:00:00+00:00"))
    new, _ = store.add(signal(origin="b", summary="Something else entirely about boilers",
                              checkable_claim="boiler recall", accountable_party="Acme"))
    assert [s.id for s in store.recent("2026-09-25T00:00:00+00:00")] == [new]
    store.mark_ignored(new, "no accountable action")
    assert store.recent("2026-09-25T00:00:00+00:00", status="new") == []


def test_similar_ranks_by_meaning(store):
    tesla, _ = store.add(signal())
    store.add(signal(origin="c", summary="City council approves a new park budget",
                     checkable_claim="parks budget amendment", accountable_party="City of Atlanta"))
    hits = store.similar("Tesla steering investigation NHTSA", k=5)
    assert hits and hits[0][0].id == tesla and 0 < hits[0][1] <= 1


def test_mark_used_records_the_hypothesis(store):
    sid, _ = store.add(signal())
    store.mark_used(sid, "hyp-1")
    store.mark_used(sid, "hyp-1")
    s = store.get(sid)
    assert s.status == "used" and s.used_by == ["hyp-1"]


def test_origin_key_ignores_tracking_scheme_and_www():
    assert origin_key("https://www.Reddit.com/r/x/abc/?utm=1") == origin_key("http://reddit.com/r/x/abc")


def test_origin_key_keeps_query_that_identifies_the_page():
    assert origin_key("https://www.youtube.com/watch?v=AAAA1111") != origin_key("https://www.youtube.com/watch?v=BBBB2222")
    assert origin_key("https://www.nhtsa.gov/recalls?nhtsaId=25V000") != origin_key("https://www.nhtsa.gov/recalls?nhtsaId=24V999")
    assert origin_key("https://news.ycombinator.com/item?id=1") != origin_key("https://news.ycombinator.com/item?id=2")


def test_origin_key_merges_other_addresses_for_the_same_post():
    video = origin_key("https://www.youtube.com/watch?v=AbC123")
    assert origin_key("https://youtu.be/AbC123?si=xyz") == video
    assert origin_key("https://m.youtube.com/watch?v=AbC123&feature=share&list=PL1") == video
    assert origin_key("https://www.youtube.com/watch?v=abc123") != video      # ids are case-sensitive
    tweet = origin_key("https://x.com/someone/status/42")
    assert tweet == origin_key("https://twitter.com/i/status/42?s=20") == "x.com/i/status/42"
    assert origin_key("https://old.reddit.com/r/x/comments/abc/t/") == origin_key("https://www.reddit.com/r/x/comments/abc/t")


def test_file_store_survives_a_restart(tmp_path):
    path = tmp_path / "signals.json"
    first = FileSignals(path)
    sid, _ = first.add(signal())
    first.mark_used(sid, "hyp-9")
    again = FileSignals(path)
    assert again.get(sid).used_by == ["hyp-9"]
    _, created = again.add(signal())          # still deduplicates against what was loaded
    assert not created
