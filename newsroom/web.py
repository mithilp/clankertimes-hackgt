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
MAX_DATA_TEXT = 2_000_000   # bulk data (ZIP indexes, CSVs): scouts search inside these with `find`
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; newsroom-research/0.1)"}
# SEC's fair-access policy: automated requests must declare who they are, or EDGAR answers 403. Its pages
# are plain HTML, so they skip the browser.
SEC_HEADERS = {"User-Agent": "Clanker Times newsroom research https://clankertimes.vercel.app/about"}


def _plain_http(url: str) -> bool:
    """Pages the browser can't or needn't open: PDFs and ZIPs start a download, and SEC wants a declared agent."""
    return urlparse(url).path.lower().endswith((".pdf", ".zip", ".csv", ".txt", ".xml")) or host(url).endswith("sec.gov")


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
        if settings.search_fallback != "brave" or not settings.brave_api_key:
            raise SearchUnavailable(str(e)) from e
        try:
            return _brave(query, count)
        except httpx.HTTPError as b:
            raise SearchUnavailable(f"{e}; brave: {type(b).__name__}: {b}"[:300]) from b


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
    text = _body_text(response, url)[:MAX_DATA_TEXT if _kind(response, url) in ("zip", "table") else MAX_TEXT]
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
        if _kind(response, url) != "html":
            text = _body_text(response, url)[:MAX_DATA_TEXT if _kind(response, url) in ("zip", "table") else MAX_TEXT]
            if text:
                remember(url, text)
            return {"url": final, "title": "", "text": text, "links": []}
        html = response.text
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


def _kind(response, url: str) -> str:
    ctype = response.headers.get("content-type", "").lower()
    path = urlparse(str(response.url) or url).path.lower()
    if "pdf" in ctype or path.endswith(".pdf"):
        return "pdf"
    if "zip" in ctype or path.endswith(".zip") or response.content[:4] == b"PK\x03\x04":
        return "zip"
    if path.endswith((".csv", ".tsv")) or "csv" in ctype:
        return "table"
    if path.endswith(".txt") and "\t" in response.text[:2000]:
        return "table"
    return "html"


def _body_text(response, url: str) -> str:
    """The readable text of a response: a PDF's text, a ZIP's text files, a CSV/TSV's labeled rows, or a page."""
    kind = _kind(response, url)
    if kind == "pdf":
        return _pdf_text(response.content)
    if kind == "zip":
        return _zip_text(response.content)
    if kind == "table":
        return _delimited_text(response.text)
    return _html_text(response.text)


def _zip_text(data: bytes) -> str:
    """The text files inside a ZIP (agencies publish bulk indexes this way, e.g. the House Clerk's yearly
    financial-disclosure index), each under its own heading. Delimited files come back as labeled rows."""
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    parts, size = [], 0
    names = {i.filename.lower() for i in archive.infolist()}
    for info in archive.infolist():
        name = info.filename
        if info.is_dir() or not name.lower().endswith((".txt", ".csv", ".tsv", ".xml", ".json")):
            continue
        stem = name.lower().rsplit(".", 1)[0]
        if name.lower().endswith(".xml") and ({f"{stem}.txt", f"{stem}.csv", f"{stem}.tsv"} & names):
            continue                  # the same data again in XML (the House Clerk ships both)
        raw = archive.read(info)[:MAX_DATA_TEXT]
        text = raw.decode("utf-8", errors="replace") if b"\x00" not in raw[:1000] else raw.decode("utf-16", errors="replace")
        if name.lower().endswith((".csv", ".tsv", ".txt")) and ("\t" in text[:2000] or name.lower().endswith(".csv")):
            text = _delimited_text(text)
        parts.append(f"== {name} ==\n{text}")
        size += len(text)
        if size >= MAX_DATA_TEXT:
            break
    return "\n\n".join(parts)


def _delimited_text(text: str) -> str:
    """CSV or tab-separated data as one line per row, every value labeled with its column name, so a quoted
    row says what each number is ("FilingDate: 3/4/2025 | DocID: 20026481") instead of a bare string of values."""
    import csv
    lines = text.splitlines()
    if not lines:
        return ""
    delimiter = "\t" if lines[0].count("\t") >= max(1, lines[0].count(",")) else ","
    rows = list(csv.reader(lines, delimiter=delimiter))
    header = [h.strip() for h in rows[0]]
    if not any(header) or len(rows) < 2:
        return text
    out = []
    for row in rows[1:]:
        cells = [f"{h}: {v.strip()}" for h, v in zip(header, row) if v.strip() and h]
        if cells:
            out.append(" | ".join(cells))
    return "\n".join(out)


def _table_rows(table) -> list[str]:
    """An HTML table as one line per row. With a header row, each value is labeled with its column."""
    rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    first = table.find("tr")
    has_header = bool(table.find("thead")) or (first is not None and first.find("th") is not None and not first.find("td"))
    if not has_header or len(rows) < 2:
        return [" | ".join(c for c in r if c) for r in rows]
    header, out = rows[0], []
    for r in rows[1:]:
        if len(r) == len(header):
            out.append(" | ".join(f"{h}: {v}" if h else v for h, v in zip(header, r) if v))
        else:
            out.append(" | ".join(c for c in r if c))
    return out


def _html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    # Tables keep their structure: one line per row, values labeled with their column headers. Flattened to
    # one cell per line, a row of numbers loses what the numbers are, and a draft built on it can't be checked.
    for table in soup.find_all("table"):
        if table.find("table"):
            continue                      # layout tables that nest data tables: handle the inner ones
        table.replace_with("\n" + "\n".join(_table_rows(table)) + "\n")
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
