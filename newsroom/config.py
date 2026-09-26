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
    reporter: str = os.getenv("MODEL_REPORTER", "claude-opus-5")     # plans sub-claims, writes the story
    tipster: str = os.getenv("MODEL_TIPSTER", "claude-sonnet-5")     # finds stories on a beat
    scout: str = os.getenv("MODEL_SCOUT", "claude-sonnet-5")         # researches one sub-claim
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
    # "anthropic" (default) or "gemini" (Google AI Studio; set MODEL_* to Gemini model ids).
    provider: str = os.getenv("LLM_PROVIDER", "anthropic")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    archive_dir: Path = Path(os.getenv("ARCHIVE_DIR", "./archive"))
    beats_file: Path = Path(os.getenv("BEATS_FILE", "./config/beats.json"))
    worker_id: str = os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
    memory_enabled: bool = os.getenv("MEMORY_ENABLED", "true").lower() != "false"  # mem0 costs a model call per write
    models: Models = field(default_factory=Models)

    # Parallelism, per process. Scale further by running more replicas.
    reporter_concurrency: int = _int("REPORTER_CONCURRENCY", 2)
    scout_concurrency: int = _int("SCOUT_CONCURRENCY", 4)
    reviewer_concurrency: int = _int("REVIEWER_CONCURRENCY", 2)
    # MVP runs the verifier alone; the full panel is "verifier,skeptic,fairness".
    reviewers: tuple = tuple(os.getenv("REVIEWERS", "verifier,skeptic,fairness").split(","))

    # Tipsters
    tipster_interval_s: int = _int("TIPSTER_INTERVAL_S", 600)
    tipster_max_calls: int = _int("TIPSTER_MAX_CALLS", 15)

    # Managing editor
    promote_threshold: float = _float("PROMOTE_THRESHOLD", 0.55)
    timeliness_days: float = _float("TIMELINESS_DAYS", 60)        # e-folding time: 60 days old -> 0.37
    coverage_similarity: float = _float("COVERAGE_SIMILARITY", 0.65)  # news item this close = already covered
    duplicate_similarity: float = _float("DUPLICATE_SIMILARITY", 0.85)
    max_active_stories: int = _int("MAX_ACTIVE_STORIES", 6)
    max_appeals_granted: int = _int("MAX_APPEALS_GRANTED", 1)
    spend_cap_usd: float = _float("SPEND_CAP_USD", 150.0)

    # Planning and scouting
    max_sub_claims: int = _int("MAX_SUB_CLAIMS", 6)
    max_follow_ups: int = _int("MAX_FOLLOW_UPS", 3)         # follow-up sub-claims, one round per story
    sub_claim_calls: int = _int("SUB_CLAIM_CALLS", 10)      # research calls per sub-claim
    yield_window: int = _int("YIELD_WINDOW", 5)             # last N page fetches with zero new facts
    lease_seconds: int = _int("LEASE_SECONDS", 180)

    fetch_chars: int = _int("FETCH_CHARS", 12000)           # page text returned per fetch call


settings = Settings()
