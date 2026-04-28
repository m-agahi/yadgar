# ── prod ──────────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS prod
ARG TARGETARCH
WORKDIR /app
COPY . /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
# torch: use PyTorch CPU wheel index for amd64; fall back to PyPI for arm64
# (whl/cpu index only carries x86_64 wheels).
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$TARGETARCH" = "arm64" ]; then \
        pip install torch; \
    else \
        pip install torch --index-url https://download.pytorch.org/whl/cpu; \
    fi && \
    pip install /app
# SurrealDB server binary — version decoupled from Python client (now httpx-based).
COPY --from=surrealdb/surrealdb:v2.6.5 /surreal /usr/local/bin/surreal
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8765 42069
VOLUME /data
ENV YADGAR_HOST=0.0.0.0 \
    YADGAR_PORT=8765 \
    YADGAR_DB_PATH=/data/surreal_db \
    YADGAR_DB_URL=http://127.0.0.1:8000
CMD ["/entrypoint.sh"]

# ── dev ───────────────────────────────────────────────────────────────────────
FROM prod AS dev
# Reinstall as editable with dev deps (pytest, ruff, etc.)
# Source is bind-mounted at runtime: -v /host/repo:/app
# Embedding model cache: -v ~/.cache/huggingface:/root/.cache/huggingface
RUN pip install --no-cache-dir -e "/app[dev]"
