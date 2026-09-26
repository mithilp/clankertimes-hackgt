"""Take turns with every other newsroom process on this machine when calling a site that rate-limits.

Several Bossman loops, the pipeline and its scouts can all hit the same site at once, and sites count
requests per IP, not per process. State lives in runs/throttle/<name>.json behind a file lock:

    with pace("duckduckgo", gap=3):     # at most one request every 3s across all processes
        ...
    pause("duckduckgo", 600)            # after a bot check: everyone skips it for 10 minutes
    paused("duckduckgo")                # seconds left on a pause, or 0

On Windows (no fcntl) the schedule is shared by threads in one process only.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

DIR = Path("runs") / "throttle"
_locks: dict[str, threading.Lock] = {}


@contextmanager
def _locked(name: str):
    DIR.mkdir(parents=True, exist_ok=True)
    lock = _locks.setdefault(name, threading.Lock())
    with lock, open(DIR / f"{name}.lock", "w") as handle:
        if fcntl:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _state(name: str) -> dict:
    try:
        return json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(name: str, state: dict) -> None:
    (DIR / f"{name}.json").write_text(json.dumps(state), encoding="utf-8")


@contextmanager
def pace(name: str, gap: float):
    """Wait for this site's turn, then hold it while the request runs."""
    with _locked(name):
        state = _state(name)
        wait = state.get("last", 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            yield
        finally:
            state = _state(name)
            state["last"] = time.time()
            _save(name, state)


def pause(name: str, seconds: float) -> None:
    with _locked(name):
        state = _state(name)
        state["paused_until"] = max(state.get("paused_until", 0), time.time() + seconds)
        _save(name, state)


def paused(name: str) -> float:
    return max(0.0, _state(name).get("paused_until", 0) - time.time())


def tally(name: str) -> int:
    """Count one use of a metered service today, across all processes. Returns today's count."""
    with _locked(name):
        state = _state(name)
        today = time.strftime("%Y-%m-%d")
        if state.get("day") != today:
            state["day"], state["uses"] = today, 0
        state["uses"] = state.get("uses", 0) + 1
        _save(name, state)
        return state["uses"]
