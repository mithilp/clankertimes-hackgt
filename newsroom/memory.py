"""Shared newsroom memory (mem0, self-hosted in our own Postgres) and local embeddings.

Memory holds hints about where to look: beat notes, kill-memo lessons, source reliability,
editorial precedent. It is never evidence and never used for coordination; those live in
Postgres tables. If mem0 is unavailable the newsroom keeps running without it.
"""

import asyncio
import logging
import os

from .config import settings

log = logging.getLogger(__name__)

NEWSROOM = "newsroom"  # shared namespace; agent_id narrows to one agent's notes
EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # 384 dims, runs locally on CPU (ONNX)
EMBED_DIMS = 384
_CACHE = os.getenv("FASTEMBED_CACHE_PATH", "./.models")

_mem = None
_mem_lock = asyncio.Lock()
_embedder = None


def _mem0_config() -> dict:
    return {
        "llm": (
            {"provider": "gemini", "config": {"model": settings.models.memory, "api_key": settings.gemini_api_key}}
            if settings.provider == "gemini"
            else {"provider": "anthropic", "config": {"model": settings.models.memory, "max_tokens": 2000}}
        ),
        "embedder": {"provider": "fastembed", "config": {"model": EMBED_MODEL, "embedding_dims": EMBED_DIMS}},
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "connection_string": settings.database_url,
                "collection_name": "newsroom_memory",
                "embedding_model_dims": EMBED_DIMS,
                "hnsw": True,
            },
        },
        "history_db_path": os.getenv("MEM0_HISTORY_DB", "./.mem0/history.db"),
    }


async def _memory():
    global _mem
    async with _mem_lock:
        if _mem is None:
            from mem0 import Memory

            os.makedirs(os.path.dirname(_mem0_config()["history_db_path"]), exist_ok=True)
            _mem = await asyncio.to_thread(Memory.from_config, _mem0_config())
    return _mem


async def recall(query: str, *, agent_id: str | None = None, k: int = 6) -> list[str]:
    if not settings.memory_enabled:
        return []
    filters = {"user_id": NEWSROOM, **({"agent_id": agent_id} if agent_id else {})}
    try:
        mem = await _memory()
        res = await asyncio.to_thread(mem.search, query, top_k=k, filters=filters)
        return [r["memory"] for r in res.get("results", [])]
    except Exception as e:  # noqa: BLE001
        log.warning("memory recall failed: %s", e)
        return []


async def remember(text: str, *, agent_id: str, kind: str) -> None:
    """kind: beat_note | kill_lesson | source_reliability | editorial_precedent."""
    if not settings.memory_enabled:
        return
    try:
        mem = await _memory()
        await asyncio.to_thread(
            mem.add, f"[{kind}] {text}", user_id=NEWSROOM, agent_id=agent_id, metadata={"kind": kind}
        )
    except Exception as e:  # noqa: BLE001
        log.warning("memory write failed: %s", e)


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(model_name=EMBED_MODEL, cache_dir=_CACHE)
    return _embedder


async def embed(text: str) -> list[float]:
    def run():
        return next(iter(_get_embedder().embed([text]))).tolist()

    return await asyncio.to_thread(run)
