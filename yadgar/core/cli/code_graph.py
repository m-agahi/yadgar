"""code-graph subcommand — host-side codebase-memory-mcp CLI (Car B, ADR-0162).

Subcommands:
  yadgar code-graph install                        # host binary + code_graph.enabled
  yadgar code-graph index   <repo>                 # index latest origin/<default>
  yadgar code-graph query   <repo> "<cypher>"      # ephemeral, capped drill-down
  yadgar code-graph refresh <repo>                 # index → render digest → EMIT block payload

``install`` is the provisioning seam BOTH shell installers call
(``scripts/install/yadgar-setup.sh`` and ``make setup``), neither of which ever
invokes ``yadgar setup`` — which is why they used to leave code_graph enabled
with no binary on disk. It is the ONE subcommand that needs no binary and no
repo, so it short-circuits ahead of the repo resolution in ``cmd_code_graph``.

THE HARD CONSTRAINT: ``index``/``refresh`` index the latest ``origin/<default>``
in a temp worktree, NEVER the working tree (see ``core.code_graph.default_branch``).

``refresh`` is the C→D seam (Car C): it indexes, fetches ``get_architecture`` +
endpoints, renders a digest, and EMITS the block payload
``{"block_name","directory","content","chars","skipped"}`` as JSON.  It does NOT
write the memory block — that is Claude-in-the-loop via Car D's stop-hook prompt
(Claude calls ``block_update``, falling back to ``block_create`` on a
not-found error — there is no block yet on a repo's FIRST-EVER refresh) with
the emitted payload, mirroring repo_wiki's ``wiki_add`` flow.

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
    ``content`` emitted here, falling back to ``block_create`` when
    ``block_update`` 404s not-found (no existing block yet).

    Secret-gate note (#30): the live block write passes ``gate_or_reject`` (same
    gate as wiki_add).  The digest is a summary (layer/hotspot/endpoint names),
    never raw code — path/identifier FP risk is reduced but real.  No gate here.

    Staleness (task:0067): this is the ONLY production producer of the identity
    dict ``digest._stale_line`` reads, so it owns the ``stale`` / ``head_sha``
    keys.  A successful index is never stale.  When the index is SKIPPED for
    ``fetch_failed`` and BOTH guards hold — a cached architecture exists and a
    sha resolved — the cached digest is re-emitted with ``skipped: false`` and a
    ``stale @ <12-char sha>`` marker on line 2, immediately under the header (a
    budget-reserved preamble, so it survives truncation on a digest that fills
    ``DIGEST_CHAR_BUDGET``); otherwise the skip stays bit-for-bit as
    before.  Rationale: a silent skip leaves the previously-written block
    serving an aged digest with no marker at all.
    """
    from yadgar.core.code_graph import default_branch, digest, runner
    from yadgar.core.code_graph.runner import CodeGraphError

    idx = default_branch.refresh_index(repo)
    allowed_root = str(Path(repo).resolve())
    proj = project or idx.get("project") or Path(repo).resolve().name

    arch: dict | None = None
    endpoints: list = []
    stale = False

    if idx.get("skipped"):
        print(f"code-graph refresh skipped: {idx.get('reason')}", file=sys.stderr)

        # task:0067 — a skip that writes NOTHING leaves the previously-written
        # block serving an aged digest with no freshness marker at all, which is
        # the exact failure `stale @ <sha>` exists to prevent. So on the ONE
        # re-render-eligible reason, re-emit the CACHED digest marked stale.
        #
        # `fetch_failed` only, deliberately: `no_remote_or_default_branch` is
        # reached precisely because no `<default>` resolved, so no sha is
        # resolvable by construction and that row could never fire; `opted_out`
        # means the user said no. Both stay bit-for-bit hard skips
        # (BC-CODEGRAPH-7).
        #
        # Doubly guarded — a cached index AND a resolvable sha — so we never emit
        # a payload we cannot honestly stamp. The guards are evaluated BEFORE any
        # runner call, so a hard skip still costs zero subprocesses (that is what
        # keeps the `opted_out` regression test green unmodified).
        if idx.get("reason") == "fetch_failed" and idx.get("head_sha"):
            try:
                arch = runner.get_architecture(proj, allowed_root=allowed_root) or None
                if arch:
                    endpoints = runner.fetch_endpoints(proj, allowed_root=allowed_root)
            except CodeGraphError:
                # The binary is absent/broken. Today this path emits a clean skip
                # JSON and exits 0; the added subprocess must not turn that into
                # an exit-2 with no payload (the hook template's step 1 expects
                # ONE JSON object on stdout). Degrade to the hard skip below.
                arch = None

        if not arch:
            if output_json:
                # skip signal — Car D branches on "skipped" and writes no block.
                print(
                    json.dumps(
                        {"block_name": "code_graph", "skipped": True, "reason": idx.get("reason")}
                    )
                )
            return
        stale = True
    else:
        arch = runner.get_architecture(proj, allowed_root=allowed_root)
        endpoints = runner.fetch_endpoints(proj, allowed_root=allowed_root)

    identity = {
        "canonical_root": idx.get("canonical_root"),
        "subdir": idx.get("subdir", ""),
        # Both keys or neither: `digest._stale_line` AND-guards them, so a
        # producer that sets only one silently renders nothing — that omission is
        # exactly why the marker shipped dead (task:0067).
        "stale": stale,
        "head_sha": idx.get("head_sha"),
    }
    payload = digest.build_block_payload(arch, endpoints, identity)

    if stale:
        print(
            f"code-graph refresh: could not re-index (reason: {idx.get('reason')}) — "
            f"re-emitting the CACHED digest for {idx.get('canonical_root')} marked "
            f"stale @ {str(idx.get('head_sha'))[:12]}, so a reader is never served "
            f"an aged digest that looks fresh.",
            file=sys.stderr,
        )
    else:
        print(f"Refreshed {idx.get('canonical_root')} →", file=sys.stderr, end=" ")

    print(
        f"digest rendered ({payload['chars']} chars). Write the code_graph block "
        f"(create-or-update): try block_update first; on a not-found error "
        f"(no existing block — e.g. the FIRST refresh of this repo), fall "
        f"back to block_create. Mirrors code_graph_refresh_prompt.md's "
        f"stop-hook step.",
        file=sys.stderr,
    )
    if output_json:
        # C→D seam: emit the block payload; Car D's hook prompt → Claude calls
        # block_update(name=block_name, content=content, directory=directory),
        # falling back to block_create on a not-found error (first refresh).
        print(json.dumps(payload))
    else:
        print(payload["content"])


def _cmd_install(opt_out: bool) -> None:
    """Provision the host binary + the ``code_graph.enabled`` flag, coherently.

    The entry point BOTH shell installers call. ``scripts/install/yadgar-setup.sh``
    and ``make setup`` run their own building-block chains and never invoke
    ``yadgar setup``, so the default-on provisioning that lives there was
    unreachable from the surfaces most users actually install through — they
    produced machines where ``code_graph.enabled`` resolved true (ADR-0163: no
    row → default true) with no ``codebase-memory-mcp`` binary on disk.

    Never raises and never exits non-zero: the callers run under
    ``set -euo pipefail`` and a failed optional provision must not abort an
    otherwise-good install. ``provision_code_graph`` already swallows a failed
    download and fails soft on the persist.
    """
    from yadgar.core.install.code_graph_provision import provision_code_graph

    provision_code_graph(opt_out=opt_out)


def cmd_code_graph(args) -> None:
    """Dispatch the code-graph subcommand."""
    from yadgar.core.code_graph.runner import CodeGraphError

    # `install` short-circuits AHEAD of the repo resolution below: it is a
    # machine-global operation with no `repo` attribute, so falling through
    # would raise a raw AttributeError. It also deliberately never reaches the
    # runner — resolving the binary would `_die_binary_missing` (exit 2) exactly
    # when there is no binary yet, which is the case `install` exists to fix.
    if getattr(args, "cg_command", None) == "install":
        _cmd_install(getattr(args, "no_code_graph", False))
        return

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
            print(
                "ERROR: specify a subcommand: install | index | query | refresh",
                file=sys.stderr,
            )
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

    p_install = cg.add_parser(
        "install",
        help=(
            "Install the codebase-memory-mcp host binary AND persist "
            "code_graph.enabled, so the flag and the filesystem agree (ADR-0162/0163)"
        ),
    )
    # Same flag vocabulary as `yadgar setup --no-code-graph`, deliberately: the
    # shell installer forwards its own --no-code-graph straight through. There is
    # no opt-IN flag — code_graph is default-on, so an opt-in would be a no-op
    # and would push scripted installs onto the negative form (the `--code-graph`
    # defect removed earlier in this train).
    p_install.add_argument(
        "--no-code-graph",
        action="store_true",
        dest="no_code_graph",
        default=False,
        help=(
            "Opt out entirely: skip the host-binary install AND persist "
            "code_graph.enabled=false in the runtime-config store, so the flag and "
            "the binary stay coherent. Opt a single repo out instead with "
            '`config_set("code_graph.enabled", false, scope="project", directory=<repo>)`.'
        ),
    )

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
