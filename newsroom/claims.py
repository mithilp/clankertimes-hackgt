"""Step 2: a model reads each complaint and writes down the claim it makes.

Product, company and severity come straight from the data; the model only writes the claim. Complaints
are sent in batches from one product at a time, so the same problem tends to get the same wording.
"""

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm

BATCH = 25
MAX_TEXT = 1500

SYSTEM = """You read safety complaints about one product and write down the problem each complaint describes.

For each complaint, write its claim as one short plain sentence (at most 12 words) about what went wrong with the product, for example "engine shuts off while driving" or "pump delivers the whole bag at once".
- Use the same wording for the same problem, so complaints about one problem can be counted together.
- Describe the product's problem only: no names, places, dates or personal details.
- If the complaint describes no clear problem with the product, use null.

Reply in JSON: {"claims": [{"id": "<complaint id>", "claim": "<sentence or null>"}]}"""


def pending(conn: sqlite3.Connection, min_complaints: int, products: list[str] | None = None) -> dict[str, list[sqlite3.Row]]:
    """Complaints without a claim yet, for products with at least min_complaints complaints (optionally only some)."""
    only = f"and c.product in ({','.join('?' * len(products))})" if products else ""
    rows = conn.execute(
        f"""select c.id, c.product, c.text from complaints c
            left join claims k on k.complaint_id = c.id
            where k.complaint_id is null
              and c.product in (select product from complaints group by product having count(*) >= ?)
              {only}
            order by c.product, c.received, c.id""",
        (min_complaints, *(products or [])),
    ).fetchall()
    by_product: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_product[row["product"]].append(row)
    return by_product


def extract(conn: sqlite3.Connection, *, min_complaints: int, limit: int | None = None, workers: int = 4,
            progress: Callable[[int, int], None] | None = None, products: list[str] | None = None) -> int:
    """Write claims for pending complaints (optionally only some products). Returns how many got a claim (or null)."""
    batches = []
    total = 0
    for product, rows in pending(conn, min_complaints, products).items():
        for start in range(0, len(rows), BATCH):
            batch = rows[start:start + BATCH]
            if limit is not None and total + len(batch) > limit:
                batch = batch[:limit - total]
            if batch:
                batches.append((product, batch))
                total += len(batch)
            if limit is not None and total >= limit:
                break
        if limit is not None and total >= limit:
            break

    done = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(_read_batch, product, batch) for product, batch in batches]
        for future in as_completed(futures):
            results = future.result()
            conn.executemany("insert or replace into claims (complaint_id, claim) values (?, ?)", results)
            conn.commit()
            done += len(results)
            if progress:
                progress(done, total)
    return done


def _read_batch(product: str, batch: list[sqlite3.Row]) -> list[tuple[str, str | None]]:
    results = [(row["id"], None) for row in batch if not row["text"].strip()]
    readable = [row for row in batch if row["text"].strip()]
    if not readable:
        return results
    listing = "\n\n".join(f"[{row['id']}]\n{row['text'][:MAX_TEXT]}" for row in readable)
    reply = llm.ask_json(SYSTEM, f"Product: {product}\n\nComplaints:\n\n{listing}", max_tokens=3000)
    wanted = {row["id"] for row in readable}
    for item in reply.get("claims", []):
        cid = str(item.get("id", "")).strip("[] ")
        if cid in wanted:
            claim = item.get("claim")
            claim = claim.strip() if isinstance(claim, str) and claim.strip() and claim.strip().lower() != "null" else None
            results.append((cid, claim))
            wanted.discard(cid)
    # Complaints the model skipped stay pending and are retried on the next run.
    return results
