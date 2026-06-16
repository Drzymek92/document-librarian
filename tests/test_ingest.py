from scripts import db, extractors, ingest


def _con(tmp_path):
    con = db.connect(str(tmp_path / "catalog.duckdb"))
    db.init_schema(con)
    return con


def test_ingest_local_folder(tmp_path, sample_dir, monkeypatch):
    # Avoid a live LLM call for the image fixture: stub the vision transcriber.
    monkeypatch.setattr(
        extractors, "_default_vision_transcribe", lambda path, model: "Receipt 99.50"
    )
    con = _con(tmp_path)
    n_docs, n_chunks = ingest.ingest_local_folder(con, sample_dir, effort="low")

    # 6 base + table_memo.docx, locale.csv, guide.md, scanned.pdf
    # + feedback.json, events.jsonl, prices.tsv, page.html
    assert n_docs == 14
    assert n_chunks > 0

    types = {r[0] for r in con.execute("SELECT DISTINCT source_type FROM documents").fetchall()}
    assert types == {"csv", "xlsx", "pdf", "docx", "text", "image", "json", "html"}

    # full-text search spans the ingested corpus
    results = db.search(con, "fox")
    assert results
    assert all(r[0] for r in con.execute("SELECT source FROM documents").fetchall())


def test_reingest_is_idempotent(tmp_path, sample_dir, monkeypatch):
    monkeypatch.setattr(
        extractors, "_default_vision_transcribe", lambda path, model: "Receipt 99.50"
    )
    con = _con(tmp_path)
    ingest.ingest_local_folder(con, sample_dir, effort="low")
    first = con.execute("SELECT count(*) FROM documents").fetchone()[0]
    ingest.ingest_local_folder(con, sample_dir, effort="low")
    second = con.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert first == second  # same files, same mtime -> same doc_ids, no duplicates


def test_existing_versions_tracks_source(tmp_path, sample_dir, monkeypatch):
    monkeypatch.setattr(
        extractors, "_default_vision_transcribe", lambda path, model: "x"
    )
    con = _con(tmp_path)
    ingest.ingest_local_folder(con, sample_dir, effort="low")
    # local rows are recorded under source='local'; gdrive map is empty
    assert db.existing_versions(con, "gdrive") == {}
    assert len(db.existing_versions(con, "local")) == 14
