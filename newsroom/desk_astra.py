"""The reporter's desk on Astra DB: the same contract as InMemoryDesk, with real vector search.

Same database and the same built-in NVIDIA embedding model as the signals store (newsroom/signals_astra.py);
a separate collection, `reporter_desk` by default (NEWSROOM_DESK_COLLECTION). Only the fields the desk filters
or sorts on are indexed, so long text (quotes, memos) never hits Astra's indexed-string limits.
"""

from __future__ import annotations

import os

from astrapy.info import CollectionDefinition, CollectionVectorOptions, VectorServiceOptions
from astrapy.constants import VectorMetric

from .desk import text_of
from .signals import now
from .signals_astra import EMBED_CHARS, EMBEDDING_DIMENSION, EMBEDDING_MODEL, EMBEDDING_PROVIDER, _at, connect

INDEXED = ["hypothesis_id", "status", "updated_at_ts"]


def definition() -> CollectionDefinition:
    return CollectionDefinition(
        vector=CollectionVectorOptions(
            dimension=EMBEDDING_DIMENSION, metric=VectorMetric.COSINE,
            service=VectorServiceOptions(provider=EMBEDDING_PROVIDER, model_name=EMBEDDING_MODEL)),
        indexing={"allow": INDEXED},
    )


class AstraDesk:
    def __init__(self, collection_name: str | None = None) -> None:
        self.name = collection_name or os.getenv("NEWSROOM_DESK_COLLECTION", "reporter_desk")
        self.database = connect()
        if self.name in self.database.list_collection_names():
            self.collection = self.database.get_collection(self.name)
        else:
            self.collection = self.database.create_collection(self.name, definition=definition())

    def save(self, record: dict) -> str:
        record = dict(record)
        record["_id"] = record.get("_id") or f"inv-{os.urandom(6).hex()}"
        record.setdefault("created_at", now())
        record["updated_at"] = now()
        doc = {k: v for k, v in record.items() if not k.startswith("$")}
        doc["updated_at_ts"] = _at(record["updated_at"])
        doc["$vectorize"] = text_of(record)[:EMBED_CHARS] or record["_id"]
        self.collection.find_one_and_replace({"_id": record["_id"]}, doc, upsert=True)
        return record["_id"]

    @staticmethod
    def _clean(doc: dict) -> dict:
        return {k: v for k, v in doc.items() if k not in ("updated_at_ts", "$vectorize", "$vector", "$similarity")}

    def get(self, record_id: str) -> dict | None:
        doc = self.collection.find_one({"_id": record_id})
        return self._clean(doc) if doc else None

    def for_hypothesis(self, hypothesis_id: str) -> list[dict]:
        return [self._clean(d) for d in self.collection.find({"hypothesis_id": hypothesis_id}, sort={"updated_at_ts": -1})]

    def recent(self, *, limit: int = 50, status: str | None = None) -> list[dict]:
        query = {"status": status} if status else {}
        return [self._clean(d) for d in self.collection.find(query, sort={"updated_at_ts": -1}, limit=limit)]

    def similar(self, text: str, *, k: int = 5) -> list[tuple[dict, float]]:
        hits = self.collection.find({}, sort={"$vectorize": text[:EMBED_CHARS]}, limit=k, include_similarity=True)
        return [(self._clean(d), d["$similarity"]) for d in hits]
