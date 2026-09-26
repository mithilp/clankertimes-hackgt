"""Published articles, in the Astra DB `articles` collection: what the website (web/) reads.

Each article is stored as structure, not as rendered text: sentences with the ids of the sources they
cite, and each source with its URL and the quoted passage. The site renders that into footnotes that
show the evidence behind any sentence. article.publish() writes here whenever Astra is configured.

    python -m newsroom try articles                    list what is published
    python -m newsroom try articles --seed-samples     load web/data/sample-articles.json (marked sample)
    python -m newsroom try articles --remove-samples   delete every sample article
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from astrapy.constants import VectorMetric
from astrapy.info import CollectionDefinition, CollectionVectorOptions, VectorServiceOptions

from .signals_astra import EMBED_CHARS, EMBEDDING_DIMENSION, EMBEDDING_MODEL, EMBEDDING_PROVIDER, connect

SAMPLES = Path(__file__).resolve().parent.parent / "web" / "data" / "sample-articles.json"
# Fixed when the collection is created. The site filters on status and beats and sorts by published_ts.
INDEXED = ["slug", "status", "beats", "published_ts", "sample"]
KIND = {"record": "record", "data": "data", "news": "news", "gov": "record", "court": "record", "web": "web page"}
# The scouts' source types, as the site names them.
KIND.update({"government_record": "record", "court_record": "record", "news_report": "news",
             "company_statement": "company", "complaint": "complaint", "social": "post", "other": "web page"})


def _ts(iso: str) -> datetime:
    at = datetime.fromisoformat(iso)
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def to_document(slug: str, article: dict, sources: dict[str, dict], *, beats: list[str] | None = None,
                published_at: str | None = None, timeline: list[dict] | None = None) -> dict:
    """timeline: how the story came together, oldest first, as [{at, who, text, result}], where who is the
    agent ("Technology desk", "Scout 2", "Skeptic") and result is "", confirmed, unclear, revise, approved,
    published or corrected. Record the path that led to the story; leave out dead ends and cut material."""
    """The site's article shape, from the pipeline's (headline, paragraphs of {text, cite}, sources)."""
    return {
        "slug": slug, "status": "published", "sample": False, "beats": beats or [],
        "kicker": article.get("kicker", ""), "headline": article["headline"].strip(), "dek": article.get("dek", ""),
        "published_at": published_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paragraphs": [[{"text": s["text"].strip(), "cite": list(s["cite"])} for s in p] for p in article["paragraphs"]],
        "sources": {sid: {"kind": KIND.get(s.get("source_type", ""), s.get("source_type", "source")),
                          "title": s.get("title") or s.get("url", ""), "publisher": s.get("publisher", ""),
                          "url": s.get("url", ""), "accessed": s.get("accessed", ""),
                          "quote": (s.get("quote") or s.get("text") or "")[:600]}
                    for sid, s in sources.items()},
        "timeline": timeline or [], "corrections": [],
    }


class AstraArticles:
    def __init__(self, collection_name: str | None = None) -> None:
        self.name = collection_name or os.getenv("NEWSROOM_ARTICLES_COLLECTION", "articles")
        self.database = connect()
        if self.name in self.database.list_collection_names():
            self.collection = self.database.get_collection(self.name)
        else:
            self.collection = self.database.create_collection(self.name, definition=CollectionDefinition(
                vector=CollectionVectorOptions(
                    dimension=EMBEDDING_DIMENSION, metric=VectorMetric.COSINE,
                    service=VectorServiceOptions(provider=EMBEDDING_PROVIDER, model_name=EMBEDDING_MODEL)),
                indexing={"allow": INDEXED}))

    def save(self, doc: dict) -> None:
        """Insert or replace by slug. The vector (for related stories) is the headline and dek."""
        row = {**doc, "_id": doc["slug"], "published_ts": _ts(doc["published_at"]),
               "$vectorize": f"{doc['headline']}\n{doc.get('dek', '')}"[:EMBED_CHARS]}
        self.collection.find_one_and_replace({"_id": doc["slug"]}, row, upsert=True)

    def list(self) -> list[dict]:
        return list(self.collection.find({}, sort={"published_ts": -1}, limit=200,
                                         projection={"slug": True, "headline": True, "status": True,
                                                     "sample": True, "beats": True, "published_at": True}))

    def seed_samples(self) -> int:
        docs = json.loads(SAMPLES.read_text(encoding="utf-8"))
        for doc in docs:
            self.save({**doc, "sample": True})
        return len(docs)

    def remove_samples(self) -> int:
        return self.collection.delete_many({"sample": True}).deleted_count


def configured() -> bool:
    return bool(os.getenv("ASTRA_DB_ID") or os.getenv("ASTRA_DB_API_ENDPOINT"))
