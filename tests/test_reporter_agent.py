"""The reporter agent on a McLovin hypothesis, with the model, the web and the scouts faked. No network, no tokens."""

import json
from pathlib import Path

import pytest

from newsroom import llm, reporter_agent, scout_agent, web
from newsroom.desk import InMemoryDesk
from newsroom.signals import InMemorySignals, Signal

GOV = "https://www.nhtsa.gov/recalls?nhtsaId=24V051"
NEWS = "https://www.reuters.com/business/autos/tesla-steering"

HYPOTHESIS = {
    "id": "h-test000001",
    "hypothesis": "NHTSA has an open investigation into loss of steering control on the 2023 Tesla Model 3.",
    "why_now": "owners posted about steering loss this week",
    "who_would_know": ["NHTSA Office of Defects Investigation", "Tesla"],
    "would_settle_it": ["NHTSA investigations file, 2023-2026", "NHTSA recalls for the 2023 Model 3"],
    "accountable_party": "Tesla",
    "evidence_so_far": {"signal_ids": [], "distinct_origins": 1, "sources": 1},
}


def finding(url, quote, finding="supports", source_type="government_record"):
    return {"url": url, "title": "t", "source_type": source_type, "quote": quote, "finding": finding, "note": ""}


SUPPORT = {
    "NHTSA opened": [finding(GOV, "NHTSA opened an engineering analysis into loss of steering control.")],
    "News outlets": [finding(NEWS, "Owners reported losing steering at highway speed.", source_type="news_report")],
    "The steering": [finding(GOV, "The condition is not related to aftermarket equipment.")],
}

ASSIGNMENTS = [
    {"statement": "NHTSA opened an investigation into 2023 Model 3 steering loss.", "covers": ["E1", "E2"],
     "needed_for": "minimum", "kind": "claim", "priority": "high", "records_first": ["M1"],
     "search_terms": ["engineering analysis", "steering assist"], "supports_if": "an open investigation number",
     "contradicts_if": "the investigation was closed", "new": True, "novelty": "record"},
    {"statement": "News outlets reported owners losing steering.", "covers": ["E3"], "needed_for": "maximum",
     "kind": "claim", "priority": "low", "records_first": ["Reuters and AP archives"], "search_terms": ["steering"],
     "supports_if": "a news report", "contradicts_if": "reports attribute it to something else"},
    {"statement": "The steering loss is not explained by aftermarket equipment.", "covers": [], "needed_for": "minimum",
     "kind": "alternative", "priority": "medium", "records_first": ["M2"], "search_terms": ["aftermarket"],
     "supports_if": "the recall rules it out", "contradicts_if": "the recall blames aftermarket parts"},
]

PLAN = {"prior_coverage": [{"url": NEWS, "what_it_established": "owners' reports"}], "gap": "whether NHTSA acted",
        "alternatives": ["aftermarket equipment"],
        "evidence_plan": [{"source": "NHTSA investigations file", "why": "settles E1", "value": "high"}],
        "sizing": {"count": 3, "why": "the investigations file settles E1 and E2 together; news and the alternative need other records"},
        "assignments": ASSIGNMENTS, "deferred": []}

FRAME = {"restated": "r", "context": "Tesla Model 3 steering",
         "elements": [{"claim": "an NHTSA investigation exists", "needed_for": "minimum"},
                      {"claim": "it concerns steering loss on the 2023 Model 3", "needed_for": "minimum"},
                      {"claim": "owners were hurt", "needed_for": "maximum"}],
         "minimum_story": "NHTSA is investigating", "maximum_story": "NHTSA is investigating and owners were hurt",
         "coverage_searches": ["tesla steering nhtsa"]}

DRAFT = {"headline": "Federal regulators are examining Model 3 steering", "paragraphs": [[
    {"text": "NHTSA's records say \"NHTSA opened an engineering analysis into loss of steering control.\"", "cite": ["F1"]},
    {"text": "Posts online raised the question.", "cite": ["D"]},
]]}

APPROVE = {"verdict": "approve", "problems": [], "notes": ""}

DEFAULTS = {
    "frame": FRAME,
    "plan": PLAN,
    "direct": {"assessments": [{"id": "H1", "action": "done"}, {"id": "H2", "action": "done"}, {"id": "H3", "action": "done"}],
               "new_sub_hypotheses": [], "spinoffs": [], "stop": True, "stop_reason": "plan covered"},
    "memo": {"downgrade_to": None, "memo": {"checked": "NHTSA files", "found": "an investigation", "would_change_it": "closure"}},
    "write": DRAFT,
    "triage": {"decision": "fix", "why": "wording", "fixes": ["cut the vague line"]},
    "revise": DRAFT,
    "skeptic": APPROVE, "virality": APPROVE, "novelty": APPROVE,
}

STEPS = [("frame", "frame the story"), ("plan", "size the investigation"), ("direct", "direct the next round"),
         ("memo", "the verdict memo"), ("triage", "council sent your draft back"), ("revise", "revise the draft"),
         ("write", "write the article")]


class FakeModel:
    """Answers each step by its task heading, and each council judge by its playbook. Records every call."""

    def __init__(self, **overrides):
        self.overrides = overrides
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system, user, **kwargs):
        step = next((name for name, marker in STEPS if f"Your job right now: " in system and marker in system.split("Your job right now: ")[-1][:120]), None)
        if step is None and "Judge the draft below" in system:
            step = next(j for j in ("skeptic", "virality", "novelty") if system.lower().startswith(f"# {j}") or
                        f"# {j}" in system.lower()[:200])
        if step is None:
            raise AssertionError(f"unexpected prompt: {system[:200]}")
        self.calls.append((step, user))
        answer = self.overrides.get(step, DEFAULTS[step])
        return answer(user) if callable(answer) else answer

    def steps(self):
        return [s for s, _ in self.calls]


@pytest.fixture
def env(conn, monkeypatch, tmp_path):
    """The reporter never searches or fetches: scouts do. Any web call from the reporter fails the test."""
    monkeypatch.chdir(tmp_path)

    def no_legwork(*a, **k):
        raise AssertionError("the reporter did legwork itself; that's the scouts' job")
    monkeypatch.setattr(web, "search", no_legwork)
    monkeypatch.setattr(web, "fetch_text", no_legwork)
    COVERAGE_TASKS.clear()
    return conn


COVERAGE_TASKS: list[dict] = []


def fake_scouts(monkeypatch, found=None, sent=None, coverage=None):
    """Scouts that return findings by the start of their statement (default: SUPPORT), with a report like a real
    scout's. The coverage scout returns `coverage`. Records every task they were sent."""
    found = SUPPORT if found is None else found

    def run(task, *, budget, context="", leads=(), avoid=(), say=None):
        if task.get("mode") == "coverage":
            COVERAGE_TASKS.append(task)
            return {"findings": [], "trail": [], "used": 1,
                    "report": {"coverage": coverage or [], "gap": "nobody tied it to NHTSA", "searched": ["news_search: tesla steering"]}}
        statement = task["statement"]
        if sent is not None:
            sent.append({"statement": statement, "budget": budget, "assignment": dict(task["assignment"]),
                         "guidance": reporter_agent.assignment_text(task), "leads": list(leads), "avoid": list(avoid)})
        hits = next((f for prefix, f in found.items() if statement.startswith(prefix)), [])
        verdict = scout_agent.held_to_evidence(hits[0]["finding"] if hits else "unclear", hits)
        return {"findings": hits, "used": 2,
                "trail": [{"tool": "web_search", "arg": f"q for {statement[:20]}", "why": "look", "result": "3 results"},
                          {"tool": "read", "arg": "R1", "why": "best result", "result": f"{len(hits)} quotes"}],
                "report": {"verdict": verdict, "confidence": "high", "summary": f"scout summary for {statement[:20]}",
                           "not_found": [], "proposals": [], "next_check": ""}}
    monkeypatch.setattr(scout_agent, "run", run)


def run(**kw):
    kw.setdefault("store", InMemorySignals())
    kw.setdefault("desk", InMemoryDesk())
    return reporter_agent.investigate(kw.pop("h", HYPOTHESIS), say=lambda *_: None, **kw)


# --- planning: how many scouts, and where they look ---------------------------------------------

def test_a_supported_hypothesis_is_published_and_recorded_on_the_desk(env, monkeypatch):
    model, desk, sent = FakeModel(), InMemoryDesk(), []
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch, sent=sent)

    result = run(desk=desk)

    assert model.steps()[:5] == ["frame", "plan", "direct", "memo", "write"]
    assert result["verdict"]["verdict"] == "write" and result["verdict"]["story"] == "maximum"
    assert result["final"]["status"] == "published" and result["checks"] == []
    assert "# Federal regulators are examining Model 3 steering" in Path(result["final"]["article"]).read_text(encoding="utf-8")
    # Sizing is recorded with its reason; McLovin's records reached the scouts verbatim, with budgets by priority.
    assert result["sizing"]["count"] == 3 and "settles E1 and E2 together" in result["sizing"]["why"]
    by = {s["statement"][:12]: s for s in sent}
    assert "NHTSA investigations file, 2023-2026" in by["NHTSA opened"]["guidance"]
    assert "It is contradicted if: the investigation was closed" in by["NHTSA opened"]["guidance"]
    assert by["NHTSA opened"]["budget"] == 18 and by["News outlets"]["budget"] == 7 and by["The steering"]["budget"] == 12
    # The desk has the whole investigation, compact, with its final status.
    [record] = desk.recent()
    assert record["_id"] == result["desk_id"] and record["status"] == "published" and record["stage"] == "done"
    assert record["sizing"]["count"] == 3 and record["sub_hypotheses"][0]["findings"][0]["quote"].startswith("NHTSA opened")
    assert record["story_id"] == result["story_id"] and [c["round"] for c in record["council"]] == [1]


def test_a_plan_that_misses_mclovins_records_or_an_alternative_is_sent_back(env, monkeypatch):
    weak = {**PLAN, "assignments": [ASSIGNMENTS[0], ASSIGNMENTS[1]]}          # no alternative; M2 never used
    plans = iter([weak, PLAN])
    model = FakeModel(plan=lambda user: next(plans))
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch)

    result = run(write=False)

    [first, second] = [u for s, u in model.calls if s == "plan"]
    assert "McLovin's records neither assigned nor deferred: M2" in second
    assert "no scout rules out an innocent explanation" in second
    assert result["sizing"]["count"] == 3 and any("repaired" in r for r in result["sizing"]["repairs"])


def test_what_the_plan_still_misses_is_filled_in_by_code(env, monkeypatch):
    lazy = {**PLAN, "assignments": [ASSIGNMENTS[1]]}                           # covers neither E1 nor E2, nor M1/M2
    monkeypatch.setattr(llm, "ask_json", FakeModel(plan=lazy))
    fake_scouts(monkeypatch)

    result = run(write=False)

    subs = result["sub_hypotheses"]
    added = [s for s in subs if s["origin"] == "desk"]
    assert [s["covers"] for s in added] == [["E1"], ["E2"]]
    assert all(s["needed_for"] == "minimum" and s["priority"] == "high" for s in added)
    records = {r for s in subs for r in s["assignment"]["records_first"]}
    assert {"NHTSA investigations file, 2023-2026", "NHTSA recalls for the 2023 Model 3"} <= records
    assert "no sub-hypothesis tests an innocent explanation" in result["checks"]


def test_the_plan_is_capped_keeping_what_the_minimum_story_needs(env, monkeypatch):
    many = [{**ASSIGNMENTS[1], "statement": f"Side question {n}.", "priority": "low"} for n in range(10)]
    monkeypatch.setattr(llm, "ask_json", FakeModel(plan={**PLAN, "assignments": many + ASSIGNMENTS}))
    fake_scouts(monkeypatch)
    result = run(write=False)
    statements = [s["statement"] for s in result["sub_hypotheses"]]
    assert len(statements) == reporter_agent.MAX_SUBS
    assert ASSIGNMENTS[0]["statement"] in statements and ASSIGNMENTS[2]["statement"] in statements


# --- verdicts -----------------------------------------------------------------------------------

def test_kill_when_the_minimum_story_is_contradicted_with_a_memo(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch, {"NHTSA opened": [finding(GOV, "NHTSA closed the review without action.", "contradicts")]})
    result = run()
    assert result["verdict"]["verdict"] == "kill" and "H1 is contradicted" in result["verdict"]["why"]
    assert result["final"]["status"] == "killed" and "article" not in result
    assert result["verdict"]["memo"]["would_change_it"]


def test_minimum_story_is_written_when_only_the_maximum_part_is_missing(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch, {k: v for k, v in SUPPORT.items() if k != "News outlets"})
    result = run(write=False)
    assert (result["verdict"]["verdict"], result["verdict"]["story"]) == ("write", "minimum")
    assert "H2" in result["verdict"]["why"]


def test_park_when_the_minimum_story_is_unsupported(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch, {"News outlets": SUPPORT["News outlets"]})
    assert run()["final"]["status"] == "parked"


def test_complaints_and_social_posts_never_count_as_proof(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch, {"": [finding("https://reddit.com/r/x", "my steering died", source_type="social")]})
    assert run()["verdict"]["verdict"] == "park"


def test_the_model_can_downgrade_a_verdict_but_never_upgrade_it(env, monkeypatch):
    fake_scouts(monkeypatch)
    monkeypatch.setattr(llm, "ask_json", FakeModel(memo={"downgrade_to": "park", "downgrade_why": "alternative untested",
                                                          "memo": {"checked": "c", "found": "f", "would_change_it": "w"}}))
    down = run()
    assert down["verdict"]["verdict"] == "park" and down["verdict"]["downgraded"]["from"] == "write"

    fake_scouts(monkeypatch, {})
    monkeypatch.setattr(llm, "ask_json", FakeModel(memo={"downgrade_to": "write", "memo": {}}))
    up = run(force=True)
    assert up["verdict"]["verdict"] == "park" and up["verdict"]["downgraded"] is None


def test_verdict_rules():
    sub = lambda i, need, found: {"id": i, "needed_for": need, "findings": found, "dropped": False}
    gov, against = [finding(GOV, "q")], [finding(GOV, "q2", "contradicts")]
    v = lambda subs: reporter_agent.verdict(subs, require_new=False)
    assert v([sub("H1", "minimum", gov + against)])[0] == "write"          # disputed isn't a kill
    assert v([sub("H1", "maximum", against), sub("H2", "minimum", gov)])[:2] == ("write", "minimum")
    assert v([sub("H1", "maximum", gov), sub("H2", "maximum", [])])[0] == "park"   # untagged -> all minimum


# --- directing the research ---------------------------------------------------------------------

def test_the_reporter_directs_scouts_between_rounds(env, monkeypatch):
    rounds = iter([
        {"assessments": [
            {"id": "H1", "action": "redirect", "why": "wrong system", "priority": "medium",
             "next_check": "NHTSA ODI resume for PE23-004", "search_terms": ["PE23-004", "steering assist"]},
            {"id": "H2", "action": "redirect", "why": "try harder", "next_check": "records"},     # refused: generic
            {"id": "H3", "action": "correct", "corrected_statement": "The steering loss is not explained by aftermarket wheels."}],
         "new_sub_hypotheses": [
             {"statement": "Tesla filed a Part 573 defect report on steering.", "records": ["NHTSA Part 573 filings for Tesla"]},
             {"statement": "Something vague is true.", "records": ["public records"]}],
         "spinoffs": [{"hypothesis": "Tesla's OTA fixes are not logged as recalls.", "why": "angle drift"}], "stop": False},
        {"assessments": [], "stop": True, "stop_reason": "plan covered"},
    ])
    monkeypatch.setattr(llm, "ask_json", FakeModel(direct=lambda user: next(rounds)))
    sent = []
    fake_scouts(monkeypatch, {"NHTSA opened": [], "News outlets": SUPPORT["News outlets"], "The steering": SUPPORT["The steering"],
                              "Tesla filed": SUPPORT["NHTSA opened"]}, sent=sent)
    store = InMemorySignals()
    sid, _ = store.add(Signal(summary="Owners say 2023 Model 3 steering fails", why_interesting="safety",
                              checkable_claim="steering loss on 2023 Model 3", origin="https://reddit.com/r/teslamotors/1",
                              accountable_party="Tesla", sources=[{"url": "https://reddit.com/r/teslamotors/1"}]))

    result = run(h={**HYPOTHESIS, "evidence_so_far": {"signal_ids": [sid]}}, store=store, write=False)

    subs = {s["id"]: s for s in result["sub_hypotheses"]}
    assert all(x["leads"] and x["leads"][0]["url"] == "https://reddit.com/r/teslamotors/1" for x in sent[:3])
    second = {x["statement"]: x for x in sent[3:]}
    # Only the properly redirected H1 and the granted H4 go out again; H2's "records" redirect was refused.
    assert set(second) == {subs["H1"]["statement"], "Tesla filed a Part 573 defect report on steering."}
    h1 = second[subs["H1"]["statement"]]
    assert "Check this next: NHTSA ODI resume for PE23-004" in h1["guidance"] and "PE23-004" in h1["guidance"]
    assert h1["budget"] == 12 and h1["avoid"] == ["web_search: q for NHTSA opened an inve"]
    actions = {a["id"]: a for a in result["rounds"][0]["actions"]}
    assert actions["H2"]["action"] == "done" and "refused" in actions["H2"]["why"]
    assert subs["H3"]["statement"].endswith("aftermarket wheels.") and subs["H3"]["findings"]
    assert [d["statement"] for d in result["declined"]] == ["Something vague is true."]
    assert result["spinoffs"][0]["hypothesis"].startswith("Tesla's OTA")
    assert result["verdict"]["verdict"] == "park"          # H1 (minimum) never found proof


def test_a_scout_the_reporter_does_not_mention_is_not_sent_again(env, monkeypatch):
    first = {"assessments": [{"id": "H1", "action": "redirect", "next_check": "NHTSA recalls API for 2023 Model 3"}], "stop": False}
    replies = iter([first, {"assessments": [], "stop": True}])
    monkeypatch.setattr(llm, "ask_json", FakeModel(direct=lambda user: next(replies)))
    sent = []
    fake_scouts(monkeypatch, sent=sent)
    run(write=False)
    assert [x["statement"][:12] for x in sent[3:]] == ["NHTSA opened"]


# --- the council, and back to the reporter ------------------------------------------------------

def test_the_skeptic_sends_it_back_and_the_reporter_fixes_it_with_a_new_scout(env, monkeypatch):
    skeptic = iter([{"verdict": "revise", "problems": [{"sentence": "Posts online raised the question.", "issue": "vague"}]}, APPROVE])
    revise = iter([
        {**DRAFT, "needs_research": [{"statement": "Tesla recall 24V051 covers the 2023 Model 3.", "records": ["NHTSA recall 24V051"]}]},
        {"headline": DRAFT["headline"], "paragraphs": [[DRAFT["paragraphs"][0][0]]], "changes": ["cut vague line"]},
    ])
    model = FakeModel(skeptic=lambda u: next(skeptic), revise=lambda u: next(revise))
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch, {**SUPPORT, "Tesla recall": [finding(GOV, "Recall 24V051 covers 2023 Model 3 vehicles.")]})

    result = run()

    assert result["final"]["status"] == "published" and len(result["council"]) == 2
    assert result["council"][0]["triage"]["decision"] == "fix"
    assert any(s["origin"] == "council" for s in result["sub_hypotheses"])
    assert any(s.get("quote") == "Recall 24V051 covers 2023 Model 3 vehicles." for s in result["article"]["sources"].values())


def test_the_virality_judge_is_a_gate_too_and_a_worthless_story_is_spiked(env, monkeypatch):
    desk = InMemoryDesk()
    flat = {"verdict": "revise", "problems": [{"sentence": "headline", "issue": "nobody affected would care"}]}
    monkeypatch.setattr(llm, "ask_json", FakeModel(virality=flat, triage={"decision": "spike", "why": "adds nothing new"}))
    fake_scouts(monkeypatch)

    result = run(desk=desk)

    assert result["final"]["status"] == "spiked" and "adds nothing new" in result["final"]["note"]
    assert not list(Path("published").glob("*.md"))
    assert desk.get(result["desk_id"])["status"] == "spiked"


def test_a_spike_without_a_reason_is_treated_as_a_fix(env, monkeypatch):
    flat = {"verdict": "revise", "problems": [{"sentence": "headline", "issue": "dull"}]}
    virality = iter([flat, APPROVE])
    monkeypatch.setattr(llm, "ask_json", FakeModel(virality=lambda u: next(virality), triage={"decision": "spike", "why": ""}))
    fake_scouts(monkeypatch)
    r = run(); assert r["final"]["status"] == "published", r["final"]


def test_novelty_is_a_gate_too(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel(novelty={"verdict": "revise", "problems": [{"sentence": "headline", "issue": "already reported"}]},
                                                   triage={"decision": "spike", "why": "Reuters reported all of it"}))
    fake_scouts(monkeypatch)
    assert run()["final"]["status"] == "spiked"


def test_a_draft_the_gates_never_approve_is_held_not_published(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel(skeptic={"verdict": "revise", "problems": [{"sentence": "headline", "issue": "x"}]}))
    fake_scouts(monkeypatch)
    result = run()
    assert result["final"]["status"] == "held" and len(result["council"]) == reporter_agent.MAX_REVISIONS + 1
    assert "article" in result and not list(Path("published").glob("*.md"))


# --- the desk: memory and recording --------------------------------------------------------------

def test_a_hypothesis_worked_on_another_machine_is_not_chased_twice(env, monkeypatch):
    desk = InMemoryDesk()
    desk.save({"hypothesis_id": HYPOTHESIS["id"], "hypothesis": HYPOTHESIS["hypothesis"], "status": "killed",
               "final": {"note": "contradicted by the recall"}})
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch)
    result = run(desk=desk)
    assert result["skipped"] and result["status"] == "killed"


def test_similar_past_investigations_reach_the_reporter(env, monkeypatch):
    desk = InMemoryDesk()
    desk.save({"hypothesis_id": "h-other", "status": "parked", "accountable_party": "Tesla",
               "hypothesis": "NHTSA is investigating steering loss on the Tesla Model 3.",
               "verdict": {"memo": {"found": "no open investigation in the 2024 file", "would_change_it": "a new ODI filing"}},
               "final": {"note": "not yet supported"}})
    model = FakeModel()
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch)
    result = run(desk=desk, write=False)
    frame_prompt = next(u for s, u in model.calls if s == "frame")
    assert "[parked]" in frame_prompt and "a new ODI filing" in frame_prompt
    assert result["memory"][0]["status"] == "parked"


def test_a_broken_desk_never_stops_the_reporting(env, monkeypatch):
    class Broken(InMemoryDesk):
        def save(self, record):
            raise ConnectionError("Astra is down")
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch)
    assert run(desk=Broken())["final"]["status"] == "published"


def test_when_every_scout_fails_the_error_surfaces_and_is_recorded(env, monkeypatch):
    desk = InMemoryDesk()
    monkeypatch.setattr(llm, "ask_json", FakeModel())

    def broken(*a, **k):
        raise web.SearchError("BRAVE_API_KEY is not set")
    monkeypatch.setattr(scout_agent, "run", broken)
    with pytest.raises(web.SearchError):
        run(desk=desk)
    story = env.execute("select status, note from stories order by id desc").fetchone()
    assert story["status"] == "failed" and "BRAVE_API_KEY" in story["note"]
    assert desk.recent()[0]["status"] == "failed"


def test_a_plain_string_hypothesis_works_and_show_can_print_it(env, monkeypatch, capsys):
    from newsroom.__main__ import show
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch)
    result = run(h="NHTSA is investigating Model 3 steering.", write=False)
    show(env, result["story_id"])
    out = capsys.readouterr().out
    assert "McLovin hypothesis h-" in out and "H1:" in out and "[supports]" in out and "3 scouts" in out
    saved = json.loads(Path(result["run_dir"], "result.json").read_text(encoding="utf-8"))
    assert saved["sizing"]["count"] == 3 and saved["desk_id"]


def test_the_reporter_sends_a_coverage_scout_and_plans_from_its_report(env, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch, coverage=[{"url": NEWS, "outlet": "Reuters", "what_it_established": "owners' reports"}])
    run(write=False)
    [task] = COVERAGE_TASKS
    assert task["mode"] == "coverage" and task["searches"] == ["tesla steering nhtsa"]
    plan_prompt = next(u for s, u in model.calls if s == "plan")
    assert "Reuters" in plan_prompt and "nobody tied it to NHTSA" in plan_prompt


def test_scout_reports_reach_the_reporter_and_published_articles_carry_the_trail(env, monkeypatch):
    model, captured = FakeModel(memo={**DEFAULTS["memo"], "narrowed_hypothesis": "NHTSA opened EA24-002, which covers the 2023 Model 3."}), {}
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch)
    from newsroom import article
    real = article.publish

    def publish(story_id, draft, sources, out_dir, site=None):
        captured.update(site=site, draft=draft)
        return real(story_id, draft, sources, out_dir, site=site)
    monkeypatch.setattr(article, "publish", publish)
    result = run()
    direct = next(u for s, u in model.calls if s == "direct")
    assert "scout's report: supports (high confidence). scout summary for NHTSA opened" in direct
    site = captured["site"]
    assert site["reporting"]["hypothesis"].startswith("NHTSA opened EA24-002")
    assert [c["result"] for c in site["reporting"]["checks"]] == ["supported"] * 3
    assert {c["judge"] for c in site["council"]} == {"skeptic", "virality", "novelty"}
    assert result["sub_hypotheses"][0]["reports"][0]["verdict"] == "supports"


# --- only what no outlet has published --------------------------------------------------------------

def test_a_story_whose_findings_are_all_already_published_is_parked_not_written(env, monkeypatch):
    """Everything holds, but the only support for the NEW scout is a news report: it's already published."""
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    news_only = {**SUPPORT, "NHTSA opened": [finding(NEWS, "NHTSA opened an investigation.", source_type="news_report")]}
    fake_scouts(monkeypatch, news_only)
    result = run()
    assert result["verdict"]["verdict"] == "park" and "nothing new" in result["verdict"]["why"]
    assert result["final"]["status"] == "parked" and "article" not in result


def test_a_record_prior_coverage_already_cited_is_not_new(env, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch, coverage=[{"url": GOV, "outlet": "Reuters", "what_it_established": "the investigation"}])
    result = run()
    assert result["verdict"]["verdict"] == "park" and "nothing new" in result["verdict"]["why"]


def test_the_new_finding_leads_the_article_and_is_recorded(env, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch)
    result = run()
    nf = result["verdict"]["new_finding"]
    assert nf["id"] == "H1" and nf["novelty"] == "record" and nf["url"] == GOV
    write_prompt = next(u for s, u in model.calls if s == "write")
    assert "THE NEW FINDING (lead with it" in write_prompt and "NHTSA opened an engineering analysis" in write_prompt
    assert result["sizing"]["new_scouts"] == ["H1"] and result["final"]["status"] == "published"


def test_a_plan_with_no_new_scout_is_sent_back(env, monkeypatch):
    stale = {**PLAN, "assignments": [{**a, "new": False} for a in ASSIGNMENTS]}
    plans = iter([stale, PLAN])
    model = FakeModel(plan=lambda user: next(plans))
    monkeypatch.setattr(llm, "ask_json", model)
    fake_scouts(monkeypatch)
    run(write=False)
    second = [u for s, u in model.calls if s == "plan"][1]
    assert "goes after something no outlet has published" in second


def test_a_story_that_is_already_fully_reported_is_spiked_before_any_scout_goes_out(env, monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "ask_json", FakeModel(plan={**PLAN, "no_new_angle": True,
                                                         "no_new_angle_why": "Reuters and AP reported the investigation and its scope"}))
    fake_scouts(monkeypatch, sent=sent, coverage=[{"url": NEWS, "outlet": "Reuters", "what_it_established": "everything"}])
    result = run()
    assert result["final"]["status"] == "spiked" and "Reuters and AP" in result["final"]["note"]
    assert sent == []                                            # no budget spent
    assert result["verdict"]["memo"]["would_change_it"]


def test_the_coverage_scout_marks_published_elements_for_the_plan(env, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(llm, "ask_json", model)

    def run_scout(task, **kw):
        if task.get("mode") == "coverage":
            assert [e["id"] for e in task["elements"]] == ["E1", "E2", "E3"]
            return {"findings": [], "trail": [], "report": {"coverage": [], "reported_elements": ["E3"], "status": "partly_reported"}}
        return {"findings": SUPPORT.get(task["statement"][:12], []), "trail": [], "report": {"verdict": "unclear"}}
    monkeypatch.setattr(reporter_agent.scout_agent, "run", run_scout)
    result = run(write=False)
    plan_prompt = next(u for s, u in model.calls if s == "plan")
    assert "E3 [maximum, ALREADY PUBLISHED] owners were hurt" in plan_prompt
    assert result["sizing"]["reported_elements"] == ["E3"] and result["sizing"]["coverage_status"] == "partly_reported"


def test_the_rule_can_be_switched_off_for_pipeline_testing(env, monkeypatch):
    monkeypatch.setenv("NEWSROOM_REQUIRE_NEW", "0")
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    news = finding(NEWS, "NHTSA opened an engineering analysis into loss of steering control.", source_type="news_report")
    fake_scouts(monkeypatch, {**SUPPORT, "NHTSA opened": [news]})
    r = run(); assert r["final"]["status"] == "published", r["final"]
