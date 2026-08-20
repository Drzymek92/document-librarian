"""Dense embeddings for semantic search, via any OpenAI-compatible endpoint.

Mirrors llm_client.py's environment handling (LLM_API_KEY / LLM_BASE_URL, plus
config/.env), so a deployment configures one gateway and both the chat and the
embedding paths follow it.

One embedding model is locked per catalog (see db.ensure_embed_model): a 1536-d
vector and a 3072-d vector cannot be cosine-compared, so mixing models inside one
catalog silently corrupts ranking rather than raising.
"""
import os
from pathlib import Path

from scripts.logger import get_logger

logger = get_logger("embed")

# Load config/.env if python-dotenv is available (no-op if absent or file missing).
try:
    from dotenv import load_dotenv

    load_dotenv(Path("config") / ".env")
except Exception:
    pass

# Embedding endpoints accept many inputs per call; batching cuts round-trips by
# ~64x on a full backfill. An explicit timeout and a bounded retry matter more
# here than for chat: a stalled call would hold the catalog's write lock open.
EMBED_BATCH = 64
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3


def _api_key() -> str:
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM API key not set. Add LLM_API_KEY to config/.env "
            "(see config/.env.example)."
        )
    return key


def get_embedder(model: str):
    from langchain_openai import OpenAIEmbeddings

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "LLM_BASE_URL not set. Add it to config/.env (see config/.env.example)."
        )
    return OpenAIEmbeddings(
        model=model,
        base_url=base_url,
        api_key=_api_key(),
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
    )


def embed_texts(texts: list[str], model: str) -> list[list[float]]:
    """Embed many texts in batches. Returns one vector per input, order preserved."""
    if not texts:
        return []
    embedder = get_embedder(model)
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(embedder.embed_documents(texts[i : i + EMBED_BATCH]))
    logger.info(f"Embedded {len(out)} texts with {model}")
    return out


def embed_query(text: str, model: str) -> list[float]:
    """Embed a single search query. Kept separate from embed_texts because some
    providers use a different prompt prefix for queries than for documents."""
    return get_embedder(model).embed_query(text)
