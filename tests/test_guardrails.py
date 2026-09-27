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


def test_browser_search_rejects_results_for_some_other_query():
    from newsroom.browser import _matches_query
    junk = [{"title": "Georgia (country) - Wikipedia", "url": "https://en.wikipedia.org/wiki/Georgia_(country)", "description": "A country in the Caucasus"}]
    real = [{"title": "GTRI wins Air Force contract", "url": "https://gtri.gatech.edu/news/x",
             "description": "The Georgia Tech Research Institute received a contract"}]
    assert not _matches_query('"Georgia Tech Research Institute" contract', junk)
    assert _matches_query('"Georgia Tech Research Institute" contract', real)
    assert not _matches_query("site:nique.net housing", [{"title": "AITAH", "url": "https://reddit.com/r/AITAH", "description": ""}])
    assert _matches_query("site:nique.net housing", [{"title": "Housing lottery", "url": "https://www.nique.net/news/1", "description": ""}])


def test_reddit_pauses_for_everyone_after_a_rate_limit_and_reuses_recent_fetches(monkeypatch, tmp_path):
    import httpx
    import pytest
    from newsroom import gather
    monkeypatch.setattr(gather, "REDDIT_DIR", tmp_path / "reddit")
    monkeypatch.setattr(gather, "REDDIT_GAP", 0)
    feed = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Water main break</title></entry></feed>'
    calls = []

    def fake_get(url, **params):
        calls.append(url)
        if "ratelimited" in url:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
        return httpx.Response(200, content=feed, request=httpx.Request("GET", url))

    monkeypatch.setattr(gather, "_get", fake_get)
    assert gather.subreddit("Atlanta")[0]["title"] == "Water main break"
    assert gather.subreddit("atlanta")[0]["title"] == "Water main break"
    assert len(calls) == 1                                    # the second read came from the shared cache

    with pytest.raises(gather.RedditPaused):
        gather.subreddit("ratelimited")
    with pytest.raises(gather.RedditPaused, match="paused until"):
        gather.subreddit("gatech")                            # a different subreddit is skipped too
    assert len(calls) == 2                                    # ...without another request


def test_a_quoted_source_may_say_currently():
    from newsroom.article import _sentence_gates
    assert not any("says what is true now" in p for p in _sentence_gates(
        "p1", 'The university said the program "has 82 students currently enrolled."', ["F1"]))
    assert any("says what is true now" in p for p in _sentence_gates("p1", "The program currently has 82 students.", ["F1"]))


def test_short_scare_quotes_are_not_checked_but_real_quotes_are():
    from newsroom.article import QUOTED
    assert QUOTED.findall('the "other" programs') == []
    assert QUOTED.findall('it said "82 students are enrolled" there') == ["82 students are enrolled"]
