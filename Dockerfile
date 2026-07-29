# ── core (prod) ────────────────────────────────────────────────────────────────
FROM python:3.14-slim-trixie AS prod
WORKDIR /app
COPY . /app
# curl is needed for HEALTHCHECK. Unpinned — base image is pinned to trixie
# so apt resolves to a single deterministic version per build.
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8765
ENV PYTHONUNBUFFERED=1 \
    YADGAR_HOST=0.0.0.0 \
    YADGAR_PORT=8765 \
    YADGAR_DB_URL=http://yadgar-backend:8000 \
    YADGAR_EMBED_URL=http://yadgar-backend:8001 \
    YADGAR_DATA_DIR=/data
RUN useradd -r -m -u 1001 -s /sbin/nologin yadgar
USER 1001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -f http://localhost:8765/health/live || exit 1
CMD ["/entrypoint.sh"]
LABEL version="5.0.0"

# ── dev ───────────────────────────────────────────────────────────────────────
FROM prod AS dev
RUN pip install --no-cache-dir -e "/app[dev]"
