# ── core (prod) ────────────────────────────────────────────────────────────────
# LAYER ORDER IS LOAD-BEARING (task 331). `COPY . /app` used to sit at line 4,
# above the apt install and the pip install, so ANY commit that touched ANY file
# in the tree rebuilt both. Everything above the `COPY . /app` below therefore
# depends on NOTHING from the repo except pyproject.toml + README.md.
FROM python:3.14-slim-trixie AS prod
WORKDIR /app
# curl is needed for HEALTHCHECK. Unpinned — base image is pinned to trixie
# so apt resolves to a single deterministic version per build.
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
# ── Dependency layer: the manifests only, never the source tree ───────────────
# pyproject.toml declares the dependency set; README.md rides along because
# `readme = "README.md"` makes it a metadata build input — hatchling fails
# without it. The stub `yadgar/` package is what lets pip resolve and install
# the DEPENDENCIES of a project whose source is not here yet: hatchling's
# `packages = ["yadgar"]` is satisfied by an empty __init__.py (verified), so
# pip builds a placeholder wheel, reads its Requires-Dist and installs the real
# third-party closure. The actual package is installed further down, after
# `COPY . /app`, with --no-deps. No extras here: core is the base install (the
# [ml] / [sql] extras are backend-only — see pyproject.toml).
COPY pyproject.toml README.md /app/
RUN mkdir -p /app/yadgar && touch /app/yadgar/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /app
# License artifacts, at a conventional path in the image. This image bundles no
# third-party binary of its own (apt packages carry their own
# /usr/share/doc/<pkg>/copyright), but it is still a distributed copy of an
# Apache-2.0 work, so LICENSE and NOTICE travel with it.
# Enforced by scripts/check_third_party_licenses.py.
COPY LICENSE NOTICE THIRD-PARTY-LICENSES /usr/share/doc/yadgar/
COPY third-party /usr/share/doc/yadgar/third-party
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8765
ENV PYTHONUNBUFFERED=1 \
    YADGAR_HOST=0.0.0.0 \
    YADGAR_PORT=8765 \
    YADGAR_DB_URL=http://yadgar-backend:8000 \
    YADGAR_EMBED_URL=http://yadgar-backend:8001 \
    YADGAR_DATA_DIR=/data
# useradd sits ABOVE the source copy on purpose (task 331): it is a real
# filesystem layer, so leaving it below would make a source-only edit rebuild it
# too. `USER 1001` below is metadata and costs nothing wherever it sits.
RUN useradd -r -m -u 1001 -s /sbin/nologin yadgar
# ── The source tree, and the package installed from it: LAST (task 331) ───────
# --no-deps because the third-party closure was already resolved and installed
# from the manifests above; --force-reinstall replaces the placeholder wheel
# from the dependency layer with the real package.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --force-reinstall /app
USER 1001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -f http://localhost:8765/health/live || exit 1
CMD ["/entrypoint.sh"]
LABEL version="5.0.0"

# ── dev ───────────────────────────────────────────────────────────────────────
FROM prod AS dev
RUN pip install --no-cache-dir -e "/app[dev]"
