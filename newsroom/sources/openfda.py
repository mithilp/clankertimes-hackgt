"""openFDA, shared by the FDA sources (MAUDE, FAERS, CAERS) and the FDA recall lookups.

openFDA pages through at most 25,000 results per query, so big sources are fetched by date range.
"""

from collections.abc import Iterator

import httpx

from .. import config

BASE_URL = "https://api.fda.gov"
PAGE_WITH_KEY = 1000
PAGE_WITHOUT_KEY = 100  # openFDA refuses bigger pages from callers without an API key
MAX_SKIP = 25_000


def query(endpoint: str, search: str, *, limit: int, sort: str | None = None) -> Iterator[dict]:
    """Yield up to `limit` records from an endpoint such as "device/event", optionally sorted ("field:desc")."""
    api_key = config.load().openfda_api_key
    page = PAGE_WITH_KEY if api_key else PAGE_WITHOUT_KEY
    fetched = 0
    while fetched < limit and fetched < MAX_SKIP:
        params = {"search": search, "limit": min(page, limit - fetched), "skip": fetched}
        if sort:
            params["sort"] = sort
        if api_key:
            params["api_key"] = api_key
        response = httpx.get(f"{BASE_URL}/{endpoint}.json", params=params, timeout=120)
        if response.status_code == 404:  # openFDA's answer to "nothing matches"
            return
        response.raise_for_status()
        results = response.json().get("results", [])
        yield from results
        fetched += len(results)
        if len(results) < params["limit"]:
            return


def ymd(value: str | None) -> str:
    """"20260105" -> "2026-01-05"."""
    v = (value or "").strip()
    return f"{v[0:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 else "1900-01-01"
