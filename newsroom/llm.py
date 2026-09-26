"""DeepSeek calls that return JSON. Replies are cached in SQLite, so reruns are free and identical."""

import hashlib
import json
import threading
import time

from openai import OpenAI

from . import config, db


class LLMError(RuntimeError):
    pass


_client: OpenAI | None = None
_lock = threading.Lock()


def _get_client() -> OpenAI:
    global _client
    with _lock:
        if _client is None:
            settings = config.load()
            if not settings.deepseek_api_key:
                raise LLMError("DEEPSEEK_API_KEY is not set: add it to .env")
            _client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
                             max_retries=3, timeout=300)
        return _client


def ask_json(system: str, user: str, *, model: str | None = None, max_tokens: int = 4000,
             thinking: bool = False) -> dict:
    """Ask the model and parse its JSON reply. The prompt must mention JSON (DeepSeek's JSON mode requires it)."""
    model = model or config.load().fast_model
    key = hashlib.sha256(json.dumps([model, system, user, max_tokens, thinking]).encode()).hexdigest()
    with db.session() as conn:
        row = conn.execute("select response from llm_cache where key = ?", (key,)).fetchone()
    if row:
        return json.loads(row["response"])

    request = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
    }
    if not thinking:
        request["temperature"] = 0  # thinking mode ignores temperature

    client = _get_client()
    for attempt in range(3):
        response = client.chat.completions.create(**request)
        _record_usage(model, response)
        content = (response.choices[0].message.content or "").strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # DeepSeek's JSON mode occasionally returns empty content; retrying usually fixes it.
            time.sleep(2 * (attempt + 1))
            continue
        if isinstance(data, dict):
            with db.session() as conn:
                conn.execute("insert or replace into llm_cache (key, response) values (?, ?)", (key, content))
            return data
    raise LLMError(f"{model} did not return valid JSON after 3 attempts")


def _record_usage(model: str, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    with db.session() as conn:
        conn.execute("insert into llm_usage (at, model, prompt_tokens, completion_tokens) values (?, ?, ?, ?)",
                     (db.now(), model, usage.prompt_tokens, usage.completion_tokens))
