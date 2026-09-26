"""Bossman's eyes: pull what's live right now from free public sources. No model calls, no API keys.

Each gatherer returns candidates in one shape:

    {"id", "source_type", "title", "url", "snippet", "spike": {"kind", "value"}, "seen_at"}

A failing source returns nothing and is reported, so one outage never stops a pass. Adding a
source is adding a function here and listing it in GATHERERS.

Not here yet: X/Twitter (no free API), Kalshi (its open-markets feed is mostly sports parlays),
TikTok. Reddit's JSON API blocks bots, so Reddit comes from its RSS feeds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)
UA = {"User-Agent": "ClankerTimesBot/0.1 (automated AI newsroom; research)"}
TIMEOUT = 25

REDDIT_SUBS = ["news", "worldnews", "technology", "business", "politics", "science"]
NEWS_FEEDS = {
    "top": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "business": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "technology": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
    "health": "https://news.google.com/rss/headlines/section/topic/HEALTH?hl=en-US&gl=US&ceid=US:en",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cid(source_type: str, key: str) -> str:
    return f"{source_type}:{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def _get(url: str, **params) -> httpx.Response:
    r = httpx.get(url, params=params or None, headers=UA, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def candidate(source_type: str, title: str, url: str, snippet: str = "", spike: dict | None = None) -> dict:
    return {"id": _cid(source_type, url or title), "source_type": source_type, "title": title.strip(),
            "url": url, "snippet": snippet[:500], "spike": spike or {}, "seen_at": _now()}


# --- gatherers ---------------------------------------------------------------------------------


def google_trends(limit: int = 20) -> list[dict]:
    """US search terms trending right now, with Google's approximate traffic."""
    root = ET.fromstring(_get("https://trends.google.com/trending/rss", geo="US").content)
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        traffic = item.findtext("ht:approx_traffic", namespaces=ns) or ""
        news = [(n.findtext("ht:news_item_title", namespaces=ns) or "", n.findtext("ht:news_item_url", namespaces=ns) or "")
                for n in item.findall("ht:news_item", ns)]
        url = news[0][1] if news and news[0][1] else f"https://trends.google.com/trends/explore?q={title}&geo=US"
        snippet = "; ".join(t for t, _ in news[:3])
        out.append(candidate("google_trends", title, url, snippet, {"kind": "search_traffic", "value": traffic}))
        if len(out) >= limit:
            break
    return out


def google_news(limit: int = 15) -> list[dict]:
    """What outlets are leading with, per section. Saturation here is a signal too."""
    out = []
    for section, feed in NEWS_FEEDS.items():
        root = ET.fromstring(_get(feed).content)
        for rank, item in enumerate(root.iter("item"), 1):
            title, link = item.findtext("title") or "", item.findtext("link") or ""
            source = item.findtext("source") or ""
            out.append(candidate("google_news", title, link, f"{source} · {section}",
                                 {"kind": "news_rank", "value": f"#{rank} in Google News {section}"}))
            if rank >= limit:
                break
    return out


def bluesky_trending(limit: int = 20) -> list[dict]:
    data = _get("https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics").json()
    out = []
    for rank, t in enumerate(data.get("topics", [])[:limit], 1):
        url = "https://bsky.app" + t.get("link", "") if t.get("link", "").startswith("/") else t.get("link", "")
        out.append(candidate("bluesky", t.get("displayName") or t.get("topic", ""), url,
                             t.get("description", ""), {"kind": "trending_rank", "value": f"#{rank} on Bluesky"}))
    return out


def reddit(limit: int = 10) -> list[dict]:
    """Hot posts from news-heavy subreddits, via RSS (the JSON API blocks bots)."""
    atom = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for n, sub in enumerate(REDDIT_SUBS):
        if n:
            time.sleep(2)          # Reddit rate-limits rapid unauthenticated feed requests
        try:
            root = ET.fromstring(_get(f"https://www.reddit.com/r/{sub}/hot/.rss", limit=limit).content)
        except Exception as e:  # noqa: BLE001 - one subreddit failing shouldn't lose the rest
            log.info("reddit r/%s failed: %s", sub, e)
            continue
        for rank, entry in enumerate(root.findall("a:entry", atom), 1):
            title = entry.findtext("a:title", default="", namespaces=atom)
            link = entry.find("a:link", atom)
            url = link.get("href", "") if link is not None else ""
            content = _strip(entry.findtext("a:content", default="", namespaces=atom))
            out.append(candidate("reddit", title, url, content[:300],
                                 {"kind": "hot_rank", "value": f"#{rank} hot in r/{sub}"}))
    return out


def polymarket(limit: int = 25) -> list[dict]:
    """The most-traded prediction markets in the last 24h, with their one-day price move."""
    markets = _get("https://gamma-api.polymarket.com/markets", active="true", closed="false",
                   limit=100, order="volume24hr", ascending="false").json()
    rows = []
    for m in markets:
        move = m.get("oneDayPriceChange") or 0
        rows.append((abs(float(move)), m))
    out = []
    for move, m in sorted(rows, key=lambda r: r[0], reverse=True)[:limit]:
        url = f"https://polymarket.com/market/{m.get('slug', '')}"
        spike = {"kind": "market_move",
                 "value": f"{float(m.get('oneDayPriceChange') or 0):+.0%} in 24h on ${float(m.get('volume24hr') or 0):,.0f} volume"}
        out.append(candidate("polymarket", m.get("question", ""), url, f"prices {m.get('outcomePrices', '')}", spike))
    return out


def federal_register(limit: int = 30) -> list[dict]:
    """What federal agencies published most recently: rules, proposed rules, notices, orders."""
    data = _get("https://www.federalregister.gov/api/v1/documents.json", per_page=limit, order="newest").json()
    out = []
    for r in data.get("results", []):
        agencies = ", ".join(a.get("name", "") for a in r.get("agencies") or [] if a.get("name"))
        out.append(candidate("gov", r.get("title", ""), r.get("html_url", ""),
                             f"{r.get('type', '')} · {agencies} · {(r.get('abstract') or '')[:250]}",
                             {"kind": "published", "value": r.get("publication_date", "")}))
    return out


def hacker_news(limit: int = 20) -> list[dict]:
    ids = _get("https://hacker-news.firebaseio.com/v0/topstories.json").json()[:limit]
    out = []
    for rank, i in enumerate(ids, 1):
        item = _get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json").json() or {}
        url = item.get("url") or f"https://news.ycombinator.com/item?id={i}"
        out.append(candidate("hacker_news", item.get("title", ""), url, "",
                             {"kind": "points", "value": f"{item.get('score', 0)} points, #{rank} on HN"}))
    return out


GATHERERS = {
    "google_trends": google_trends,
    "google_news": google_news,
    "bluesky": bluesky_trending,
    "reddit": reddit,
    "polymarket": polymarket,
    "gov": federal_register,
    "hacker_news": hacker_news,
}


def gather(sources: list[str] | None = None) -> tuple[list[dict], dict[str, str]]:
    """Run gatherers in parallel. Returns (candidates, {source: error}) for sources that failed."""
    names = sources or list(GATHERERS)
    unknown = [n for n in names if n not in GATHERERS]
    if unknown:
        raise ValueError(f"unknown sources {unknown}; choose from {list(GATHERERS)}")
    candidates, errors = [], {}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {name: pool.submit(GATHERERS[name]) for name in names}
        for name, future in futures.items():
            try:
                candidates += future.result()
            except Exception as e:  # noqa: BLE001 - report it and keep the other sources
                errors[name] = f"{type(e).__name__}: {e}"
    seen, unique = set(), []
    for c in candidates:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)
    return unique, errors


def dump(candidates: list[dict]) -> str:
    return json.dumps(candidates, indent=1, ensure_ascii=False)
