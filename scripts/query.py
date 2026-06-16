import argparse
import sys

from scripts import db
from scripts.logger import get_logger

logger = get_logger("query")


def search(
    con,
    query: str,
    limit: int = 10,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
) -> list[dict]:
    sql = """
        SELECT d.doc_id, d.filename, d.source_type, d.source_uri, d.summary,
               c.locator, c.text, s.score
        FROM (
            SELECT chunk_id, fts_main_chunks.match_bm25(chunk_id, ?) AS score
            FROM chunks
        ) s
        JOIN chunks c USING (chunk_id)
        JOIN documents d USING (doc_id)
        WHERE s.score IS NOT NULL
    """
    params: list = [query]
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
    sql += " ORDER BY s.score DESC LIMIT ?"
    params.append(limit)

    cols = ["doc_id", "filename", "source_type", "source_uri", "summary",
            "locator", "text", "score"]
    return [dict(zip(cols, row)) for row in con.execute(sql, params).fetchall()]


def format_results(results: list[dict], snippet_chars: int = 240) -> str:
    if not results:
        return "No matches."
    lines = []
    for i, r in enumerate(results, 1):
        snippet = " ".join(r["text"].split())[:snippet_chars]
        lines.append(
            f"{i}. [{r['score']:.2f}] {r['filename']} ({r['source_type']}, {r['locator']})\n"
            f"   uri: {r['source_uri']}\n"
            f"   {snippet}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the librarian catalog (full-text search).")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--db", default="catalog/librarian.duckdb", help="Catalog DB path")
    parser.add_argument("--type", dest="types", action="append", help="Filter by source_type (repeatable)")
    parser.add_argument("--tag", dest="tags", action="append", help="Filter by tag (repeatable)")
    parser.add_argument("--source", choices=["local", "gdrive"], default=None)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    try:
        con = db.connect(args.db)
        results = search(
            con, args.query, limit=args.limit, types=args.types, tags=args.tags, source=args.source
        )
        print(format_results(results))
    except Exception:
        logger.exception("Query failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
