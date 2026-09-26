"""Bossman, McLovin and the council, with the model stubbed out. No network, no tokens."""

import pytest

from newsroom import article, bossman, council, llm, mclovin, playbooks
from newsroom.signals import InMemorySignals, Signal


def cand(cid, title, url, source_type="reddit", spike="#1 hot in r/news"):
    return {"id": cid, "source_type": source_type, "title": title, "url": url, "snippet": "",
            "spike": {"kind": "rank", "value": spike}, "seen_at": "2026-09-26T12:00:00+00:00"}


CANDIDATES = [
    cand("reddit:1", "Pennsylvania measles outbreak grows to 890 cases", "https://reddit.com/r/news/comments/1/measles"),
    cand("news:2", "Measles outbreak in Pennsylvania passes 890 cases", "https://example-news.com/measles", "google_news", "#3 in top"),
    cand("pm:3", "Faroe Islands vs Kazakhstan", "https://polymarket.com/market/far-kaz", "polymarket", "+40% in 24h"),
]


def test_every_playbook_loads():
    for agent in ("bossman", "mclovin", "reporter", "scout", "council/skeptic", "council/virality", "council/novelty"):
        assert "playbook" in playbooks.load(agent).lower() or len(playbooks.load(agent)) > 500


def test_bossman_turns_decisions_into_signals_with_gathered_urls(monkeypatch):
    decisions = {"signals": [{
        "candidate_ids": ["reddit:1", "news:2", "made-up:9"],
        "summary": "Pennsylvania's measles outbreak has grown to 890 cases, per the state health department",
        "why_interesting": "moving now; state health department accountable; case counts are checkable",
        "accountable_party": "Pennsylvania Department of Health",
        "checkable_claim": "The state reports 890 measles cases",
        "records_trail": ["state health department case counts", "CDC measles data"],
        "origin": "https://reddit.com/r/news/comments/1/measles",
    }], "skipped": [{"candidate_id": "pm:3", "reason": "sports market"}]}
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: decisions)

    signals = bossman.to_signals(bossman.judge(CANDIDATES), CANDIDATES)
    assert len(signals) == 1
    s = signals[0]
    # URLs and spikes come from what was gathered, never from the model; unknown ids are dropped.
    assert [x["url"] for x in s.sources] == ["https://reddit.com/r/news/comments/1/measles", "https://example-news.com/measles"]
    assert s.source_types == ["google_news", "reddit"] and s.spike["value"]
    assert bossman.hard_checks(s) == []


def test_bossman_resolves_ids_the_model_mangled():
    decisions = {"signals": [{"candidate_ids": ["1", "[news:2]"], "summary": "s", "why_interesting": "w",
                              "checkable_claim": "c", "accountable_party": "a", "records_trail": [], "origin": ""}]}
    cands = [cand("reddit:1", "t", "https://a.com/1"), cand("news:2", "t", "https://b.com/2", "google_news")]
    assert len(bossman.to_signals(decisions, cands)[0].sources) == 2


def test_bossman_hard_checks_catch_missing_spike_and_handles():
    s = Signal(summary="u/throwaway123 says the plant is leaking", why_interesting="x",
               checkable_claim="y", origin="https://reddit.com/x", sources=[{"url": "https://reddit.com/x"}])
    problems = bossman.hard_checks(s)
    assert any("spike" in p for p in problems) and any("handle" in p for p in problems)


def test_bossman_run_merges_repeats_across_passes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    decisions = {"signals": [{"candidate_ids": ["reddit:1"], "summary": "Measles outbreak grows to 890 cases",
                              "why_interesting": "moving now", "accountable_party": "PA Department of Health",
                              "checkable_claim": "890 cases", "records_trail": [], "origin": ""}], "skipped": []}
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: decisions)
    monkeypatch.setattr(bossman, "gather", lambda sources=None: (CANDIDATES[:1], {}))
    store = InMemorySignals()
    first = bossman.run_once(store, say=lambda *_: None)
    second = bossman.run_once(store, say=lambda *_: None)
    assert len(first["created"]) == 1 and len(second["merged"]) == 1
    assert (tmp_path / first["run_dir"] / "candidates.json").exists()


def test_mclovin_counts_origins_itself(monkeypatch):
    store = InMemorySignals()
    a, _ = store.add(Signal(summary="Recall A", why_interesting="w", checkable_claim="c", origin="https://nhtsa.gov/r/1",
                            accountable_party="Acme", spike={"value": "1"}))
    b, _ = store.add(Signal(summary="Totally different boiler story", why_interesting="w", checkable_claim="boilers",
                            origin="https://reddit.com/r/x/2", accountable_party="Other", spike={"value": "1"}))
    reply = {"hypotheses": [{
        "hypothesis": "Acme's heaters were recalled after complaints the company says it never received.",
        "why_now": "recall published this week", "who_would_know": ["NHTSA", "Acme"],
        "would_settle_it": ["NHTSA recall filing 26V-001", "complaints to NHTSA 2025-2026"],
        "accountable_party": "Acme", "signal_ids": [a, b, "nope"]}]}
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: reply)
    h = mclovin.finish(mclovin.read(store.recent("")), store.recent(""))[0]
    assert h["evidence_so_far"]["distinct_origins"] == 2
    assert any("don't exist" in p for p in h["problems"])


def test_mclovin_rejects_generic_settle_it():
    h = {"hypothesis": "X did Y.", "why_now": "now", "accountable_party": "X", "who_would_know": ["X"],
         "would_settle_it": ["public records", "documents"], "evidence_so_far": {"signal_ids": ["s1"]}}
    assert any("generic" in p for p in mclovin.hard_checks(h))


def test_council_fixture_passes_mechanical_checks():
    draft = council.load_draft()
    assert article.check(draft["article"], draft["sources"]) == []


@pytest.mark.parametrize("name", list(council.MUTATIONS))
def test_every_seeded_error_changes_the_draft(name):
    base = council.load_draft()
    planted, where = council.MUTATIONS[name](base)
    assert planted != base and where


def test_seeded_reports_mechanical_and_judge_catches(monkeypatch):
    def fake_skeptic(system, user, **kw):
        # Flags the last sentence of the body, which is where _append plants errors, plus the headline.
        body = [l for l in user.split("SENTENCES:\n")[1].split("\n\nSOURCES")[0].splitlines() if l.startswith("- ")]
        last = body[-1][2:].split("  [cites")[0]
        return {"verdict": "revise", "problems": [{"sentence": last, "issue": "planted"},
                                                  {"sentence": "headline", "issue": "planted"}]}
    monkeypatch.setattr(llm, "ask_json", fake_skeptic)
    result = council.seeded(say=lambda *_: None)
    assert result["total"] == len(council.MUTATIONS) and result["caught"] == result["total"]
    assert sum(r["caught_by"] == "mechanical" for r in result["rows"]) == 5


def test_claude_backend_caches_parsed_json_not_raw_replies(monkeypatch, tmp_path):
    import json as _json
    import subprocess
    from newsroom import db, llm_claude
    monkeypatch.setenv("NEWSROOM_DB", str(tmp_path / "c.db"))
    reply = {"is_error": False, "result": 'Here you go:\n```json\n{"ok": true}\n```', "usage": {}}
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=_json.dumps(reply), stderr=""))
    assert llm_claude.ask_json("sys", "user", model="claude-haiku-4-5") == {"ok": True}
    assert llm_claude.ask_json("sys", "user", model="claude-haiku-4-5") == {"ok": True}   # served from cache
    with db.session() as conn:
        assert _json.loads(conn.execute("select response from llm_cache").fetchone()["response"]) == {"ok": True}


def test_bossman_beat_pass_plans_traces_and_follows_up(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from newsroom import gather
    calls = []

    def fake_step(tool, arg):
        calls.append((tool, arg))
        if arg == "gatech":
            return [cand("reddit:a", "Housing fee jumps 12% for fall", "https://reddit.com/r/gatech/a"),
                    cand("reddit:b", "Anyone want to get boba", "https://reddit.com/r/gatech/b")]
        if arg == "GT housing fee":
            return [cand("news:c", "Georgia Tech raises housing rates", "https://ajc.com/c", "news_search"),
                    cand("reddit:a", "Housing fee jumps 12% for fall", "https://reddit.com/r/gatech/a")]
        return []

    def fake_llm(system, user, **kw):
        if "plan this pass" in system:
            return {"steps": [{"tool": "subreddit", "arg": "gatech", "why": "student reaction"},
                              {"tool": "federal_register", "arg": "Georgia Institute of Technology", "why": "federal"}]}
        if "look back at this pass" in system:
            assert "step 2" in user and "0 kept as evidence" in user
            return {"assessment": "ok", "dead_ends": [{"step": 2, "why": "nothing new"}],
                    "follow_ups": [{"tool": "news_search", "arg": "GT housing fee", "why": "confirm", "lead": "fee"}],
                    "beat_notes": ["add the housing office feed"]}
        ids = [line.split("]")[0][1:] for line in user.splitlines() if line.startswith("[")]
        keep = [i for i in ids if i in ("reddit:a", "news:c")]
        return {"signals": [{"candidate_ids": keep, "summary": "Georgia Tech raised housing rates 12%",
                             "why_interesting": "money", "accountable_party": "Georgia Tech Housing",
                             "checkable_claim": "rates rose 12%", "records_trail": ["Regents minutes"], "origin": ""}] if keep else [],
                "skipped": [{"candidate_id": i, "reason": "off beat"} for i in ids if i not in keep]}

    monkeypatch.setattr(gather, "run_step", fake_step)
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    store = InMemorySignals()
    report = bossman.run_once(store, beat="georgia-tech", say=lambda *_: None)

    assert calls[-1] == ("news_search", "GT housing fee")          # the follow-up ran
    trace = report["reflection"] and __import__("json").loads((tmp_path / report["run_dir"] / "trace.json").read_text())
    assert [t["round"] for t in trace] == [1, 1, 2]
    assert trace[0]["kept"] == 1 and trace[0]["skip_reasons"] == ["off beat"]
    assert trace[2]["new"] == 1                                     # reddit:a was already found in round 1
    signals = store.recent("")
    assert signals and all(s.beats == ["georgia-tech"] for s in signals)


def test_bossman_consolidates_same_event_signals_from_different_batches(monkeypatch):
    decisions = {"signals": [
        {"candidate_ids": ["a"], "summary": "Regents pick Fanning", "records_trail": ["minutes"]},
        {"candidate_ids": ["b"], "summary": "New residence hall opens", "records_trail": []},
        {"candidate_ids": ["c", "d"], "summary": "Fanning named sole finalist", "records_trail": ["search contract"]},
    ], "skipped": []}
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"groups": [[0, 2], [1, 99]]})
    assert bossman.consolidate(decisions) == [[0, 2]]
    assert [s["summary"] for s in decisions["signals"]] == ["New residence hall opens", "Fanning named sole finalist"]
    merged = decisions["signals"][1]
    assert merged["candidate_ids"] == ["c", "d", "a"] and merged["records_trail"] == ["search contract", "minutes"]


def test_bossman_origin_is_always_a_bare_url():
    cands = [cand("news:1", "t", "https://ajc.com/story")]
    base = {"candidate_ids": ["news:1"], "summary": "s", "why_interesting": "w", "checkable_claim": "c",
            "accountable_party": "a", "records_trail": []}
    for origin, expected in [("The Atlanta Journal-Constitution", "https://ajc.com/story"),
                             ("https://www.usg.edu (USG press release, via WABE)", "https://ajc.com/story"),
                             ("https://www.usg.edu/news/1", "https://www.usg.edu/news/1")]:
        signal = bossman.to_signals({"signals": [{**base, "origin": origin}]}, cands)[0]
        assert signal.origin == expected


def test_bossman_merges_a_rewrite_of_a_stored_event(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    store = InMemorySignals()
    old_id, _ = store.add(Signal(summary="Regents name Fanning sole finalist for chancellor", why_interesting="w",
                                 checkable_claim="c", origin="https://news.example/a", accountable_party="USG Board of Regents",
                                 spike={"value": "1"}, sources=[{"url": "https://news.example/a"}]))
    decisions = {"signals": [{"candidate_ids": ["news:2"], "summary": "Former Southern Company CEO picked to lead USG",
                              "why_interesting": "governance", "accountable_party": "University System of Georgia",
                              "checkable_claim": "the vote", "records_trail": [], "origin": ""}], "skipped": []}

    def fake(system, user, **kw):
        if "about to be stored" in system:
            assert "Regents name Fanning" in user
            return {"same_as": 0}
        return decisions
    monkeypatch.setattr(llm, "ask_json", fake)
    monkeypatch.setattr(bossman, "gather", lambda sources=None: (CANDIDATES[1:2], {}))
    report = bossman.run_once(store, say=lambda *_: None)
    assert report["merged"] and not report["created"]
    assert len(store.get(old_id).sources) == 2


def test_bossman_next_plan_sees_the_last_pass():
    prev = {"reflection": {"assessment": "thin", "dead_ends": [{"step": 2, "why": "all sports"}],
                           "productive": [], "beat_notes": ["drop hot"]},
            "trace": [{"step": 2, "tool": "news_search", "arg": "Atlanta"}], "when": "14:00 EDT", "stored": ["x"]}
    text = bossman._previous(prev)
    assert "Dead end: news_search('Atlanta'): all sports" in text and "Stored last pass: x" in text
    assert bossman._previous(None) == ""


def _leaf(i, **kw):
    return {"id": f"l{i}", "path": ["Area", f"Part {i}"], "look_for": "", "records": [], "queries": [],
            "added_by": "survey", "added_at": "", "status": "active", "visits": 0, "last_visit": None,
            "kept": 0, "empty_visits": 0, **kw}


def test_beat_map_prefers_unvisited_and_productive_parts_and_prunes_dead_ones():
    import random
    from datetime import datetime, timezone
    from newsroom import beatmap
    just_now = datetime.now(timezone.utc).isoformat()
    m = {"leaves": [_leaf(1), _leaf(2, last_visit=just_now, empty_visits=2), _leaf(3, status="pruned")]}
    picks = [beatmap.pick(m, 1, random.Random(seed))[0]["id"] for seed in range(200)]
    assert "l3" not in picks and picks.count("l1") > 190          # never pruned; stale beats just-visited-and-empty

    leaf = m["leaves"][1]
    beatmap.record_visits(m, [leaf], [{"leaf": "l2", "kept": 0}])
    assert leaf["status"] == "pruned" and leaf["visits"] == 1
    beatmap.record_visits(m, [m["leaves"][0]], [{"leaf": "l1", "kept": 2}])
    assert m["leaves"][0]["kept"] == 2 and m["leaves"][0]["empty_visits"] == 0

    added = beatmap.add(m, [{"path": ["Area", "Part 1"]}, {"path": ["Area", "Brand new"], "look_for": "x"}])
    assert [x["path"][-1] for x in added] == ["Brand new"] and added[0]["added_by"] == "reflection"


def test_bossman_focused_pass_runs_sweep_and_skips_what_it_already_judged(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from newsroom import beatmap, gather
    beatmap.save("georgia-tech", {"beat": "georgia-tech", "built_at": "", "leaves": [_leaf(1)],
                                  "sweep": [{"tool": "subreddit", "arg": "gatech", "why": "breaking"}]})

    def fake_step(tool, arg):
        return [cand("reddit:a", "Housing fee jumps 12%", "https://reddit.com/r/gatech/a")] if arg == "gatech" else []

    systems = []

    def fake_llm(system, user, **kw):
        systems.append(system)
        if "plan this pass" in system:
            return {"steps": [{"tool": "news_search", "arg": "GT fees", "why": "focus", "leaf": "l1"}]}
        if "look back at this pass" in system:
            return {"assessment": "ok", "new_leaves": [{"path": ["Money", "Housing rates"]}]}
        ids = [line.split("]")[0][1:] for line in user.splitlines() if line.startswith("[")]
        return {"signals": [], "skipped": [{"candidate_id": i, "reason": "meh"} for i in ids]}

    monkeypatch.setattr(gather, "run_step", fake_step)
    monkeypatch.setattr(llm, "ask_json", fake_llm)
    first = bossman.run_once(InMemorySignals(), beat="georgia-tech", say=lambda *_: None)
    assert any("Part 1" in s for s in systems if "plan this pass" in s)            # the focus reached the plan
    trace = first["trace"]
    assert trace[0]["leaf"] == "sweep" and trace[1]["leaf"] == "l1"
    assert first["candidates"] == 1

    second = bossman.run_once(InMemorySignals(), beat="georgia-tech", say=lambda *_: None)
    assert second["candidates"] == 0 and second["trace"][0]["already_judged"] == 1   # not judged twice
    m = beatmap.load("georgia-tech")
    assert m["leaves"][0]["visits"] == 2 and [x["path"] for x in m["leaves"]][-1] == ["Money", "Housing rates"]
