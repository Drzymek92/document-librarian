import re
from pathlib import Path
from typing import Iterator

from scripts import db, extractors
from scripts.logger import get_logger

logger = get_logger("gdrive_source")

# Google-native types must be EXPORTED (not downloaded). Map mime -> (R export type, extension).
GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": ("docx", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("xlsx", ".xlsx"),
    "application/vnd.google-apps.presentation": ("pdf", ".pdf"),
}
FOLDER_MIME = "application/vnd.google-apps.folder"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# R helpers defined once per process (after auth). Listing is recursive and
# returns a flat data.frame; download/export is one call per file.
_R_HELPERS = """
suppressMessages(library(googledrive))

gd_list_folder <- function(folder_id) {
  files <- drive_ls(path = as_id(folder_id), recursive = TRUE)
  if (nrow(files) == 0) {
    return(data.frame(id=character(0), name=character(0), mime=character(0),
                      modified=character(0), md5=character(0),
                      stringsAsFactors=FALSE))
  }
  getf <- function(r, key) { v <- r[[key]]; if (is.null(v)) NA_character_ else as.character(v) }
  data.frame(
    id       = files$id,
    name     = files$name,
    mime     = vapply(files$drive_resource, getf, character(1), key="mimeType"),
    modified = vapply(files$drive_resource, getf, character(1), key="modifiedTime"),
    md5      = vapply(files$drive_resource, getf, character(1), key="md5Checksum"),
    stringsAsFactors = FALSE
  )
}

gd_download_one <- function(file_id, local_path, export_type) {
  ty <- if (is.null(export_type) || is.na(export_type) || export_type == "") NULL else export_type
  drive_download(as_id(file_id), path = local_path, type = ty, overwrite = TRUE)
  invisible(TRUE)
}
"""

_R_READY = False


def _sanitize(name: str) -> str:
    return _ILLEGAL.sub("_", name or "").strip() or "untitled"


def target_for(file_id: str, name: str, mime: str) -> tuple[str | None, str | None]:
    """Decide how to stage a Drive file.

    Returns (export_type, local_filename):
      - native Google type  -> (export type, "<id>__<name><ext>")
      - supported binary     -> (None, "<id>__<name>")  (download as-is)
      - unsupported          -> (None, None)             (skip)
    """
    if mime in GOOGLE_EXPORT:
        export_type, ext = GOOGLE_EXPORT[mime]
        return export_type, f"{file_id}__{_sanitize(name)}{ext}"
    ext = Path(name).suffix.lower()
    if ext not in extractors._EXT_TO_TYPE:
        return None, None
    return None, f"{file_id}__{_sanitize(name)}"


def _ensure_r() -> None:
    global _R_READY
    if _R_READY:
        return
    import rpy2.robjects as ro

    from scripts.gdrive_auth import authenticate_gdrive

    authenticate_gdrive()
    ro.r(_R_HELPERS)
    _R_READY = True


def _na(value) -> str | None:
    if value is None:
        return None
    s = str(value)
    return None if s in ("NA", "NA_character_", "<NA>") else s


def list_folder(folder_id: str) -> list[dict]:
    import rpy2.robjects as ro

    _ensure_r()
    ro.globalenv["gd_folder_id"] = ro.StrVector([folder_id])
    res = ro.r("gd_list_folder(gd_folder_id)")
    cols = {name: list(res.rx2(name)) for name in ("id", "name", "mime", "modified", "md5")}
    n = len(cols["id"])
    return [
        {
            "id": _na(cols["id"][i]),
            "name": _na(cols["name"][i]),
            "mime": _na(cols["mime"][i]),
            "modified": _na(cols["modified"][i]),
            "md5": _na(cols["md5"][i]),
        }
        for i in range(n)
    ]


def _download(file_id: str, local_path: Path, export_type: str | None) -> None:
    import rpy2.robjects as ro

    _ensure_r()
    ro.globalenv["gd_file_id"] = ro.StrVector([file_id])
    ro.globalenv["gd_local_path"] = ro.StrVector([str(local_path)])
    ro.globalenv["gd_export_type"] = ro.StrVector([export_type or ""])
    ro.r("gd_download_one(gd_file_id, gd_local_path, gd_export_type)")


def iter_documents(
    folder_id: str, dest_dir: str | Path, known_versions: dict[str, str] | None = None
) -> Iterator[dict]:
    """List a Drive folder recursively, stage new/changed files locally, and
    yield an ingest item per staged file. Unchanged files (same modifiedTime as
    the catalog) and unsupported types are skipped."""
    known_versions = known_versions or {}
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    files = list_folder(folder_id)
    total = staged = skipped_unchanged = skipped_unsupported = 0
    for f in files:
        if f["mime"] == FOLDER_MIME or not f["id"]:
            continue
        total += 1
        export_type, filename = target_for(f["id"], f["name"] or f["id"], f["mime"] or "")
        if filename is None:
            skipped_unsupported += 1
            logger.warning(f"Skipping unsupported file: {f['name']} ({f['mime']})")
            continue
        modified = f["modified"] or ""
        if known_versions.get(f["id"]) == modified:
            skipped_unchanged += 1
            continue
        local = dest / filename
        _download(f["id"], local, export_type)
        staged += 1
        yield {
            "local_path": str(local),
            "source": "gdrive",
            "source_id": f["id"],
            "source_uri": f"https://drive.google.com/open?id={f['id']}",
            "source_modified": modified,
            "mime_type": f["mime"],
            "filename": filename,
            "doc_id": db.make_doc_id(f["id"], modified),
        }
    logger.info(
        f"GDrive scan: {total} files listed, {staged} staged, "
        f"{skipped_unchanged} unchanged (skipped), {skipped_unsupported} unsupported (skipped)"
    )
