from datetime import date

from newsroom import article

SOURCES = {
    "D": {"title": "NHTSA complaints", "url": "https://www.nhtsa.gov/", "text": "The car shut off on the highway."},
    "F1": {"title": "Recall notice", "url": "https://www.nhtsa.gov/recall", "text": "GM is recalling 2006 Cobalts.",
           "quote": "GM is recalling 2006 Cobalts."},
}


def draft(*sentences):
    return {"headline": "Cobalts shut off", "paragraphs": [[{"text": t, "cite": c} for t, c in sentences]]}


def test_cited_sentences_with_real_quotes_pass():
    good = draft(("Owners reported that \"the car shut off on the highway.\"", ["D"]),
                 ("A recall notice says “GM is recalling 2006 Cobalts.”", ["F1"]))
    assert article.check(good, SOURCES) == []


def test_uncited_sentence_fails():
    assert "cites no source" in article.check(draft(("Cobalts are dangerous.", [])), SOURCES)[0]


def test_unknown_source_fails():
    assert "unknown source F9" in article.check(draft(("Something.", ["F9"])), SOURCES)[0]


def test_invented_quote_fails():
    problems = article.check(draft(('GM said "we knew about it for years."', ["F1"])), SOURCES)
    assert "quote not found" in problems[0]


def test_quote_must_come_from_a_cited_source():
    problems = article.check(draft(('The notice says "GM is recalling 2006 Cobalts."', ["D"])), SOURCES)
    assert "quote not found" in problems[0]


def test_render_numbers_sources_in_order_of_citation():
    text = article.render(draft(("First.", ["F1"]), ("Second.", ["D", "F1"])), SOURCES, date(2026, 9, 26))
    assert "First. [1] Second. [2][1]" in text
    assert "1. [Recall notice](https://www.nhtsa.gov/recall)" in text
    assert "September 26, 2026" in text
