"""Web access: search, fetch, a headless browser, and the local archive quoted spans are checked against."""

import asyncio
import hashlib
import io
import logging
import re

import httpx
import trafilatura
from pypdf import PdfReader

from . import db
from .config import settings

log = logging.getLogger(__name__)

USER_AGENT = "ClankerTimesBot/0.1 (automated AI newsroom, HackGT 13)"
_http = httpx.AsyncClient(
    headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0)
)
_BLOCK_MARKERS = ("captcha", "are you a robot", "verify you are human", "access denied", "cf-challenge")


class Blocked(Exception):
    """Page is behind bot detection. We never bypass these; the agent should look elsewhere."""


async def search(query: str, *, news: bool = False, count: int = 10, freshness: str | None = None) -> list[dict]:
    """Brave Search. freshness: 'pd' day, 'pw' week, 'pm' month, 'py' year."""
    kind = "news" if news else "web"
    params = {"q": query, "count": count}
    if freshness:
        params["freshness"] = freshness
    r = await _http.get(
        f"https://api.search.brave.com/res/v1/{kind}/search",
        params=params,
        headers={"X-Subscription-Token": settings.brave_api_key, "Accept": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    results = data.get("results", []) if news else data.get("web", {}).get("results", [])
    return [
        {
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "snippet": re.sub(r"<[^>]+>", "", x.get("description", "")),
            "age": x.get("age") or x.get("page_age") or "",
            "source": (x.get("meta_url") or {}).get("hostname", ""),
        }
        for x in results
    ]


def _check_blocked(text: str) -> None:
    head = text[:3000].lower()
    if len(text) < 3000 and any(m in head for m in _BLOCK_MARKERS):
        raise Blocked("page is behind bot detection; not bypassing it")


def _html_to_text(html: str) -> str:
    text = trafilatura.extract(html, include_tables=True, include_links=False, favor_recall=True)
    if not text:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))


async def fetch(url: str) -> str:
    r = await _http.get(url)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text = await asyncio.to_thread(_pdf_to_text, r.content)
    elif "html" in ctype or "xml" in ctype:
        text = await asyncio.to_thread(_html_to_text, r.text)
    else:
        text = r.text
    _check_blocked(text)
    return text


class Browser:
    """One headless Chromium per process, shared by that process's concurrent agents."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)
        return self._browser

    async def render(self, url: str, wait_for: str | None = None) -> str:
        browser = await self._ensure()
        ctx = await browser.new_context(user_agent=USER_AGENT)
        try:
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=15_000)
            html = await page.content()
        finally:
            await ctx.close()
        text = await asyncio.to_thread(_html_to_text, html)
        _check_blocked(text)
        return text


browser = Browser()


# --- archive -----------------------------------------------------------------------------


async def archive(url: str, text: str, seen_by: str):
    """Store extracted text for a URL. Returns (source_id, changed) where changed means new content."""
    source_id, _ = await db.upsert_source(url, seen_by)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    path = settings.archive_dir / f"{db.url_hash(url)}.txt"
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode()).hexdigest()
    p = await db.pool()
    old = await p.fetchval(
        """update sources s set archive_path = $2, fetched_at = now(), content_hash = $3
           from (select content_hash from sources where id = $1) prev
           where s.id = $1 returning prev.content_hash""",
        source_id, str(path), digest,
    )
    return source_id, old != digest


def read_archive(url: str) -> str | None:
    path = settings.archive_dir / f"{db.url_hash(url)}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def span_in_source(url: str, quoted_span: str) -> bool:
    """Deterministic citation check: the quoted span must appear verbatim (modulo whitespace) in what we fetched."""
    text = read_archive(url)
    return bool(text) and _norm(quoted_span) in _norm(text)


def span_context(url: str, quoted_span: str, chars: int = 600) -> str:
    text = read_archive(url) or ""
    norm_text, norm_span = _norm(text), _norm(quoted_span)
    i = norm_text.find(norm_span)
    if i < 0:
        return ""
    return norm_text[max(0, i - chars): i + len(norm_span) + chars]
