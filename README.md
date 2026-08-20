# Document Librarian

> Ingest a pile of mixed documents — PDFs, Word, Excel, CSV/TSV, JSON, HTML, Markdown, images — into one searchable catalog, then ask it questions and get back ranked, source-cited snippets.

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/Drzymek92/document-librarian/actions/workflows/ci.yml/badge.svg)

## Overview

Document Librarian turns a heterogeneous folder of files (local or Google Drive) into a single
**DuckDB** catalog with full-text search. Each file is extracted into text chunks tagged with a
precise locator (page, sheet, row range, heading), enriched with metadata, and indexed for
**hybrid retrieval** — BM25 keyword search and dense-vector semantic search, fused with Reciprocal
Rank Fusion. Instead of re-reading raw files, any downstream tool or person can ask
*"what do I know about X?"* and get back the most relevant passages with their source and location.

It is built around a **cost-tiered effort model**: the economical tier spends zero LLM tokens
beyond unavoidable image OCR, while the high tier adds vision OCR for scanned pages plus
LLM-generated summaries and topical tags — so you trade cost for richness explicitly.

## Features

- **Eight file types, each handled on its own terms:**
  - **PDF** — per-page text; scanned/image-only pages fall back to vision OCR (high tier).
  - **DOCX** — paragraphs *and* table cells, walked in document order.
  - **XLSX** — per-sheet rows, header repeated per chunk, sheet-named locators.
  - **CSV / TSV** — delimiter and encoding auto-sniffed (`,`/`;`/tab, UTF-8→CP1252 fallback) so locale exports don't collapse into one column.
  - **JSON / JSONL** — nested records flattened to searchable `a.b[0].c: value` lines.
  - **HTML** — scripts/styles stripped, `<title>` captured.
  - **Markdown** — split by headings, with each heading used as the chunk locator.
  - **Images** — transcribed and described by a vision model.
- **Hybrid retrieval with a mode selector** — `bm25` (lexical), `vector` (dense embeddings,
  exact cosine), `hybrid` (both, fused by Reciprocal Rank Fusion at k=60), or `auto`, which
  picks hybrid when the catalog has embeddings and BM25 when it does not. Filters by file type,
  tag, and source apply to every mode.
- **Idempotent ingestion** — documents are keyed by a hash of identity + modified time, so
  re-scanning a large folder skips unchanged files.
- **Local *and* Google Drive sources** — native Google files are exported (Doc→docx, Sheet→xlsx,
  Slides→pdf) before extraction. *(Optional; see Google Drive support below.)*
- **Two effort tiers** trading token cost for enrichment depth.
- **59 tests** covering every extractor, the ingest pipeline, and each retrieval mode.

## Demo

### How it works

```mermaid
flowchart TD
    A([Start]) --> B{Source?}
    B -- Local folder --> C[Walk folder for supported files]
    B -- Google Drive --> D[Authenticate, list folder recursively]
    D --> E{Changed since last scan?}
    E -- No --> F[Skip: unchanged]
    E -- Yes --> G{Supported type?}
    G -- No --> H[Skip: log unsupported]
    G -- Native Google --> I[Export: Doc to docx, Sheet to xlsx, Slides to pdf]
    G -- Binary --> J[Download as-is]
    C --> K[For each file]
    I --> K
    J --> K
    K --> L[Extract text into located chunks]
    L --> M{Effort tier?}
    M -- low --> N[Cheap keyword tags, no LLM]
    M -- high --> O[LLM summary and tags]
    N --> P[Upsert document and chunks]
    O --> P
    P --> Q[Rebuild full-text index]
    Q --> R([Catalog ready])
    R --> S{Retrieval mode}
    S -- bm25 --> U[BM25 keyword ranking]
    S -- vector --> V[Cosine over chunk embeddings]
    S -- hybrid --> U
    S -- hybrid --> V
    U --> W[Reciprocal Rank Fusion k=60]
    V --> W
    W --> T([Ranked results: file, locator, snippet])
```

### Sample run

```console
$ python -m scripts.ingest local ./docs --effort low
INFO | Ingested quarterly_report.pdf (8 chunks)
INFO | Ingested feedback_export.json (12 chunks)
INFO | Ingested locale_matrix.tsv (3 chunks)
INFO | Ingest complete: 14 documents, 96 chunks

$ python -m scripts.query "locale specific issues" --type xlsx --limit 3
1. [7.41] locale_matrix.xlsx (xlsx, sheet 'EU')
   uri: /docs/locale_matrix.xlsx
   region,locale,issue ... fr-CA,date format mismatch in export ...
2. [5.12] feedback_export.json (json, records 3-5)
   uri: /docs/feedback_export.json
   locale: es-CO  note.text: currency symbol rendered incorrectly ...
```

## Architecture / How it works

The pipeline is four modular stages, each independently testable:

1. **Extract** (`extractors.py`) — dispatch by file type to a tuned extractor that produces
   normalized chunks, each with a human-readable `locator`.
2. **Load** (`db.py`, `schema.sql`) — upsert documents and chunks into DuckDB; a stable
   `doc_id = hash(path + modified_time)` makes re-ingestion idempotent.
3. **Enrich** (`enrich.py`, `config.py`) — per effort tier, attach cheap keyword tags or
   LLM-generated summaries + tags.
4. **Index & Query** (`db.py`, `query.py`, `embed.py`, `vector_store.py`) — rebuild the BM25
   full-text index, optionally embed chunks into vectors, then serve ranked results filtered by
   type/tag/source through the selected retrieval mode.

The catalog schema separates `documents`, `chunks`, `chunk_embeddings`, `catalog_meta`,
`doc_metadata`, and `tags`.

**Why fuse on rank rather than on score.** A BM25 score and a cosine similarity live on different,
unbounded, query-dependent scales, so blending them into one number needs a fudge factor that has
to be re-tuned per corpus. RRF scores each chunk `1/(k + rank)` in every list it appears in and
sums, so a chunk both retrievers ranked well beats one that only a single retriever loved. `k=60`
is the value from the original RRF paper.

**Why vectors live in their own table.** An in-place `UPDATE chunks SET embedding = ?` on a
variable-length `FLOAT[]` column crashes DuckDB natively on catalogs whose row groups were written
by an older build. Inserting into a separate `chunk_embeddings` table never rewrites those row
groups, so it sidesteps the crash. Catalogs created before this change are migrated automatically
on open.

**Why the vector store sits behind an interface.** `vector_store.py` defines a `VectorStore` ABC
with a DuckDB implementation — exact brute-force cosine, no service to run, exact by construction
and therefore the honest baseline for an approximate store to be measured against. Swapping in a
managed store means implementing five methods; nothing above the interface changes.

## Tech Stack

- **Language:** Python 3.12
- **Catalog & search:** DuckDB 1.5 (single-file database, native FTS / BM25, and exact cosine
  over stored embeddings)
- **Extraction:** PyMuPDF (PDF), python-docx (Word), openpyxl (Excel), pandas (CSV/TSV),
  BeautifulSoup4 (HTML), Pillow (images)
- **LLM integration:** langchain-openai against any **OpenAI-compatible** gateway (vision OCR +
  enrichment), configured via environment variables
- **Optional Google Drive source:** R's `googledrive`/`googlesheets4` bridged through `rpy2`
- **Retrieval:** BM25 + dense embeddings fused with Reciprocal Rank Fusion (k=60), behind a
  swappable `VectorStore` interface
- **Testing:** pytest (59 tests), reportlab for synthetic PDF fixtures; retrieval tests run
  offline against a deterministic stand-in embedder, so no API key is needed to run the suite

## Getting Started

### Prerequisites

- Python 3.12 on PATH (from [python.org](https://www.python.org/) — tick "Add to PATH" on Windows)

### Installation

```bash
git clone https://github.com/Drzymek92/document-librarian.git
cd document-librarian
python -m venv .venv
.venv\Scripts\activate        # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
cp config/.env.example config/.env   # then fill in your LLM gateway values
```

LLM-backed features (image OCR, the `high` effort tier, and embeddings) need an OpenAI-compatible
endpoint — set `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` in `config/.env`. The default `low`
tier works without any LLM for all text-based formats, and BM25 search needs no LLM at all.

### Usage

```bash
# Ingest a local folder (economical tier)
python -m scripts.ingest local ./docs --effort low

# Query the catalog — `auto` uses hybrid if the catalog has vectors, else BM25
python -m scripts.query "client feedback" --type pdf --tag transcription --limit 5

# Embed the catalog's chunks to unlock vector and hybrid search
python -m scripts.embed_backfill --db catalog/librarian.duckdb

# Pick a retrieval mode explicitly
python -m scripts.query "why did the export fail" --mode hybrid
python -m scripts.query "ERR_LOCALE_MISMATCH" --mode bm25

# Run the test suite (no API key required)
pytest tests/
```

Ingest flags: `--effort {low,high}`, `--no-enrich`, `--db <path>`.
Query flags: `--mode {auto,bm25,vector,hybrid}`, `--type` (repeatable), `--tag` (repeatable),
`--source {local,gdrive}`, `--limit`.
Backfill flags: `--db <path>`, `--model <name>`, `--doc-id <id>`.

Embedding is a separate, resumable pass rather than part of ingest: cost stays opt-in, and a run
that dies halfway keeps the vectors it already wrote. The first backfill **locks the catalog to one
embedding model** — a 1536-d vector and a 3072-d vector cannot be compared, so a later `--model`
is ignored (with a warning) until the catalog is re-embedded.

### Google Drive support (optional)

Google Drive ingestion is an optional extra that requires [R](https://www.r-project.org/) plus the
`googledrive`, `googlesheets4`, `readr`, `dplyr`, and `gargle` R packages, bridged via `rpy2`:

```bash
pip install -r requirements-gdrive.txt
python -m scripts.ingest gdrive <FOLDER_ID> --effort low
```

The first run opens a browser for one-time authentication; the token is cached and reused after.

## Project Structure

```
document-librarian/
├── scripts/
│   ├── ingest.py          # CLI entry: walk source → extract → enrich → load
│   ├── query.py           # CLI entry: hybrid/bm25/vector/auto search with filters
│   ├── embed.py           # dense embeddings via any OpenAI-compatible endpoint
│   ├── embed_backfill.py  # CLI entry: embed chunks that have no vector yet
│   ├── vector_store.py    # VectorStore interface + DuckDB cosine implementation
│   ├── extractors.py      # per-file-type extraction into located chunks
│   ├── db.py / schema.sql # DuckDB catalog + FTS index
│   ├── enrich.py          # summaries/tags per effort tier
│   ├── config.py          # effort tiers → model + enrichment selection
│   ├── llm_client.py       # OpenAI-compatible LLM access
│   ├── gdrive_*.py        # optional Google Drive listing/export/auth
│   └── manifest.py        # build a low-token descriptive index of a catalog
├── tests/                 # 41 pytest tests + fixtures
├── config/.env.example
├── requirements.txt
└── .github/workflows/ci.yml
```

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE).
