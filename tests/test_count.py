import json
from datetime import date

from newsroom import claims, count, db, llm
from tests.conftest import complaint

LONG = "The engine shut off without warning while I was driving on the interstate and I lost power steering completely."


def test_same_text_submitted_twice_counts_once():
    marks = count.distinct([{"id": "a", "text": LONG}, {"id": "b", "text": LONG}])
    assert [m["counted"] for m in marks] == [True, False]
    assert marks[1]["duplicate_of"] == "a"


TEMPLATE = ("I am writing to report a serious safety defect in my vehicle. While driving at normal speed the engine "
            "shut off without any warning and I lost power steering and power brakes. The dealer could not find the "
            "cause and told me no repair was available. This defect puts my family and other drivers at risk and the "
            "manufacturer must issue a recall immediately.")


def test_copied_template_with_small_edits_counts_once():
    edited = TEMPLATE.replace("my family", "my children") + " Please help us before someone is killed."
    marks = count.distinct([{"id": "a", "text": TEMPLATE}, {"id": "b", "text": edited}])
    assert [m["counted"] for m in marks] == [True, False]


def test_two_people_describing_the_same_problem_both_count():
    a = ("My 2006 Cobalt stalled on the freeway last week. The dash went dark, the steering got heavy and I barely "
         "made it to the shoulder. Second time this year.")
    b = ("Engine shut off while I was merging onto the highway. Lost steering assist and brakes were hard to press. "
         "Dealer says nothing is wrong with the car.")
    assert all(m["counted"] for m in count.distinct([{"id": "a", "text": a}, {"id": "b", "text": b}]))


def test_short_identical_texts_are_different_people():
    marks = count.distinct([{"id": "a", "text": "Brakes failed."}, {"id": "b", "text": "Brakes failed."}])
    assert all(m["counted"] for m in marks)


def test_ten_different_people_count_ten():
    members = [{"id": str(i), "text": f"Complaint number {i}: " + " ".join(f"word{i}x{j}" for j in range(15))}
               for i in range(10)]
    assert sum(m["counted"] for m in count.distinct(members)) == 10


def fake_grouping(system, user, **kwargs):
    """Groups every claim mentioning 'engine' together, everything else alone."""
    lines = [line for line in user.split("Claims:\n", 1)[1].splitlines() if line.strip()]
    engine = [int(line.split(".")[0]) for line in lines if "engine" in line]
    rest = [int(line.split(".")[0]) for line in lines if "engine" not in line]
    return {"groups": [{"label": "engine shuts off while driving", "claims": engine}]
            + [{"label": f"other {n}", "claims": [n]} for n in rest]}


def test_count_groups_claims_and_counts_distinct_people(conn, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", fake_grouping)
    db.save_complaints(conn, [
        complaint("nhtsa:1", text=LONG, received="2026-01-10", severe=True),
        complaint("nhtsa:2", text=LONG, received="2026-08-01"),                        # a copy of 1
        complaint("nhtsa:3", text="Car died on the highway with my kids in it, no warning at all, twice now.",
                  received="2026-08-15"),
        complaint("nhtsa:4", text="Paint is peeling off the hood after one winter of driving it daily.",
                  received="2026-08-20"),
        complaint("nhtsa:5", text=LONG + " Again.", received="2026-12-01"),              # after as_of
    ])
    conn.executemany("insert into claims (complaint_id, claim) values (?, ?)", [
        ("nhtsa:1", "engine shuts off while driving"), ("nhtsa:2", "engine shuts off while driving"),
        ("nhtsa:3", "engine stalls on highway"), ("nhtsa:4", "paint peels"), ("nhtsa:5", "engine shuts off"),
    ])

    as_of, n = count.count(conn, as_of=date(2026, 9, 1))

    assert as_of == date(2026, 9, 1)
    engine = next(g for g in count.top(conn, 10) if g["label"] == "engine shuts off while driving")
    assert engine["total"] == 2          # 1 and 3; 2 is a copy; 5 arrived after as_of
    assert engine["last_90"] == 1        # only 3 was received in the 90 days before Sept 1
    assert engine["severe"] == 1
    members = {m["id"]: m for m in json.loads(engine["members"])}
    assert members["nhtsa:2"]["duplicate_of"] == "nhtsa:1"
    assert "nhtsa:5" not in members


def test_planted_pattern_shows_up_in_the_top(conn, monkeypatch):
    """Test plan, step 3: a planted claim among many unrelated ones reaches the top of the list."""
    monkeypatch.setattr(llm, "ask_json", fake_grouping)
    haystack = [complaint(f"nhtsa:h{i}", product=f"2020 MAKE{i % 40} MODEL",
                          text=f"Unrelated complaint {i} about the radio: " + " ".join(f"w{i}x{j}" for j in range(14)))
                for i in range(400)]
    needle = [complaint(f"nhtsa:n{i}", product="2024 NORVEX IP3",
                        text=f"Person {i} reports: " + " ".join(f"engine{i}x{j}" for j in range(14)))
              for i in range(30)]
    db.save_complaints(conn, haystack + needle)
    conn.executemany("insert into claims (complaint_id, claim) values (?, ?)",
                     [(c["id"], f"radio problem {i}") for i, c in enumerate(haystack)]
                     + [(c["id"], "engine shuts off while driving") for c in needle])
    count.count(conn)
    best = count.top(conn, 1)[0]
    assert (best["product"], best["total"]) == ("2024 NORVEX IP3", 30)


def test_claims_are_read_per_product_and_skipped_ones_retried(conn, monkeypatch):
    seen_products = []

    def fake_reader(system, user, **kwargs):
        seen_products.append(user.splitlines()[0])
        ids = [line.strip("[]") for line in user.splitlines() if line.startswith("[nhtsa:")]
        return {"claims": [{"id": i, "claim": "engine shuts off"} for i in ids[:-1]]}  # skips the last one

    monkeypatch.setattr(llm, "ask_json", fake_reader)
    db.save_complaints(conn, [complaint(f"nhtsa:{i}") for i in range(3)]
                       + [complaint("nhtsa:lonely", product="1999 RARE CAR")])
    done = claims.extract(conn, min_complaints=2)

    assert done == 2
    assert seen_products == ["Product: 2006 CHEVROLET COBALT"]   # the rare car is below min_complaints
    assert len(claims.pending(conn, 2)["2006 CHEVROLET COBALT"]) == 1
