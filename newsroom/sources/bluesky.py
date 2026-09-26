"""Bluesky posts: the newsroom's social media source.

Bluesky's search is free and needs no account (through api.bsky.app; public.api.bsky.app refuses searches).
Posts aren't coded, so a model reads them to find posts reporting a problem with a specific product, and
writes down the product, the maker and the claim. Posts that don't report one are not stored.

Posters are never identified: only a one-way hash of the account is kept, to count distinct people.
"""

import hashlib
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx

from .. import db, llm

SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
BATCH = 25
DEFAULT_QUERIES = ["recall", "caught fire", "exploded", "defective", "stalled while driving", "overheated",
                   "burned my", "safety hazard", "got hurt using", "malfunctioned"]

SYSTEM = """You read social media posts and find the ones where someone reports a problem with a specific product: a car, device, appliance, drug, food or other product they own, used or saw first-hand.

For each post, give:
- "product": the specific product, with brand and model (and year if given), e.g. "2023 Hyundai Tucson" or "Ninja Foodi pressure cooker". Use null if the post doesn't report a problem with a specific product (jokes, news links, general opinions, politics).
- "company": the maker, if known, else null.
- "claim": one short plain sentence (at most 12 words) about what went wrong, e.g. "engine shuts off while driving".
- "severe": true if the post reports an injury, fire or death.

Reply in JSON: {"posts": [{"id": "<post id>", "product": "...", "company": "...", "claim": "...", "severe": false}]}"""


def search(query: str, *, since: date, limit: int) -> Iterator[dict]:
    """Posts matching a query, newest first, back to `since`."""
    cursor, fetched = None, 0
    while fetched < limit:
        params = {"q": query, "limit": min(100, limit - fetched), "sort": "latest", "since": f"{since.isoformat()}T00:00:00Z"}
        if cursor:
            params["cursor"] = cursor
        response = httpx.get(SEARCH_URL, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        posts = data.get("posts", [])
        yield from posts
        fetched += len(posts)
        cursor = data.get("cursor")
        if not posts or not cursor:
            return


def ingest(conn: sqlite3.Connection, queries: list[str], *, since: date, limit: int, workers: int = 4) -> tuple[int, int]:
    """Search, read and store posts that report a product problem. Returns (posts read, posts stored)."""
    posts: dict[str, dict] = {}
    for query in queries:
        for post in search(query, since=since, limit=limit):
            posts.setdefault(post_id(post), post)
    # Posts already read are skipped, including ones that turned out not to report a product problem,
    # so no post is paid for twice.
    seen = {row[0] for row in conn.execute("select id from seen_posts")}
    fresh = [p for pid, p in posts.items() if pid not in seen and (p.get("record") or {}).get("text", "").strip()]
    batches = [fresh[i:i + BATCH] for i in range(0, len(fresh), BATCH)]
    with ThreadPoolExecutor(workers) as pool:
        complaints = [c for batch in pool.map(read_batch, batches) for c in batch]
    stored = db.save_complaints(conn, complaints)
    conn.executemany("insert or ignore into seen_posts (id) values (?)", [(post_id(p),) for p in fresh])
    conn.commit()
    return len(fresh), stored


def read_batch(posts: list[dict]) -> list[dict]:
    by_id = {post_id(p): p for p in posts}
    listing = "\n\n".join(f"[{pid}]\n{p['record']['text'][:1000]}" for pid, p in by_id.items())
    reply = llm.ask_json(SYSTEM, f"Posts:\n\n{listing}", max_tokens=3000)
    complaints = []
    for item in reply.get("posts", []):
        pid = str(item.get("id", "")).strip("[] ")
        post, product, claim = by_id.get(pid), item.get("product"), item.get("claim")
        if post is None or not isinstance(product, str) or not product.strip() or not isinstance(claim, str) or not claim.strip():
            continue
        record = post["record"]
        complaints.append({
            "id": pid,
            "source": "bluesky",
            "received": (record.get("createdAt") or post.get("indexedAt") or "1900-01-01")[:10],
            "product": product.strip().upper(),
            "company": item.get("company") if isinstance(item.get("company"), str) else None,
            "severe": item.get("severe") is True,
            "text": record["text"],
            "claim": claim.strip(),
            "coded": False,  # a model wrote it, so claims are grouped like other model-read complaints
            "fields": {"person": "bluesky:" + hashlib.sha256(post["author"]["did"].encode()).hexdigest()[:16]},
        })
    return complaints


def post_id(post: dict) -> str:
    """"bluesky:<author did>/<post key>", stable across searches."""
    uri = post["uri"]  # at://<did>/app.bsky.feed.post/<rkey>
    did, rkey = uri.removeprefix("at://").split("/app.bsky.feed.post/")
    return f"bluesky:{hashlib.sha256(did.encode()).hexdigest()[:16]}/{rkey}"
