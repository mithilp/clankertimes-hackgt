from newsroom import llm, scout, web

PAGE = "Safety Recall 14V-047. General Motors is recalling certain 2005-2007 Chevrolet Cobalt vehicles. The ignition switch may move out of the run position."


def test_quotes_not_on_the_page_are_dropped(monkeypatch):
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"relevant": True, "source_type": "government_record", "quotes": [
        {"quote": "General Motors is recalling certain 2005-2007 Chevrolet Cobalt vehicles.", "finding": "contradicts"},
        {"quote": "GM admitted it hid the defect for a decade.", "finding": "supports"},
    ]})
    findings = scout.read("The Cobalt has not been recalled.", "https://www.nhtsa.gov/recalls", "Recall", PAGE)
    assert [f["quote"][:21] for f in findings] == ["General Motors is rec"]
    assert findings[0]["finding"] == "contradicts"


def test_social_media_is_always_social(monkeypatch):
    assert scout.source_type_of("https://www.reddit.com/r/cars/x", "news_report") == "social"
    assert scout.source_type_of("https://old.reddit.com/r/cars/x", "government_record") == "social"
    assert scout.source_type_of("https://www.reuters.com/x", "news_report") == "news_report"
    assert scout.source_type_of("https://example.com", "made_up_type") == "other"


def test_scout_stops_when_a_source_that_counts_settles_it(monkeypatch):
    searches = []

    def fake_llm(system, user, **kwargs):
        if system is scout.PLAN_SYSTEM:
            return {"queries": ["cobalt recall", "cobalt ignition switch"]}
        if system is scout.CHOOSE_SYSTEM:
            return {"urls": ["https://www.nhtsa.gov/recalls"]}
        return {"relevant": True, "source_type": "government_record", "quotes": [
            {"quote": "General Motors is recalling certain 2005-2007 Chevrolet Cobalt vehicles.", "finding": "supports"}]}

    monkeypatch.setattr(llm, "ask_json", fake_llm)
    monkeypatch.setattr(web, "search", lambda q, count=10: searches.append(q) or [
        {"url": "https://www.nhtsa.gov/recalls", "title": "Recall", "description": ""}])
    monkeypatch.setattr(web, "fetch_text", lambda url: PAGE)

    findings = scout.research("GM recalled the Cobalt.", "2006 CHEVROLET COBALT: engine shuts off", budget=30)

    assert len(findings) == 1 and findings[0]["finding"] == "supports"
    assert searches == ["cobalt recall", "cobalt ignition switch"]   # one round was enough


def test_scout_respects_its_budget(monkeypatch):
    def fake_llm(system, user, **kwargs):
        if system is scout.PLAN_SYSTEM:
            return {"queries": ["a", "b", "c"]}
        if system is scout.CHOOSE_SYSTEM:
            return {"urls": [f"https://example.com/{i}" for i in range(5)]}
        return {"relevant": False}

    calls = {"search": 0, "fetch": 0}
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    monkeypatch.setattr(web, "search", lambda q, count=10: calls.__setitem__("search", calls["search"] + 1) or [
        {"url": f"https://example.com/{i}", "title": "", "description": ""} for i in range(5)])
    monkeypatch.setattr(web, "fetch_text", lambda url: calls.__setitem__("fetch", calls["fetch"] + 1) or "text")

    scout.research("Something.", "context", budget=4)
    assert calls["search"] + calls["fetch"] == 4
