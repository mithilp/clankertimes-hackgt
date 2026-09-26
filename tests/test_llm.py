from types import SimpleNamespace

import pytest

from newsroom import llm


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        content = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


@pytest.fixture
def fake(conn, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def install(*replies):
        client = FakeClient(replies)
        monkeypatch.setattr(llm, "_get_client", lambda: client)
        return client
    return install


def test_empty_reply_is_retried(fake):
    client = fake("", '{"ok": true}')
    assert llm.ask_json("Reply in JSON.", "hi") == {"ok": True}
    assert len(client.requests) == 2


def test_replies_are_cached(fake):
    client = fake('{"n": 1}')
    assert llm.ask_json("Reply in JSON.", "same question") == {"n": 1}
    assert llm.ask_json("Reply in JSON.", "same question") == {"n": 1}
    assert len(client.requests) == 1


def test_thinking_is_off_and_json_mode_is_on_by_default(fake):
    client = fake('{"n": 1}')
    llm.ask_json("Reply in JSON.", "q")
    request = client.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["temperature"] == 0


def test_gives_up_after_three_bad_replies(fake):
    fake("", "not json", "")
    with pytest.raises(llm.LLMError):
        llm.ask_json("Reply in JSON.", "q")


def test_usage_is_recorded(fake, conn):
    fake('{"n": 1}')
    llm.ask_json("Reply in JSON.", "count me")
    row = conn.execute("select prompt_tokens, completion_tokens from llm_usage").fetchone()
    assert (row[0], row[1]) == (10, 5)
