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
    token_estimate  INTEGER
);

-- Per-chunk embedding vectors, deliberately in their OWN table rather than a
-- FLOAT[] column on `chunks`. An in-place `UPDATE chunks SET embedding = ?` of a
-- variable-length FLOAT[] crashes DuckDB natively (STATUS_STACK_BUFFER_OVERRUN /
-- 0xC0000409) on catalogs whose rowgroups were written by an older DuckDB build.
-- Writing vectors as INSERTs into a fresh table never rewrites those rowgroups in
-- place, so it sidesteps the crash on every catalog.
-- One row per embedded chunk; absence of a row means "not yet embedded".
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id   VARCHAR PRIMARY KEY,
    doc_id     VARCHAR NOT NULL,      -- denormalized for cheap per-doc cleanup
    embedding  FLOAT[] NOT NULL
);

-- Catalog-level settings (one row per key). Used to lock ONE embedding model per
-- catalog so chunk vectors stay dimensionally consistent: a 1536-d vector from a
-- "small" model and a 3072-d vector from a "large" one cannot be cosine-compared.
-- Set on the first embed, read back at query time.
CREATE TABLE IF NOT EXISTS catalog_meta (
    key    VARCHAR PRIMARY KEY,
    value  VARCHAR
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
CREATE INDEX IF NOT EXISTS idx_cemb_doc   ON chunk_embeddings(doc_id);
CREATE INDEX IF NOT EXISTS idx_meta_doc   ON doc_metadata(doc_id);
CREATE INDEX IF NOT EXISTS idx_tags_doc   ON tags(doc_id);
