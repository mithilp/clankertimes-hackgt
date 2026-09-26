"""The signals store on DataStax Astra DB: the same contract as InMemorySignals, with real vector search.

Embeddings: NVIDIA nv-embedqa-e5-v5, 1024 dimensions, cosine, computed by Astra itself (vectorize). It's
hosted by Astra, so it needs no API key and costs nothing beyond the database, but only databases in AWS
us-east-2 or Google Cloud us-east1 have it. It reads at most 512 tokens, so embedded text is capped.
Changing the model or dimension means re-embedding everything: EMBEDDING_* below is the only place they live.

Vector scores only find candidates. Whether a new signal duplicates one of them is decided by the same word
overlap InMemorySignals uses, because embeddings put different claims about the same party close together.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import fields
from datetime import datetime

from astrapy import DataAPIClient
from astrapy.constants import VectorMetric
from astrapy.info import CollectionDefinition, CollectionVectorOptions, VectorServiceOptions

from .signals import MERGE_SIMILARITY, Signal, _overlap, now, origin_key

EMBEDDING_PROVIDER = "nvidia"
EMBEDDING_MODEL = "nvidia/nv-embedqa-e5-v5"
EMBEDDING_DIMENSION = 1024
EMBED_CHARS = 1500           # comfortably under the model's 512-token limit
NEAR_CANDIDATES = 5          # closest same-party signals checked for a near-duplicate

SIGNAL_FIELDS = [f.name for f in fields(Signal) if f.name != "id"]
# Only what the store filters or sorts on. `_id` and the vector are always indexed; long text is not.
INDEXED = ["last_seen_at", "status", "origin_key", "party_key"]


def connect():
    """The Astra database named in .env, by ASTRA_DB_API_ENDPOINT or by ASTRA_DB_ID (+ ASTRA_DB_REGION)."""
    token = os.getenv("ASTRA_DB_APPLICATION_TOKEN", "").strip()
    if not token.startswith("AstraCS:"):
        raise RuntimeError("ASTRA_DB_APPLICATION_TOKEN is missing or doesn't start with AstraCS: (see .env)")
    client = DataAPIClient(token)
    keyspace = os.getenv("ASTRA_DB_KEYSPACE") or None
    if endpoint := os.getenv("ASTRA_DB_API_ENDPOINT", "").strip():
        return client.get_database(endpoint, keyspace=keyspace)
    db_id = os.getenv("ASTRA_DB_ID", "").strip()
    if not db_id:
        raise RuntimeError("Set ASTRA_DB_ID (or ASTRA_DB_API_ENDPOINT) in .env")
    admin = client.get_admin()
    if region := os.getenv("ASTRA_DB_REGION", "").strip():
        return admin.get_database(id=db_id, region=region, keyspace=keyspace)
    # No region given: ask Astra where the database lives.
    return client.get_database(admin.database_info(db_id).regions[0].api_endpoint, keyspace=keyspace)


def definition() -> CollectionDefinition:
    return CollectionDefinition(
        vector=CollectionVectorOptions(
            dimension=EMBEDDING_DIMENSION,
            metric=VectorMetric.COSINE,
            service=VectorServiceOptions(provider=EMBEDDING_PROVIDER, model_name=EMBEDDING_MODEL),
        ),
        indexing={"allow": INDEXED},
    )


def _at(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp)


class AstraSignals:
    def __init__(self, collection_name: str | None = None) -> None:
        self.name = collection_name or os.getenv("NEWSROOM_SIGNALS_COLLECTION", "signals")
        self.database = connect()
        if self.name in self.database.list_collection_names():
            self.collection = self.database.get_collection(self.name)
        else:
            self.collection = self.database.create_collection(self.name, definition=definition())

    @staticmethod
    def _document(signal: Signal) -> dict:
        doc = {name: getattr(signal, name) for name in SIGNAL_FIELDS}
        return {**doc, "_id": signal.id, "origin_key": origin_key(signal.origin),
                "party_key": signal.accountable_party.strip().lower(),
                "last_seen_at": _at(signal.last_seen), "$vectorize": signal.text()[:EMBED_CHARS]}

    @staticmethod
    def _signal(doc: dict) -> Signal:
        return Signal(id=doc["_id"], **{name: doc[name] for name in SIGNAL_FIELDS if name in doc})

    def _find_duplicate(self, signal: Signal) -> Signal | None:
        if doc := self.collection.find_one({"origin_key": origin_key(signal.origin)}):
            return self._signal(doc)
        candidates = self.collection.find({"party_key": signal.accountable_party.strip().lower()},
                                          sort={"$vectorize": signal.text()[:EMBED_CHARS]}, limit=NEAR_CANDIDATES)
        for doc in candidates:
            existing = self._signal(doc)
            if _overlap(existing.text(), signal.text()) >= MERGE_SIMILARITY:
                return existing
        return None

    def add(self, signal: Signal) -> tuple[str, bool]:
        seen = signal.last_seen or now()
        if existing := self._find_duplicate(signal):
            known = {s.get("url") for s in existing.sources}
            last_seen = max(existing.last_seen, seen)
            update = {"sources": existing.sources + [s for s in signal.sources if s.get("url") not in known],
                      "source_types": sorted(set(existing.source_types) | set(signal.source_types)),
                      "beats": sorted(set(existing.beats) | set(signal.beats)),
                      "last_seen": last_seen, "last_seen_at": _at(last_seen)}
            if signal.spike:
                update["spike"] = signal.spike           # the newest evidence it is moving
            self.collection.update_one({"_id": existing.id}, {"$set": update})
            return existing.id, False
        signal.id = signal.id or uuid.uuid4().hex
        signal.first_seen = signal.first_seen or seen
        signal.last_seen = seen
        self.collection.insert_one(self._document(signal))
        return signal.id, True

    def get(self, signal_id: str) -> Signal | None:
        doc = self.collection.find_one({"_id": signal_id})
        return self._signal(doc) if doc else None

    def recent(self, since: str, *, limit: int = 200, status: str | None = None) -> list[Signal]:
        query: dict = {"last_seen_at": {"$gte": _at(since)}}
        if status is not None:
            query["status"] = status
        return [self._signal(d) for d in self.collection.find(query, sort={"last_seen_at": -1}, limit=limit)]

    def similar(self, text: str, *, k: int = 20, since: str | None = None) -> list[tuple[Signal, float]]:
        query = {"last_seen_at": {"$gte": _at(since)}} if since else {}
        hits = self.collection.find(query, sort={"$vectorize": text[:EMBED_CHARS]}, limit=k,
                                    include_similarity=True)
        return [(self._signal(d), d["$similarity"]) for d in hits]

    def _update(self, signal_id: str, changes: dict) -> None:
        if self.collection.update_one({"_id": signal_id}, {"$set": changes}).update_info["n"] == 0:
            raise KeyError(signal_id)

    def mark_used(self, signal_id: str, hypothesis_id: str) -> None:
        signal = self.get(signal_id)
        if signal is None:
            raise KeyError(signal_id)
        used_by = signal.used_by if hypothesis_id in signal.used_by else signal.used_by + [hypothesis_id]
        self._update(signal_id, {"status": "used", "used_by": used_by})

    def mark_ignored(self, signal_id: str, reason: str) -> None:
        self._update(signal_id, {"status": "ignored", "ignored_reason": reason})
