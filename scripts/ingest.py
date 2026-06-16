import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from scripts import config, db, enrich, extractors
from scripts.logger import get_logger

logger = get_logger("ingest")

SUPPORTED = set(extractors._EXT_TO_TYPE)


def _ingest_item(con, item: dict, effort: str | None, do_enrich: bool) -> int:
    res = extractors.extract(item["local_path"], effort=effort)
    doc_id = item["doc_id"]
    p = Path(item["local_path"])
    db.upsert_document(con, {
        "doc_id": doc_id,
        "path": item.get("source_uri") or str(p),
        "filename": item.get("filename") or p.name,
        "ext": p.suffix.lower(),
        "source_type": res.source_type,
        "size_bytes": p.stat().st_size if p.exists() else None,
        "ingested_at": datetime.now(),
        "title": res.title,
        "author": res.author,
        "page_count": res.page_count,
        "effort": effort or config.default_effort(),
        "status": "active",
        "source": item.get("source", "local"),
        "source_id": item.get("source_id"),
        "source_uri": item.get("source_uri"),
        "source_modified": item.get("source_modified"),
        "mime_type": item.get("mime_type"),
    })
    db.replace_chunks(con, doc_id, res.chunks)
    if res.metadata:
        db.set_metadata(con, doc_id, res.metadata)
    if do_enrich:
        full_text = "\n\n".join(c["text"] for c in res.chunks)
        enrich.enrich_document(
            con, doc_id, item.get("filename") or p.name, res.source_type, full_text, effort
        )
    return len(res.chunks)


def ingest_items(
    con, items: Iterable[dict], effort: str | None = None, do_enrich: bool = True
) -> tuple[int, int]:
    n_docs = n_chunks = 0
    for item in items:
        try:
            c = _ingest_item(con, item, effort, do_enrich)
            n_docs += 1
            n_chunks += c
            logger.info(f"Ingested {item.get('filename')} ({c} chunks)")
        except Exception:
            logger.exception(f"Failed to ingest {item.get('local_path')}")
    db.rebuild_fts_index(con)
    logger.info(f"Ingest complete: {n_docs} documents, {n_chunks} chunks")
    return n_docs, n_chunks


def iter_local_folder(folder: str | Path) -> Iterator[dict]:
    folder = Path(folder)
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED:
            continue
        resolved = str(p.resolve())
        mtime = str(p.stat().st_mtime)
        yield {
            "local_path": str(p),
            "source": "local",
            "source_id": resolved,
            "source_uri": resolved,
            "source_modified": mtime,
            "mime_type": None,
            "filename": p.name,
            "doc_id": db.make_doc_id(resolved, mtime),
        }


def ingest_local_folder(
    con, folder: str | Path, effort: str | None = None, do_enrich: bool = True
) -> tuple[int, int]:
    return ingest_items(con, iter_local_folder(folder), effort, do_enrich)


def ingest_gdrive_folder(
    con, folder_id: str, staging_dir: str | Path,
    effort: str | None = None, incremental: bool = True, do_enrich: bool = True,
) -> tuple[int, int]:
    from scripts import gdrive_source  # lazy import: only loads rpy2 when used

    known = db.existing_versions(con, "gdrive") if incremental else {}
    items = gdrive_source.iter_documents(folder_id, staging_dir, known)
    return ingest_items(con, items, effort, do_enrich)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into the librarian catalog.")
    parser.add_argument("--db", default="catalog/librarian.duckdb", help="Catalog DB path")
    parser.add_argument("--effort", choices=["low", "high"], default=None)
    parser.add_argument("--no-enrich", action="store_true", help="Skip summary/tag enrichment")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_local = sub.add_parser("local", help="Ingest a local folder")
    p_local.add_argument("folder")

    p_gd = sub.add_parser("gdrive", help="Ingest a Google Drive folder")
    p_gd.add_argument("folder_id")
    p_gd.add_argument("--staging", default="scripts/inputs/gdrive", help="Local staging dir")
    p_gd.add_argument("--full", action="store_true", help="Re-ingest all (ignore unchanged)")

    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(args.db)
    db.init_schema(con)
    try:
        if args.cmd == "local":
            ingest_local_folder(con, args.folder, args.effort, do_enrich=not args.no_enrich)
        else:
            ingest_gdrive_folder(
                con, args.folder_id, args.staging, args.effort,
                incremental=not args.full, do_enrich=not args.no_enrich,
            )
    except Exception:
        logger.exception("Ingest failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
