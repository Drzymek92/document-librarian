import hashlib
from pathlib import Path
from typing import Any

import duckdb

from scripts.logger import get_logger

logger = get_logger("librarian_db")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def make_doc_id(identifier: str, version: str) -> str:
    # identifier = absolute path (local) or Drive file id (gdrive);
    # version = mtime (local) or modifiedTime (gdrive). Changing either re-keys.
    raw = f"{identifier}|{version}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def doc_id_for(path: str, mtime: float) -> str:
    return make_doc_id(str(Path(path).resolve()), str(mtime))


def chunk_id_for(doc_id: str, ordinal: int) -> str:
    return f"{doc_id}-{ordinal:05d}"


def connect(db_path: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path)
    con.execute("INSTALL fts; LOAD fts;")
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    # DuckDB executes a multi-statement script in one call; don't split on ';'
    # (comments may contain semicolons and naive splitting corrupts statements).
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_schema(con)


def migrate_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Bring an older catalog up to the current schema.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so schema
    changes to tables that already exist have to be applied by hand here.

    Migration 1 — drop the unused `chunks.embedding` column. Early catalogs
    reserved it for semantic search but nothing ever wrote to it; vectors now live
    in `chunk_embeddings` (see schema.sql for why a separate table).
    """
    cols = {r[0] for r in con.execute("DESCRIBE chunks").fetchall()}
    if "embedding" not in cols:
        return

    # DuckDB refuses ALTER TABLE while anything depends on the table, and every
    # real catalog has both a doc_id index and an FTS index over chunks. Drop
    # them, alter, then put them back.
    try:
        con.execute("PRAGMA drop_fts_index('chunks')")
        had_fts = True
    except duckdb.Error:
        had_fts = False  # never indexed (fresh or chunk-less catalog)
    con.execute("DROP INDEX IF EXISTS idx_chunks_doc")
    con.execute("ALTER TABLE chunks DROP COLUMN embedding")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
    if had_fts:
        rebuild_fts_index(con)
    logger.info("Migrated catalog: dropped unused chunks.embedding column")


_DOC_COLS = [
    "doc_id", "path", "filename", "ext", "source_type", "size_bytes",
    "created_at", "modified_at", "ingested_at", "title", "author",
    "page_count", "summary", "effort", "status",
    "source", "source_id", "source_uri", "source_modified", "mime_type",
]


def upsert_document(con: duckdb.DuckDBPyConnection, doc: dict[str, Any]) -> None:
    values = [doc.get(c) for c in _DOC_COLS]
    placeholders = ", ".join("?" for _ in _DOC_COLS)
    cols = ", ".join(_DOC_COLS)
    con.execute(
        f"INSERT OR REPLACE INTO documents ({cols}) VALUES ({placeholders})",
        values,
    )


def replace_chunks(
    con: duckdb.DuckDBPyConnection, doc_id: str, chunks: list[dict[str, Any]]
) -> None:
    # Vectors are keyed by chunk_id, and chunk_ids are re-minted per ingest, so
    # stale vectors must go with their chunks or they would outlive the text.
    con.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", [doc_id])
    con.execute("DELETE FROM chunks WHERE doc_id = ?", [doc_id])
    rows = []
    for ch in chunks:
        ordinal = ch["ordinal"]
        text = ch["text"]
        rows.append((
            chunk_id_for(doc_id, ordinal),
            doc_id,
            ordinal,
            text,
            ch.get("locator"),
            ch.get("token_estimate", len(text) // 4),
        ))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO chunks "
            "(chunk_id, doc_id, ordinal, text, locator, token_estimate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def update_summary(con: duckdb.DuckDBPyConnection, doc_id: str, summary: str) -> None:
    con.execute("UPDATE documents SET summary = ? WHERE doc_id = ?", [summary, doc_id])


# --- Catalog settings -------------------------------------------------------


def get_meta(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT value FROM catalog_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def set_meta(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO catalog_meta (key, value) VALUES (?, ?)", [key, value]
    )


def ensure_embed_model(con: duckdb.DuckDBPyConnection, requested: str) -> str:
    """Return the embedding model this catalog is locked to.

    Locks to `requested` on first use; afterwards always returns the locked model
    (warning on a mismatch) so every vector in a catalog shares dimensionality and
    stays comparable. Switching models means re-embedding the whole catalog.
    """
    locked = get_meta(con, "embedding_model")
    if locked is None:
        set_meta(con, "embedding_model", requested)
        return requested
    if locked != requested:
        logger.warning(
            f"Catalog is locked to embedding model '{locked}'; ignoring requested "
            f"'{requested}' to keep vectors comparable. Re-embed all chunks to switch."
        )
    return locked


# --- Embeddings -------------------------------------------------------------


def store_embeddings(
    con: duckdb.DuckDBPyConnection, items: list[tuple[str, list[float]]]
) -> int:
    """Write vectors for `items` = [(chunk_id, vector), ...]; returns the count.

    Always an INSERT into chunk_embeddings, never an in-place UPDATE of a FLOAT[]
    column (see schema.sql). doc_id is looked up from chunks so callers don't have
    to carry it around.
    """
    if not items:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO chunk_embeddings (chunk_id, doc_id, embedding) "
        "SELECT c.chunk_id, c.doc_id, ?::FLOAT[] FROM chunks c WHERE c.chunk_id = ?",
        [(vec, cid) for cid, vec in items],
    )
    return len(items)


def chunks_without_embeddings(
    con: duckdb.DuckDBPyConnection, doc_id: str | None = None
) -> list[tuple[str, str]]:
    """Return [(chunk_id, text)] for chunks that have no vector yet — the work
    list for a backfill. A missing row in chunk_embeddings means "not embedded"."""
    sql = (
        "SELECT c.chunk_id, c.text FROM chunks c "
        "LEFT JOIN chunk_embeddings e USING (chunk_id) "
        "WHERE e.chunk_id IS NULL"
    )
    params: list = []
    if doc_id is not None:
        sql += " AND c.doc_id = ?"
        params.append(doc_id)
    sql += " ORDER BY c.doc_id, c.ordinal"
    return con.execute(sql, params).fetchall()


def embedding_count(con: duckdb.DuckDBPyConnection) -> int:
    return con.execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0]


def vector_search(
    con: duckdb.DuckDBPyConnection,
    query_vec: list[float],
    limit: int = 10,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
) -> list[dict]:
    """Brute-force cosine ranking over stored chunk vectors.

    Exact rather than approximate: at catalog scale a full scan is fast enough and
    costs no index build. The VectorStore interface exists so this can be swapped
    for an ANN-backed store without touching the query layer.
    """
    sql = """
        SELECT d.doc_id, d.filename, d.source_type, d.source_uri, d.summary,
               c.chunk_id, c.locator, c.text,
               list_cosine_similarity(e.embedding, ?::FLOAT[]) AS score
        FROM chunk_embeddings e
        JOIN chunks c USING (chunk_id)
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE 1 = 1
    """
    params: list = [query_vec]
    if types:
        sql += f" AND d.source_type IN ({','.join('?' for _ in types)})"
        params += types
    if source:
        sql += " AND d.source = ?"
        params.append(source)
    if tags:
        sql += (
            f" AND d.doc_id IN (SELECT doc_id FROM tags WHERE tag IN "
            f"({','.join('?' for _ in tags)}))"
        )
        params += tags
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    cols = ["doc_id", "filename", "source_type", "source_uri", "summary",
            "chunk_id", "locator", "text", "score"]
    return [dict(zip(cols, row)) for row in con.execute(sql, params).fetchall()]


def set_metadata(
    con: duckdb.DuckDBPyConnection, doc_id: str, metadata: dict[str, Any]
) -> None:
    con.execute("DELETE FROM doc_metadata WHERE doc_id = ?", [doc_id])
    rows = [(doc_id, k, None if v is None else str(v)) for k, v in metadata.items()]
    if rows:
        con.executemany(
            "INSERT INTO doc_metadata (doc_id, key, value) VALUES (?, ?, ?)", rows
        )


def set_tags(con: duckdb.DuckDBPyConnection, doc_id: str, tags: list[str]) -> None:
    con.execute("DELETE FROM tags WHERE doc_id = ?", [doc_id])
    rows = [(doc_id, t) for t in dict.fromkeys(tags)]  # dedupe, preserve order
    if rows:
        con.executemany("INSERT INTO tags (doc_id, tag) VALUES (?, ?)", rows)


def existing_versions(con: duckdb.DuckDBPyConnection, source: str) -> dict[str, str]:
    # Map source_id -> source_modified for a given source, to skip unchanged files.
    rows = con.execute(
        "SELECT source_id, source_modified FROM documents "
        "WHERE source = ? AND source_id IS NOT NULL",
        [source],
    ).fetchall()
    return {sid: mod for sid, mod in rows}


def rebuild_fts_index(con: duckdb.DuckDBPyConnection) -> None:
    # DuckDB FTS indexes are static snapshots — rebuild after any chunk change.
    con.execute(
        "PRAGMA create_fts_index('chunks', 'chunk_id', 'text', overwrite=1)"
    )


def search(
    con: duckdb.DuckDBPyConnection, query: str, limit: int = 10
) -> list[tuple]:
    return con.execute(
        """
        SELECT d.filename, c.locator, c.text, s.score
        FROM (
            SELECT chunk_id, fts_main_chunks.match_bm25(chunk_id, ?) AS score
            FROM chunks
        ) s
        JOIN chunks c USING (chunk_id)
        JOIN documents d USING (doc_id)
        WHERE s.score IS NOT NULL
        ORDER BY s.score DESC
        LIMIT ?
        """,
        [query, limit],
    ).fetchall()
