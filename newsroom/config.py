"""Settings, read once from the environment. Every knob the trial run might tune lives here."""

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Models:
    reporter: str = os.getenv("MODEL_REPORTER", "claude-opus-5")
    scout: str = os.getenv("MODEL_SCOUT", "claude-sonnet-5")
    scorer: str = os.getenv("MODEL_SCORER", "claude-sonnet-5")
    editor: str = os.getenv("MODEL_EDITOR", "claude-sonnet-5")
    memory: str = os.getenv("MODEL_MEMORY", "claude-haiku-4-5")
    # Reviewers deliberately span models so their catches aren't correlated.
    verifier: str = os.getenv("MODEL_VERIFIER", "claude-sonnet-5")
    skeptic: str = os.getenv("MODEL_SKEPTIC", "claude-opus-5")
    fairness: str = os.getenv("MODEL_FAIRNESS", "claude-sonnet-5")


# $ per million tokens (input, output). Cache reads bill at 10%, writes at 125% of input.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://newsroom:newsroom@localhost:5432/newsroom")
    brave_api_key: str = os.getenv("BRAVE_API_KEY", "")
    archive_dir: Path = Path(os.getenv("ARCHIVE_DIR", "./archive"))
    beats_file: Path = Path(os.getenv("BEATS_FILE", "./config/beats.json"))
    worker_id: str = os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
    models: Models = field(default_factory=Models)

    # Parallelism, per process. Scale further by running more replicas.
    reporter_concurrency: int = _int("REPORTER_CONCURRENCY", 3)
    reviewer_concurrency: int = _int("REVIEWER_CONCURRENCY", 2)
    # MVP runs the verifier alone; the full panel is "verifier,skeptic,fairness".
    reviewers: tuple = tuple(os.getenv("REVIEWERS", "verifier,skeptic,fairness").split(","))

    # Scouts
    scout_interval_s: int = _int("SCOUT_INTERVAL_S", 600)
    scout_max_calls: int = _int("SCOUT_MAX_CALLS", 15)

    # Managing editor
    promote_threshold: float = _float("PROMOTE_THRESHOLD", 0.55)
    duplicate_similarity: float = _float("DUPLICATE_SIMILARITY", 0.85)
    max_active_stories: int = _int("MAX_ACTIVE_STORIES", 6)
    max_appeals_granted: int = _int("MAX_APPEALS_GRANTED", 1)
    spend_cap_usd: float = _float("SPEND_CAP_USD", 150.0)

    # Reporter stopping rules
    base_tool_calls: int = _int("BASE_TOOL_CALLS", 25)      # budget = base + score * extra
    extra_tool_calls: int = _int("EXTRA_TOOL_CALLS", 35)
    base_minutes: int = _int("BASE_MINUTES", 15)
    extra_minutes: int = _int("EXTRA_MINUTES", 30)
    yield_window: int = _int("YIELD_WINDOW", 5)             # last N page fetches with zero new facts
    lease_seconds: int = _int("LEASE_SECONDS", 180)

    fetch_chars: int = _int("FETCH_CHARS", 12000)           # page text returned per fetch call


settings = Settings()
