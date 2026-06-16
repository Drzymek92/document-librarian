import pytest

from scripts import extractors


def test_csv(sample_dir):
    r = extractors.extract(sample_dir / "data.csv")
    assert r.source_type == "csv"
    joined = " ".join(c["text"] for c in r.chunks)
    assert "Alice" in joined and "name,amount" in joined
    assert r.chunks[0]["locator"].startswith("rows")


def test_xlsx(sample_dir):
    r = extractors.extract(sample_dir / "sheet.xlsx")
    assert r.source_type == "xlsx"
    joined = " ".join(c["text"] for c in r.chunks)
    assert "widget" in joined
    assert any("Q1" in (c["locator"] or "") for c in r.chunks)


def test_pdf(sample_dir):
    r = extractors.extract(sample_dir / "doc.pdf")
    assert r.source_type == "pdf"
    assert r.page_count == 2
    joined = " ".join(c["text"] for c in r.chunks)
    assert "fox" in joined
    assert any(c["locator"] == "page 2" for c in r.chunks)


def test_docx(sample_dir):
    r = extractors.extract(sample_dir / "memo.docx")
    assert r.source_type == "docx"
    joined = " ".join(c["text"] for c in r.chunks)
    assert "quick brown fox" in joined


def test_text(sample_dir):
    r = extractors.extract(sample_dir / "notes.txt")
    assert r.source_type == "text"
    joined = " ".join(c["text"] for c in r.chunks)
    assert "transcription" in joined


def test_image_uses_effort_vision_model(sample_dir):
    captured = {}

    def fake_vision(path, model):
        captured["model"] = model
        captured["path"] = path
        return "Receipt 99.50"

    r = extractors.extract(sample_dir / "scan.png", effort="low", vision_fn=fake_vision)
    assert r.source_type == "image"
    assert any("Receipt" in c["text"] for c in r.chunks)
    # low-effort tier must select the economical vision model
    assert captured["model"] == "gpt-4o-mini"


def test_image_high_effort_model(sample_dir):
    captured = {}

    def fake_vision(path, model):
        captured["model"] = model
        return "text"

    extractors.extract(sample_dir / "scan.png", effort="high", vision_fn=fake_vision)
    assert captured["model"] == "gpt-4o"


def test_docx_table_cells_are_captured(sample_dir):
    r = extractors.extract(sample_dir / "table_memo.docx")
    joined = " ".join(c["text"] for c in r.chunks)
    # Content that lives only inside table cells must be searchable.
    assert "sprocket" in joined and "314" in joined
    assert "Inventory snapshot" in joined


def test_csv_sniffs_semicolon_and_cp1252(sample_dir):
    r = extractors.extract(sample_dir / "locale.csv")
    assert r.source_type == "csv"
    joined = " ".join(c["text"] for c in r.chunks)
    # Semicolon delimiter + cp1252 accents must round-trip, not collapse.
    assert "Montréal" in joined and "Bogotá" in joined
    assert "name,city" in joined  # normalized to comma-joined output


def test_markdown_uses_headings_as_locators(sample_dir):
    r = extractors.extract(sample_dir / "guide.md")
    locators = {c["locator"] for c in r.chunks}
    assert "Locale Setup" in locators and "Troubleshooting" in locators
    assert r.title == "Overview"


def test_pdf_ocr_fallback_high_effort(sample_dir):
    captured = {}

    def fake_vision(path, model):
        captured["model"] = model
        return "Scanned invoice total 42.00"

    r = extractors.extract(sample_dir / "scanned.pdf", effort="high", vision_fn=fake_vision)
    joined = " ".join(c["text"] for c in r.chunks)
    assert "Scanned invoice" in joined
    assert captured["model"] == "gpt-4o"
    assert r.metadata.get("ocr_pages") == 1
    assert any("(ocr)" in (c["locator"] or "") for c in r.chunks)


def test_pdf_ocr_skipped_on_low_effort(sample_dir):
    def fail_vision(path, model):
        raise AssertionError("OCR must not run on the low-effort tier")

    r = extractors.extract(sample_dir / "scanned.pdf", effort="low", vision_fn=fail_vision)
    # Image-only page yields no chunks on low effort (no token spend).
    assert r.chunks == []


def test_json_flattens_nested_records(sample_dir):
    r = extractors.extract(sample_dir / "feedback.json")
    assert r.source_type == "json"
    assert r.page_count == 2  # two records
    joined = " ".join(c["text"] for c in r.chunks)
    # nested leaf values are flattened to searchable lines
    assert "locale: fr-CA" in joined
    assert "note.text: needs review" in joined
    assert any("record" in (c["locator"] or "") for c in r.chunks)


def test_jsonl_one_record_per_line(sample_dir):
    r = extractors.extract(sample_dir / "events.jsonl")
    assert r.source_type == "json"
    assert r.page_count == 2
    joined = " ".join(c["text"] for c in r.chunks)
    assert "event: transcribed" in joined and "count: 42" in joined


def test_tsv_routes_through_csv_sniffer(sample_dir):
    r = extractors.extract(sample_dir / "prices.tsv")
    assert r.source_type == "csv"
    joined = " ".join(c["text"] for c in r.chunks)
    # tab delimiter detected → columns split, then normalized to comma-joined
    assert "sku,price" in joined and "A-100,9.99" in joined


def test_html_strips_scripts_and_keeps_title(sample_dir):
    r = extractors.extract(sample_dir / "page.html")
    assert r.source_type == "html"
    assert r.title == "Locale Report"
    joined = " ".join(c["text"] for c in r.chunks)
    assert "transcription quality improved" in joined
    # script/style content must not leak into the text
    assert "console.log" not in joined and "color:red" not in joined


def test_unsupported_type(tmp_path):
    p = tmp_path / "archive.zip"
    p.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError):
        extractors.extract(p)
