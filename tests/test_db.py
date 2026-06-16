from scripts import db


def _make_con(tmp_path):
    con = db.connect(str(tmp_path / "test.duckdb"))
    db.init_schema(con)
    return con


def test_schema_creates_tables(tmp_path):
    con = _make_con(tmp_path)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"documents", "chunks", "doc_metadata", "tags"} <= tables


def test_document_roundtrip(tmp_path):
    con = _make_con(tmp_path)
    doc_id = db.doc_id_for("/some/report.pdf", 1234.0)
    db.upsert_document(con, {
        "doc_id": doc_id,
        "path": "/some/report.pdf",
        "filename": "report.pdf",
        "ext": ".pdf",
        "source_type": "pdf",
        "page_count": 2,
        "effort": "low",
    })
    row = con.execute(
        "SELECT filename, source_type, page_count FROM documents WHERE doc_id = ?",
        [doc_id],
    ).fetchone()
    assert row == ("report.pdf", "pdf", 2)


def test_upsert_is_idempotent(tmp_path):
    con = _make_con(tmp_path)
    doc_id = db.doc_id_for("/a.csv", 1.0)
    base = {"doc_id": doc_id, "path": "/a.csv", "filename": "a.csv"}
    db.upsert_document(con, base)
    db.upsert_document(con, {**base, "title": "updated"})
    count, title = con.execute(
        "SELECT count(*), max(title) FROM documents WHERE doc_id = ?", [doc_id]
    ).fetchone()
    assert count == 1
    assert title == "updated"


def test_replace_chunks_and_search(tmp_path):
    con = _make_con(tmp_path)
    doc_id = db.doc_id_for("/a.pdf", 1.0)
    db.upsert_document(con, {"doc_id": doc_id, "path": "/a.pdf", "filename": "a.pdf"})
    db.replace_chunks(con, doc_id, [
        {"ordinal": 0, "text": "the quick brown fox", "locator": "page 1"},
        {"ordinal": 1, "text": "lazy dog sleeps", "locator": "page 2"},
    ])
    db.rebuild_fts_index(con)
    results = db.search(con, "fox")
    assert results
    assert "fox" in results[0][2]


def test_replace_chunks_overwrites(tmp_path):
    con = _make_con(tmp_path)
    doc_id = db.doc_id_for("/a.pdf", 1.0)
    db.upsert_document(con, {"doc_id": doc_id, "path": "/a.pdf", "filename": "a.pdf"})
    db.replace_chunks(con, doc_id, [{"ordinal": 0, "text": "first version"}])
    db.replace_chunks(con, doc_id, [{"ordinal": 0, "text": "second version"}])
    n = con.execute(
        "SELECT count(*) FROM chunks WHERE doc_id = ?", [doc_id]
    ).fetchone()[0]
    assert n == 1


def test_metadata_and_tags(tmp_path):
    con = _make_con(tmp_path)
    doc_id = db.doc_id_for("/a.pdf", 1.0)
    db.upsert_document(con, {"doc_id": doc_id, "path": "/a.pdf", "filename": "a.pdf"})
    db.set_metadata(con, doc_id, {"language": "en", "project": "librarian"})
    db.set_tags(con, doc_id, ["finance", "q1", "finance"])  # dupe dropped
    meta = dict(con.execute(
        "SELECT key, value FROM doc_metadata WHERE doc_id = ?", [doc_id]
    ).fetchall())
    tags = [r[0] for r in con.execute(
        "SELECT tag FROM tags WHERE doc_id = ?", [doc_id]
    ).fetchall()]
    assert meta == {"language": "en", "project": "librarian"}
    assert sorted(tags) == ["finance", "q1"]
