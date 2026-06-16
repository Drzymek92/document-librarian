from scripts import config, db, enrich


def _con(tmp_path):
    con = db.connect(str(tmp_path / "c.duckdb"))
    db.init_schema(con)
    return con


def test_cheap_tags_excludes_stopwords_and_digits():
    text = "Locale locale feedback feedback feedback the the and 123 transcription"
    tags = enrich.cheap_tags("notes.txt", "text", text, k=5)
    assert "feedback" in tags
    assert "locale" in tags
    assert "the" not in tags and "and" not in tags
    assert "123" not in tags


def test_low_effort_enrich_uses_cheap_tags_no_llm(tmp_path, monkeypatch):
    # If anything tries to call the LLM on the low tier, fail loudly.
    def boom(*a, **k):
        raise AssertionError("low effort must not call the LLM")

    monkeypatch.setattr(enrich, "_llm_summary", boom)
    monkeypatch.setattr(enrich, "_llm_tags", boom)

    con = _con(tmp_path)
    doc_id = "doc1"
    db.upsert_document(con, {"doc_id": doc_id, "path": "/n.txt", "filename": "n.txt"})
    out = enrich.enrich_document(
        con, doc_id, "n.txt", "text",
        "quarterly revenue analysis revenue revenue growth", effort="low",
    )
    assert out["summary"] is None
    assert "revenue" in out["tags"]
    # summary column stays empty on low tier
    assert con.execute("SELECT summary FROM documents WHERE doc_id=?", [doc_id]).fetchone()[0] is None


def test_high_effort_enrich_calls_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "_llm_summary", lambda text, profile: "A short summary.")
    monkeypatch.setattr(enrich, "_llm_tags", lambda text, profile: ["alpha", "beta"])

    con = _con(tmp_path)
    doc_id = "doc2"
    db.upsert_document(con, {"doc_id": doc_id, "path": "/n.txt", "filename": "n.txt"})
    out = enrich.enrich_document(con, doc_id, "n.txt", "text", "some text", effort="high")

    assert out["summary"] == "A short summary."
    assert out["tags"] == ["alpha", "beta"]
    assert con.execute("SELECT summary FROM documents WHERE doc_id=?", [doc_id]).fetchone()[0] == "A short summary."


def test_profiles_drive_enrichment_flags():
    assert config.get_profile("low").enrich_summary is False
    assert config.get_profile("high").enrich_summary is True
