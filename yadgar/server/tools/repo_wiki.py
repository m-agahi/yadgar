"""MCP tool: repo_wiki_generate — native code-structure wiki generation (T8, Option A).

Exposes yadgar.repo_wiki as an agent-callable MCP tool.

COLLISION NOTE: yadgar/server/tools/wiki.py is being rewritten by the
recall-rebuild train (T6). This tool lives in a separate file to avoid
merge conflicts. If wiki.py integration (e.g. calling wiki_add from inside
this tool) is needed, it should be wired after T6 lands.

TODO (post-T6): consider calling wiki_add directly from _submit_page_to_storage()
instead of returning page dicts for the caller to submit. For now the tool returns
the generated page dicts and the caller is responsible for submitting them via
wiki_add (or the CLI does it via the daemon REST endpoint).

I26 secret-gate: this is a READ-ONLY scanning tool (no secrets written, only
repo source code read for structure). No secret gate needed — no wiki_add call
is made from this tool; the generated pages are returned for the caller to review.
If a future version auto-submits pages via wiki_add, add the gate there.
"""

from __future__ import annotations

import logging
from pathlib import Path

from yadgar.server._app import _tool

logger = logging.getLogger(__name__)


@_tool()
def repo_wiki_generate(
    directory: str,
    include_tests: bool = False,
    skip_parse_errors: bool = True,
    max_pages: int = 500,
) -> dict:
    """Generate native code-structure wiki pages for a Python repository.

    Walks the given directory, extracts module/function signatures and
    docstrings via Python AST (no LLM pass), and returns page dicts
    ready for submission via wiki_add.  Each page is stamped with
    directory_context=directory (the repo root) — never 'global'.

    Returns:
      {
        "total": N,
        "pages": [{"slug": ..., "title": ..., "content": ...,
                   "tags": [...], "category": "code", "page_type": "code",
                   "directory_context": "/abs/path"}, ...],
        "parse_errors": [{"module_path": ..., "error": ...}],
        "directory_context": "/abs/path/to/repo",
      }

    The caller should iterate pages and call wiki_add(slug, content, title,
    tags=..., category="code", page_type="code",
    directory_context=page["directory_context"]) for each page.

    TODO (post-T6/wiki.py land): auto-submit via wiki_add directly from here.

    Args:
      directory: absolute path to the repository root to scan.
      include_tests: if True, include test directories (default False).
      skip_parse_errors: if True, omit modules with syntax errors from output.
      max_pages: safety cap on output pages (default 500).
    """
    from yadgar.repo_wiki.generator import generate_wiki_pages
    from yadgar.repo_wiki.scanner import scan_repo

    resolved = str(Path(directory).resolve())

    try:
        records = scan_repo(resolved, include_tests=include_tests)
    except ValueError as exc:
        return {"error": str(exc), "total": 0, "pages": [], "parse_errors": []}
    except Exception as exc:
        logger.error("repo_wiki_generate: scan_repo failed: %s", exc)
        return {"error": f"scan failed: {exc}", "total": 0, "pages": [], "parse_errors": []}

    parse_errors = [
        {"module_path": r.module_path, "error": r.parse_error} for r in records if r.parse_error
    ]

    try:
        pages = generate_wiki_pages(records, resolved, skip_parse_errors=skip_parse_errors)
    except Exception as exc:
        logger.error("repo_wiki_generate: generate_wiki_pages failed: %s", exc)
        return {
            "error": f"generation failed: {exc}",
            "total": 0,
            "pages": [],
            "parse_errors": parse_errors,
            "directory_context": resolved,
        }

    # Safety cap
    capped = pages[:max_pages]
    truncated = len(pages) > max_pages

    result = {
        "total": len(capped),
        "pages": capped,
        "parse_errors": parse_errors,
        "directory_context": resolved,
    }
    if truncated:
        result["truncated"] = True
        result["total_before_cap"] = len(pages)

    return result
