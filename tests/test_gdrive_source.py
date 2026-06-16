from scripts import gdrive_source as gd


def test_export_type_for_google_native():
    assert gd.target_for("ID1", "My Doc", "application/vnd.google-apps.document") == (
        "docx", "ID1__My Doc.docx",
    )
    assert gd.target_for("ID2", "Budget", "application/vnd.google-apps.spreadsheet") == (
        "xlsx", "ID2__Budget.xlsx",
    )
    assert gd.target_for("ID3", "Deck", "application/vnd.google-apps.presentation") == (
        "pdf", "ID3__Deck.pdf",
    )


def test_target_for_binary_supported():
    export_type, filename = gd.target_for("ID4", "report.xlsx", "application/vnd.ms-excel")
    assert export_type is None
    assert filename == "ID4__report.xlsx"


def test_target_for_unsupported_is_skipped():
    assert gd.target_for("ID5", "archive.zip", "application/zip") == (None, None)
    assert gd.target_for("ID6", "Form", "application/vnd.google-apps.form") == (None, None)


def test_target_for_text_is_supported():
    assert gd.target_for("ID7", "transcript.txt", "text/plain") == (None, "ID7__transcript.txt")


def test_sanitize_strips_illegal_chars():
    assert gd._sanitize('a/b:c*?.txt') == "a_b_c__.txt"
    assert gd._sanitize("") == "untitled"


def test_na_helper():
    assert gd._na("NA") is None
    assert gd._na(None) is None
    assert gd._na("real-value") == "real-value"
