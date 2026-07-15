"""Trusted per-directory git-context — core read-through cache (Car 0).

The canonical-write decision (§0.4) hangs on two TRUSTED per-directory facts:

  * ``gitness``        — is this directory a git work-tree?
  * ``default_branch`` — the repo default branch (``None`` when non-git).

They are computed HOST-SIDE by the SessionStart context hook (the container
cannot see the host ``.git``), POSTed to the SessionStart context endpoint (the
SOLE set-channel — no model-callable tool writes them), and persisted DURABLY in
the DB keyed by directory (restart-safe; ``upsert_dir_branch_context``).

Every wiki write needs the directory's ``gitness`` to decide the branch. Reading
the durable store from the backend on every write would add a core↔backend round
trip to the hot path, so this module wraps the durable store in a read-through
cache on the shared core ``Cache`` engine:

  * namespace ``dir_branch_context`` (registered in ``cache._NAMESPACE_WEIGHTS``).
  * ``Manual`` invalidation — ``invalidate(directory)`` fired ON the SessionStart
    upsert so a gitness change (rare) does not keep a stale value.
  * ``TTL`` backstop — for the git-init-mid-life edge (a non-git dir becomes git,
    or vice versa, without a fresh SessionStart).

Fail-safe (§0.3): a backend error is surfaced to the caller as an *error*, never
silently coerced to canonical. The write path treats an error / unknown-directory
result as "require branch_hint" (flow 4). A cache MISS is not "unknown directory"
— only an empty backend result (``found=False``) is; a cold cache on a known dir
triggers one backend read and fills.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# TTL backstop for the git-init-mid-life edge (module constant, NOT a Settings
# field — no I25 three-way-sync burden; mirrors _WIKI_READ_CACHE_TTL).
_DIR_BRANCH_CACHE_TTL = 300.0

_cache: Any = None


@observe(tier="stage", metric="tools._dir_branch._make_cache")
def _make_dir_branch_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("dir_branch_context", total)
    return Cache(
        name="dir_branch_context",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget)
        invalidation=TTL(_DIR_BRANCH_CACHE_TTL),  # Manual invalidate + TTL backstop
        deep_copy=True,  # returned dict is caller-owned / mutable
        obs_tier="cold",  # low call rate
    )


@observe(tier="hot", metric="tools._dir_branch._get_cache")
def _get_cache():
    """Return the singleton cache (built once at import).

    The core ``Cache`` self-registers into a module-global registry and raises on
    a duplicate name, so the instance must be built exactly once (mirrors the
    module-level ``_wiki_read_cache`` etc.). If a prior instance is already
    registered (e.g. after a module reload), reuse it rather than rebuild.
    """
    global _cache
    if _cache is None:
        from yadgar.core.cache.cache import _REGISTRY  # noqa: PLC0415

        existing = _REGISTRY.get("dir_branch_context")
        _cache = existing if existing is not None else _make_dir_branch_cache()
    return _cache


@observe(tier="stage", metric="tools._dir_branch.invalidate")
def invalidate(directory: str) -> None:
    """Drop the cached context for *directory* (Manual bust on SessionStart upsert)."""
    try:
        _get_cache().invalidate(directory)
    except Exception:  # noqa: BLE001 — invalidation must never break the write path
        logger.debug("dir_branch_context invalidate failed for %s", directory, exc_info=True)


@observe(tier="hot", metric="tools._dir_branch.get_context")
def get_context(directory: str | None) -> dict:
    """Return the TRUSTED git-context for *directory* via the read-through cache.

    Return shape (always a dict — never raises to the caller):
      {"found": True,  "gitness": bool, "default_branch": str | None}  — known dir
      {"found": False, "gitness": False, "default_branch": None}       — unknown dir
      {"error": True,  "found": False, ...}                             — backend down

    ``found=False`` (no error) = §0.4 flow 4 "unknown directory". ``error=True`` =
    fail-safe: the write path treats it as flow 4 too (require branch_hint), NEVER
    as canonical. A cache MISS silently triggers one backend read then fills — it
    is not surfaced as unknown.
    """
    _dir = (directory or "").strip() or None
    if _dir is None:
        return {"found": False, "gitness": False, "default_branch": None}

    cache = _get_cache()
    cached = cache.get(_dir)
    if cached is not None:
        return cached

    # Miss → ONE backend read (ADR-0078: core never reads the DB directly).
    try:
        from yadgar.core.server.tools._forward import _forward_admin  # noqa: PLC0415

        result = _forward_admin("get_dir_branch_context", {"directory": _dir})
    except Exception as exc:  # noqa: BLE001 — surface as fail-safe error, do NOT cache
        logger.warning("dir_branch_context backend read failed for %s: %s", _dir, exc)
        return {"error": True, "found": False, "gitness": False, "default_branch": None}

    ctx = {
        "found": bool(result.get("found")),
        "gitness": bool(result.get("gitness")),
        "default_branch": result.get("default_branch"),
    }
    # Cache both found + not-found (a known-unknown is a real answer worth caching;
    # the SessionStart upsert Manual-invalidates when the dir becomes known).
    cache.put(_dir, ctx)
    return ctx
