"""SQLite storage: complaints, claims, claim groups, stories, and the log of every decision."""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
create table if not exists complaints (
  id        text primary key,            -- "nhtsa:<ODINO>" or "maude:<mdr_report_key>"
  source    text not null,
  received  text not null,               -- YYYY-MM-DD: when the agency received it
  product   text not null,
  company   text,
  severe    integer not null default 0,  -- injury, fire or death reported
  text      text not null default '',
  fields    text not null default '{}'   -- a few structured fields; no personal data
);
create index if not exists complaints_product on complaints(product);

create table if not exists claims (
  complaint_id text primary key references complaints(id),
  claim        text,                     -- null: the complaint describes no clear problem
  coded        integer not null default 0  -- 1: taken from the source's own codes, no model involved
);

create table if not exists investigations (   -- NHTSA defect investigations: reference data for scouts
  action    text primary key,
  company   text,
  component text,
  opened    text,
  closed    text,
  recall    text,
  subject   text,
  summary   text
);
create table if not exists investigation_vehicles (
  action text not null,
  make   text not null,
  model  text not null,
  year   text not null,
  primary key (action, make, model, year)
);

create table if not exists claim_groups (
  id       integer primary key,
  as_of    text not null,
  source   text not null,
  product  text not null,
  company  text,
  label    text not null,
  total    integer not null,              -- distinct people
  last_90  integer not null,
  severe   integer not null,
  members  text not null                  -- json: [{"id", "counted", "duplicate_of"}]
);
create index if not exists claim_groups_as_of on claim_groups(as_of);

create table if not exists stories (
  id       integer primary key,
  created  text not null,
  source   text not null,
  product  text not null,
  company  text,
  label    text not null,
  counts   text not null,                 -- json: total, last_90, severe, as_of, group_id
  status   text not null,                 -- reporting | killed | parked | published | failed
  angle    text,
  note     text,
  article  text                           -- path of the published article
);

create table if not exists hypotheses (
  id        integer primary key,
  story_id  integer not null references stories(id),
  n         integer not null,
  statement text not null
);

create table if not exists findings (
  id            integer primary key,
  hypothesis_id integer not null references hypotheses(id),
  url           text not null,
  title         text,
  source_type   text not null,
  quote         text not null,
  finding       text not null,            -- supports | contradicts | unclear
  note          text
);

create table if not exists events (
  id       integer primary key,
  at       text not null,
  story_id integer,
  actor    text not null,
  action   text not null,
  reason   text not null,
  detail   text
);

create table if not exists seen_posts (id text primary key);  -- Bluesky posts already read, stored or not

create table if not exists scanned (      -- products the hunt has finished with, and how big they were then
  source     text not null,
  product    text not null,
  complaints integer not null,
  at         text not null,
  primary key (source, product)
);

create table if not exists reviewed (     -- claim groups the reporter has judged, so none is judged twice
  source  text not null,
  product text not null,
  label   text not null,
  outcome text not null,                  -- not_worth | already_reported | picked
  reason  text,
  at      text not null,
  primary key (source, product, label)
);
create table if not exists judged (       -- candidates a Bossman beat already judged, so passes don't re-judge them
  beat    text not null,
  id      text not null,
  at      text not null,
  outcome text not null,                  -- kept | skipped
  primary key (beat, id)
);
create table if not exists llm_cache (key text primary key, response text not null);
create table if not exists llm_usage (at text not null, model text not null, prompt_tokens integer, completion_tokens integer);
create table if not exists pages (url text primary key, fetched text not null, text text not null);
"""

_ready: set[Path] = set()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    path = config.load().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path.resolve() not in _ready:
        conn.execute("pragma journal_mode=wal")
        conn.executescript(SCHEMA)
        # Databases created before claims had a "coded" column get it added.
        if "coded" not in {row["name"] for row in conn.execute("pragma table_info(claims)")}:
            conn.execute("alter table claims add column coded integer not null default 0")
            conn.commit()
        _ready.add(path.resolve())
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_complaints(conn: sqlite3.Connection, complaints: Iterable[dict]) -> int:
    """Insert complaints, skipping ones already stored. Returns how many were new.

    A complaint may arrive with its claim already known ("claim", plus "coded": False if a model wrote it);
    those claims are stored too, so the claims step skips them.
    """
    rows = [{**c, "severe": int(bool(c["severe"])), "fields": json.dumps(c.get("fields", {}))} for c in complaints]
    new = conn.executemany(
        "insert or ignore into complaints (id, source, received, product, company, severe, text, fields)"
        " values (:id, :source, :received, :product, :company, :severe, :text, :fields)",
        rows,
    ).rowcount
    known = [(r["id"], r["claim"], int(r.get("coded", True))) for r in rows if r.get("claim")]
    if known:
        conn.executemany("insert or ignore into claims (complaint_id, claim, coded) values (?, ?, ?)", known)
    conn.commit()
    return new


def event(conn: sqlite3.Connection, actor: str, action: str, reason: str, *,
          story_id: int | None = None, detail: Any = None) -> None:
    conn.execute(
        "insert into events (at, story_id, actor, action, reason, detail) values (?, ?, ?, ?, ?, ?)",
        (now(), story_id, actor, action, reason, json.dumps(detail) if detail is not None else None),
    )
    conn.commit()
