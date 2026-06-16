import re
from collections import Counter

from scripts import config, db
from scripts.logger import get_logger

logger = get_logger("enrich")

# Tiny stopword list for cheap (no-LLM) keyword tags on the low-effort tier.
_STOP = set(
    "the a an and or of to in for on with at by from is are was were be been being "
    "this that these those it its as not no but if then than there here we you they "
    "i he she him her our your their his out into over under more most some any all "
    "can will would should could may might do does did has have had per via etc".split()
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")

_SAMPLE_CHARS = 6000  # cap the text sent to the LLM for enrichment


def cheap_tags(filename: str, source_type: str, text: str, k: int = 6) -> list[str]:
    tokens = [w.lower() for w in _WORD.findall(text)]
    tokens = [t for t in tokens if t not in _STOP and not t.isdigit()]
    common = [w for w, _ in Counter(tokens).most_common(k)]
    return common


def _sample(text: str) -> str:
    return text[:_SAMPLE_CHARS]


def _llm_summary(text: str, profile: config.EffortProfile) -> str:
    from scripts.prompt_compressor import compressed_call

    prompt = (
        f"Summarize the following document in at most {profile.summary_max_words} words. "
        f"Focus on what it is about and its key content:\n\n{_sample(text)}"
    )
    return compressed_call(
        prompt, system="Output only the summary as plain text.", model=profile.text_model
    ).strip()


def _llm_tags(text: str, profile: config.EffortProfile) -> list[str]:
    from scripts.llm_client import llm_json
    from scripts.prompt_compressor import compress, print_report

    base = (
        "Extract 3-8 short, lowercase, topical tags describing this document's subject "
        f"matter:\n\n{_sample(text)}"
    )
    r = compress(base)
    print_report(r)
    data = llm_json(
        r["final"],
        system='Return ONLY JSON of the form {"tags": ["tag1", "tag2"]}.',
        model=profile.text_model,
    )
    tags = data.get("tags", []) if isinstance(data, dict) else []
    return [str(t).lower().strip() for t in tags if str(t).strip()][:8]


def enrich_document(
    con,
    doc_id: str,
    filename: str,
    source_type: str,
    text: str,
    effort: str | None = None,
) -> dict:
    profile = config.get_profile(effort)
    summary = None
    if profile.enrich_summary and text.strip():
        try:
            summary = _llm_summary(text, profile)
            db.update_summary(con, doc_id, summary)
        except Exception:
            logger.exception(f"Summary enrichment failed for {filename}")

    if profile.enrich_tags_llm and text.strip():
        try:
            tags = _llm_tags(text, profile)
        except Exception:
            logger.exception(f"LLM tag enrichment failed for {filename}; using cheap tags")
            tags = cheap_tags(filename, source_type, text)
    else:
        tags = cheap_tags(filename, source_type, text)

    if tags:
        db.set_tags(con, doc_id, tags)
    return {"summary": summary, "tags": tags}
