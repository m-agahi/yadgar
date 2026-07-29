"""seed subcommand — bootstrap memory for an existing project."""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_DAEMON_PORT = os.environ.get("YADGAR_PORT", "8765")
_DAEMON_BASE = f"http://127.0.0.1:{_DAEMON_PORT}"


def _load_anchors_yaml(anchors_path: str) -> list[dict]:
    """Load anchor entries from a YAML file. Returns list of dicts."""
    path = Path(anchors_path)
    if not path.exists():
        raise FileNotFoundError(f"Anchors file not found: {path}")
    try:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
    except ImportError:
        from ruamel.yaml import YAML

        yaml_parser = YAML()
        with open(path) as f:
            data = yaml_parser.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "anchors" in data:
        return list(data["anchors"])
    raise ValueError(f"anchors.yaml must be a list or dict with 'anchors' key, got: {type(data)}")


def _read_auth_token() -> str:
    """Read YADGAR_MCP_AUTH_TOKEN from environment, falling back to secrets.env file.

    Environment variable wins over file (allows CI/systemd override).

    2026-07-29: this was one of three hand-rolled copies of the same resolution.
    The body now delegates to the single ``core.install.auth_token`` resolver
    (whose parser is the superset of the two it replaced, so nothing this
    function used to find is lost). Kept as a named function rather than an
    alias because three call sites in this module and several tests patch
    ``yadgar.core.cli.seed._read_auth_token`` by name.
    """
    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

    return resolve_auth_token()


def _daemon_health_ok() -> bool:
    """Probe /health endpoint. Returns True if daemon responds 200, False otherwise."""
    url = f"{_DAEMON_BASE}/health"
    token = _read_auth_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        urllib.request.urlopen(req, timeout=3.0)  # noqa: S310
        return True
    except Exception:
        return False


def _seed_anchors(anchors: list[dict], db_path: str | None, dry_run: bool) -> dict:
    """Seed anchor entries into yadgar memory via daemon REST endpoint.

    v5.46.15: rewrites pre-SurrealDB SQLite path (yadgar.db — dead code) to
    POST /hooks/seed-anchor on the daemon. Daemon handles SurrealDB write,
    similarity gate, and branch resolution — CLI is a thin client.

    Architecture note: uses /hooks/seed-anchor REST route (same pattern as
    the other /hooks/* REST routes) rather than JSON-RPC POST /mcp. This avoids
    SSE framing complexity and is consistent with all existing hook callers.
    """
    results = {"loaded": len(anchors), "created": 0, "skipped": 0, "dry_run": dry_run}

    if dry_run:
        for entry in anchors:
            content = entry.get("content", "")
            tags = entry.get("tags", [])
            print(
                f"  [DRY RUN] Would seed anchor: [{', '.join(tags)}] {content[:80]}...",
                file=sys.stderr,
            )
        results["created"] = len(anchors)
        return results

    # Probe daemon health first
    if not _daemon_health_ok():
        print(
            "  WARN: Daemon not running. Skipping anchor seed.",
            file=sys.stderr,
        )
        print(
            "  To seed anchors manually, start the daemon then run:",
            file=sys.stderr,
        )
        print(
            "    systemctl --user start yadgar.target",
            file=sys.stderr,
        )
        print(
            "    yadgar seed --anchors <path/to/anchors.yaml>",
            file=sys.stderr,
        )
        results["skipped"] = len(anchors)
        results["reason"] = "daemon_unreachable"
        return results

    token = _read_auth_token()
    url = f"{_DAEMON_BASE}/hooks/seed-anchor"
    base_headers = {"Content-Type": "application/json"}
    if token:
        base_headers["Authorization"] = f"Bearer {token}"

    for entry in anchors:
        content = entry.get("content", "")
        tags = list(entry.get("tags", []))

        if not content:
            print("  WARN: anchor entry missing 'content', skipping", file=sys.stderr)
            results["skipped"] += 1
            continue

        # Ensure _anchor tag is always present
        if "_anchor" not in tags:
            tags.append("_anchor")

        payload = json.dumps(
            {
                "content": content,
                "tags": tags,
                "is_protected": True,
                "context": str(Path.home()),
            }
        ).encode()

        try:
            req = urllib.request.Request(url, data=payload, headers=base_headers)
            resp = urllib.request.urlopen(req, timeout=10.0)  # noqa: S310
            resp_data = json.loads(resp.read().decode())
            if resp_data.get("created", 0):
                results["created"] += 1
            else:
                results["skipped"] += 1
        except urllib.error.HTTPError as e:
            # 409 Conflict = similarity gate deduped — count as skipped
            if e.code == 409:
                results["skipped"] += 1
            else:
                print(f"  WARN: seed-anchor HTTP {e.code}: {e}", file=sys.stderr)
                results["skipped"] += 1
        except Exception as e:
            print(f"  WARN: seed-anchor failed: {e}", file=sys.stderr)
            results["skipped"] += 1

    return results


def _seed_agent_prompts(db_path: str | None, dry_run: bool) -> dict:
    """Seed the 15 built-in starter agent-prompts via daemon REST endpoint.

    Mirrors _seed_anchors. POST to /hooks/seed-agent-prompts; daemon handles
    the idempotency (create-if-absent per pattern).
    dry_run: prints what would be seeded, makes zero HTTP calls.
    """
    _STARTER_PATTERNS = [
        "pr-review",
        "debug-investigate",
        "explore-codebase",
        "implement-tdd",
        "plan-executing-build",
        # v5.124.0 consolidation: generic subset of the consolidated live library
        # (mirrors seed/materials/agent_prompts.yaml prompts: block).
        "rca-diagnose",
        "plan-audit",
        "scope-and-plan",
        "build-car",
        "drift-audit",
        "feasibility-design",
        "feature-kill-closeout",
        "dispatch-fix-test-migration",
        "mechanical-refactor-chunk-commit-early",
        "stacked-car-parallel-build",
    ]
    results = {"seeded": True, "created": 0, "skipped": 0, "dry_run": dry_run}

    if dry_run:
        for pat in _STARTER_PATTERNS:
            print(
                f"  [DRY RUN] Would seed agent-prompt: {pat}",
                file=sys.stderr,
            )
        results["created"] = len(_STARTER_PATTERNS)
        return results

    if not _daemon_health_ok():
        print("  WARN: Daemon not running. Skipping agent-prompt seed.", file=sys.stderr)
        print("  To seed manually, start the daemon then run:", file=sys.stderr)
        print("    systemctl --user start yadgar.target", file=sys.stderr)
        print("    yadgar seed --agent-prompts", file=sys.stderr)
        results["skipped"] = len(_STARTER_PATTERNS)
        results["reason"] = "daemon_unreachable"
        return results

    token = _read_auth_token()
    url = f"{_DAEMON_BASE}/hooks/seed-agent-prompts"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = json.dumps({}).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30.0)  # noqa: S310
        resp_data = json.loads(resp.read().decode())
        results["created"] = resp_data.get("created", 0)
        results["skipped"] = resp_data.get("skipped", 0)
    except Exception as e:
        print(f"  WARN: seed-agent-prompts failed: {e}", file=sys.stderr)
        results["skipped"] = len(_STARTER_PATTERNS)
        results["reason"] = "request_failed"

    return results


def cmd_seed(args):
    """Bootstrap memory for an existing project by scanning its structure."""
    # Handle --agent-prompts mode
    if getattr(args, "agent_prompts", False):
        result = _seed_agent_prompts(db_path=getattr(args, "db_path", None), dry_run=args.dry_run)
        if args.dry_run:
            print(
                f"\n[DRY RUN] Would seed {result['created']} starter agent-prompts",
                file=sys.stderr,
            )
        elif result.get("reason") == "daemon_unreachable":
            pass  # instructional message already printed in _seed_agent_prompts
        else:
            print(
                f"\nSeeded {result['created']} agent-prompts ({result['skipped']} skipped, already present)",
                file=sys.stderr,
            )
        print(json.dumps(result))
        return

    # Handle --anchors mode
    if getattr(args, "anchors", None):
        anchors_path = args.anchors
        print(f"Loading anchors from: {anchors_path}", file=sys.stderr)
        try:
            anchors = _load_anchors_yaml(anchors_path)
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        result = _seed_anchors(
            anchors, db_path=getattr(args, "db_path", None), dry_run=args.dry_run
        )

        if args.dry_run:
            print(
                f"\n[DRY RUN] Would seed {result['created']} anchors from {anchors_path}",
                file=sys.stderr,
            )
        elif result.get("reason") == "daemon_unreachable":
            pass  # instructional message already printed in _seed_anchors
        else:
            print(
                f"\nSeeded {result['created']} anchors ({result['skipped']} skipped, already present)",
                file=sys.stderr,
            )
        print(json.dumps(result))
        return

    from yadgar.core.seed import seed_project

    directory = str(Path(args.directory).resolve())
    print(f"Seeding project: {directory}", file=sys.stderr)

    # T2 Car E1: the store phase forwards to the backend seed_store /admin op;
    # db_path no longer applies (the backend owns the DB).
    result = seed_project(
        directory=directory,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(
            f"\n[DRY RUN] Would create {result['memories_generated']} memories for {result['project']}\n",
            file=sys.stderr,
        )
        for mem in result.get("memories", []):
            tags = ", ".join(mem["tags"])
            print(f"  [{tags}] {mem['content'][:120]}...", file=sys.stderr)
    else:
        replaced_msg = f", replaced {result['replaced']} old" if result.get("replaced") else ""
        print(
            f"\nSeeded {result['project']}: "
            f"{result['created']} created{replaced_msg} "
            f"(from {result['memories_generated']} total)",
            file=sys.stderr,
        )

    print(json.dumps(result))


def register(subparsers):
    p = subparsers.add_parser("seed", help="Bootstrap memory for an existing project")
    p.add_argument("directory", nargs="?", help="Project directory to scan and seed")
    p.add_argument(
        "--anchors",
        type=str,
        default=None,
        metavar="FILE",
        help="YAML file of anchor entries to seed into memory (v5.45.0+)",
    )
    p.add_argument(
        "--agent-prompts",
        action="store_true",
        default=False,
        help="Seed the 4 built-in starter agent-prompts into the library (v5.85 S8)",
    )
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.add_argument(
        "--dry-run", action="store_true", help="Scan and show what would be stored without storing"
    )
    p.set_defaults(func=cmd_seed)
