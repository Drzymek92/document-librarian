import pytest

from scripts import db, embed_backfill
from scripts.vector_store import DuckDBVectorStore, Filters, get_vector_store


def _catalog(tmp_path):
    con = db.connect(str(tmp_path / "v.duckdb"))
    db.init_schema(con)
    db.upsert_document(con, {"doc_id": "d1", "path": "/tmp/a.txt", "filename": "a.txt",
                             "source_type": "txt", "source": "local", "source_uri": "/tmp/a.txt"})
    db.upsert_document(con, {"doc_id": "d2", "path": "/tmp/b.pdf", "filename": "b.pdf",
                             "source_type": "pdf", "source": "local", "source_uri": "/tmp/b.pdf"})
    db.replace_chunks(con, "d1", [{"ordinal": 0, "text": "the quick brown fox", "locator": "line 1"}])
    db.replace_chunks(con, "d2", [{"ordinal": 0, "text": "invoice totals and payment terms", "locator": "page 1"}])
    return con


def test_migration_drops_legacy_embedding_column(tmp_path):
    """A catalog created before vectors moved out of `chunks` must lose the unused
    column rather than keep advertising it."""
    con = db.connect(str(tmp_path / "legacy.duckdb"))
    db.init_schema(con)
    db.upsert_document(con, {"doc_id": "d1", "path": "/tmp/a.txt", "filename": "a.txt",
                             "source_type": "txt", "source": "local", "source_uri": "/tmp/a.txt"})
    db.replace_chunks(con, "d1", [{"ordinal": 0, "text": "the quick brown fox", "locator": "line 1"}])
    # A real legacy catalog carries an FTS index and a doc_id index over `chunks`;
    # both block ALTER TABLE, so the migration has to clear and restore them.
    db.rebuild_fts_index(con)
    con.execute("ALTER TABLE chunks ADD COLUMN embedding FLOAT[]")
    assert "embedding" in {r[0] for r in con.execute("DESCRIBE chunks").fetchall()}

    db.migrate_schema(con)
    assert "embedding" not in {r[0] for r in con.execute("DESCRIBE chunks").fetchall()}
    # The rebuilt FTS index must still serve queries after the alter.
    assert db.search(con, "fox")


def test_model_lock_holds_first_choice(tmp_path):
    con = _catalog(tmp_path)
    store = get_vector_store(con)
    assert store.locked_model() is None
    assert store.ensure_model("text-embedding-3-small") == "text-embedding-3-small"
    # A later, different request must NOT switch the catalog: 1536-d and 3072-d
    # vectors cannot be compared, and mixing them corrupts ranking silently.
    assert store.ensure_model("text-embedding-3-large") == "text-embedding-3-small"
    assert store.locked_model() == "text-embedding-3-small"


def test_upsert_and_missing_chunks(tmp_path, toy_embedder):
    con = _catalog(tmp_path)
    store = get_vector_store(con)
    assert isinstance(store, DuckDBVectorStore)

    pending = store.missing_chunks()
    assert len(pending) == 2
    written = store.upsert([(cid, toy_embedder(text)) for cid, text in pending])
    assert written == 2
    assert store.missing_chunks() == []
    assert store.count() == 2


def test_search_ranks_by_similarity(tmp_path, toy_embedder):
    con = _catalog(tmp_path)
    embed_backfill.backfill(con, model="toy-model")
    store = get_vector_store(con)

    results = store.search(toy_embedder("fox"), limit=10)
    assert results[0]["filename"] == "a.txt"
    assert results[0]["score"] > results[-1]["score"]
    assert {"chunk_id", "doc_id", "locator", "text", "score"} <= set(results[0])


def test_search_honours_filters(tmp_path, toy_embedder):
    con = _catalog(tmp_path)
    embed_backfill.backfill(con, model="toy-model")
    store = get_vector_store(con)

    results = store.search(toy_embedder("fox"), limit=10, filters=Filters(types=["pdf"]))
    assert results
    assert all(r["source_type"] == "pdf" for r in results)


def test_backfill_is_resumable(tmp_path, toy_embedder):
    con = _catalog(tmp_path)
    assert embed_backfill.backfill(con, model="toy-model") == 2
    # Second run finds nothing left to do — the pass is idempotent, so a run that
    # died halfway can simply be re-issued.
    assert embed_backfill.backfill(con, model="toy-model") == 0


def test_reingest_drops_stale_vectors(tmp_path, toy_embedder):
    con = _catalog(tmp_path)
    embed_backfill.backfill(con, model="toy-model")
    assert db.embedding_count(con) == 2

    # Re-ingesting a document re-mints its chunk_ids; its old vectors must not
    # outlive the text they were built from.
    db.replace_chunks(con, "d1", [{"ordinal": 0, "text": "entirely new text", "locator": "line 1"}])
    assert db.embedding_count(con) == 1
    assert len(db.chunks_without_embeddings(con)) == 1
