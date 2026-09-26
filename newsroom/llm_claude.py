"""Claude Code backend: the same ask_json contract, run through the local `claude` CLI.

Why: `claude -p` is Anthropic's own harness, so a Claude Pro/Max subscription covers the usage
instead of billing per token. DeepSeek stays the default; this is for running locally on a
subscription. Switch with NEWSROOM_LLM_PROVIDER=claude_code.

Requires the CLI installed (`curl -fsSL https://claude.ai/install.sh | bash`) and logged in
(`claude`, then /login). Replies are cached in SQLite exactly like the DeepSeek path, and token
counts land in llm_usage, so cost reporting keeps working.

Three things that will waste your afternoon if you touch this:
  * --bare skips credential discovery, so every call comes back "Not logged in". Don't add it.
  * stdin must be /dev/null or the CLI waits 3s for piped input on every call.
  * the CLI reports its own failures inside the JSON envelope (is_error), not on stderr.
"""

import hashlib
import json
import subprocess
import threading

from . import config, db

# One call at a time by default: each invocation is a process, and parallel reporters would
# otherwise launch a dozen. Raised with NEWSROOM_CLAUDE_CONCURRENCY.
_sem: threading.Semaphore | None = None
_sem_lock = threading.Lock()


class ClaudeCodeError(RuntimeError):
    pass


def _gate() -> threading.Semaphore:
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.Semaphore(config.load().claude_concurrency)
    return _sem


def ask_json(system: str, user: str, *, model: str, max_tokens: int = 4000, thinking: bool = False) -> dict:
    """Ask Claude through the CLI and parse its JSON reply. Cached, so reruns are free."""
    settings = config.load()
    key = hashlib.sha256(json.dumps(["claude_code", model, system, user, max_tokens, thinking]).encode()).hexdigest()
    with db.session() as conn:
        row = conn.execute("select response from llm_cache where key = ?", (key,)).fetchone()
    if row:
        return json.loads(row["response"])

    prompt = user + "\n\nReply with one JSON object and nothing else: no prose, no code fences."
    argv = [
        settings.claude_bin, "--print", "--output-format", "json",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",                      # no file or bash tools: this is a plain completion
        *(["--effort", "high"] if thinking and not model.startswith("claude-haiku") else []),
        "--", prompt,
    ]

    last = ""
    for attempt in range(3):
        with _gate():
            proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900)
        try:
            envelope = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            raise ClaudeCodeError(
                f"claude exited {proc.returncode} without JSON: {(proc.stderr or proc.stdout)[:300]}"
            ) from None
        _record_usage(model, envelope)
        if envelope.get("is_error") or proc.returncode != 0:
            raise ClaudeCodeError(f"claude failed: {str(envelope.get('result'))[:300]}")

        result = envelope.get("result")
        if isinstance(result, dict):
            data, text = result, json.dumps(result)
        else:
            text = str(result or "")
            try:
                data = json.loads(_only_json(text))
            except (ValueError, json.JSONDecodeError):
                last = text
                prompt = user + "\n\nYour previous reply was not valid JSON. Reply with one JSON object only."
                continue
        if isinstance(data, dict):
            with db.session() as conn:
                conn.execute("insert or replace into llm_cache (key, response) values (?, ?)", (key, text))
            return data
        last = text
    raise ClaudeCodeError(f"{model} did not return a JSON object after 3 attempts: {last[:200]}")


def _only_json(text: str) -> str:
    """Pull the JSON object out of a reply that still has fences or chatter around it."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return t[start : end + 1]


def _record_usage(model: str, envelope: dict) -> None:
    usage = envelope.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0)
    completion = int(usage.get("output_tokens", 0) or 0)
    if not (prompt_tokens or completion):
        return
    with db.session() as conn:
        conn.execute("insert into llm_usage (at, model, prompt_tokens, completion_tokens) values (?, ?, ?, ?)",
                     (db.now(), model, prompt_tokens, completion))
