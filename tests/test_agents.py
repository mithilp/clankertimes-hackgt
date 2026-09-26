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
