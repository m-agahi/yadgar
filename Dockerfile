# ── core (prod) ────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS prod
WORKDIR /app
COPY . /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
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

CMD ["/entrypoint.sh"]
LABEL version="4.4.6"

# ── dev ───────────────────────────────────────────────────────────────────────
FROM prod AS dev
RUN pip install --no-cache-dir -e "/app[dev]"
