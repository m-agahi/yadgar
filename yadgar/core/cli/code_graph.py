"""code-graph subcommand — host-side codebase-memory-mcp CLI (Car B, ADR-0162).

Subcommands:
  yadgar code-graph index   <repo>                 # index latest origin/<default>
  yadgar code-graph query   <repo> "<cypher>"      # ephemeral, capped drill-down
  yadgar code-graph refresh <repo>                 # index → render digest → EMIT block payload

THE HARD CONSTRAINT: ``index``/``refresh`` index the latest ``origin/<default>``
in a temp worktree, NEVER the working tree (see ``core.code_graph.default_branch``).

``refresh`` is the C→D seam (Car C): it indexes, fetches ``get_architecture`` +
endpoints, renders a digest, and EMITS the block payload
``{"block_name","directory","content","chars","skipped"}`` as JSON.  It does NOT
write the memory block — that is Claude-in-the-loop via Car D's stop-hook prompt
(Claude calls ``block_update`` with the emitted payload), mirroring repo_wiki's
``wiki_add`` flow.

The binary is HOST-SIDE only; nothing here contacts the MCP daemon.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _die_binary_missing(exc: Exception) -> None:
    """Print a friendly (stacktrace-free) error and exit non-zero."""
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)


def _cmd_index(repo: str, output_json: bool) -> None:
    from yadgar.core.code_graph import default_branch

    result = default_branch.refresh_index(repo)
    if result.get("skipped"):
        print(f"code-graph index skipped: {result.get('reason')}", file=sys.stderr)
    elif result.get("indexed"):
        print(
            f"Indexed {result.get('canonical_root')} "
            f"(origin/{result.get('default_branch')}) → project={result.get('project')}",
            file=sys.stderr,
        )
    if output_json:
        print(json.dumps(result))


def _cmd_query(repo: str, project: str | None, cypher: str, output_json: bool) -> None:
    from yadgar.core.code_graph import runner

    allowed_root = str(Path(repo).resolve())
    # project defaults to the repo basename when not given (passthrough; the exact
    # project identity is a codebase-memory-mcp concern, verified live in Car F).
    proj = project or Path(repo).resolve().name
    out = runner.query_graph(proj, cypher, allowed_root=allowed_root)
    if output_json:
        print(json.dumps(out))
    else:
        rows = out.get("rows", [])
        print(f"{len(rows)} rows (truncated={out.get('truncated', False)})", file=sys.stderr)
        for row in rows:
            print(json.dumps(row))


def _cmd_refresh(repo: str, project: str | None, output_json: bool) -> None:
    """Index → get_architecture → endpoints → render digest → EMIT block payload.

    C→D seam: this EMITS the block payload as JSON but does NOT write the block.
    The block write (``block_update``, secret-gated) is Claude-in-the-loop via
    Car D's stop-hook prompt — mirrors repo_wiki's ``wiki_add`` flow.  Car D wires
    Claude to call ``block_update`` with the ``block_name`` / ``directory`` /
    ``content`` emitted here.

    Secret-gate note (#30): the live block write passes ``gate_or_reject`` (same
    gate as wiki_add).  The digest is a summary (layer/hotspot/endpoint names),
    never raw code — path/identifier FP risk is reduced but real.  No gate here.
    """
    from yadgar.core.code_graph import default_branch, digest, runner

    idx = default_branch.refresh_index(repo)
    if idx.get("skipped"):
        print(f"code-graph refresh skipped: {idx.get('reason')}", file=sys.stderr)
        if output_json:
            # skip signal — Car D branches on "skipped" and does not write a block.
            print(
                json.dumps(
                    {"block_name": "code_graph", "skipped": True, "reason": idx.get("reason")}
                )
            )
        return

    allowed_root = str(Path(repo).resolve())
    proj = project or idx.get("project") or Path(repo).resolve().name
    arch = runner.get_architecture(proj, allowed_root=allowed_root)
    endpoints = runner.fetch_endpoints(proj, allowed_root=allowed_root)

    identity = {
        "canonical_root": idx.get("canonical_root"),
        "subdir": idx.get("subdir", ""),
    }
    payload = digest.build_block_payload(arch, endpoints, identity)

    print(
        f"Refreshed {idx.get('canonical_root')} → digest rendered "
        f"({payload['chars']} chars). Car D → Claude calls block_update.",
        file=sys.stderr,
    )
    if output_json:
        # C→D seam: emit the block payload; Car D's hook prompt → Claude calls
        # block_update(name=block_name, content=content, directory=directory).
        print(json.dumps(payload))
    else:
        print(payload["content"])


def cmd_code_graph(args) -> None:
    """Dispatch the code-graph subcommand."""
    from yadgar.core.code_graph.runner import CodeGraphError

    repo = str(Path(args.repo or ".").resolve())
    if not Path(repo).is_dir():
        print(f"ERROR: not a directory: {repo}", file=sys.stderr)
        sys.exit(1)

    output_json = getattr(args, "json", False)
    cg_command = getattr(args, "cg_command", None)

    try:
        if cg_command == "index":
            _cmd_index(repo, output_json)
        elif cg_command == "query":
            _cmd_query(repo, getattr(args, "project", None), args.cypher, output_json)
        elif cg_command == "refresh":
            _cmd_refresh(repo, getattr(args, "project", None), output_json)
        else:
            print("ERROR: specify a subcommand: index | query | refresh", file=sys.stderr)
            sys.exit(1)
    except CodeGraphError as exc:
        # Binary-absent (CodeGraphBinaryMissing) and other runner failures →
        # friendly typed error, never a stacktrace.
        _die_binary_missing(exc)


def register(subparsers) -> None:
    """Register the 'code-graph' subcommand with nested index|query|refresh."""
    p = subparsers.add_parser(
        "code-graph",
        help="Host-side code-structure indexing/query via codebase-memory-mcp (ADR-0162)",
    )
    cg = p.add_subparsers(dest="cg_command")

    p_index = cg.add_parser(
        "index",
        help="Index the latest origin/<default-branch> (never the working tree)",
    )
    p_index.add_argument("repo", nargs="?", default=".", help="Repository root path")
    p_index.add_argument("--json", action="store_true", help="Emit JSON result to stdout")

    p_query = cg.add_parser(
        "query",
        help="Run a capped Cypher query against the indexed project (ephemeral)",
    )
    p_query.add_argument("repo", help="Repository root path")
    p_query.add_argument("cypher", help="Cypher query string")
    p_query.add_argument(
        "--project",
        default=None,
        help="Project name (defaults to repo basename; passthrough to the indexer)",
    )
    p_query.add_argument("--json", action="store_true", help="Emit JSON result to stdout")

    p_refresh = cg.add_parser(
        "refresh",
        help="Index + fetch architecture JSON (Car C renders the digest + block write)",
    )
    p_refresh.add_argument("repo", nargs="?", default=".", help="Repository root path")
    p_refresh.add_argument(
        "--project",
        default=None,
        help="Project name (defaults to index result / repo basename)",
    )
    p_refresh.add_argument("--json", action="store_true", help="Emit JSON result to stdout")

    p.set_defaults(func=cmd_code_graph)
