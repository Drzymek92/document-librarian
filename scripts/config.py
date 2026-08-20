import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EffortProfile:
    name: str
    vision_model: str          # model used to transcribe/describe images
    text_model: str            # model used for enrichment (summary, tags)
    embedding_model: str       # model used for dense retrieval vectors
    enrich_summary: bool       # call the LLM to summarize each document
    enrich_tags_llm: bool      # call the LLM to generate tags (vs. cheap keyword tags)
    summary_max_words: int
    pdf_ocr_fallback: bool      # rasterize image-only PDF pages and OCR via vision_model


# Two tiers. `low` is the economical default: it spends tokens only where
# unavoidable (extracting text from images). `high` adds richer enrichment.
_PROFILES: dict[str, EffortProfile] = {
    "low": EffortProfile(
        name="low",
        vision_model="gpt-4o-mini",
        text_model="claude-haiku-4-5",
        embedding_model="text-embedding-3-small",
        enrich_summary=False,
        enrich_tags_llm=False,
        summary_max_words=0,
        pdf_ocr_fallback=False,
    ),
    "high": EffortProfile(
        name="high",
        vision_model="gpt-4o",
        text_model="claude-sonnet-4-6",
        embedding_model="text-embedding-3-large",
        enrich_summary=True,
        enrich_tags_llm=True,
        summary_max_words=60,
        pdf_ocr_fallback=True,
    ),
}


def default_effort() -> str:
    return os.environ.get("LIBRARIAN_EFFORT", "low").lower()


def get_profile(effort: str | None = None) -> EffortProfile:
    name = (effort or default_effort()).lower()
    if name not in _PROFILES:
        raise ValueError(
            f"Unknown effort '{name}'. Choose from {list(_PROFILES)}."
        )
    return _PROFILES[name]
