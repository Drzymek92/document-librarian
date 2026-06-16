-- Librarian catalog — standardized schema (DuckDB).
-- doc_id is a stable hash of (absolute path + modified time): re-ingesting an
-- unchanged file is a no-op, a changed file replaces its rows.

CREATE TABLE IF NOT EXISTS documents (
    doc_id        VARCHAR PRIMARY KEY,
    path          VARCHAR NOT NULL,
    filename      VARCHAR NOT NULL,
    ext           VARCHAR,
    source_type   VARCHAR,            -- pdf | docx | xlsx | csv | image
    size_bytes    BIGINT,
    created_at    TIMESTAMP,
    modified_at   TIMESTAMP,
    ingested_at   TIMESTAMP,
    title         VARCHAR,
    author        VARCHAR,
    page_count    INTEGER,
    summary       VARCHAR,            -- populated by enrichment (high effort)
    effort        VARCHAR,            -- effort tier used at ingest
    status        VARCHAR DEFAULT 'active',
    source          VARCHAR DEFAULT 'local',  -- local | gdrive
    source_id       VARCHAR,          -- gdrive file id (NULL for local)
    source_uri      VARCHAR,          -- web link (gdrive) or absolute path (local)
    source_modified VARCHAR,          -- modifiedTime / mtime used for idempotent re-scan
    mime_type       VARCHAR
);

-- Retrievable text units. One document fans out to many chunks.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        VARCHAR PRIMARY KEY,
    doc_id          VARCHAR NOT NULL,
    ordinal         INTEGER NOT NULL,
    text            VARCHAR NOT NULL,
    locator         VARCHAR,          -- "page 3", "sheet 'Q1'", "rows 1-50"
    token_estimate  INTEGER,
    embedding       FLOAT[]           -- nullable, reserved for future semantic search
);

-- Flexible key/value for anything the rigid columns can't hold.
CREATE TABLE IF NOT EXISTS doc_metadata (
    doc_id  VARCHAR NOT NULL,
    key     VARCHAR NOT NULL,
    value   VARCHAR
);

-- Normalized labels for filtering.
CREATE TABLE IF NOT EXISTS tags (
    doc_id  VARCHAR NOT NULL,
    tag     VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_meta_doc   ON doc_metadata(doc_id);
CREATE INDEX IF NOT EXISTS idx_tags_doc   ON tags(doc_id);
