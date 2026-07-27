"""code_graph — host-side multi-language code-structure via codebase-memory-mcp.

Car B of the code_graph train (ADR-0162).

Thin host-side adapter around the codebase-memory-mcp static binary (installed
by Car A).  Provides:

- ``config``          — enable flag, opt-out, cache dir, Car C/D forward keys.
- ``runner``          — subprocess wrapper (stdin args, stderr strip, row/byte cap).
- ``default_branch``  — index latest origin/<default> in a temp worktree, NEVER
                        the working tree (THE HARD CONSTRAINT).

The binary + indexer runs are HOST-SIDE ONLY — the MCP core daemon is a
read-only container that cannot reach host repos.  Nothing here touches the
daemon; the digest render + memory-block write is Car C.

Enable mechanism (ADR-0163 — SUPERSEDES ADR-0162's env flag + repo marker)
--------------------------------------------------------------------------
``config.is_enabled(directory)`` / ``is_opted_out`` read the ``code_graph.enabled``
row from the DB-backed ``runtime_config`` store (dir-scoped: per-repo overrides
global). The old ``CODE_GRAPH_ENABLED`` env var (runtime enable) and the
``.code-graph-disable`` repo-marker FILE are GONE. ``CODE_GRAPH_ENABLED`` survives
ONLY in ``cli/setup.py`` as an INSTALL trigger for the host-side binary.

The SessionStart soft-suggest's former CONTAINER-BLINDNESS is FIXED: ``http.py``
injects the in-process daemon resolver (``config_get``), so the daemon reads the
flag from its OWN DB rather than a host env var it never saw.

Known limitations / follow-ups
------------------------------
- **Digest-actually-used pilot-gate (ADR-0162 risk #1) — SATISFIED 2026-07-27.**
  The core value bet was that the injected digest gets read + acted on
  (repo-wiki's recall pages were not). The pilot proved out live on ≥1
  non-Python repo (Java/Go/PHP) + on yadgar itself, and the digest-layers
  PII/URL-literal leak was fixed first. ``code_graph.enabled`` now DEFAULTS TO
  TRUE (opt-out, fail-open to enabled): absent any row, or with the daemon
  down, ``is_enabled``/``is_opted_out`` resolve as ON. Opt out per-repo or
  globally via ``config_set("code_graph.enabled", false, scope=...)``.
"""

from __future__ import annotations
