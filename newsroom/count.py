"""Step 3: group claims that say the same thing about the same product, and count distinct people."""

import json
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import llm
from .text import words

COPY_MIN_WORDS = 12     # shorter texts ("brakes failed") match by chance, so they never count as copies
COPY_SIMILARITY = 0.8   # share of the shorter text's 5-word runs found in the other: above this, it's a copy
CHUNK = 300             # distinct claims per grouping call

GROUP_SYSTEM = """You group short complaint claims about one product by the problem they describe.

Claims that describe the same underlying problem go in one group, even if worded differently ("engine shut off while driving" and "car stalled on the highway"). Different problems with the same part stay separate ("brakes failed" and "brakes squeal").

Give each group a short plain label (at most 10 words). Every claim number must appear in exactly one group.

Reply in JSON: {"groups": [{"label": "...", "claims": [1, 5, 9]}]}"""


def distinct(members: list[dict]) -> list[dict]:
    """Decide which members of a group count, in the order given (oldest first).

    The same person counts once (members may carry a "person": the same report, or the same Bluesky
    account), and the same text submitted again, or copied with small edits, counts once.
    Returns [{"id", "counted", "duplicate_of"}].
    """
    kept: list[tuple[str, str, set]] = []
    people: dict[str, str] = {}
    marks = []
    for member in members:
        person = member.get("person")
        duplicate_of = people.get(person) if person else None
        ws = words(member["text"])
        if duplicate_of is None and len(ws) >= COPY_MIN_WORDS:
            joined, runs = " ".join(ws), _runs(ws)
            for kept_id, kept_joined, kept_runs in kept:
                if joined == kept_joined or len(runs & kept_runs) / min(len(runs), len(kept_runs)) >= COPY_SIMILARITY:
                    duplicate_of = kept_id
                    break
            if duplicate_of is None:
                kept.append((member["id"], joined, runs))
        if duplicate_of is None and person:
            people[person] = member["id"]
        marks.append({"id": member["id"], "counted": duplicate_of is None, "duplicate_of": duplicate_of})
    return marks


def _runs(ws: list[str], k: int = 5) -> set[tuple[str, ...]]:
    return {tuple(ws[i:i + k]) for i in range(max(1, len(ws) - k + 1))}


def group_claims(product: str, claims: list[str]) -> list[tuple[str, list[int]]]:
    """Group distinct claim sentences. Returns (label, indexes into claims) pairs covering every claim."""
    if len(claims) <= 1:
        return [(claims[0], [0])] if claims else []
    groups: list[tuple[str, list[int]]] = []
    for start in range(0, len(claims), CHUNK):
        for label, idx in _ask_groups(product, claims[start:start + CHUNK]):
            groups.append((label, [start + i for i in idx]))
    if len(claims) > CHUNK:
        # Groups from different chunks may be the same problem: group their labels once more.
        merged = _ask_groups(product, [label for label, _ in groups])
        groups = [(label, sorted(i for g in idx for i in groups[g][1])) for label, idx in merged]
    return groups


def _ask_groups(product: str, claims: list[str]) -> list[tuple[str, list[int]]]:
    listing = "\n".join(f"{n}. {claim}" for n, claim in enumerate(claims, 1))
    reply = llm.ask_json(GROUP_SYSTEM, f"Product: {product}\n\nClaims:\n{listing}", max_tokens=8000)
    seen: set[int] = set()
    groups = []
    for group in reply.get("groups", []):
        idx = []
        for n in group.get("claims", []):
            if isinstance(n, int) and 1 <= n <= len(claims) and n - 1 not in seen:
                idx.append(n - 1)
                seen.add(n - 1)
        if idx:
            groups.append((str(group.get("label") or claims[idx[0]]).strip(), idx))
    groups += [(claims[i], [i]) for i in range(len(claims)) if i not in seen]
    return groups


def count(conn: sqlite3.Connection, *, as_of: date | None = None, workers: int = 4,
          only: tuple[str, str] | None = None) -> tuple[date | None, int]:
    """Build claim groups from complaints received up to as_of (default: the latest complaint).

    only: a (source, product) pair, to recount just that product and leave every other group alone.
    Returns (as_of, number of groups built).
    """
    if as_of is None:
        latest = conn.execute("select max(received) from complaints").fetchone()[0]
        if latest is None:
            return None, 0
        as_of = date.fromisoformat(latest)
    rows = conn.execute(
        f"""select c.id, c.source, c.product, c.company, c.received, c.severe, c.text, k.claim, k.coded,
                   json_extract(c.fields, '$.person') as person
            from complaints c join claims k on k.complaint_id = c.id
            where k.claim is not null and c.received <= ? {'and c.source = ? and c.product = ?' if only else ''}
            order by c.source, c.product, c.received, c.id""",
        (as_of.isoformat(), *(only or ())),
    ).fetchall()
    by_product: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_product[(row["source"], row["product"])].append(row)
    since = (as_of - timedelta(days=90)).isoformat()

    def build(key: tuple[str, str]) -> list[dict]:
        source, product = key
        items = by_product[key]
        # Coded claims (OSHA, FAERS, CAERS) are grouped by their exact label, with no model call.
        unique = sorted({row["claim"].strip().lower() for row in items if not row["coded"]})
        label_of = {}
        for label, idx in group_claims(product, unique):
            for i in idx:
                label_of[unique[i]] = label
        members_by_label: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in items:
            label = row["claim"].strip() if row["coded"] else label_of[row["claim"].strip().lower()]
            members_by_label[label].append(row)
        groups = []
        for label, members in members_by_label.items():
            marks = distinct([{"id": m["id"], "text": m["text"], "person": m["person"]} for m in members])
            counted = {m["id"] for m in marks if m["counted"]}
            groups.append({
                "source": source,
                "product": product,
                "company": Counter(m["company"] for m in members).most_common(1)[0][0],
                "label": label,
                "total": len(counted),
                "last_90": sum(1 for m in members if m["id"] in counted and m["received"] >= since),
                "severe": sum(1 for m in members if m["id"] in counted and m["severe"]),
                "members": json.dumps(marks),
            })
        return groups

    with ThreadPoolExecutor(workers) as pool:
        built = [g for groups in pool.map(build, list(by_product)) for g in groups]
    if only:
        conn.execute("delete from claim_groups where as_of = ? and source = ? and product = ?", (as_of.isoformat(), *only))
    else:
        conn.execute("delete from claim_groups where as_of = ?", (as_of.isoformat(),))
    conn.executemany(
        "insert into claim_groups (as_of, source, product, company, label, total, last_90, severe, members)"
        " values (:as_of, :source, :product, :company, :label, :total, :last_90, :severe, :members)",
        ({**g, "as_of": as_of.isoformat()} for g in built),
    )
    conn.commit()
    return as_of, len(built)


def top(conn: sqlite3.Connection, n: int, *, as_of: date | None = None, source: str | None = None,
        product: str | None = None, skip_reported: bool = False) -> list[sqlite3.Row]:
    """The n biggest claim groups (most distinct people), from the latest count unless as_of is given."""
    when = as_of.isoformat() if as_of else conn.execute("select max(as_of) from claim_groups").fetchone()[0]
    if when is None:
        return []
    where, params = ["as_of = ?"], [when]
    if source:
        where.append("source = ?")
        params.append(source)
    if product:
        where.append("product = ?")
        params.append(product)
    if skip_reported:  # skip groups already investigated or already judged by the reporter
        where.append("not exists (select 1 from stories s where s.product = g.product and s.label = g.label)")
        where.append("not exists (select 1 from reviewed r where r.source = g.source and r.product = g.product"
                     " and r.label = g.label)")
    return conn.execute(
        f"select * from claim_groups g where {' and '.join(where)} order by total desc, last_90 desc, id limit ?",
        (*params, n),
    ).fetchall()


def samples(conn: sqlite3.Connection, group: sqlite3.Row, k: int = 3) -> list[str]:
    """Texts of the first k counted complaints in a group."""
    ids = [m["id"] for m in json.loads(group["members"]) if m["counted"]][:k]
    if not ids:
        return []
    rows = conn.execute(f"select id, text from complaints where id in ({','.join('?' * len(ids))})", ids).fetchall()
    text_of = {row["id"]: row["text"] for row in rows}
    return [text_of[i] for i in ids if i in text_of]
