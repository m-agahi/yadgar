# ── prod ──────────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS prod
WORKDIR /app
COPY . /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir /app
# SurrealDB v2.x server binary — same major version as Python surrealdb==2.0.0 client.
# v3.x uses an incompatible surrealkv manifest format (version 0 vs newer).
COPY --from=surrealdb/surrealdb:v2.0.4 /surreal /usr/local/bin/surreal
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8765 42069
VOLUME /data
ENV YADGAR_HOST=0.0.0.0 \
    YADGAR_PORT=8765 \
    YADGAR_DB_PATH=/data/surreal_db \
    YADGAR_DB_URL=ws://127.0.0.1:8000
CMD ["/entrypoint.sh"]

# ── dev ───────────────────────────────────────────────────────────────────────
FROM prod AS dev
# Reinstall as editable with dev deps (pytest, ruff, etc.)
# Source is bind-mounted at runtime: -v /host/repo:/app
# Embedding model cache: -v ~/.cache/huggingface:/root/.cache/huggingface
RUN pip install --no-cache-dir -e "/app[dev]"
