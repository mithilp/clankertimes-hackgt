"""Entry point: python -m newsroom <role>

Roles:
  initdb    apply db/schema.sql if the database is empty (idempotent)
  scout     all beats, in parallel (one advisory lock per beat)
  editor    managing editor (singleton; extra replicas wait as standbys)
  reporter  REPORTER_CONCURRENCY story slots
  reviewer  REVIEWER_CONCURRENCY review slots, three reviewers each
  all       every role in one process, for local development
"""

import asyncio
import logging
import sys
from pathlib import Path

import asyncpg

from . import editor, reporter, reviewer, scout
from .config import settings

ROLES = {"scout": scout.main, "editor": editor.main, "reporter": reporter.main, "reviewer": reviewer.main}


async def initdb() -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        if await conn.fetchval("select to_regclass('public.leads')"):
            logging.info("schema already present")
            return
        await conn.execute((Path(__file__).parent.parent / "db" / "schema.sql").read_text(encoding="utf-8"))
        logging.info("schema applied")
    finally:
        await conn.close()


async def run_all() -> None:
    await initdb()
    await asyncio.gather(*(fn() for fn in ROLES.values()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "initdb":
        asyncio.run(initdb())
    elif role == "all":
        asyncio.run(run_all())
    elif role in ROLES:
        asyncio.run(ROLES[role]())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
