"""Bossman's eyes: pull what's live right now from free public sources. No model calls, no API keys.

Each gatherer returns candidates in one shape:

    {"id", "source_type", "title", "url", "snippet", "spike": {"kind", "value"}, "seen_at"}

A failing source returns nothing and is reported, so one outage never stops a pass. Adding a
source is adding a function here and listing it in GATHERERS. Tools that take an argument (a query,
a subreddit, a feed URL) go in TOOLS, where a beat's plan can call them.

Not here yet: X/Twitter (no free API), Kalshi (its open-markets feed is mostly sports parlays),
TikTok. Reddit's JSON API blocks bots, so Reddit comes from its RSS feeds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl                     # macOS and Linux: a lock shared by every process on the machine
except ImportError:                  # Windows: threads in one process still share the schedule
    fcntl = None

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


_ATOM = {"a": "http://www.w3.org/2005/Atom"}
# Reddit limits unauthenticated feed requests per IP, so every Bossman on this machine shares one
# schedule, kept in runs/reddit/: at most one request a minute (REDDIT_GAP) across all processes; after
# a 429, everyone pauses Reddit (5 minutes, doubling if it happens again soon after, up to an hour); and a
# subreddit fetched in the last REDDIT_CACHE seconds is reused instead of fetched again.
REDDIT_DIR = Path("runs") / "reddit"
REDDIT_GAP = float(os.getenv("NEWSROOM_REDDIT_GAP", "60"))   # Reddit 429s unregistered clients even at 6s
REDDIT_CACHE = 300
_reddit_lock = threading.Lock()


class RedditPaused(RuntimeError):
    """Reddit rate-limited this machine recently; its calls are skipped until the pause ends."""


@contextmanager
def _machine_lock():
    """Held by one process on this machine at a time (and, via _reddit_lock, one thread)."""
    REDDIT_DIR.mkdir(parents=True, exist_ok=True)
    with _reddit_lock, open(REDDIT_DIR / "lock", "w") as handle:
        if fcntl:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _reddit_state() -> dict:
    try:
        return json.loads((REDDIT_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _reddit_fetch(sub: str, sort: str, limit: int) -> bytes:
    cache = REDDIT_DIR / "cache" / f"{sub.lower()}_{sort}_{limit}.xml"

    def cached() -> bytes | None:
        fresh = cache.exists() and time.time() - cache.stat().st_mtime < REDDIT_CACHE
        return cache.read_bytes() if fresh else None

    if (hit := cached()) is not None:
        return hit
    with _machine_lock():
        if (hit := cached()) is not None:              # another process fetched it while we waited
            return hit
        state = _reddit_state()
        if state.get("paused_until", 0) > time.time():
            until = datetime.fromtimestamp(state["paused_until"]).astimezone()
            raise RedditPaused(f"skipped: Reddit is paused until {until:%H:%M %Z} after a rate limit")
        wait = state.get("last", 0) + REDDIT_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            response = _get(f"https://www.reddit.com/r/{sub}/{sort}/.rss", limit=limit)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                raise
            recent = time.time() - state.get("paused_until", 0) < 1800
            pause = min(state.get("pause", 300) * 2, 3600) if recent else 300
            retry_after = e.response.headers.get("retry-after", "")
            if retry_after.replace(".", "", 1).isdigit():
                pause = max(pause, min(float(retry_after), 3600))
            state.update(paused_until=time.time() + pause, pause=pause, last=time.time())
            (REDDIT_DIR / "state.json").write_text(json.dumps(state), encoding="utf-8")
            raise RedditPaused(f"skipped: Reddit rate-limited us; pausing Reddit for {pause / 60:g} minutes") from e
        state["last"] = time.time()
        (REDDIT_DIR / "state.json").write_text(json.dumps(state), encoding="utf-8")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(response.content)
        return response.content


def _subreddit_feed(sub: str, sort: str, limit: int) -> list[dict]:
    root = ET.fromstring(_reddit_fetch(sub, sort, limit))
    out = []
    for rank, entry in enumerate(root.findall("a:entry", _ATOM), 1):
        title = entry.findtext("a:title", default="", namespaces=_ATOM)
        link = entry.find("a:link", _ATOM)
        url = link.get("href", "") if link is not None else ""
        content = _strip(entry.findtext("a:content", default="", namespaces=_ATOM))
        posted = (entry.findtext("a:published", default="", namespaces=_ATOM) or "")[:16]
        value = f"#{rank} {sort} in r/{sub}" + (f", posted {posted}" if posted and sort == "new" else "")
        out.append(candidate("reddit", title, url, content[:300], {"kind": f"{sort}_rank", "value": value}))
    return out


def reddit(limit: int = 10) -> list[dict]:
    """Hot posts from news-heavy subreddits, via RSS (the JSON API blocks bots)."""
    out = []
    for sub in REDDIT_SUBS:
        try:
            out += _subreddit_feed(sub, "hot", limit)
        except Exception as e:  # noqa: BLE001 - one subreddit failing shouldn't lose the rest
            log.info("reddit r/%s failed: %s", sub, e)
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


# --- tools that take an argument, for a beat's plan --------------------------------------------
# A beat (agents/bossman/beats/) needs sources the national feeds above never carry. Bossman plans
# which of these to call and with what; the calls themselves are plain code, so a pass can be replayed.

STEP_LIMIT = 20   # candidates kept per tool call


def news_search(query: str, limit: int = STEP_LIMIT) -> list[dict]:
    """Google News search, newest coverage first. Defaults to the past 14 days."""
    q = query if "when:" in query else f"{query} when:14d"
    root = ET.fromstring(_get("https://news.google.com/rss/search", q=q, hl="en-US", gl="US", ceid="US:en").content)
    items = []
    for item in root.iter("item"):
        published = item.findtext("pubDate") or ""
        try:
            when = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            when = datetime.min.replace(tzinfo=timezone.utc)
        items.append((when, item))
    out = []
    for when, item in sorted(items, key=lambda x: x[0], reverse=True)[:limit]:
        source = item.findtext("source") or ""
        out.append(candidate("news_search", item.findtext("title") or "", item.findtext("link") or "", source,
                             {"kind": "published", "value": f"published {when:%Y-%m-%d %H:%M} UTC by {source}"}))
    return out


def subreddit(name: str, limit: int = STEP_LIMIT) -> list[dict]:
    """One subreddit's newest posts. "gatech" or "gatech:hot" for the hot list instead."""
    sub, _, sort = name.strip().removeprefix("r/").partition(":")
    return _subreddit_feed(sub, sort or "new", limit)


def feed(url: str, limit: int = STEP_LIMIT) -> list[dict]:
    """Any RSS or Atom feed."""
    root = ET.fromstring(_get(url).content)
    out = []
    items = list(root.iter("item")) or root.findall("a:entry", _ATOM)
    for rank, item in enumerate(items[:limit], 1):
        if item.tag == "item":
            title, link = item.findtext("title") or "", item.findtext("link") or ""
            when, text = item.findtext("pubDate") or "", item.findtext("description") or ""
        else:
            title = item.findtext("a:title", default="", namespaces=_ATOM)
            el = item.find("a:link", _ATOM)
            link = el.get("href", "") if el is not None else ""
            when = item.findtext("a:updated", default="", namespaces=_ATOM)
            text = item.findtext("a:summary", default="", namespaces=_ATOM)
        out.append(candidate("feed", title, link, _strip(text)[:300],
                             {"kind": "published", "value": f"published {when}" if when else f"#{rank} in feed"}))
    return out


def federal_register_search(term: str, limit: int = STEP_LIMIT) -> list[dict]:
    """Federal Register documents mentioning a term, newest first. A multi-word term is searched as an
    exact phrase unless it already has quotes: unquoted, the API matches the words separately, so
    "Georgia Institute of Technology" returns every notice mentioning Georgia (3,115 vs 146)."""
    if " " in term.strip() and '"' not in term:
        term = f'"{term.strip()}"'
    data = _get("https://www.federalregister.gov/api/v1/documents.json",
                **{"conditions[term]": term, "per_page": limit, "order": "newest"}).json()
    out = []
    for r in data.get("results", []):
        agencies = ", ".join(a.get("name", "") for a in r.get("agencies") or [] if a.get("name"))
        out.append(candidate("gov", r.get("title", ""), r.get("html_url", ""),
                             f"{r.get('type', '')} · {agencies} · {(r.get('abstract') or '')[:250]}",
                             {"kind": "published", "value": r.get("publication_date", "")}))
    return out


def sec_filings(arg: str, limit: int = STEP_LIMIT) -> list[dict]:
    """SEC EDGAR full-text search: "exact phrase | forms", e.g. "going concern | 8-K". Past 30 days, newest
    first, one candidate per filing (a filing's exhibits are folded into it)."""
    from datetime import date, timedelta
    from .records import sec_filings as search_sec
    phrase, _, forms = (p.strip() for p in arg.partition("|"))
    out, seen = [], set()
    for r in search_sec(phrase, forms, (date.today() - timedelta(days=30)).isoformat()):
        accession = r["url"].rsplit("/", 2)[-2]
        if accession in seen:
            continue
        seen.add(accession)
        out.append(candidate("sec", f"{r['form']} {r['filer'][:120]}: {', '.join(r['items']) or phrase}", r["url"],
                             f"filed {r['date']} · {r['place']} · contains \"{phrase}\"",
                             {"kind": "published", "value": r["date"]}))
        if len(out) >= limit:
            break
    return out


def web_search(query: str, limit: int = 10) -> list[dict]:
    """A general web search (Brave, or the local browser with NEWSROOM_SEARCH_BACKEND=browser).
    Results carry no date, so they show what exists, not what is moving."""
    from . import web
    return [candidate("web_search", r["title"], r["url"], r.get("description", ""),
                      {"kind": "search_rank", "value": f"#{rank} {r.get('engine', 'web')} result for {query!r}"})
            for rank, r in enumerate(web.search(query, count=limit), 1)]


TOOLS = {
    "news_search": (news_search, "query", "Google News search, past 14 days unless the query says when:Nd. Best for what outlets are covering."),
    "subreddit": (subreddit, "subreddit name", "newest posts in a subreddit; add :hot for the hot list, e.g. gatech:hot"),
    "feed": (feed, "RSS or Atom URL", "any RSS or Atom feed, e.g. an institution's newsroom"),
    "federal_register": (federal_register_search, "search term", "Federal Register documents containing the exact phrase, newest first"),
    "sec_filings": (sec_filings, "exact phrase | forms", "SEC filings from the past 30 days containing the phrase, e.g. "
                    "\"going concern | 8-K\", \"subpoena | 10-Q\", \"Atlanta | 4\". Titles show what an 8-K reports "
                    "(auditor changed, executive departure, restatement)"),
    "web_search": (web_search, "query", "general web search; undated, so good for finding what exists, weak for what is moving. Keep queries short: site:example.org plus one or two words"),
}


def run_step(tool: str, arg: str) -> list[dict]:
    """One planned call: a tool from TOOLS with its argument, or a national feed from GATHERERS."""
    if tool in TOOLS:
        return TOOLS[tool][0](arg)
    if tool in GATHERERS:
        return GATHERERS[tool]()
    raise ValueError(f"unknown tool {tool!r}")


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
