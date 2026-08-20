import argparse
import os
import sys
from pathlib import Path

from scripts import db, ingest, manifest, query
from scripts.logger import get_logger

logger = get_logger("resources_kb")

# The shared, cross-project knowledge base. Source files live under
# resources/files/, the catalog under resources/catalog/resources.duckdb, and a
# descriptive INDEX.md is regenerated on every ingest for cheap agent browsing.
#
# Defaults to `resources/` beside the repo root, anchored via __file__ rather than
# the CWD so a run launched from anywhere resolves to the same place. Point
# LIBRARIAN_RESOURCES_DIR elsewhere to keep the knowledge base outside the repo.

_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "resources"


def resources_dir() -> Path:
    return Path(os.environ.get("LIBRARIAN_RESOURCES_DIR", _DEFAULT_ROOT))


def _paths() -> tuple[Path, Path, Path]:
    root = resources_dir()
    return root / "files", root / "catalog" / "resources.duckdb", root / "INDEX.md"


def _open(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(str(db_path))
    db.init_schema(con)
    return con


def cmd_ingest(args) -> None:
    files_dir, db_path, index_path = _paths()
    files_dir.mkdir(parents=True, exist_ok=True)
    # Default to high effort so each resource gets an LLM summary + tags — the
    # whole point of the KB is "already described" entries.
    effort = args.effort or "high"
    con = _open(db_path)
    n_docs, n_chunks = ingest.ingest_local_folder(
        con, files_dir, effort=effort, do_enrich=not args.no_enrich
    )
    manifest.write_manifest(con, index_path, title="Resources Index")
    logger.info(
        f"Resources ingest complete: {n_docs} docs, {n_chunks} chunks "
        f"(effort={effort}). Index: {index_path}"
    )


def cmd_query(args) -> None:
    _, db_path, _ = _paths()
    if not db_path.exists():
        print("No resources catalog yet — run `resources_kb ingest` first.")
        return
    con = db.connect(str(db_path))
    results = query.search(
        con, args.query, limit=args.limit, types=args.types,
        tags=args.tags, source=args.source,
    )
    print(query.format_results(results))


def cmd_index(args) -> None:
    _, db_path, index_path = _paths()
    con = _open(db_path)
    out = manifest.write_manifest(con, index_path, title="Resources Index")
    print(f"Wrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the shared resources knowledge base (librarian-backed)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="Index resources/files/ and regenerate INDEX.md")
    p_ing.add_argument("--effort", choices=["low", "high"], default=None,
                       help="Effort tier (default: high — generates summaries + tags)")
    p_ing.add_argument("--no-enrich", action="store_true", help="Skip summary/tag enrichment")

    p_q = sub.add_parser("query", help="Full-text search the resources catalog")
    p_q.add_argument("query")
    p_q.add_argument("--type", dest="types", action="append", help="Filter by source_type (repeatable)")
    p_q.add_argument("--tag", dest="tags", action="append", help="Filter by tag (repeatable)")
    p_q.add_argument("--source", choices=["local", "gdrive"], default=None)
    p_q.add_argument("--limit", type=int, default=10)

    sub.add_parser("index", help="Regenerate INDEX.md from the current catalog")

    args = parser.parse_args()
    try:
        {"ingest": cmd_ingest, "query": cmd_query, "index": cmd_index}[args.cmd](args)
    except Exception:
        logger.exception(f"resources_kb {args.cmd} failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
