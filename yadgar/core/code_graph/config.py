"""code_graph configuration — enable flag, opt-out, cache dir, forward keys.

Car B of the code_graph train (ADR-0162); enable/opt-out mechanism migrated to
the DB-backed runtime config store in Car G4 (ADR-0163).

ENABLE/OPT-OUT (ADR-0163 — SUPERSEDES ADR-0162's env-flag + repo-marker):
``code_graph.enabled`` is a directory-scoped row in the ``runtime_config`` store,
read via a fail-open resolver (default: the stdlib-urllib host client
``runtime_config_client.get``). code_graph is ON BY DEFAULT (opt-out, flipped
2026-07-27 once the digest-renderer PII leak was fixed and the pilot proved out
on a non-Python repo): with no row at all — or the daemon down (fail-open) —
``is_enabled`` resolves ``True``. The store's per-dir → global → default
resolution folds the two opt-out layers into ONE key:

  - global OFF: ``code_graph.enabled=false`` at the global scope, OR
  - per-repo OFF: ``code_graph.enabled=false`` at the repo directory (overrides a
    global ``true``, and overrides the default too).

The old ``CODE_GRAPH_ENABLED`` env var (runtime enable) and the ``.code-graph-disable``
marker FILE are GONE. (``CODE_GRAPH_ENABLED`` survives ONLY in ``cli/setup.py`` as an
INSTALL-time trigger for the codebase-memory-mcp binary — not a runtime flag.)

Resolver injection: ``is_enabled`` / ``is_opted_out`` / ``session_suggest_line`` accept
an optional ``resolver`` callable ``(key, directory, default) -> value``. It defaults
to the host client (``runtime_config_client.get``), which HOST-side callers (the
``yadgar code-graph`` CLI, the stop-hook) use. DAEMON-side callers (``http.py``'s
SessionStart soft-suggest) inject the in-process resolver
(``server.tools._runtime_config.config_get``) so the daemon reads its OWN DB — this
FIXES the container-blindness the env-var mechanism had (the container never saw the
host env). The callable is passed in (never module-imported here) to avoid a
``code_graph`` → ``server.tools`` circular import.

Forward keys (``DIGEST_CHAR_BUDGET``, ``CODE_GRAPH_REFRESH_STOP_INTERVAL``) are
DEFINED here and consumed by Car C (digest render) and Car D (hook cadence).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe

# ── Enable / opt-out (ADR-0163 runtime config store) ──────────────────────────

#: Runtime config store key for the code_graph enable flag. Directory-scoped:
#: a per-repo value overrides the global one (per-dir → global → default).
ENABLED_KEY = "code_graph.enabled"

#: Resolver signature: ``(key, directory, default) -> value``. Both the host
#: client and the in-process daemon resolver satisfy it.
Resolver = Callable[[str, "str | None", Any], Any]


def _default_resolver() -> Resolver:
    """Return the fail-open HOST-side client (``runtime_config_client.get``).

    Imported lazily so the module still imports when the host client's stdlib-only
    deps are unavailable, and so tests can monkeypatch the client symbol.
    """
    from yadgar.core.runtime_config_client import get as rc_get

    return rc_get


@observe(tier="stage")
def is_enabled(directory: str | None = None, resolver: Resolver | None = None) -> bool:
    """Return True when ``code_graph.enabled`` resolves truthy for ``directory``.

    Reads the runtime config store (ADR-0163) via ``resolver`` (default: the
    fail-open host client). Per-dir override → global fallback → ``True`` default
    is handled by the resolver, so a global ``false`` (or a per-repo ``false``
    override) is required to disable — ON by default (opt-out). Fail-open:
    daemon down / any error → the ``True`` default → code_graph active (same as
    "nothing configured" — a downed daemon is not a signal to opt out).
    """
    r = resolver or _default_resolver()
    return bool(r(ENABLED_KEY, directory, True))


@observe(tier="stage")
def is_opted_out(repo_path: str | Path, resolver: Resolver | None = None) -> bool:
    """Return True when code_graph must skip for ``repo_path``.

    Opted out when ``code_graph.enabled`` does NOT resolve truthy for the repo dir
    — i.e. an explicit ``false`` at the global scope, or a per-repo ``false``
    override (the store's per-dir resolution replaces the old
    ``.code-graph-disable`` marker). Absent any row, code_graph is NOT opted out
    (on by default).
    """
    return not is_enabled(str(repo_path), resolver=resolver)


# ── Cache dir (yadgar-owned; keeps SQLite out of the user tree) ───────────────


@observe(tier="stage")
def cache_dir() -> Path:
    """Return the yadgar-owned cache dir for codebase-memory-mcp SQLite state.

    Lives under yadgar's ``CACHE_DIR`` so indexing never pollutes the user's
    repo tree (``CBM_CACHE_DIR`` is pointed here by the runner).

    Resolved lazily (``CACHE_DIR`` is a PEP-562 lazy attribute keyed off ``HOME``
    / ``XDG_CACHE_HOME``) so it tracks the current environment rather than a
    value frozen at import time.
    """
    from yadgar._shared.paths.paths import CACHE_DIR

    return CACHE_DIR / "code_graph"


# ── Digest budget (Car C) ─────────────────────────────────────────────────────

#: Max chars for the rendered architecture digest written to a memory block.
#: Block hard cap is 8000; target ~2000.
DIGEST_CHAR_BUDGET = 2000

# ── Stop-hook cadence (Car D) ─────────────────────────────────────────────────

#: Backward-compat module constant mirroring the shared-config knob
#: ``CODE_GRAPH_REFRESH_STOP_INTERVAL`` (default 200). The stop-hook reads the
#: SHARED-config value via ``get_settings()`` — this constant exists only so
#: the Car-B forward key stays importable and there is ONE default (200)
#: across the codebase.
CODE_GRAPH_REFRESH_STOP_INTERVAL = 200


# ── SessionStart soft-suggest predicate (Car D) ───────────────────────────────


@observe(tier="stage")
def session_suggest_line(
    directory: str | Path,
    blocks: list[dict],
    resolver: Resolver | None = None,
) -> str | None:
    """Return a one-line SessionStart suggestion, or None when none is warranted.

    Car D soft-suggest (NOTHING forced): when code_graph is active for ``directory``
    (``is_opted_out`` False — which folds in the global ``code_graph.enabled`` AND a
    per-repo override, resolved from the runtime config store) AND there is NO
    ``code_graph`` memory block for ``directory`` yet, return a one-line nudge to
    build a digest.

    When a ``code_graph`` block already exists it is already injected at SessionStart
    (``render_blocks_section``) → return None (no suggestion). When opted out →
    return None. Never auto-runs anything.

    ``blocks`` is the ``list_blocks(scope=None, directory=cwd)`` result already
    fetched by the caller; each entry is a dict with a ``name`` key.

    Container note (ADR-0163 FIX): this predicate runs DAEMON-side (``http.py``). The
    caller injects the in-process resolver (``config_get``) so the daemon reads the
    flag from its OWN DB — the container-blindness of the old env-var mechanism (the
    container never saw the host ``CODE_GRAPH_ENABLED``) is fixed. With no injected
    resolver it falls back to the host client, which fail-opens to enabled (the
    same on-by-default behavior as ``is_enabled``).
    """
    if is_opted_out(directory, resolver=resolver):
        return None
    if any(b.get("name") == "code_graph" for b in blocks):
        return None
    repo = Path(str(directory)).name or str(directory)
    return (
        f"[yadgar] No code_graph digest for {repo} — run "
        "`yadgar code-graph refresh` to build one "
        "(`yadgar code-graph query <repo>` for live drill-down)."
    )
