"""Local-browser search and fetching: a free alternative to the Brave Search API.

Brave stays the default. Set NEWSROOM_SEARCH_BACKEND=browser to drive a real headless browser
on this machine instead, which costs nothing and reads pages that block plain HTTP.

    pip install playwright && playwright install chromium

By default it launches Playwright's bundled Chromium. Set NEWSROOM_BROWSER_CHANNEL=chrome to use
the Chrome already installed on the machine instead.

Searches read Bing's results page, which a real browser gets served normally (DuckDuckGo's HTML
endpoint refuses headless traffic, so it is only a fallback). If a search page comes back as a bot
challenge we raise rather than trying to get around it, and the newsroom can fall back to the Brave
API by flipping the env var back.
"""

import re
import threading
import urllib.parse

from bs4 import BeautifulSoup

from . import config

MAX_TEXT = 200_000
# (engine url template, result selector, link selector, snippet selector)
_ENGINES = [
    ("https://www.bing.com/search?q={q}&count={n}", "li.b_algo", "h2 a", ".b_caption p, .b_algoSlug"),
    ("https://html.duckduckgo.com/html/?q={q}", ".result, .web-result", "a.result__a", ".result__snippet"),
]
_BLOCK_MARKERS = ("captcha", "are you a robot", "verify you are human", "unusual traffic", "cf-challenge",
                  "access denied", "permission to access", "just a moment")


class BrowserError(RuntimeError):
    pass


class Blocked(BrowserError):
    """The page is behind bot detection. We never bypass these; look somewhere else."""


class _Browser:
    """One headless browser for the process, started on first use."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            if self._browser is None:
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError as e:
                    raise BrowserError(
                        "playwright is not installed: pip install playwright && playwright install chromium"
                    ) from e
                channel = config.load().browser_channel
                self._pw = sync_playwright().start()
                launch = {"headless": True}
                if channel:
                    launch["channel"] = channel      # e.g. "chrome": use the installed browser
                self._browser = self._pw.chromium.launch(**launch)
            return self._browser

    def html(self, url: str, wait_for: str | None = None) -> str:
        browser = self._ensure()
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            locale="en-US",
        )
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if wait_for:
                page.wait_for_selector(wait_for, timeout=15_000)
            # A page that is mid-redirect refuses content(); settle, then retry once or twice.
            last = None
            for _ in range(3):
                try:
                    return page.content()
                except Exception as e:  # noqa: BLE001 - playwright raises a generic Error here
                    last = e
                    page.wait_for_timeout(1_500)
            raise BrowserError(f"could not read {url}: {last}")
        finally:
            context.close()

    def close(self) -> None:
        with self._lock:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None


_browser = _Browser()


def search(query: str, count: int = 10) -> list[dict]:
    """Same shape as web.search: [{title, url, description}]."""
    last_error = None
    for template, result_sel, link_sel, snippet_sel in _ENGINES:
        url = template.format(q=urllib.parse.quote_plus(query), n=max(count, 10))
        try:
            html = _browser.html(url)
            _check_blocked(html)
        except Blocked as e:
            last_error = e
            continue
        results = _parse(html, result_sel, link_sel, snippet_sel, count)
        if results:
            return results
        last_error = BrowserError(f"no results parsed from {urllib.parse.urlparse(url).hostname}")
    raise last_error or BrowserError("every search engine returned nothing")


def _parse(html: str, result_sel: str, link_sel: str, snippet_sel: str, count: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for block in soup.select(result_sel):
        link = block.select_one(link_sel)
        if not link:
            continue
        url = _real_url(link.get("href", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = block.select_one(snippet_sel)
        results.append({
            "title": link.get_text(" ", strip=True),
            "url": url,
            "description": snippet.get_text(" ", strip=True) if snippet else "",
        })
        if len(results) >= count:
            break
    return results


def fetch_text(url: str) -> str:
    """The readable text of a page, rendered by the browser. Raises on a bot wall."""
    from .web import _html_text        # one text extractor for both backends

    html = _browser.html(url)
    _check_blocked(html)
    return _html_text(html)[:MAX_TEXT]


def _real_url(href: str) -> str:
    """Unwrap the redirectors search engines put in front of results."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    host, query = parsed.hostname or "", urllib.parse.parse_qs(parsed.query)
    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
        return urllib.parse.unquote(query.get("uddg", [""])[0])
    if host.endswith("bing.com") and parsed.path.startswith("/ck/"):
        # Bing sometimes routes through /ck/a with the target base64 in "u" (prefixed "a1").
        raw = query.get("u", [""])[0]
        if raw.startswith("a1"):
            import base64
            padded = raw[2:] + "=" * (-len(raw[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - unwrappable redirector: skip the result
                return ""
        return ""
    return href if href.startswith("http") else ""


def _check_blocked(html: str) -> None:
    head = re.sub(r"<[^>]+>", " ", html[:4000]).lower()
    if len(html) < 20_000 and any(marker in head for marker in _BLOCK_MARKERS):
        raise Blocked("page is behind bot detection; not bypassing it")


def close() -> None:
    _browser.close()
