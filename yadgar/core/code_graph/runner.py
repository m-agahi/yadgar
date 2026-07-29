"""code_graph runner — subprocess wrapper around codebase-memory-mcp.

Car B of the code_graph train (ADR-0162).

CLI form (verified against the v0.9.0 binary):
    codebase-memory-mcp cli <tool> [json]

The raw-JSON positional is DEPRECATED — we pass the args JSON on **stdin**
instead (sidesteps guessing a ``--args-file`` flag name that cannot be verified
while the binary is mocked; a wrong guess is a one-line fix here, not a rewrite).
Everything argv/stdin/env-shaped is isolated in ``_run_tool`` so Car F live-smoke
can correct a single function.

stderr carries a ``level=info msg=mem.init ...`` log line — ignored; only stdout
is parsed as JSON.

Containment: ``CBM_ALLOWED_ROOT`` is ALWAYS exported (hard perimeter — refuses
any repo_path resolving outside it via symlink/``..``).  ``CBM_CACHE_DIR`` points
at a yadgar-owned dir so SQLite never lands in the user tree.

Binary resolution: ``shutil.which`` OR Car A's install path (``~/.local/bin`` —
which is invisible to ``which`` when not on PATH).  Absent → ``CodeGraphBinaryMissing``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar.core.code_graph import config
from yadgar.core.install.codebase_memory_mcp import BINARY_NAME, _default_bin_dir

# ── Caps (db_inspect 500-row precedent) ───────────────────────────────────────

#: Max rows returned from a Cypher query (hard ceiling).
MAX_QUERY_ROWS = 500

#: Max serialized bytes of returned rows (defensive against few-but-huge rows).
MAX_QUERY_BYTES = 256_000

#: Max endpoint rows pulled by the route_method query (Car C digest).  Small — a
#: digest lists a handful of routes, not the whole surface.
MAX_ENDPOINT_ROWS = 50

#: Cypher for HTTP endpoints (Car C).  Endpoints come from ``Method.route_method``
#: ONLY (Java ✅); ``Route`` nodes are URL-literal NOISE and are never queried.
#: PHP/Go framework routes are not parsed → 0 rows → digest emits "(none extracted)".
_ENDPOINT_CYPHER = (
    "MATCH (m:Method) WHERE m.route_method <> '' "
    "RETURN m.route_method, m.route_path, m.name LIMIT {cap}"
)


class CodeGraphError(RuntimeError):
    """Base error for code_graph runner failures."""


class CodeGraphBinaryMissing(CodeGraphError):
    """The codebase-memory-mcp binary could not be located.

    Raised (not a stacktrace) so callers can surface a friendly install hint:
    ``yadgar setup`` (which installs the binary by default — task:0082).
    """


# ── Binary resolution ─────────────────────────────────────────────────────────


@observe(tier="stage")
def resolve_binary() -> str | None:
    """Return the path to the codebase-memory-mcp binary, or None if absent.

    ``shutil.which`` first; then Car A's install dir (``~/.local/bin``), which
    ``which`` misses when the dir is not on PATH.
    """
    found = shutil.which(BINARY_NAME)
    if found:
        return found
    candidate = _default_bin_dir() / BINARY_NAME
    if candidate.exists():
        return str(candidate)
    return None


@observe(tier="stage")
def _require_binary() -> str:
    binary = resolve_binary()
    if binary is None:
        raise CodeGraphBinaryMissing(
            f"{BINARY_NAME} not found. Install it host-side with "
            f"`yadgar setup` (installs it by default) "
            f"(or ensure ~/.local/bin is on PATH)."
        )
    return binary


# ── Core invocation ───────────────────────────────────────────────────────────


@observe(tier="stage")
def _build_env(allowed_root: str) -> dict[str, str]:
    """Return the subprocess env with the containment perimeter set.

    CBM_ALLOWED_ROOT = the indexed path (hard perimeter).
    CBM_CACHE_DIR    = yadgar-owned cache (SQLite out of the user tree).
    """
    env = dict(os.environ)
    env["CBM_ALLOWED_ROOT"] = allowed_root
    cache = config.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    env["CBM_CACHE_DIR"] = str(cache)
    return env


@observe(tier="boundary")
def _run_tool(tool: str, args: dict[str, Any], allowed_root: str) -> dict[str, Any]:
    """Invoke ``codebase-memory-mcp cli <tool>`` with ``args`` JSON on stdin.

    THE single argv/stdin/env construction site — Car F live-smoke corrects here.

    Returns the parsed JSON stdout.  Raises ``CodeGraphBinaryMissing`` when the
    binary is absent, ``CodeGraphError`` on non-zero exit or unparseable stdout.
    """
    binary = _require_binary()
    env = _build_env(allowed_root)

    proc = subprocess.run(  # noqa: S603 — binary path resolved via which/install dir
        [binary, "cli", tool],
        input=json.dumps(args),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise CodeGraphError(
            f"{BINARY_NAME} cli {tool} exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )

    # stdout is JSON; the `level=info msg=mem.init` log line is on stderr → ignored.
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CodeGraphError(
            f"{BINARY_NAME} cli {tool} returned non-JSON stdout: {stdout[:200]!r}"
        ) from exc


# ── Wrapped tools ─────────────────────────────────────────────────────────────


@observe(tier="stage")
def index_repository(
    repo_path: str, *, allowed_root: str | None = None, name: str | None = None
) -> dict[str, Any]:
    """Index ``repo_path``.  CBM_ALLOWED_ROOT defaults to the indexed path.

    ``allowed_root`` override lets the default-branch flow point the perimeter at
    a temp worktree while ``repo_path`` is that same temp path.

    ``name`` maps to the indexer's ``--name`` ("override the derived project
    name") and is echoed back verbatim in the result's ``project``.  Without it
    the indexer derives the name from the indexed PATH — which, on the
    default-branch flow, is a random ``tempfile.mkdtemp`` worktree, so every
    refresh minted a fresh throwaway project and no later run could ever address
    the cached index (task:0067).  Callers pass a deterministic name keyed to
    canonical_root + subdir, which is ADR-0162's stated project key.  Omitted ⇒
    the arg dict is byte-identical to before.
    """
    root = allowed_root or repo_path
    args: dict[str, Any] = {"repo_path": repo_path}
    if name:
        args["name"] = name
    return _run_tool("index_repository", args, allowed_root=root)


@observe(tier="stage")
def get_architecture(
    project: str, *, aspects: list[str] | None = None, allowed_root: str
) -> dict[str, Any]:
    """Return the architecture aspects for ``project`` (default ``["all"]``).

    ``project`` is a passthrough (its exact format is a codebase-memory-mcp
    concern verified live in Car F).
    """
    return _run_tool(
        "get_architecture",
        {"project": project, "aspects": aspects or ["all"]},
        allowed_root=allowed_root,
    )


@observe(tier="stage")
def query_graph(project: str, query: str, *, allowed_root: str) -> dict[str, Any]:
    """Run a Cypher ``query`` against ``project``; cap returned rows + bytes.

    db_inspect 500-row precedent: rows beyond ``MAX_QUERY_ROWS`` or bytes beyond
    ``MAX_QUERY_BYTES`` are dropped and ``truncated: True`` is set.
    """
    out = _run_tool(
        "query_graph",
        {"project": project, "query": query},
        allowed_root=allowed_root,
    )
    return _cap_rows(out)


@observe(tier="stage")
def fetch_endpoints(
    project: str, *, allowed_root: str, cap: int = MAX_ENDPOINT_ROWS
) -> list[dict[str, Any]]:
    """Return HTTP endpoint rows for ``project`` via the route_method Cypher.

    Endpoints are ``Method.route_method`` rows ONLY (Car C digest source) — NOT
    ``routes[]`` from get_architecture, NOT ``Route`` nodes.  0 rows (PHP/Go
    framework routes not parsed) → ``[]`` → the digest emits "(none extracted)".

    The row-key shape (``m.route_method`` vs bare ``route_method``) is parsed
    tolerantly in ``digest._extract_endpoint`` (single Car-F-correctable site).
    """
    out = query_graph(project, _ENDPOINT_CYPHER.format(cap=cap), allowed_root=allowed_root)
    rows = out.get("rows")
    return rows if isinstance(rows, list) else []


@observe(tier="stage")
def list_projects(*, allowed_root: str) -> dict[str, Any]:
    """List indexed projects (git metadata: canonical_root, head_sha, ...)."""
    return _run_tool("list_projects", {}, allowed_root=allowed_root)


@observe(tier="stage")
def detect_changes(project: str, *, allowed_root: str) -> dict[str, Any]:
    """Return freshness/change info for ``project`` (Car C staleness authority)."""
    return _run_tool("detect_changes", {"project": project}, allowed_root=allowed_root)


# ── Row/byte cap ──────────────────────────────────────────────────────────────


@observe(tier="stage")
def _cap_rows(out: dict[str, Any]) -> dict[str, Any]:
    """Cap ``out['rows']`` to MAX_QUERY_ROWS and MAX_QUERY_BYTES.

    Sets ``truncated: True`` when either cap fires.  Non-dict / rowless output is
    returned unchanged.
    """
    rows = out.get("rows")
    if not isinstance(rows, list):
        return out

    truncated = False
    if len(rows) > MAX_QUERY_ROWS:
        rows = rows[:MAX_QUERY_ROWS]
        truncated = True

    # Byte cap: trim from the tail until the serialized rows fit.
    while rows and len(json.dumps(rows)) > MAX_QUERY_BYTES:
        rows = rows[: max(1, len(rows) // 2)]
        truncated = True
        if len(rows) == 1:
            break

    out["rows"] = rows
    if truncated:
        out["truncated"] = True
    return out
