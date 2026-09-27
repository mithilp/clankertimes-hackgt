"""Web search and page fetching for scouts. Fetched pages are cached, so quotes can be re-checked later.

Search defaults to newsroom/browser.py: no API key, DuckDuckGo first. When every engine there is paused or
blocked, the Brave Search API takes the query (metered, so it is the fallback; NEWSROOM_SEARCH_FALLBACK=none
turns that off). NEWSROOM_SEARCH_BACKEND=brave uses Brave for everything."""

import io
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config, db

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_TEXT = 200_000
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; newsroom-research/0.1)"}
# SEC's fair-access policy: automated requests must declare who they are, or EDGAR answers 403. Its pages
# are plain HTML, so they skip the browser.
SEC_HEADERS = {"User-Agent": "Clanker Times newsroom research https://clankertimes.vercel.app/about"}


def _plain_http(url: str) -> bool:
    """Pages the browser can't or needn't open: PDFs start a download, and SEC wants a declared agent."""
    return urlparse(url).path.lower().endswith(".pdf") or host(url).endswith("sec.gov")


def _headers(url: str) -> dict:
    return SEC_HEADERS if host(url).endswith("sec.gov") else HEADERS


class SearchError(RuntimeError):
    """Search can't work at all as configured (e.g. the Brave backend without a key)."""


class SearchUnavailable(SearchError):
    """Every engine was paused, blocked or unhelpful just now. Temporary: try other sources, or later."""


def search(query: str, count: int = 10) -> list[dict]:
    """The free browser engines first (newsroom/browser.py); the metered Brave API only when they are all
    paused or blocked, or when NEWSROOM_SEARCH_BACKEND=brave."""
    settings = config.load()
    if settings.search_backend == "brave":
        return _brave(query, count)
    from . import browser
    try:
        return browser.search(query, count)
    except browser.BrowserError as e:
        problems = [str(e)]
        if settings.search_fallback == "none":
            raise SearchUnavailable(str(e)) from e
        # Metered fallbacks, in order: Firecrawl, then Brave. Each is skipped when unset or out of credit.
        for name, key, run in (("firecrawl", settings.firecrawl_api_key, _firecrawl),
                               ("brave", settings.brave_api_key, _brave)):
            if not key:
                continue
            try:
                return run(query, count)
            except httpx.HTTPError as err:
                problems.append(f"{name}: {type(err).__name__}: {str(err)[:120]}")
        raise SearchUnavailable("; ".join(problems)[:400]) from e


def _firecrawl(query: str, count: int) -> list[dict]:
    from . import throttle
    with throttle.pace("firecrawl", 0.5):
        response = httpx.post("https://api.firecrawl.dev/v2/search", timeout=45,
                              headers={"Authorization": f"Bearer {config.load().firecrawl_api_key}"},
                              json={"query": query, "limit": min(count, 10)})
    throttle.tally("firecrawl")
    response.raise_for_status()
    return [{"title": r.get("title", ""), "url": r["url"], "description": r.get("description", ""), "engine": "firecrawl"}
            for r in (response.json().get("data") or {}).get("web", []) if r.get("url")]


def _brave(query: str, count: int) -> list[dict]:
    from . import throttle
    key = config.load().brave_api_key
    if not key:
        raise SearchError("NEWSROOM_SEARCH_BACKEND=brave but BRAVE_API_KEY is not set in .env")
    with throttle.pace("brave", 0.2):
        response = httpx.get(BRAVE_URL, params={"q": query, "count": count},
                             headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=30)
    throttle.tally("brave")
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
    if row and not (urlparse(url).hostname == "news.google.com" and len(row["text"]) < 500):
        return row["text"]      # (a tiny cached Google News page is its redirect stub, not the article)
    if config.load().search_backend == "browser" and not _plain_http(url):
        # The browser renders JS and gets past plain-HTTP blocks; fall through to httpx if it fails.
        from . import browser
        try:
            text = browser.fetch_text(url)
            if text and not (urlparse(url).hostname == "news.google.com" and len(text) < 500):
                remember(url, text)
                return text
        except browser.Blocked:
            return ""
        except browser.BrowserError:
            pass
    try:
        response = httpx.get(url, headers=_headers(url), timeout=30, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return ""
    if urlparse(str(response.url)).hostname == "news.google.com":
        return ""        # still Google's redirect page, not the article: only the browser can follow it
    is_pdf = "pdf" in response.headers.get("content-type", "") or url.lower().endswith(".pdf")
    if response.content[:4] == b"PK\x03\x04":          # a ZIP (or a Word file, which is one): read what's inside
        text = _zip_text(response.content)[:MAX_TEXT]
    else:
        text = (_pdf_text(response.content) if is_pdf or response.content[:5] == b"%PDF-" else _html_text(response.text))[:MAX_TEXT]
    if text:
        with db.session() as conn:
            conn.execute("insert or replace into pages (url, fetched, text) values (?, ?, ?)", (url, db.now(), text))
    return text


def open_page(url: str) -> dict:
    """A page as a person browsing sees it: {url, title, text, links: [{url, text}]}. For navigating a site
    (an agency's report index, a docket, a filing list) without a search engine. PDFs come back as text with
    no links. The text is cached, so reading the page afterwards doesn't fetch it again."""
    from urllib.parse import urljoin
    html, final = "", url
    if config.load().search_backend == "browser" and not _plain_http(url):
        from . import browser
        try:
            final, html = browser.fetch_page(url)
        except browser.Blocked:
            return {"url": url, "title": "", "text": "", "links": [], "blocked": True}
        except browser.BrowserError:
            html = ""
    if not html:
        try:
            response = httpx.get(url, headers=_headers(url), timeout=30, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            return {"url": url, "title": "", "text": "", "links": []}
        final = str(response.url)
        if "pdf" in response.headers.get("content-type", "") or url.lower().endswith(".pdf"):
            text = _pdf_text(response.content)[:MAX_TEXT]
            if text:
                remember(url, text)
            return {"url": final, "title": "", "text": text, "links": []}
        html = response.text
    return _page(url, final, html)


def search_site(site: str, query: str) -> dict:
    """A site searched with its own search box, the way a person would: {url, title, text, links}.
    For sites whose records aren't in a general search engine, or when the engines are paused."""
    from . import browser
    url = site if site.startswith("http") else f"https://{site.strip('/')}"
    try:
        final, html = browser.search_site(url, query)
    except browser.Blocked:
        return {"url": url, "title": "", "text": "", "links": [], "blocked": True}
    except browser.BrowserError as e:
        return {"url": url, "title": "", "text": "", "links": [], "error": str(e)[:200]}
    return _page(final, final, html)


def _page(url: str, final: str, html: str) -> dict:
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(final, a["href"].strip()).split("#")[0]
        label = " ".join(a.get_text(" ", strip=True).split())[:120]
        if href.startswith("http") and href not in seen and href.rstrip("/") != final.rstrip("/") and label:
            seen.add(href)
            links.append({"url": href, "text": label})
    text = _html_text(html)[:MAX_TEXT]
    if text:
        remember(url, text)
    return {"url": final, "title": title, "text": text, "links": links}


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


def _zip_text(data: bytes) -> str:
    """Text of a Word document, or of the PDFs, Word files and text files inside a ZIP."""
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    names = archive.namelist()
    if "word/document.xml" in names:                    # the file itself is a .docx: join each paragraph's runs
        import html as html_lib
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
        paragraphs = ("".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p)) for p in xml.split("</w:p>"))
        return "\n".join(html_lib.unescape(p).strip() for p in paragraphs if p.strip())
    parts = []
    for name in names[:20]:
        low, raw = name.lower(), archive.read(name)
        if low.endswith(".pdf"):
            parts.append(f"[{name}]\n{_pdf_text(raw)}")
        elif low.endswith(".docx"):
            parts.append(f"[{name}]\n{_zip_text(raw)}")
        elif low.endswith((".txt", ".htm", ".html", ".csv")):
            parts.append(f"[{name}]\n{_html_text(raw.decode('utf-8', 'replace'))}")
    return "\n\n".join(p for p in parts if p.strip())


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages[:400])
    except Exception:  # malformed PDFs are common; treat them as unreadable
        return ""


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)
