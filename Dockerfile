FROM python:3.12-slim

WORKDIR /app

# Dependencies in their own layer, before the source: application code changes far
# more often than requirements.txt, so pip install stays cached across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/

# Nothing here needs root.
RUN useradd --create-home --uid 10001 librarian && chown -R librarian:librarian /app
USER librarian

# No credentials are baked in: the LLM gateway is supplied at run time, e.g.
#   docker run --rm -e LLM_API_KEY -e LLM_BASE_URL -e LLM_MODEL ...
# The catalog is state rather than code, so it is mounted rather than built in:
#   docker run --rm -v /host/catalog:/data document-librarian \
#     python -m scripts.query "your question" --db /data/librarian.duckdb
CMD ["python", "-m", "scripts.query", "--help"]
