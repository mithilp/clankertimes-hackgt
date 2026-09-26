"""DeepSeek provider (LLM_PROVIDER=deepseek): the same agent loop and structured calls as llm.py,
over DeepSeek's OpenAI-compatible API.

Two DeepSeek rules shape this file:
- In thinking mode with tools, every earlier assistant turn's `reasoning_content` must be sent
  back, or the API returns 400. Assistant turns are replayed exactly as received.
- JSON output is `json_object` only (no schemas), so the schema goes in the prompt and the
  reply is validated here, with retries (the API can occasionally return empty content).
"""

import asyncio
import json
import logging
import os
from types import SimpleNamespace
from typing import TypeVar

import openai
from pydantic import BaseModel, ValidationError

from . import db
from .config import settings
from .llm import AgentContext, QuotaExhausted, Refused, Tool, TurnHook, _run_tool

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_client: openai.AsyncOpenAI | None = None
_slots = asyncio.Semaphore(int(os.getenv("DEEPSEEK_MAX_CONCURRENT", "8")))


def client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
                                     max_retries=6, timeout=600)
    return _client


def _thinking(effort: str) -> dict:
    return {"thinking": {"type": "disabled" if effort == "low" else "enabled"}}


async def _chat(agent: str, model: str, **kwargs):
    try:
        async with _slots:
            resp = await client().chat.completions.create(model=model, **kwargs)
    except openai.APIStatusError as e:
        if e.status_code == 402:
            raise QuotaExhausted(f"{model}: DeepSeek balance is used up") from e
        raise
    u = resp.usage
    cached = (getattr(u, "prompt_cache_hit_tokens", 0) or 0) if u else 0
    await db.record_usage(agent, model, SimpleNamespace(
        input_tokens=(u.prompt_tokens - cached) if u else 0,
        output_tokens=u.completion_tokens if u else 0,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    ))
    choice = resp.choices[0]
    if choice.finish_reason == "content_filter":
        raise Refused(f"{agent}: content filter")
    return choice


async def structured(agent: str, model: str, system: str, prompt: str, schema: type[T], *, effort: str = "medium") -> T:
    system = (f"{system}\n\nReply with a single JSON object that matches this JSON schema exactly "
              f"(no prose, no code fences):\n{json.dumps(schema.model_json_schema())}")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    last_error = ""
    for _ in range(3):
        choice = await _chat(agent, model, messages=messages, response_format={"type": "json_object"},
                             max_tokens=16000, extra_body=_thinking(effort))
        text = choice.message.content or ""
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            last_error = str(e)[:500]
            log.warning("%s: invalid JSON from %s, retrying (%s)", agent, model, last_error[:120])
            messages = [*messages[:2], {"role": "user", "content": f"{prompt}\n\nYour previous reply was not "
                        f"valid for the schema: {last_error}. Reply with the JSON object only."}]
    raise ValueError(f"{agent}: no valid JSON after 3 tries: {last_error}")


async def run_agent(
    ctx: AgentContext, *, model: str, system: str, task: str, tools: list[Tool], max_turns: int,
    effort: str = "high", on_turn: TurnHook | None = None, heartbeat=None,
) -> str:
    by_name = {t.name: t for t in tools}
    specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                               "parameters": t.schema()["input_schema"]}} for t in tools]
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    final_text = ""

    for _ in range(max_turns):
        if heartbeat and not await heartbeat():
            log.warning("%s lost its lease; stopping", ctx.agent)
            ctx.outcome = "lease_lost"
            return final_text
        choice = await _chat(ctx.agent, model, messages=messages, tools=specs, max_tokens=32000,
                             extra_body=_thinking(effort))
        msg = choice.message
        turn: dict = {"role": "assistant", "content": msg.content or ""}
        reasoning = getattr(msg, "reasoning_content", None) or (msg.model_extra or {}).get("reasoning_content")
        if reasoning:
            turn["reasoning_content"] = reasoning  # required back on every later request that has tools
        calls = msg.tool_calls or []
        if calls:
            turn["tool_calls"] = [{"id": c.id, "type": "function",
                                   "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                  for c in calls]
        messages.append(turn)
        final_text = msg.content or final_text
        if not calls:
            break

        async def one(call) -> dict:
            tool = by_name.get(call.function.name)
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as e:
                out = f"Error: arguments were not valid JSON ({e}); call the tool again."
            else:
                if tool is None:
                    out = f"Unknown tool {call.function.name}"
                else:
                    out, _ = await _run_tool(ctx, tool, dict(args))
            return {"role": "tool", "tool_call_id": call.id, "content": out}

        messages.extend(await asyncio.gather(*(one(c) for c in calls)))
        if ctx.done:
            break
        if on_turn:
            guidance = await on_turn(ctx)
            if ctx.done:
                break
            if guidance:
                messages.append({"role": "user", "content": f"[newsroom] {guidance}"})
    else:
        ctx.outcome = ctx.outcome or "max_turns"
    return final_text
