"""Retrieval: BM25, dense vector, and their fusion.

Three ways to find a chunk, and one selector that picks between them:

  bm25    lexical — exact terms, identifiers, error codes, rare words
  vector  semantic — paraphrase, synonyms, "the thing about X" queries
  hybrid  both, fused with Reciprocal Rank Fusion
  auto    hybrid when the catalog has vectors, bm25 when it doesn't

Neither retriever dominates: BM25 wins on a literal token the embedding smooths
away, dense wins when the wording of the question shares no words with the answer.
Fusing them is what makes the failure modes stop overlapping.
"""
import argparse
import sys

from scripts import db
from scripts.logger import get_logger
from scripts.vector_store import Filters, get_vector_store

logger = get_logger("query")

MODES = ("auto", "bm25", "vector", "hybrid")

# Reciprocal-rank-fusion constant. k=60 is the value from the original RRF paper
# (Cormack et al., 2009) and the de-facto default: large enough that the top few
# ranks don't swamp the sum, small enough that deep ranks still fade out.
RRF_K = 60

# Every result row carries the same keys whichever retriever produced it, so RRF
# can fuse them and one formatter can render them.
_SELECT_COLS = (
    "d.doc_id, d.filename, d.source_type, d.source_uri, d.summary, "
    "c.chunk_id, c.locator, c.text"
)
_ROW_KEYS = ["doc_id", "filename", "source_type", "source_uri", "summary",
             "chunk_id", "locator", "text", "score"]


def _filter_sql(
    types: list[str] | None, tags: list[str] | None, source: str | None
) -> tuple[str, list]:
    """WHERE-clause fragment shared by both retrievers, so a filter can never apply
    to one half of a hybrid search and not the other."""
    sql = ""
    params: list = []
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
    return sql, params


def _bm25(
    con, query: str, limit: int,
    types: list[str] | None = None, tags: list[str] | None = None,
    source: str | None = None,
) -> list[dict]:
    fsql, fparams = _filter_sql(types, tags, source)
    sql = f"""
        SELECT {_SELECT_COLS}, s.score
        FROM (
            SELECT chunk_id, fts_main_chunks.match_bm25(chunk_id, ?) AS score
            FROM chunks
        ) s
        JOIN chunks c USING (chunk_id)
        JOIN documents d USING (doc_id)
        WHERE s.score IS NOT NULL{fsql}
        ORDER BY s.score DESC LIMIT ?
    """
    params = [query, *fparams, limit]
    return [dict(zip(_ROW_KEYS, row)) for row in con.execute(sql, params).fetchall()]


def _vector(
    con, query: str, limit: int,
    types: list[str] | None = None, tags: list[str] | None = None,
    source: str | None = None,
) -> list[dict]:
    from scripts import embed

    store = get_vector_store(con)
    model = store.locked_model()
    if not model:
        raise RuntimeError(
            "This catalog has no embeddings yet. Run "
            "`python -m scripts.embed_backfill --db <catalog>` before using "
            "vector or hybrid search."
        )
    qvec = embed.embed_query(query, model)
    return store.search(qvec, limit=limit,
                        filters=Filters(types=types, tags=tags, source=source))


def _rrf(bm25: list[dict], vector: list[dict], limit: int) -> list[dict]:
    """Reciprocal Rank Fusion: score each chunk 1/(k + rank) in every list it
    appears in, and sum.

    Fusing on RANK rather than on score is the whole point — a BM25 score and a
    cosine similarity live on different, unbounded, query-dependent scales, so any
    attempt to normalize them into one number needs a fudge factor that has to be
    re-tuned per corpus. Ranks need none, and a chunk both retrievers liked lands
    above one that only a single retriever ranked highly.
    """
    fused: dict[str, dict] = {}
    for ranked in (bm25, vector):
        for rank, row in enumerate(ranked):
            entry = fused.setdefault(row["chunk_id"], {"row": row, "rrf": 0.0})
            entry["rrf"] += 1.0 / (RRF_K + rank)
    out = []
    for entry in sorted(fused.values(), key=lambda e: e["rrf"], reverse=True):
        row = dict(entry["row"])
        row["score"] = entry["rrf"]
        out.append(row)
    return out[:limit]


def search(
    con,
    query: str,
    limit: int = 10,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    mode: str = "auto",
) -> tuple[list[dict], str]:
    """Search the catalog. Returns (results, mode_used).

    `mode_used` is returned rather than assumed because `auto` resolves at call
    time: a caller (or an API response) needs to know which retriever actually ran.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode '{mode}'. Choose from {list(MODES)}.")
    filt = dict(types=types, tags=tags, source=source)

    if mode == "auto":
        mode = "hybrid" if db.get_meta(con, "embedding_model") else "bm25"

    if mode == "bm25":
        return _bm25(con, query, limit, **filt), "bm25"
    if mode == "vector":
        return _vector(con, query, limit, **filt), "vector"

    # Fuse deeper lists than the caller asked for: a chunk ranked 8th by one
    # retriever and 30th by the other should still be able to surface, which it
    # cannot if each list is truncated at `limit` before fusion.
    pool = max(limit * 4, 20)
    return _rrf(_bm25(con, query, pool, **filt),
                _vector(con, query, pool, **filt), limit), "hybrid"


def format_results(
    results: list[dict], mode: str | None = None, snippet_chars: int = 240
) -> str:
    if not results:
        return "No matches."
    lines = [f"(mode: {mode})"] if mode else []
    for i, r in enumerate(results, 1):
        snippet = " ".join(r["text"].split())[:snippet_chars]
        lines.append(
            f"{i}. [{r['score']:.4f}] {r['filename']} ({r['source_type']}, {r['locator']})\n"
            f"   uri: {r['source_uri']}\n"
            f"   {snippet}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the librarian catalog.")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--db", default="catalog/librarian.duckdb", help="Catalog DB path")
    parser.add_argument("--type", dest="types", action="append", help="Filter by source_type (repeatable)")
    parser.add_argument("--tag", dest="tags", action="append", help="Filter by tag (repeatable)")
    parser.add_argument("--source", choices=["local", "gdrive"], default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--mode", choices=list(MODES), default="auto",
        help="Retrieval mode (default auto: hybrid if the catalog has embeddings, else bm25)",
    )
    args = parser.parse_args()

    try:
        con = db.connect(args.db)
        results, mode = search(
            con, args.query, limit=args.limit, types=args.types,
            tags=args.tags, source=args.source, mode=args.mode,
        )
        print(format_results(results, mode))
    except Exception:
        logger.exception("Query failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
