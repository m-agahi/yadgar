"""repo-wiki subcommand — generate native code-structure wiki pages for a repo.

Walks the target repo with yadgar's native Python AST scanner, emits one
wiki page per module (per-module granularity; not per-function), and
submits each page to the yadgar daemon via the /hooks/wiki-generate REST
endpoint.

Unlike the external /repo-wiki:repo-wiki skill, this scanner:
- always stamps directory_context = repo root (fixes the 364-page 'global' leak)
- is deterministic + repeatable (no LLM pass for signature/docstring extraction)
- runs entirely offline / in-process

Usage:
  yadgar repo-wiki [REPO_PATH]           # scan + submit to daemon
  yadgar repo-wiki [REPO_PATH] --dry-run # scan + print, don't submit
  yadgar repo-wiki [REPO_PATH] --json    # machine-readable page list on stdout
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yadgar.paths as _paths

_DAEMON_PORT = os.environ.get("YADGAR_PORT", "8765")
_DAEMON_BASE = f"http://127.0.0.1:{_DAEMON_PORT}"


def _read_auth_token() -> str:
    """Read YADGAR_MCP_AUTH_TOKEN from env or secrets.env (same as cli/seed.py)."""
    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    if token:
        return token
    secrets_env = _paths.SECRETS_ENV_PATH
    if not secrets_env.exists():
        return ""
    try:
        for line in secrets_env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                if key.strip() == "YADGAR_MCP_AUTH_TOKEN":
                    return val.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _daemon_health_ok() -> bool:
    """Probe /health endpoint. Returns True if daemon responds 200."""
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


def _submit_page(page: dict, base_headers: dict) -> tuple[bool, str]:
    """POST one page to /hooks/wiki-generate.  Returns (ok, reason)."""
    url = f"{_DAEMON_BASE}/hooks/wiki-generate"
    payload = json.dumps(page).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers=base_headers)
        resp = urllib.request.urlopen(req, timeout=15.0)  # noqa: S310
        resp_data = json.loads(resp.read().decode())
        # Daemon returns {"status": "ok"} or {"status": "skipped"} etc.
        return True, resp_data.get("status", "ok")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return True, "skipped (duplicate)"
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body}"
    except Exception as exc:
        return False, str(exc)


def _submit_all_pages(pages: list, output_json: bool) -> tuple[int, int, int, list]:
    """Submit all pages to daemon. Returns (submitted, skipped, failed, results)."""
    token = _read_auth_token()
    base_headers = {"Content-Type": "application/json"}
    if token:
        base_headers["Authorization"] = f"Bearer {token}"

    submitted = 0
    skipped = 0
    failed = 0
    results = []
    for page in pages:
        ok, reason = _submit_page(page, base_headers)
        if ok:
            if "skipped" in reason:
                skipped += 1
            else:
                submitted += 1
        else:
            failed += 1
            print(f"  WARN: failed to submit [{page['slug']}]: {reason}", file=sys.stderr)
        results.append({"slug": page["slug"], "status": reason if ok else f"error: {reason}"})
    return submitted, skipped, failed, results


def cmd_repo_wiki(args) -> None:
    """Scan a repo and generate/submit native code-structure wiki pages."""
    from yadgar.repo_wiki.generator import generate_wiki_pages
    from yadgar.repo_wiki.scanner import scan_repo

    repo_path = Path(args.repo or ".").resolve()
    if not repo_path.is_dir():
        print(f"ERROR: not a directory: {repo_path}", file=sys.stderr)
        sys.exit(1)

    directory_context = str(repo_path)
    include_tests = getattr(args, "include_tests", False)
    skip_errors = not getattr(args, "include_errors", False)
    output_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)

    print(f"Scanning: {repo_path}", file=sys.stderr)
    records = scan_repo(repo_path, include_tests=include_tests)
    pages = generate_wiki_pages(records, directory_context, skip_parse_errors=skip_errors)

    total = len(pages)
    errors = sum(1 for r in records if r.parse_error)
    print(
        f"  Found {len(records)} modules → {total} pages ({errors} parse errors"
        + (" excluded" if skip_errors else " included")
        + ")",
        file=sys.stderr,
    )

    if dry_run:
        print(f"\n[DRY RUN] Would submit {total} pages to daemon.", file=sys.stderr)
        for page in pages:
            print(
                f"  [{page['slug']}] {page['title']} (dir={page['directory_context'][:40]}...)",
                file=sys.stderr,
            )
        if output_json:
            print(json.dumps({"pages": pages, "dry_run": True, "total": total}))
        return

    if not _daemon_health_ok():
        print("  WARN: daemon not reachable. Cannot submit pages.", file=sys.stderr)
        print("  Start daemon: systemctl --user start yadgar.target", file=sys.stderr)
        print("  Then retry: yadgar repo-wiki", file=sys.stderr)
        if output_json:
            print(json.dumps({"error": "daemon_unreachable", "total": total, "submitted": 0}))
        sys.exit(1)

    submitted, skipped, failed, results = _submit_all_pages(pages, output_json)
    print(
        f"\nSubmitted {submitted} pages ({skipped} skipped/deduped, {failed} failed)",
        file=sys.stderr,
    )
    result: dict = {
        "total": total,
        "submitted": submitted,
        "skipped": skipped,
        "failed": failed,
        "directory_context": directory_context,
    }
    if output_json:
        result["results"] = results
        print(json.dumps(result))


def register(subparsers) -> None:
    """Register the 'repo-wiki' subcommand."""
    p = subparsers.add_parser(
        "repo-wiki",
        help="Generate native code-structure wiki pages for a repository",
    )
    p.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Repository root path (default: current directory)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and show pages without submitting to daemon",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON result to stdout",
    )
    p.add_argument(
        "--include-tests",
        action="store_true",
        help="Include test directories in scan (excluded by default)",
    )
    p.add_argument(
        "--include-errors",
        action="store_true",
        help="Include modules with parse errors in output (excluded by default)",
    )
    p.set_defaults(func=cmd_repo_wiki)
