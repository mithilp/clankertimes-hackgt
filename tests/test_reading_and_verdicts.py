"""Reading records the scouts couldn't read before (ZIPs, tables, long documents), and the verdict fixes that
stop wrong kills and endless "not new" rewrites. No network, no tokens."""

import io
import zipfile

from newsroom import llm, reporter_agent, scout_agent, web
from newsroom.desk import InMemoryDesk
from tests.test_reporter_agent import APPROVE, GOV, NEWS, FakeModel, fake_scouts, finding, run
from tests.test_reporter_agent import env  # noqa: F401 - the fixture

INDEX = "Prefix\tLast\tFirst\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n" \
        "Hon.\tCastor\tKathy\tO\tFL14\t2025\t5/15/2026\t10071111\n" \
        "Hon.\tSpartz\tVictoria\tP\tIN05\t2025\t2/10/2025\t20026754\n"


def test_tab_separated_rows_keep_their_column_names():
    text = web._delimited_text(INDEX)
    assert "Last: Spartz | First: Victoria | FilingType: P | StateDst: IN05 | Year: 2025 | FilingDate: 2/10/2025 | DocID: 20026754" in text


def test_a_zip_index_is_read_and_its_xml_copy_skipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2025FD.txt", INDEX)
        z.writestr("2025FD.xml", "<Member><Last>Spartz</Last></Member>")
    text = web._zip_text(buf.getvalue())
    assert "== 2025FD.txt ==" in text and "DocID: 20026754" in text and "2025FD.xml" not in text


def test_html_tables_keep_their_headers():
    html = ("<table><thead><tr><th>Fiscal year</th><th>Receivable</th></tr></thead>"
            "<tbody><tr><td>FY2024</td><td>$235,748,158</td></tr></tbody></table>")
    assert "Fiscal year: FY2024 | Receivable: $235,748,158" in web._html_text(html)


def test_find_returns_the_matching_lines_of_a_long_document(conn, monkeypatch):
    rows = "\n".join(f"Last: Member{n} | DocID: {n}" for n in range(5000)) + "\nLast: Spartz | FilingType: P | DocID: 20026754"
    monkeypatch.setattr(web, "fetch_text", lambda url: rows)
    s = scout_agent.Scout({"id": "H1", "statement": "x", "assignment": {}}, budget=3)
    out = s.call("find", "https://disclosures-clerk.house.gov/2025FD.zip | spartz")
    assert out.startswith("1 matching line(s)") and "DocID: 20026754" in out


def test_a_focused_read_shows_the_part_that_matters_and_quotes_still_check():
    long = "filler words here. " * 5000 + "Victoria Spartz filed PTR 20026754 on 2/10/2025. " + "more filler. " * 3000
    seen = scout_agent.focused(long, "spartz")
    assert len(seen) <= scout_agent.MAX_PAGE_CHARS and "Victoria Spartz filed PTR 20026754" in seen
    assert scout_agent.focused("short page", "anything") == "short page"


def test_news_reporting_alone_cannot_kill_a_story(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    news_against = [finding(NEWS, "In 2021, she was caught violating the STOCK Act.", "contradicts", "news_report")]
    fake_scouts(monkeypatch, {"NHTSA opened": news_against})
    result = run()
    assert result["verdict"]["verdict"] == "park"
    assert "contradicted only by news reporting" in result["verdict"]["why"]


def test_a_primary_record_still_kills(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(llm, "ask_json", FakeModel())
    fake_scouts(monkeypatch, {"NHTSA opened": [finding(GOV, "NHTSA closed the review without action.", "contradicts")]})
    assert run()["verdict"]["verdict"] == "kill"


def test_two_not_new_rounds_without_a_new_record_to_chase_spike_the_story(env, monkeypatch):  # noqa: F811
    desk = InMemoryDesk()
    not_new = {"verdict": "revise", "problems": [{"sentence": "headline", "issue": "Axios reported this last week"}]}
    triages = []

    def triage(user):
        triages.append(user)
        return {"decision": "fix", "why": "reword the lede", "fixes": ["move the finding up"], "research": []}
    monkeypatch.setattr(llm, "ask_json", FakeModel(novelty=not_new, triage=triage))
    fake_scouts(monkeypatch)
    result = run(desk=desk)
    assert result["final"]["status"] == "spiked" and "nothing new in 2 rounds running" in result["final"]["note"]
    assert "NOTHING NEW IN 2 ROUNDS RUNNING" in triages[-1] and "NOTHING NEW" not in triages[0]


def test_after_two_not_new_rounds_research_is_still_allowed(env, monkeypatch):  # noqa: F811
    verdicts = iter([{"verdict": "revise", "problems": [{"sentence": "headline", "issue": "reported"}]}] * 2 + [APPROVE] * 5)
    monkeypatch.setattr(llm, "ask_json", FakeModel(
        novelty=lambda u: next(verdicts),
        triage={"decision": "fix", "why": "chase the audit table", "fixes": [],
                "research": [{"statement": "The ACFR lists the receivable by year.", "records": ["Atlanta ACFR FY2022-2026"]}]}))
    fake_scouts(monkeypatch)
    assert run()["final"]["status"] != "spiked"
