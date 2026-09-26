"""The scout agent's loop, with the model, the web and the record lookups faked. No network, no tokens."""

import pytest

from newsroom import llm, records, scout_agent, web

GOV = "https://www.nhtsa.gov/recalls?nhtsaId=24V051"
BLOG = "https://someblog.example/tesla"
TASK = {"id": "H1", "statement": "NHTSA opened an investigation into 2023 Model 3 steering loss.",
        "assignment": {"records_first": ["NHTSA investigations file"], "search_terms": ["engineering analysis"],
                       "supports_if": "an open investigation", "contradicts_if": "it was closed", "next_check": ""}}


class Model:
    """Plays the scout: a scripted list of steps, then a report."""

    def __init__(self, steps, report=None):
        self.steps, self.report = list(steps), report or {}
        self.prompts: list[str] = []

    def __call__(self, system, user, **kw):
        self.prompts.append(user)
        if "take the next step" in system:
            return self.steps.pop(0) if self.steps else {"finish": True}
        if "report back to your reporter" in system or "report the prior coverage" in system:
            return self.report
        raise AssertionError(system[-200:])


@pytest.fixture
def env(conn, monkeypatch):
    monkeypatch.setattr(web, "search", lambda q, count=10: [
        {"url": GOV, "title": "NHTSA recall", "description": "steering"}, {"url": BLOG, "title": "a blog", "description": ""}])
    pages = {GOV: "NHTSA opened an engineering analysis into loss of steering control.", BLOG: "I think Tesla is bad."}
    monkeypatch.setattr(web, "fetch_text", lambda url: pages.get(url, ""))

    def read(statement, url, title, text, source_type=None, context=""):
        if url == GOV or "recall" in url:
            return [{"url": url, "title": title, "source_type": source_type or "government_record",
                     "quote": text.split(".")[0] + ".", "finding": "supports", "note": ""}]
        return []
    monkeypatch.setattr(scout_agent, "read_page", read)
    return conn


def test_the_scout_searches_reads_and_reports_with_its_reasons(env, monkeypatch):
    model = Model([{"calls": [{"tool": "web_search", "arg": "NHTSA engineering analysis Model 3 steering", "why": "find the record"}]},
                   {"calls": [{"tool": "read", "arg": "R1", "why": "the NHTSA page is the record"},
                              {"tool": "read", "arg": "R2", "why": "check the blog for a contradiction"}]}],
                  report={"verdict": "supports", "confidence": "high", "summary": "NHTSA's page says it opened one.",
                          "searched": ["web: NHTSA engineering analysis"], "proposals": [{"hypothesis": "x", "records": ["y"]}]})
    monkeypatch.setattr(llm, "ask_json", model)

    out = scout_agent.run(TASK, budget=5, context="Tesla")

    assert [t["tool"] for t in out["trail"]] == ["web_search", "read", "read"]
    assert out["trail"][1]["why"] == "the NHTSA page is the record"
    assert out["findings"][0]["url"] == GOV and out["findings"][0]["source_type"] == "government_record"
    assert out["report"]["verdict"] == "supports" and out["report"]["proposals"][0]["hypothesis"] == "x"
    assert "It is CONTRADICTED if: it was closed" in model.prompts[0]      # the reporter's direction reaches it


def test_a_verdict_no_verified_source_backs_is_held_back(env, monkeypatch):
    model = Model([{"calls": [{"tool": "web_search", "arg": "tesla", "why": "look"}]},
                   {"calls": [{"tool": "read", "arg": BLOG, "why": "read it"}]}],
                  report={"verdict": "contradicts", "summary": "a blog says no"})
    monkeypatch.setattr(llm, "ask_json", model)
    out = scout_agent.run(TASK, budget=5)
    assert out["report"]["verdict"] == "unclear" and "no verified quote" in out["report"]["held_back"]


def test_the_budget_caps_calls_and_repeats_are_refused(env, monkeypatch):
    same = {"calls": [{"tool": "web_search", "arg": "tesla", "why": "a"}, {"tool": "web_search", "arg": "tesla", "why": "b"},
                      {"tool": "web_search", "arg": "nhtsa", "why": "c"}, {"tool": "hack_the_planet", "arg": "x", "why": "d"}]}
    monkeypatch.setattr(llm, "ask_json", Model([same, same, same, same]))
    out = scout_agent.run(TASK, budget=3)
    assert [t["arg"] for t in out["trail"]] == ["tesla", "nhtsa"]          # the repeat and the unknown tool were dropped
    assert out["used"] == 2                                               # and a step with nothing new ended the loop


def test_official_records_are_readable_with_their_source_type_fixed(env, monkeypatch):
    monkeypatch.setattr(records, "nhtsa_recalls", lambda make, model, year: [
        {"url": "https://www.nhtsa.gov/recalls?nhtsaId=24V999", "title": "NHTSA recall 24V999: STEERING",
         "text": "NHTSA recall 24V999 covers loss of steering assist.", "source_type": "government_record"}])
    remembered = {}
    monkeypatch.setattr(web, "remember", lambda url, text: remembered.update({url: text}))
    monkeypatch.setattr(web, "fetch_text", lambda url: remembered.get(url, ""))
    model = Model([{"calls": [{"tool": "nhtsa_recalls", "arg": "TESLA | MODEL 3 | 2023", "why": "official records first"}]},
                   {"calls": [{"tool": "read", "arg": "R1", "why": "the recall"}]}], report={"verdict": "supports"})
    monkeypatch.setattr(llm, "ask_json", model)
    out = scout_agent.run(TASK, budget=4)
    assert "1 recalls" in out["trail"][0]["result"] and out["report"]["verdict"] == "supports"
    assert out["findings"][0]["title"] == "NHTSA recall 24V999: STEERING"


def test_a_failed_call_is_a_result_not_a_crash(env, monkeypatch):
    monkeypatch.setattr(records, "cpsc_recalls", lambda p: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setattr(llm, "ask_json", Model([{"calls": [{"tool": "cpsc_recalls", "arg": "air fryer", "why": "x"}]}]))
    out = scout_agent.run(TASK, budget=2)
    assert out["trail"][0]["result"].startswith("failed: ConnectionError")


def test_the_coverage_scout_only_reports_coverage_it_saw(env, monkeypatch):
    model = Model([{"calls": [{"tool": "web_search", "arg": "tesla steering", "why": "prior coverage"},
                              {"tool": "nhtsa_recalls", "arg": "x", "why": "not a coverage tool"}]}],
                  report={"coverage": [{"url": GOV, "outlet": "NHTSA", "what_it_established": "x"},
                                       {"url": "https://invented.example/story", "outlet": "?", "what_it_established": "y"}]})
    monkeypatch.setattr(llm, "ask_json", model)
    out = scout_agent.run({"mode": "coverage", "statement": "s", "searches": ["tesla steering"]}, budget=3)
    assert [t["tool"] for t in out["trail"]] == ["web_search"]
    assert [c["url"] for c in out["report"]["coverage"]] == [GOV] and out["report"]["dropped_unseen"] == 1


def test_leads_from_the_newsroom_db_are_offered_first(env, monkeypatch):
    model = Model([])
    monkeypatch.setattr(llm, "ask_json", model)
    scout_agent.run(TASK, budget=2, leads=[{"url": "https://reddit.com/r/x/1", "title": "owners post", "description": "d"}])
    assert "R1 [lead from the newsroom DB] owners post" in model.prompts[0]


def test_a_coverage_scout_reads_pages_against_every_element_not_the_narrow_hypothesis():
    from newsroom import scout_agent
    elements = [{"id": "E1", "claim": "A fatal robotaxi crash happened in Austin"},
                {"id": "E2", "claim": "NHTSA's crash data lists it"}]
    cov = scout_agent.Scout({"id": "coverage", "mode": "coverage", "statement": "NHTSA data lists the Austin crash",
                             "elements": elements}, budget=3)
    text = cov._reading_statement()
    assert "any part of this story" in text and "E1: A fatal robotaxi crash happened in Austin" in text
    plain = scout_agent.Scout({"id": "H1", "statement": "NHTSA data lists the Austin crash",
                               "assignment": {}}, budget=3)
    assert plain._reading_statement() == "NHTSA data lists the Austin crash"
