"""Settings, read from the environment (and .env) each time they are needed."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_provider: str        # "deepseek" (default) or "claude_code": the local `claude` CLI on a Claude plan
    claude_bin: str
    claude_concurrency: int  # concurrent `claude -p` processes
    search_backend: str      # "browser" (default): no API key, DuckDuckGo first; or "brave": the Brave API
    search_fallback: str     # "brave" (default): when every browser engine is paused, use the Brave API; or "none"
    browser_channel: str | None  # e.g. "chrome" to drive the installed Chrome instead of bundled Chromium
    deepseek_api_key: str | None
    deepseek_base_url: str
    fast_model: str          # bulk work: reading complaints, grouping claims, scouts reading pages
    smart_model: str         # the reporter's judgment and writing
    brave_api_key: str | None
    openfda_api_key: str | None
    courtlistener_token: str | None  # optional: CourtListener search works without one, at a lower rate limit
    db_path: Path
    published_dir: Path
    top_n: int               # claim groups the reporter looks at
    min_complaints: int      # products with fewer complaints are not read at all
    min_group: int           # hunt: a claim group needs this many distinct people to be a lead
    scout_budget: int        # searches plus page reads per scout
    reporters: int           # stories worked on at once, one per reporter


def load() -> Settings:
    env = os.getenv
    provider = env("NEWSROOM_LLM_PROVIDER", "deepseek").lower()
    # Callers just ask for the fast or smart model; which family that is depends on the provider.
    if provider == "claude_code":
        fast_model = env("CLAUDE_FAST_MODEL", "claude-sonnet-5")
        smart_model = env("CLAUDE_SMART_MODEL", "claude-opus-5")
    else:
        fast_model = env("DEEPSEEK_FAST_MODEL", "deepseek-flash")
        smart_model = env("DEEPSEEK_SMART_MODEL", "deepseek-v4-pro")
    return Settings(
        llm_provider=provider,
        claude_bin=env("CLAUDE_BIN", "claude"),
        claude_concurrency=int(env("NEWSROOM_CLAUDE_CONCURRENCY", "2")),
        search_backend=env("NEWSROOM_SEARCH_BACKEND", "browser").lower(),
        search_fallback=env("NEWSROOM_SEARCH_FALLBACK", "brave").lower(),
        browser_channel=env("NEWSROOM_BROWSER_CHANNEL") or None,
        deepseek_api_key=env("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        fast_model=fast_model,
        smart_model=smart_model,
        brave_api_key=env("BRAVE_API_KEY") or None,
        openfda_api_key=env("OPENFDA_API_KEY") or None,
        courtlistener_token=env("COURTLISTENER_TOKEN") or None,
        db_path=Path(env("NEWSROOM_DB", "data/newsroom.db")),
        published_dir=Path(env("NEWSROOM_PUBLISHED", "published")),
        top_n=int(env("NEWSROOM_TOP_N", "20")),
        min_complaints=int(env("NEWSROOM_MIN_COMPLAINTS", "10")),
        min_group=int(env("NEWSROOM_MIN_GROUP", "10")),
        scout_budget=int(env("NEWSROOM_SCOUT_BUDGET", "30")),
        reporters=int(env("NEWSROOM_REPORTERS", "3")),
    )
