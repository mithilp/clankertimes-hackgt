"""Brave Search and page fetching for scouts. Fetched pages are cached, so quotes can be re-checked later."""

import io
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config, db

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_TEXT = 200_000
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; newsroom-research/0.1)"}


class SearchError(RuntimeError):
    pass


def search(query: str, count: int = 10) -> list[dict]:
    """Brave by default; NEWSROOM_SEARCH_BACKEND=browser searches with a local headless browser instead."""
    key = config.load().brave_api_key
    if config.load().search_backend == "browser":
        from . import browser
        try:
            return browser.search(query, count)
        except browser.BrowserError:
            if not key:
                raise
            # The engine blocked us or served junk; the Brave API is the honest fallback.
    if not key:
        raise SearchError("BRAVE_API_KEY is not set: add it to .env")
    response = httpx.get(BRAVE_URL, params={"q": query, "count": count},
                         headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=30)
    response.raise_for_status()
    return [
        {"title": _strip_tags(r.get("title", "")), "url": r["url"], "description": _strip_tags(r.get("description", "")),
         "engine": "brave"}
        for r in response.json().get("web", {}).get("results", []) if r.get("url")
    ]


def fetch_text(url: str) -> str:
    """The readable text of a page or PDF, or "" if it can't be fetched."""
    with db.session() as conn:
        row = conn.execute("select text from pages where url = ?", (url,)).fetchone()
    if row:
        return row["text"]
    if config.load().search_backend == "browser":
        # The browser renders JS and gets past plain-HTTP blocks; fall through to httpx if it fails.
        from . import browser
        try:
            text = browser.fetch_text(url)
            if text:
                remember(url, text)
                return text
        except browser.Blocked:
            return ""
        except browser.BrowserError:
            pass
    try:
        response = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return ""
    is_pdf = "pdf" in response.headers.get("content-type", "") or url.lower().endswith(".pdf")
    text = (_pdf_text(response.content) if is_pdf else _html_text(response.text))[:MAX_TEXT]
    if text:
        with db.session() as conn:
            conn.execute("insert or replace into pages (url, fetched, text) values (?, ?, ?)", (url, db.now(), text))
    return text


def remember(url: str, text: str) -> None:
    """Store text for a URL, e.g. an official record looked up through an API, so it can be read and quoted."""
    with db.session() as conn:
        conn.execute("insert or replace into pages (url, fetched, text) values (?, ?, ?)", (url, db.now(), text))


def host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages[:50])
    except Exception:  # malformed PDFs are common; treat them as unreadable
        return ""


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)
