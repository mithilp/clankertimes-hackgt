"""Depth-first mode: scan the data one product at a time, and investigate each lead as soon as it's found.

Instead of reading every complaint before looking for stories, the hunt takes the next product (most serious
first), reads only its complaints, counts them, and asks the reporter about any big claim group. A lead is
investigated right away; then the hunt moves on to the next product. Tokens are spent only on products the
hunt actually reaches, and the next hunt carries on where the last one stopped.
"""

import sqlite3
from collections.abc import Callable, Iterator
from itertools import chain, zip_longest

from . import claims, config, count, db, reporter

CANDIDATES_PER_PRODUCT = 5


def queue(conn: sqlite3.Connection, source: str | None = None) -> Iterator[sqlite3.Row]:
    """Products still to scan, most serious first, taking turns between sources.

    A product is scanned again if it has gained complaints since it was last scanned.
    """
    min_complaints = config.load().min_complaints
    rows = conn.execute(
        f"""select c.source, c.product, count(*) as complaints, sum(c.severe) as severe
            from complaints c left join scanned s on s.source = c.source and s.product = c.product
            where c.product not like 'UNKNOWN %' {'and c.source = ?' if source else ''}
            group by c.source, c.product
            having count(*) >= ? and count(*) > coalesce(max(s.complaints), 0)
            order by severe desc, complaints desc""",
        (*([source] if source else []), min_complaints),
    ).fetchall()
    by_source: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)
    yield from (row for row in chain.from_iterable(zip_longest(*by_source.values())) if row is not None)


def hunt(conn: sqlite3.Connection, *, stories: int = 1, source: str | None = None, max_products: int = 50,
         say: Callable[[str], None] = print) -> list[int]:
    """Scan until `stories` leads have been investigated, or `max_products` products scanned. Returns story ids."""
    settings = config.load()
    investigated: list[int] = []
    scanned = 0
    for product in queue(conn, source):
        if len(investigated) >= stories or scanned >= max_products:
            break
        scanned += 1
        key = (product["source"], product["product"])
        say(f"[{scanned}] {product['product']} ({product['source']}): {product['complaints']} complaints, "
            f"{product['severe']} serious")

        read = claims.extract(conn, min_complaints=settings.min_complaints, products=[product["product"]])
        if read:
            say(f"    read {read} complaints")
        count.count(conn, only=key)

        candidates = [g for g in count.top(conn, CANDIDATES_PER_PRODUCT, source=key[0], product=key[1], skip_reported=True)
                      if g["total"] >= settings.min_group]
        if candidates:
            say("    candidates: " + "; ".join(f"{g['label']} ({g['total']})" for g in candidates))
            chosen = reporter.pick(conn, candidates, stories - len(investigated))
            for row in _judged(conn, key, candidates, outcome_not="picked"):
                say(f"    skipped {row['label']}: {row['reason']}")
            for group, reason in chosen:
                story_id = reporter.open_story(conn, group, reason)
                say(f"    LEAD: {group['label']}. {reason}\n    investigating as story {story_id}...")
                reporter.report(story_id)
                story = conn.execute("select status, note, article from stories where id = ?", (story_id,)).fetchone()
                say(f"    story {story_id}: {story['status']}  {story['article'] or story['note'] or ''}")
                investigated.append(story_id)
        else:
            say(f"    no new claim reported by {settings.min_group}+ people")

        # Done with this product unless the hunt stopped before the reporter judged all its candidates;
        # then the next hunt comes back for the rest.
        if len(_judged(conn, key, candidates)) == len(candidates):
            conn.execute("insert or replace into scanned (source, product, complaints, at) values (?, ?, ?, ?)",
                         (*key, product["complaints"], db.now()))
            conn.commit()
    if len(investigated) < stories:
        say(f"stopped after {scanned} products: {len(investigated)} of {stories} leads investigated")
    return investigated


def _judged(conn: sqlite3.Connection, key: tuple[str, str], groups: list[sqlite3.Row],
            outcome_not: str | None = None) -> list[sqlite3.Row]:
    """The reporter's judgments on these groups (optionally excluding one outcome)."""
    if not groups:
        return []
    labels = [g["label"] for g in groups]
    return conn.execute(
        f"select label, outcome, reason from reviewed where source = ? and product = ?"
        f" and label in ({','.join('?' * len(labels))}) {'and outcome != ?' if outcome_not else ''}",
        (*key, *labels, *([outcome_not] if outcome_not else [])),
    ).fetchall()
