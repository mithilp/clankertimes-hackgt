"""Postgres access: pool, leases, singleton locks, LISTEN wake-ups, and the event/trail logs.

All coordination between parallel workers goes through here. Two primitives do the work:

- Leases (`claim_story`): any number of workers race for rows with FOR UPDATE SKIP LOCKED.
  Exactly one wins each row; a crashed worker's lease expires and the row is reclaimed.
- Advisory locks (`singleton`): exactly one holder per key across all processes. Used for
  the managing editor and for each scout beat, so extra replicas act as hot standbys.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from .config import PRICES, settings

log = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10, init=_init_conn)
    return _pool


# Every lead column except the embedding (fetched rows don't need it).
LEAD_COLS = ("id, scout, beat, hypothesis, why_now, why_now_at, who_would_know, would_settle_it, score, "
             "score_components, score_reason, fingerprint, status, duplicate_of, created_at")


def vec(embedding: list[float]) -> str:
    """pgvector text form; pass with a ::vector cast."""
    return "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"


# --- trail -------------------------------------------------------------------------------


async def event(agent: str, action: str, reason: str, *, story_id=None, lead_id=None, detail: Any = None) -> None:
    p = await pool()
    await p.execute(
        "insert into agent_events (agent, action, reason, story_id, lead_id, detail) values ($1,$2,$3,$4,$5,$6)",
        agent, action, reason, story_id, lead_id, detail,
    )
    log.info("[%s] %s: %s", agent, action, reason)


async def log_tool_call(
    agent: str, tool: str, reason: str, tool_input: dict, *,
    story_id=None, lead_id=None, result_summary: str | None = None, source_id=None,
    duration_ms: int | None = None, billable: bool = True,
) -> int:
    p = await pool()
    return await p.fetchval(
        """insert into tool_calls (agent, story_id, lead_id, tool, reason, input, result_summary, source_id,
                                   duration_ms, billable)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) returning id""",
        agent, story_id, lead_id, tool, reason, tool_input, result_summary, source_id, duration_ms, billable,
    )


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


async def upsert_source(url: str, seen_by: str) -> tuple[Any, bool]:
    """Returns (source_id, is_new). is_new=False means some agent already touched this URL."""
    p = await pool()
    row = await p.fetchrow(
        """insert into sources (url, url_hash, first_seen_by) values ($1,$2,$3)
           on conflict (url_hash) do update set url_hash = excluded.url_hash
           returning id, (xmax = 0) as inserted""",
        url, url_hash(url), seen_by,
    )
    return row["id"], row["inserted"]


async def record_usage(agent: str, model: str, usage: Any) -> None:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5"])
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = (
        usage.input_tokens * price_in
        + cache_read * price_in * 0.1
        + cache_write * price_in * 1.25
        + usage.output_tokens * price_out
    ) / 1_000_000
    p = await pool()
    await p.execute(
        """insert into model_usage (agent, model, input_tokens, output_tokens, cache_read, cache_write, cost_usd)
           values ($1,$2,$3,$4,$5,$6,$7)""",
        agent, model, usage.input_tokens, usage.output_tokens, cache_read, cache_write, cost,
    )


async def spend_usd() -> float:
    p = await pool()
    return float(await p.fetchval("select coalesce(sum(cost_usd), 0) from model_usage"))


async def over_budget() -> bool:
    return await spend_usd() >= settings.spend_cap_usd


# --- leases ------------------------------------------------------------------------------


async def claim_story(statuses: list[str], new_status: str, owner: str) -> asyncpg.Record | None:
    """Claim one unleased story in any of `statuses` or `new_status` (the latter = a crashed worker's story).

    Safe under any number of concurrent workers: SKIP LOCKED means each candidate row goes to
    exactly one claimant, and nobody blocks waiting on a row someone else is claiming.
    """
    p = await pool()
    return await p.fetchrow(
        f"""
        update stories s set status = ($2::text)::story_status, lease_owner = $3,
               lease_expires_at = now() + make_interval(secs => {settings.lease_seconds})
        where s.id = (
          select id from stories
          where (status::text = any($1::text[]) or status::text = $2::text)
            and (lease_expires_at is null or lease_expires_at < now())
          order by updated_at
          for update skip locked
          limit 1
        )
        returning s.*""",
        statuses, new_status, owner,
    )


async def heartbeat(story_id, owner: str) -> bool:
    """Extend the lease. False means we lost it (expired and reclaimed) and must stop."""
    p = await pool()
    res = await p.execute(
        f"""update stories set lease_expires_at = now() + make_interval(secs => {settings.lease_seconds})
            where id = $1 and lease_owner = $2""",
        story_id, owner,
    )
    return res.endswith(" 1")


async def release(story_id, owner: str, **fields: Any) -> bool:
    """Write final fields and drop the lease, only if we still hold it."""
    p = await pool()
    cols = list(fields)
    sets = ", ".join(f"{c} = ${i + 3}" for i, c in enumerate(cols))
    sets = (sets + ", " if sets else "") + "lease_owner = null, lease_expires_at = null"
    res = await p.execute(
        f"update stories set {sets} where id = $1 and lease_owner = $2",
        story_id, owner, *[fields[c] for c in cols],
    )
    return res.endswith(" 1")


# --- singletons --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def singleton(key: str, *, retry_s: float = 15.0) -> AsyncIterator[asyncpg.Connection]:
    """Block until this process holds the advisory lock for `key`, then hold it for the block.

    The lock lives on a dedicated connection, so if the process dies Postgres releases it
    and a standby replica takes over within `retry_s`.
    """
    conn = await asyncpg.connect(settings.database_url)
    try:
        while not await conn.fetchval("select pg_try_advisory_lock(hashtext($1))", key):
            await asyncio.sleep(retry_s)
        log.info("acquired singleton %s", key)
        yield conn
    finally:
        await conn.close()


# --- wake-ups ----------------------------------------------------------------------------


class Waker:
    """LISTEN on channels; `wait()` returns on a notification or after `timeout` (poll fallback)."""

    def __init__(self, *channels: str) -> None:
        self.channels = channels
        self._event = asyncio.Event()
        self._conn: asyncpg.Connection | None = None

    async def start(self) -> "Waker":
        self._conn = await asyncpg.connect(settings.database_url)
        for ch in self.channels:
            await self._conn.add_listener(ch, lambda *_: self._event.set())
        return self

    async def wait(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout)
        self._event.clear()

    def poke(self) -> None:
        self._event.set()
