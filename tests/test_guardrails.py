"""The mechanical publish gates: each case is a way a real newsroom published something false."""

from newsroom import article

SOURCES = {
    "D": {"title": "NHTSA complaints", "url": "https://x.gov", "text": "engine shut off while driving"},
    "F1": {"title": "Recall notice", "url": "https://y.gov", "text": 'the company said it would "fix it free of charge"'},
}


def draft(*sentences):
    return {"headline": "A headline", "paragraphs": [[{"text": t, "cite": c} for t, c in sentences]]}


def test_clean_draft_passes():
    assert article.check(draft(("Fifteen owners reported that the engine shut off while driving.", ["D"]),
                               ('The company said it would "fix it free of charge".', ["F1"])), SOURCES) == []


def test_office_cannot_be_the_defendant():
    problems = article.check(draft(("The district attorney was charged with murder.", ["F1"])), SOURCES)
    assert any("criminal verb" in p for p in problems), problems


def test_office_as_charging_party_is_fine():
    assert article.check(draft(("The district attorney charged a Redwood City man with murder.", ["F1"])), SOURCES) == []


def test_crime_word_needs_more_than_complaint_data():
    problems = article.check(draft(("The pattern points to fraud by the manufacturer.", ["D"])), SOURCES)
    assert any("complaint data" in p for p in problems), problems
    # Same sentence is allowed when a record backs it.
    assert article.check(draft(("The pattern points to fraud by the manufacturer.", ["D", "F1"])), SOURCES) == []


def test_vague_count_when_the_number_is_known():
    problems = article.check(draft(("Many owners reported the same failure.", ["D"])), SOURCES)
    assert any("Use the number" in p for p in problems), problems


def test_present_tense_absolutes_are_blocked():
    problems = article.check(draft(("The defect currently affects the 2023 model.", ["F1"])), SOURCES)
    assert any("Date-scope" in p for p in problems), problems


def test_pipeline_leakage_is_blocked():
    for leak in ("The [[MODEL_NAME]] stalls.", "Here is the JSON you asked for.", "As an AI I cannot verify this."):
        problems = article.check(draft((leak, ["F1"])), SOURCES)
        assert any("pipeline text" in p for p in problems), (leak, problems)


def test_leakage_in_the_headline_is_blocked():
    bad = draft(("Owners reported a stall.", ["D"]))
    bad["headline"] = "TODO: write a headline"
    assert any("pipeline text" in p for p in article.check(bad, SOURCES)), article.check(bad, SOURCES)


def test_existing_checks_still_run():
    assert any("cites no source" in p for p in article.check(draft(("Uncited sentence.", [])), SOURCES))
    problems = article.check(draft(('He said "this quote is not in any source at all".', ["F1"])), SOURCES)
    assert any("quote not found" in p for p in problems), problems
