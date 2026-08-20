"""The vector-store seam.

Retrieval talks to a `VectorStore`, never to a specific database. The catalog ships
with `DuckDBVectorStore` — exact brute-force cosine over the `chunk_embeddings`
table, which needs no extra service and is fast enough at catalog scale — but the
interface is what the query layer depends on, so a managed store (Qdrant, pgvector)
can be dropped in by implementing these five methods and nothing above has to change.

Filters are passed as a `Filters` value object rather than as SQL, precisely so a
non-SQL backend can translate them into its own filter dialect.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

import duckdb

from scripts import db
from scripts.logger import get_logger

logger = get_logger("vector_store")


@dataclass(frozen=True)
class Filters:
    """Metadata restrictions applied alongside vector similarity."""

    types: list[str] | None = None
    tags: list[str] | None = None
    source: str | None = None


class VectorStore(ABC):
    """Storage and similarity search for per-chunk embeddings."""

    @abstractmethod
    def locked_model(self) -> str | None:
        """The embedding model this store's vectors were built with, or None if
        nothing has been embedded yet."""

    @abstractmethod
    def ensure_model(self, requested: str) -> str:
        """Lock the store to `requested` on first use; afterwards return the
        already-locked model so vector dimensionality stays consistent."""

    @abstractmethod
    def upsert(self, items: list[tuple[str, list[float]]]) -> int:
        """Store [(chunk_id, vector), ...]; returns how many were written."""

    @abstractmethod
    def missing_chunks(self, doc_id: str | None = None) -> list[tuple[str, str]]:
        """[(chunk_id, text)] for chunks with no vector yet — the backfill worklist."""

    @abstractmethod
    def search(
        self, query_vec: list[float], limit: int = 10, filters: Filters | None = None
    ) -> list[dict]:
        """Rank chunks by similarity to `query_vec`, most similar first."""


class DuckDBVectorStore(VectorStore):
    """Exact cosine search over the catalog's own `chunk_embeddings` table.

    No separate service and no index to build or keep warm: results are exact by
    construction, which also makes it the honest baseline to measure an approximate
    store against.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con

    def locked_model(self) -> str | None:
        return db.get_meta(self.con, "embedding_model")

    def ensure_model(self, requested: str) -> str:
        return db.ensure_embed_model(self.con, requested)

    def upsert(self, items: list[tuple[str, list[float]]]) -> int:
        return db.store_embeddings(self.con, items)

    def missing_chunks(self, doc_id: str | None = None) -> list[tuple[str, str]]:
        return db.chunks_without_embeddings(self.con, doc_id)

    def search(
        self, query_vec: list[float], limit: int = 10, filters: Filters | None = None
    ) -> list[dict]:
        f = filters or Filters()
        return db.vector_search(
            self.con, query_vec, limit=limit,
            types=f.types, tags=f.tags, source=f.source,
        )

    def count(self) -> int:
        return db.embedding_count(self.con)


def get_vector_store(con: duckdb.DuckDBPyConnection) -> VectorStore:
    """Build the configured vector store. Only the DuckDB backend ships today; the
    factory is the single place a managed backend gets wired in."""
    return DuckDBVectorStore(con)
