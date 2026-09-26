"""Gemini provider (LLM_PROVIDER=gemini): the same agent loop and structured calls as llm.py,
on Google's google-genai SDK. Built for the AI Studio free tier: calls are throttled by a
per-process semaphore and retried on rate limits."""

import asyncio
import logging
import os
from types import SimpleNamespace
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from . import db
from .config import settings
from .llm import AgentContext, Refused, Tool, TurnHook, _run_tool

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_client: genai.Client | None = None
# Free-tier limits are per minute; keep this process's concurrent calls low.
_slots = asyncio.Semaphore(int(os.getenv("GEMINI_MAX_CONCURRENT", "2")))
_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


async def _generate(model: str, contents, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    delay = 10.0
    for attempt in range(8):
        try:
            async with _slots:
                return await client().aio.models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as e:
            if e.code not in (429, 500, 503) or attempt == 7:
                raise
            log.warning("gemini %s (%s); retrying in %.0fs", e.code, e.status, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)
    raise RuntimeError("unreachable")


async def _record(agent: str, model: str, resp: types.GenerateContentResponse) -> None:
    u = resp.usage_metadata
    usage = SimpleNamespace(
        input_tokens=(u.prompt_token_count or 0) if u else 0,
        output_tokens=((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
        cache_read_input_tokens=(u.cached_content_token_count or 0) if u else 0,
        cache_creation_input_tokens=0,
    )
    await db.record_usage(agent, model, usage)


def _check_blocked(agent: str, resp: types.GenerateContentResponse) -> types.Candidate:
    feedback = resp.prompt_feedback
    if feedback and feedback.block_reason:
        raise Refused(f"{agent}: prompt blocked ({feedback.block_reason})")
    if not resp.candidates:
        raise Refused(f"{agent}: no candidates returned")
    cand = resp.candidates[0]
    reason = getattr(cand.finish_reason, "name", str(cand.finish_reason or ""))
    if reason in _BLOCKED:
        raise Refused(f"{agent}: response blocked ({reason})")
    return cand


async def structured(agent: str, model: str, system: str, prompt: str, schema: type[T], *, effort: str = "medium") -> T:
    config = types.GenerateContentConfig(
        system_instruction=system, response_mime_type="application/json", response_schema=schema,
    )
    resp = await _generate(model, prompt, config)
    await _record(agent, model, resp)
    _check_blocked(agent, resp)
    if isinstance(resp.parsed, schema):
        return resp.parsed
    return schema.model_validate_json(resp.text or "")


async def run_agent(
    ctx: AgentContext, *, model: str, system: str, task: str, tools: list[Tool], max_turns: int,
    effort: str = "high", on_turn: TurnHook | None = None, heartbeat=None,
) -> str:
    by_name = {t.name: t for t in tools}
    decls = [
        types.FunctionDeclaration(name=t.name, description=t.description,
                                  parameters_json_schema=t.schema()["input_schema"])
        for t in tools
    ]
    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=[types.Tool(function_declarations=decls)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part(text=task)])]
    final_text = ""

    for _ in range(max_turns):
        if heartbeat and not await heartbeat():
            log.warning("%s lost its lease; stopping", ctx.agent)
            ctx.outcome = "lease_lost"
            return final_text
        resp = await _generate(model, contents, config)
        await _record(ctx.agent, model, resp)
        cand = _check_blocked(ctx.agent, resp)
        if cand.content is None:
            break
        # Append the model's turn as returned: it carries Gemini's thought signatures.
        contents.append(cand.content)
        text = "".join(p.text for p in (cand.content.parts or []) if p.text and not p.thought)
        final_text = text or final_text
        calls = resp.function_calls or []
        if not calls:
            break

        async def one(call: types.FunctionCall) -> types.Part:
            tool = by_name.get(call.name)
            if tool is None:
                out, is_error = f"Unknown tool {call.name}", True
            else:
                out, is_error = await _run_tool(ctx, tool, dict(call.args or {}))
            return types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response={"error": out} if is_error else {"result": out},
            ))

        parts = list(await asyncio.gather(*(one(c) for c in calls)))
        if ctx.done:
            break
        if on_turn:
            guidance = await on_turn(ctx)
            if ctx.done:
                break
            if guidance:
                parts.append(types.Part(text=f"[newsroom] {guidance}"))
        contents.append(types.Content(role="user", parts=parts))
    else:
        ctx.outcome = ctx.outcome or "max_turns"
    return final_text
