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
            None,  # embedding — reserved
        ))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO chunks "
            "(chunk_id, doc_id, ordinal, text, locator, token_estimate, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def update_summary(con: duckdb.DuckDBPyConnection, doc_id: str, summary: str) -> None:
    con.execute("UPDATE documents SET summary = ? WHERE doc_id = ?", [summary, doc_id])


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
