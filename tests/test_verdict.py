"""Test plan, step 7: sets of scout findings, each with a known right call."""

import pytest

from newsroom.reporter import decide


def f(finding, source_type="government_record"):
    return {"url": "https://example.gov/x", "quote": "a quote", "finding": finding, "source_type": source_type}


CASES = [
    ("all supported by records", [[f("supports")], [f("supports", "news_report")], [f("supports", "court_record")]], "write"),
    ("one contradicted by a record", [[f("supports")], [f("contradicts")]], "kill"),
    ("contradicted by news", [[f("contradicts", "news_report")], [f("supports")]], "kill"),
    ("company denial doesn't kill", [[f("supports")], [f("supports"), f("contradicts", "company_statement")]], "write"),
    ("complaints don't prove it", [[f("supports", "complaint")], [f("supports")]], "park"),
    ("social posts don't prove it", [[f("supports", "social")]], "park"),
    ("nothing found", [[], [f("supports")]], "park"),
    ("only unclear findings", [[f("unclear")], [f("unclear", "news_report")]], "park"),
    ("contradiction beats missing support", [[f("contradicts")], []], "kill"),
    ("no hypotheses", [], "park"),
    ("disputed but confirmed by records is not a kill",
     [[f("supports"), f("supports"), f("contradicts", "news_report")], [f("supports", "news_report")]], "write"),
    ("disputed and unconfirmed parks later hypotheses", [[f("supports"), f("contradicts")], []], "park"),
]


@pytest.mark.parametrize("name,findings,expected", CASES, ids=[c[0] for c in CASES])
def test_verdict(name, findings, expected):
    verdict, note = decide(findings)
    assert verdict == expected
    assert note
