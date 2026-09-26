"""The signals-store contract. Any implementation (in-memory, Astra DB) must pass these.

To run them against Astra instead, point `make_store` at AstraSignals with a throwaway collection.
"""

import pytest

from newsroom.signals import FileSignals, InMemorySignals, Signal, origin_key


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    return InMemorySignals() if request.param == "memory" else FileSignals(tmp_path / "signals.json")


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


def test_origin_key_ignores_query_scheme_and_www():
    assert origin_key("https://www.Reddit.com/r/x/abc/?utm=1") == origin_key("http://reddit.com/r/x/abc")


def test_file_store_survives_a_restart(tmp_path):
    path = tmp_path / "signals.json"
    first = FileSignals(path)
    sid, _ = first.add(signal())
    first.mark_used(sid, "hyp-9")
    again = FileSignals(path)
    assert again.get(sid).used_by == ["hyp-9"]
    _, created = again.add(signal())          # still deduplicates against what was loaded
    assert not created
