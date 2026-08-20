"""Embed catalog chunks that have no vector yet.

Kept as a separate pass rather than folded into ingest so that embedding cost is
opt-in and resumable: a run that dies halfway leaves the vectors it already wrote,
and re-running picks up exactly the chunks still missing.
"""
import argparse
import sys

from scripts import config, db, embed
from scripts.logger import get_logger
from scripts.vector_store import get_vector_store

logger = get_logger("embed_backfill")


def backfill(con, model: str | None = None, doc_id: str | None = None,
             batch: int = 256) -> int:
    """Embed every chunk missing a vector. Returns how many were written."""
    store = get_vector_store(con)
    requested = model or config.get_profile().embedding_model
    # The catalog, not the caller, has the last word on the model: mixing
    # dimensionalities inside one catalog breaks cosine comparison silently.
    model = store.ensure_model(requested)

    pending = store.missing_chunks(doc_id)
    if not pending:
        logger.info("Nothing to embed — every chunk already has a vector.")
        return 0

    logger.info(f"Embedding {len(pending)} chunks with {model}")
    written = 0
    # Commit in batches so an interrupted run keeps its progress.
    for i in range(0, len(pending), batch):
        window = pending[i : i + batch]
        vectors = embed.embed_texts([text for _, text in window], model)
        written += store.upsert(
            [(cid, vec) for (cid, _), vec in zip(window, vectors)]
        )
        logger.info(f"Progress: {written}/{len(pending)} chunks embedded")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embed catalog chunks that do not have a vector yet."
    )
    parser.add_argument("--db", default="catalog/librarian.duckdb", help="Catalog DB path")
    parser.add_argument("--model", default=None,
                        help="Embedding model (default: the effort profile's). Ignored "
                             "if the catalog is already locked to another model.")
    parser.add_argument("--doc-id", default=None, help="Only embed one document's chunks")
    args = parser.parse_args()

    try:
        con = db.connect(args.db)
        db.init_schema(con)
        written = backfill(con, model=args.model, doc_id=args.doc_id)
        print(f"Embedded {written} chunks. Catalog total: {db.embedding_count(con)}")
    except Exception:
        logger.exception("Embedding backfill failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
