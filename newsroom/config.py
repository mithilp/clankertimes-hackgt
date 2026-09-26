"""Settings, read from the environment (and .env) each time they are needed."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
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
    return Settings(
        deepseek_api_key=env("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        fast_model=env("DEEPSEEK_FAST_MODEL", "deepseek-flash"),
        smart_model=env("DEEPSEEK_SMART_MODEL", "deepseek-v4-pro"),
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
