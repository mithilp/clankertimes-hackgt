"""Claude calls: one agent loop shared by every role, plus structured one-shot calls.

Every tool an agent can call must carry a `reason` argument; the loop writes it to
`tool_calls` before running the tool. That trail is the demo.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

import anthropic
from pydantic import BaseModel

from . import db

log = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic(max_retries=6)
T = TypeVar("T", bound=BaseModel)

# Opus 5 can decline on safety classifiers; "default" re-runs a declined request on
# Anthropic's recommended fallback model inside the same call.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Refused(Exception):
    pass


def _request_kwargs(model: str) -> dict:
    if model.startswith("claude-opus"):
        return {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
    return {}


async def structured(agent: str, model: str, system: str, prompt: str, schema: type[T], *, effort: str = "medium") -> T:
    """One call, validated JSON out."""
    kwargs = _request_kwargs(model)
    if not model.startswith("claude-haiku"):
        kwargs["output_config"] = {"effort": effort}
    resp = await client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        **kwargs,
    )
    await db.record_usage(agent, resp.model, resp.usage)
    if resp.stop_reason == "refusal":
        raise Refused(f"{agent}: {getattr(resp.stop_details, 'category', None)}")
    if resp.parsed_output is None:
        raise ValueError(f"{agent}: no structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


# --- tools -------------------------------------------------------------------------------

# A handler returns the text for the model, or (text, source_id) when it touched a source.
Handler = Callable[[dict], Awaitable[str | tuple[str, Any]]]


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list[str]
    handler: Handler
    billable: bool = True       # research calls count toward budget and marginal yield
    terminal: bool = False      # ends the agent run (submit, park, kill, ...)

    def schema(self) -> dict:
        props = {
            "reason": {
                "type": "string",
                "description": "Why you are making this call right now, in one sentence. Shown on the public dashboard.",
            },
            **self.properties,
        }
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {"type": "object", "properties": props, "required": ["reason", *self.required]},
        }


@dataclass
class AgentContext:
    agent: str
    story_id: Any = None
    lead_id: Any = None
    done: bool = False
    outcome: str | None = None
    scratch: dict = field(default_factory=dict)


async def _run_tool(ctx: AgentContext, tool: Tool, tool_input: dict) -> tuple[str, bool]:
    reason = str(tool_input.pop("reason", "")).strip() or "(no reason given)"
    started = time.monotonic()
    source_id = None
    try:
        result, is_error = await tool.handler(dict(tool_input)), False
        if isinstance(result, tuple):
            result, source_id = result
    except Exception as e:  # noqa: BLE001 - every tool failure goes back to the model as data
        log.warning("%s tool %s failed: %s", ctx.agent, tool.name, e)
        result, is_error = f"Error: {type(e).__name__}: {e}", True
    await db.log_tool_call(
        ctx.agent, tool.name, reason, tool_input,
        story_id=ctx.story_id, lead_id=ctx.lead_id, result_summary=result[:500],
        source_id=source_id, duration_ms=int((time.monotonic() - started) * 1000),
        billable=tool.billable,
    )
    if tool.terminal and not is_error:
        ctx.done = True
    return result, is_error


# Called after each round of tool results. Returns guidance text to append (or None), and
# may set ctx.done. This is where the reporter's stopping rules plug in.
TurnHook = Callable[[AgentContext], Awaitable[str | None]]


async def run_agent(
    ctx: AgentContext,
    *,
    model: str,
    system: str,
    task: str,
    tools: list[Tool],
    max_turns: int,
    effort: str = "high",
    on_turn: TurnHook | None = None,
    heartbeat: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """Manual tool loop. Returns the final assistant text."""
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": task}]
    kwargs = _request_kwargs(model)
    if not model.startswith("claude-haiku"):
        kwargs["output_config"] = {"effort": effort}
    final_text = ""

    for _ in range(max_turns):
        if heartbeat and not await heartbeat():
            log.warning("%s lost its lease; stopping", ctx.agent)
            ctx.outcome = "lease_lost"
            return final_text
        resp = await client.beta.messages.create(
            model=model,
            max_tokens=32000,
            system=system,
            tools=[t.schema() for t in tools],
            messages=messages,
            cache_control={"type": "ephemeral"},
            **kwargs,
        )
        await db.record_usage(ctx.agent, resp.model, resp.usage)
        if resp.stop_reason == "refusal":
            raise Refused(f"{ctx.agent}: {getattr(resp.stop_details, 'category', None)}")

        messages.append({"role": "assistant", "content": resp.content})
        final_text = "\n".join(b.text for b in resp.content if b.type == "text") or final_text
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            break

        # Parallel tool calls from one turn run concurrently; results go back in one message.
        async def one(block):
            tool = by_name.get(block.name)
            if tool is None:
                return {"type": "tool_result", "tool_use_id": block.id, "content": f"Unknown tool {block.name}", "is_error": True}
            text, is_error = await _run_tool(ctx, tool, dict(block.input))
            return {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error}

        results: list[dict] = list(await asyncio.gather(*(one(b) for b in uses)))
        if ctx.done:
            break
        if on_turn:
            guidance = await on_turn(ctx)
            if ctx.done:
                break
            if guidance:
                results.append({"type": "text", "text": f"[newsroom] {guidance}"})
        messages.append({"role": "user", "content": results})
    else:
        ctx.outcome = ctx.outcome or "max_turns"
    return final_text


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
