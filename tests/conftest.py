import pytest

from newsroom import db


@pytest.fixture(autouse=True)
def default_backends(monkeypatch):
    """Tests assume the default backends, whatever a developer's .env picks (e.g. claude_code, browser),
    so a local .env can never make the suite call a real model or open a browser."""
    monkeypatch.setenv("NEWSROOM_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("NEWSROOM_SEARCH_BACKEND", "brave")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A fresh database in a temp folder, with no API keys, so tests never reach the network."""
    monkeypatch.setenv("NEWSROOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("NEWSROOM_PUBLISHED", str(tmp_path / "published"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("BRAVE_API_KEY", "")
    connection = db.connect()
    yield connection
    connection.close()


def complaint(id, *, product="2006 CHEVROLET COBALT", text="The engine shut off while I was driving on the highway at speed.",
              received="2026-09-01", severe=False, company="General Motors LLC", source="nhtsa"):
    return {"id": id, "source": source, "received": received, "product": product, "company": company,
            "severe": severe, "text": text, "fields": {}}
