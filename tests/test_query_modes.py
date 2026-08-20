"""One passing test per retrieval mode, plus the fusion rule itself."""
import pytest

from scripts import db, embed_backfill, extractors, ingest, query


def _ingested(tmp_path, sample_dir, monkeypatch):
    monkeypatch.setattr(
        extractors, "_default_vision_transcribe", lambda path, model: "Receipt 99.50"
    )
    con = db.connect(str(tmp_path / "c.duckdb"))
    db.init_schema(con)
    ingest.ingest_local_folder(con, sample_dir, effort="low")
    return con


def _embedded(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    embed_backfill.backfill(con, model="toy-model")
    return con


# --- one test per mode ------------------------------------------------------


def test_mode_bm25(tmp_path, sample_dir, monkeypatch):
    results, mode = query.search(con := _ingested(tmp_path, sample_dir, monkeypatch),
                                 "fox", mode="bm25")
    assert mode == "bm25"
    assert results and results[0]["score"] >= results[-1]["score"]
    del con


def test_mode_vector(tmp_path, sample_dir, monkeypatch, toy_embedder):
    con = _embedded(tmp_path, sample_dir, monkeypatch)
    results, mode = query.search(con, "fox", mode="vector")
    assert mode == "vector"
    assert results and results[0]["score"] >= results[-1]["score"]
    # Cosine similarity is bounded, unlike BM25 — a cheap check that the vector
    # path really ran rather than silently falling back to lexical scores.
    assert all(-1.0 <= r["score"] <= 1.0 for r in results)


def test_mode_hybrid(tmp_path, sample_dir, monkeypatch, toy_embedder):
    con = _embedded(tmp_path, sample_dir, monkeypatch)
    results, mode = query.search(con, "fox", mode="hybrid", limit=5)
    assert mode == "hybrid"
    assert results and len(results) <= 5
    assert results[0]["score"] >= results[-1]["score"]
    # Fused scores are RRF sums, so they sit in (0, 2/RRF_K] — never a raw BM25 score.
    assert all(0 < r["score"] <= 2 / query.RRF_K for r in results)


def test_mode_auto_falls_back_to_bm25_without_vectors(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    results, mode = query.search(con, "fox", mode="auto")
    assert mode == "bm25"
    assert results


def test_mode_auto_uses_hybrid_once_embedded(tmp_path, sample_dir, monkeypatch, toy_embedder):
    con = _embedded(tmp_path, sample_dir, monkeypatch)
    results, mode = query.search(con, "fox", mode="auto")
    assert mode == "hybrid"
    assert results


# --- selector edges ---------------------------------------------------------


def test_vector_mode_without_embeddings_is_a_clear_error(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    with pytest.raises(RuntimeError, match="no embeddings yet"):
        query.search(con, "fox", mode="vector")


def test_unknown_mode_rejected(tmp_path, sample_dir, monkeypatch):
    con = _ingested(tmp_path, sample_dir, monkeypatch)
    with pytest.raises(ValueError, match="Unknown mode"):
        query.search(con, "fox", mode="semantic")


def test_hybrid_applies_filters_to_both_retrievers(tmp_path, sample_dir, monkeypatch, toy_embedder):
    con = _embedded(tmp_path, sample_dir, monkeypatch)
    results, _ = query.search(con, "fox", mode="hybrid", types=["docx"], limit=20)
    assert results
    assert all(r["source_type"] == "docx" for r in results)


# --- the fusion rule itself -------------------------------------------------


def test_rrf_rewards_agreement_over_a_single_strong_rank():
    """A chunk ranked 2nd by both retrievers must beat one ranked 1st by only one:
    that trade is the entire reason to fuse rather than to pick a winner."""
    bm25 = [{"chunk_id": "solo", "text": "x", "score": 99.0},
            {"chunk_id": "both", "text": "y", "score": 98.0}]
    vector = [{"chunk_id": "other", "text": "z", "score": 0.9},
              {"chunk_id": "both", "text": "y", "score": 0.8}]

    fused = query._rrf(bm25, vector, limit=10)
    assert fused[0]["chunk_id"] == "both"
    # Ranks are 1 in each list (0-indexed), so the score is 2/(60+1).
    assert fused[0]["score"] == pytest.approx(2 / (query.RRF_K + 1))
    assert fused[1]["score"] == pytest.approx(1 / query.RRF_K)


def test_rrf_k_is_the_published_constant():
    assert query.RRF_K == 60


def test_rrf_deduplicates_chunks_seen_twice():
    row = {"chunk_id": "c1", "text": "t", "score": 1.0}
    fused = query._rrf([dict(row)], [dict(row)], limit=10)
    assert len(fused) == 1
