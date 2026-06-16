from scripts import db, extractors, ingest, query


def _ingested(tmp_path, sample_dir, monkeypatch):
    monkeypatch.setattr(
        extractors, "_default_vision_transcribe", lambda path, model: "Receipt 99.50"
    )
    con = db.connect(str(tmp_path / "c.duckdb"))
    db.init_schema(con)
    ingest.ingest_local_folder(con, sample_dir, effort="low")
    return con


def test_query_returns_ranked_hits(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    results = query.search(con, "fox")
    assert results
    assert results[0]["score"] >= results[-1]["score"]
    assert "filename" in results[0] and "locator" in results[0]


def test_query_type_filter(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    # "fox" appears in both the pdf and the docx; the filter must restrict to docx
    results = query.search(con, "fox", types=["docx"], limit=20)
    assert results
    assert all(r["source_type"] == "docx" for r in results)


def test_query_source_filter(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    assert query.search(con, "fox", source="gdrive", limit=20) == []
    assert query.search(con, "fox", source="local", limit=20)


def test_query_tag_filter(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    # tag the top hit, then confirm the tag filter narrows results to just it
    hit = query.search(con, "fox", limit=20)[0]
    db.set_tags(con, hit["doc_id"], ["special-marker"])
    results = query.search(con, "fox", tags=["special-marker"], limit=20)
    assert results
    assert {r["doc_id"] for r in results} == {hit["doc_id"]}


def test_format_results_empty():
    assert query.format_results([]) == "No matches."
