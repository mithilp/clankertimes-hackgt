"""The depth-first hunt: it reads products one at a time and stops at the first lead it investigates."""

import pytest

from newsroom import claims, count, db, hunt, llm, records, reporter, scout, web
from tests.conftest import complaint

CLAIM_BY_PRODUCT = {"2020 FIRST CAR": "brakes squeal", "2021 SECOND CAR": "engine stalls on highway",
                    "2022 THIRD CAR": "radio freezes"}
SUPPORT = {"url": "https://www.nhtsa.gov/recalls?nhtsaId=1", "title": "Recall", "source_type": "government_record",
           "quote": "No recall covers the stalling.", "finding": "supports", "note": ""}


def fake_llm(system, user, **kwargs):
    if system is claims.SYSTEM:
        product = user.splitlines()[0].removeprefix("Product: ")
        ids = [line.strip("[]") for line in user.splitlines() if line.startswith("[nhtsa:")]
        return {"claims": [{"id": i, "claim": CLAIM_BY_PRODUCT[product]} for i in ids]}
    if system is count.GROUP_SYSTEM:
        return {"groups": [{"label": user.split("Claims:\n1. ")[1].splitlines()[0], "claims": [1]}]}
    if system is reporter.PICK_SYSTEM:
        return {"ranked": [{"group": 1, "worth": "engine" in user, "reason": "stalling at speed" if "engine" in user else "minor"}]}
    if system is reporter.REPORTED_SYSTEM:
        return {"already_reported": False, "reason": "nothing found"}
    if system is reporter.HYPOTHESES_SYSTEM:
        return {"angle": "Stalling without a recall.", "hypotheses": ["No recall covers the 2021 Second Car stalling."]}
    if system is reporter.WRITE_SYSTEM:
        return {"headline": "Second Car stalls", "paragraphs": [[
            {"text": "Owners reported that the engine stalls on the highway.", "cite": ["D"]},
            {"text": "Records say \"No recall covers the stalling.\"", "cite": ["F1"]}]]}
    raise AssertionError(f"unexpected prompt: {system[:40]}")


@pytest.fixture
def data(conn, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    monkeypatch.setattr(web, "search", lambda q, count=10: [])
    monkeypatch.setattr(records, "for_story", lambda conn, story: [])
    monkeypatch.setattr(scout, "research", lambda h, context, budget, official=(): [SUPPORT])
    rows = []
    for product, n, severe in (("2020 FIRST CAR", 15, 10), ("2021 SECOND CAR", 12, 5), ("2022 THIRD CAR", 11, 1)):
        rows += [complaint(f"nhtsa:{product[-9:]}{i}", product=product, severe=i < severe,
                           text=f"Owner {i} of the {product} describes the problem in their own words, number {i}.")
                 for i in range(n)]
    db.save_complaints(conn, rows)
    return conn


def claimed(conn, product):
    return conn.execute("select count(*) from claims k join complaints c on c.id = k.complaint_id where c.product = ?",
                        (product,)).fetchone()[0]


def test_hunt_stops_at_the_first_lead_without_reading_the_rest(data):
    lines = []
    [story_id] = hunt.hunt(data, stories=1, say=lines.append)

    story = data.execute("select status, product from stories where id = ?", (story_id,)).fetchone()
    assert (story["product"], story["status"]) == ("2021 SECOND CAR", "published")
    assert claimed(data, "2020 FIRST CAR") == 15      # most serious: read first, judged not worth a story
    assert claimed(data, "2021 SECOND CAR") == 12     # the lead
    assert claimed(data, "2022 THIRD CAR") == 0       # never reached: no tokens spent on it
    assert any(line.startswith("    skipped brakes squeal") for line in lines)
    assert any("LEAD: engine stalls on highway" in line for line in lines)


def test_the_next_hunt_carries_on_where_the_last_stopped(data):
    hunt.hunt(data, stories=1, say=lambda line: None)
    lines = []
    assert hunt.hunt(data, stories=1, say=lines.append) == []

    assert [line for line in lines if line.startswith("[")] == ["[1] 2022 THIRD CAR (nhtsa): 11 complaints, 1 serious"]
    assert claimed(data, "2022 THIRD CAR") == 11
    assert lines[-1] == "stopped after 1 products: 0 of 1 leads investigated"


def test_a_product_that_gains_complaints_is_scanned_again(data):
    hunt.hunt(data, stories=5, say=lambda line: None)
    assert list(hunt.queue(data)) == []
    db.save_complaints(data, [complaint("nhtsa:new1", product="2022 THIRD CAR", text="A brand new complaint about the radio freezing again.")])
    assert [row["product"] for row in hunt.queue(data)] == ["2022 THIRD CAR"]
