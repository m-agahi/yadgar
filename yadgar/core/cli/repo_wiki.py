"""repo-wiki subcommand — generate native code-structure wiki pages for a repo.

Walks the target repo with yadgar's native Python AST scanner and emits one
wiki page per module (per-module granularity; not per-function).

Unlike the external /repo-wiki:repo-wiki skill, this scanner:
- always stamps directory_context = repo root (fixes the 364-page 'global' leak)
- is deterministic + repeatable (no LLM pass for signature/docstring extraction)
- runs entirely offline / in-process
- NEVER contacts the daemon — this is a host-side generate-and-emit-only CLI

Usage:
  yadgar repo-wiki [REPO_PATH]           # scan + print summary; pages on stderr
  yadgar repo-wiki [REPO_PATH] --dry-run # same (alias)
  yadgar repo-wiki [REPO_PATH] --json    # machine-readable page list on stdout

Stale-only refresh (host-side hash diff — never reaches the daemon):
  <caller builds {slug: hash} via wiki_list(directory) MCP> \\
    | yadgar repo-wiki [REPO_PATH] --stale-only --stored-hashes - --json

  ``--stale-only`` generates every page host-side (hashes included) and emits, as
  ``--json``, ONLY the module pages whose SHA256 differs from the stored baseline
  (drifted) or that have no stored entry (new), plus a ``deleted`` list of stored
  slugs with no matching source module (source file removed).  The stored baseline
  is supplied by the CALLER via ``--stored-hashes <path|->`` (JSON ``{slug: hash}``,
  read from a file or, with ``-``, from stdin).  Omitting ``--stored-hashes`` uses an
  empty baseline → every module page counts as new (correct first-run behaviour).

Regen write policy (the CALLER writes; this CLI only generates/diffs):
  For each drifted/new page returned in ``pages``, the caller writes it back through
  the validated MCP path, forwarding the stamped hash + source_file so --stale-only
  can diff again next time.  To survive the 0.80 HARD wiki similarity gate (near-
  identical thin code pages hard-reject each other):
    - EXISTING slug → wiki_add(replace_slug=<slug>, hash=…, source_file=…, wait=True)
    - NEW slug      → wiki_add(force=True, hash=…, source_file=…, wait=True)
  For each slug in ``deleted`` → wiki_delete(<slug>) (its source file is gone).
  When ``toc_stale`` is True (module set changed), also re-write the
  ``<project>-repo-wiki-index`` TOC page from the full ``--json`` (non-stale) output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_stored_hashes(spec: str | None) -> dict:
    """Load the caller-supplied stored-hashes baseline (JSON ``{slug: hash}``).

    spec is a file path, ``-`` for stdin, or None (→ empty baseline).  An empty /
    blank stdin or file yields ``{}`` rather than crashing so a first run (no stored
    pages) treats everything as new.
    """
    if not spec:
        return {}
    if spec == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(spec).read_text()
    raw = raw.strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--stored-hashes must be a JSON object of {slug: hash}")
    return data


def _emit_stale_only(pages: list, stored: dict, directory_context: str) -> None:
    """Diff generated pages against a stored {slug: hash} baseline; print JSON.

    Emits ONLY hash-bearing module pages that drifted (hash differs) or are new (no
    stored entry).  The hashless TOC index page is never in ``pages`` (nothing to
    diff); its staleness is surfaced via the ``toc_stale`` flag (True when the module
    SET changed — new or deleted modules).  ``deleted`` = stored slugs with no matching
    generated module (source file removed).  NEVER contacts the daemon.
    """
    # Hash-bearing pages are exactly the module pages; the TOC has no hash key.
    module_pages = [p for p in pages if "hash" in p]
    generated_slugs = {p["slug"] for p in module_pages}

    drifted_or_new = [p for p in module_pages if p["hash"] != stored.get(p["slug"])]
    new_slugs = [p["slug"] for p in module_pages if p["slug"] not in stored]
    deleted = sorted(slug for slug in stored if slug not in generated_slugs)

    # TOC is stale only when the module set changed (new or removed modules); a
    # content-only drift leaves the tree identical, so regenerating it is pointless.
    toc_stale = bool(new_slugs) or bool(deleted)

    result = {
        "stale_only": True,
        "pages": drifted_or_new,
        "deleted": deleted,
        "toc_stale": toc_stale,
        "total": len(drifted_or_new),
        "directory_context": directory_context,
    }
    print(
        f"  Stale-only: {len(drifted_or_new)} drifted/new, "
        f"{len(deleted)} deleted, toc_stale={toc_stale}",
        file=sys.stderr,
    )
    print(json.dumps(result))


def cmd_repo_wiki(args) -> None:
    """Scan a repo and generate/submit native code-structure wiki pages."""
    from yadgar.core.repo_wiki.generator import generate_wiki_pages
    from yadgar.core.repo_wiki.scanner import scan_repo

    repo_path = Path(args.repo or ".").resolve()
    if not repo_path.is_dir():
        print(f"ERROR: not a directory: {repo_path}", file=sys.stderr)
        sys.exit(1)

    directory_context = str(repo_path)
    include_tests = getattr(args, "include_tests", False)
    skip_errors = not getattr(args, "include_errors", False)
    output_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    stale_only = getattr(args, "stale_only", False)

    print(f"Scanning: {repo_path}", file=sys.stderr)
    records = scan_repo(repo_path, include_tests=include_tests)
    # Car B0 (#83): pass project=<repo basename> so the navigable TOC index page
    # (<project>-repo-wiki-index) is emitted alongside the module pages. Each page
    # already carries hash/source_file, which the ingest agent forwards to
    # wiki_add(hash=..., source_file=...) so they persist for --stale-only.
    pages = generate_wiki_pages(
        records, directory_context, skip_parse_errors=skip_errors, project=repo_path.name
    )

    total = len(pages)
    errors = sum(1 for r in records if r.parse_error)
    print(
        f"  Found {len(records)} modules → {total} pages ({errors} parse errors"
        + (" excluded" if skip_errors else " included")
        + ")",
        file=sys.stderr,
    )

    if stale_only:
        # Host-side hash diff — the daemon is container-blind, so the CLI never
        # contacts it here.  The caller feeds the stored baseline in via
        # --stored-hashes and writes the drifted/new pages back itself.
        stored = _load_stored_hashes(getattr(args, "stored_hashes", None))
        _emit_stale_only(pages, stored, directory_context)
        return

    if dry_run:
        print(
            f"\n[DRY RUN] Would emit {total} pages (caller writes via wiki_add).", file=sys.stderr
        )
        for page in pages:
            print(
                f"  [{page['slug']}] {page['title']} (dir={page['directory_context'][:40]}...)",
                file=sys.stderr,
            )
        if output_json:
            print(json.dumps({"pages": pages, "dry_run": True, "total": total}))
        return

    # Default: generate-and-emit-only (no daemon contact).
    # Caller is responsible for writing pages back via wiki_add (see module docstring).
    print(
        f"\nGenerated {total} pages — write via wiki_add (see regen write policy).", file=sys.stderr
    )
    for page in pages:
        print(
            f"  [{page['slug']}] {page['title']} (dir={page['directory_context'][:40]}...)",
            file=sys.stderr,
        )
    if output_json:
        print(json.dumps({"pages": pages, "total": total, "directory_context": directory_context}))


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
    p.add_argument(
        "--stale-only",
        action="store_true",
        help=(
            "Emit only drifted/new module pages (host-side SHA256 diff vs the "
            "--stored-hashes baseline) as --json {pages, deleted, toc_stale, ...}. "
            "Never contacts the daemon; the caller supplies the baseline and writes "
            "the pages back via wiki_add(replace_slug=/force=, hash=, source_file=, wait=True)."
        ),
    )
    p.add_argument(
        "--stored-hashes",
        metavar="PATH|-",
        default=None,
        help=(
            "Path to a JSON {slug: hash} baseline (or '-' for stdin) that --stale-only "
            "diffs current host hashes against. Build it from wiki_list(directory)/"
            "list_wiki_hashes via MCP. Omit → empty baseline (every module page is new)."
        ),
    )
    p.set_defaults(func=cmd_repo_wiki)
