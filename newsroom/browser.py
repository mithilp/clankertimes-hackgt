"""Web search without an API key, and page reading with a local browser. The default search backend.

Search tries, in order, until one gives results that are plainly about the query:
    1. DuckDuckGo's lite page, over plain HTTP (no browser needed)
    2. DuckDuckGo's HTML page, over plain HTTP
    3. Bing, in a local headless browser (needs playwright; Bing often serves headless browsers
       results for some other query, which the relevance check below throws out)

Every engine is shared fairly with the other newsroom processes on this machine (newsroom/throttle.py).
If an engine answers with a bot check, we never try to get past it: that engine is paused for everyone
for ten minutes and the search moves on. The Brave Search API is still available, but only when
NEWSROOM_SEARCH_BACKEND=brave is set; it is never a silent fallback.

Reading pages uses the headless browser (render JS, get past plain-HTTP blocks):
    pip install playwright && playwright install chromium
NEWSROOM_BROWSER_CHANNEL=chrome uses the installed Chrome instead of bundled Chromium.
"""

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

from . import config

MAX_TEXT = 200_000
ENGINE_GAP = 10           # seconds between requests to one engine, across all processes (3s drew bot checks)
BLOCK_PAUSE = 600         # seconds an engine is skipped after it shows a bot check
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/140.0.0.0 Safari/537.36")
_BLOCK_MARKERS = ("captcha", "are you a robot", "verify you are human", "unusual traffic", "cf-challenge",
                  "access denied", "permission to access", "just a moment")


class BrowserError(RuntimeError):
    pass


class Blocked(BrowserError):
    """The page is behind bot detection. We never bypass these; look somewhere else."""


class _Browser:
    """One headless browser for the process, started on first use.

    Playwright's sync API only works on the thread that started it, and agents call search and fetch
    from thread pools. So one dedicated thread owns the browser, and every call is queued to it: calls
    from many threads are safe, and run one page at a time.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")

    def _ensure(self):
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
        return self._thread.submit(self._html, url, wait_for).result()

    def _html(self, url: str, wait_for: str | None) -> str:
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
        self._thread.submit(self._close).result()

    def _close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None


_browser = _Browser()


def _http(url: str, query: str) -> str:
    import httpx
    response = httpx.post(url, data={"q": query}, headers={"User-Agent": _UA}, timeout=25, follow_redirects=True)
    if response.status_code == 202 or "anomaly" in response.text[:20000].lower():
        raise Blocked(f"{url} answered with a bot check")
    response.raise_for_status()
    return response.text


def _ddg_lite(query: str, count: int) -> list[dict]:
    html = _http("https://lite.duckduckgo.com/lite/", query)
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for link in soup.select("a.result-link"):
        url = _real_url(link.get("href", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        row = link.find_parent("tr")
        snippet = row.find_next_sibling("tr") if row else None
        cell = snippet.select_one(".result-snippet") if snippet else None
        results.append({"title": link.get_text(" ", strip=True), "url": url,
                        "description": cell.get_text(" ", strip=True) if cell else ""})
        if len(results) >= count:
            break
    return results


def _ddg_html(query: str, count: int) -> list[dict]:
    return _parse(_http("https://html.duckduckgo.com/html/", query), ".result, .web-result", "a.result__a",
                  ".result__snippet", count)


def _bing(query: str, count: int) -> list[dict]:
    html = _browser.html(f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}&count={max(count, 10)}")
    _check_blocked(html)
    return _parse(html, "li.b_algo", "h2 a", ".b_caption p, .b_algoSlug", count)


ENGINES = [("duckduckgo-lite", _ddg_lite), ("duckduckgo", _ddg_html), ("bing", _bing)]


def search(query: str, count: int = 10) -> list[dict]:
    """Same shape as web.search: [{title, url, description, engine}]."""
    from . import throttle
    problems = []
    for name, engine in ENGINES:
        if (left := throttle.paused(name)) > 0:
            problems.append(f"{name} paused for {left / 60:.0f} more minutes after a bot check")
            continue
        try:
            with throttle.pace(name, ENGINE_GAP):
                results = engine(query, count)
        except Blocked as e:
            throttle.pause(name, BLOCK_PAUSE)
            problems.append(f"{name}: {e}; pausing it for everyone")
            continue
        except Exception as e:  # noqa: BLE001 - a dead engine (network, playwright missing): try the next
            problems.append(f"{name}: {type(e).__name__}: {e}"[:200])
            continue
        if not results:
            problems.append(f"{name}: no results")
        elif not _matches_query(query, results):
            problems.append(f"{name}: results unrelated to the query")
        else:
            return [{**r, "engine": name} for r in results]
    raise BrowserError("no search engine gave usable results: " + "; ".join(problems))


_STOP = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "not"}


def _matches_query(query: str, results: list[dict]) -> bool:
    """Search engines sometimes serve an automated browser plausible-looking results for some other
    query. Accept a page only if it is plainly about the query: with site:, some result is on that
    site; otherwise some result mentions at least half of the query's words."""
    site = re.search(r"site:(\S+)", query)
    if site:
        domain = site.group(1).lower().removeprefix("www.")
        return any((urllib.parse.urlparse(r["url"]).hostname or "").lower().removeprefix("www.").endswith(domain)
                   for r in results)
    words = {w for w in re.findall(r"[a-z0-9]{3,}", re.sub(r"(^|\s)-\S+", " ", query.lower())) if w not in _STOP}
    if not words:
        return True
    return any(len(words & set(re.findall(r"[a-z0-9]{3,}", f"{r['title']} {r['description']}".lower()))) * 2 >= len(words)
               for r in results[:5])


def _parse(html: str, result_sel: str, link_sel: str, snippet_sel: str, count: int, title_sel: str | None = None) -> list[dict]:
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
        title = block.select_one(title_sel) if title_sel else None
        results.append({
            "title": (title or link).get_text(" ", strip=True),
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
