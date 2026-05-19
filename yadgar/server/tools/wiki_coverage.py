"""wiki_coverage MCP tool — module wiki coverage analyzer (v5.3.5 Q3).

Returns coverage metrics: which .py files under a directory have
at least one wiki page tagged 'mod' or 'fn'.

Matching convention:
  A wiki page is considered to cover a module when it has EITHER:
    - a tag of the form  source_file:<path>  (exact or suffix match)
    - a title or slug that ends with or equals the module basename (without .py)

  The canonical way to associate a wiki page with a source file is to add
  a tag like  source_file:yadgar/server/tools/recall.py  when creating the page.
"""

from __future__ import annotations

import logging
from pathlib import Path

from yadgar.server._app import _tool

logger = logging.getLogger(__name__)

# Directories to exclude when scanning for .py files
_EXCLUDE_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        "tests",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "dist",
        "build",
    }
)


def _scan_py_files(directory: str) -> list[str]:
    """Return sorted list of .py file paths under directory, excluding noise dirs."""
    root = Path(directory).expanduser().resolve()
    result: list[str] = []
    for p in root.rglob("*.py"):
        # Exclude any path whose parts include an excluded dir
        if any(part in _EXCLUDE_DIRS for part in p.parts):
            continue
        result.append(str(p))
    return sorted(result)


def _extract_source_file_tags(tags: list[str]) -> list[str]:
    """Return the path values from tags of the form 'source_file:<path>'."""
    return [t[len("source_file:") :] for t in tags if t.startswith("source_file:")]


def _is_covered(module_path: str, wiki_pages: list[dict]) -> bool:
    """Return True if any wiki page covers the given module path.

    Coverage is detected when a wiki page tagged 'mod' or 'fn' has:
    1. A source_file:<path> tag that matches (exact or suffix)
    2. OR a slug/title containing the module's basename (without .py)
    """
    module_path_norm = module_path.replace("\\", "/")
    module_basename = Path(module_path).stem  # e.g. "recall" from "recall.py"

    for page in wiki_pages:
        tags = page.get("tags", [])

        # Check source_file: tags first (explicit, most reliable)
        for sf_path in _extract_source_file_tags(tags):
            sf_norm = sf_path.replace("\\", "/")
            # Match if exact or if module_path ends with the tag value (or vice versa)
            if (
                sf_norm == module_path_norm
                or module_path_norm.endswith(sf_norm)
                or sf_norm.endswith(module_path_norm)
            ):
                return True

        # Fallback: slug/title contains module basename
        slug = page.get("slug", "")
        title = page.get("title", "").lower()
        if module_basename and (module_basename in slug or module_basename in title):
            return True

    return False


@_tool()
def wiki_coverage(directory: str = ".") -> dict:
    """Analyze wiki coverage for Python modules under a directory.

    Scans all .py files (excluding tests/, .venv/, __pycache__, etc.) and
    checks which have at least one wiki page tagged 'mod' or 'fn'.

    Coverage matching:
    - Wiki page has a tag  source_file:<path>  matching the module path, OR
    - Wiki page slug/title contains the module's basename.

    Returns:
        {
          "total_modules": int,
          "covered_modules": [<path>, ...],
          "uncovered_modules": [<path>, ...],
          "coverage_pct": float  # 0.0 – 1.0
        }
    """
    import yadgar.server._state as _st

    # Get all wiki pages tagged 'mod' or 'fn'
    wiki = _st._wiki
    if wiki is None:
        # No wiki store — nothing can be covered
        py_files = _scan_py_files(directory)
        return {
            "total_modules": len(py_files),
            "covered_modules": [],
            "uncovered_modules": py_files,
            "coverage_pct": 0.0,
        }

    # Query wiki for mod/fn tagged pages — combine both tag searches
    try:
        mod_pages = wiki.query("", tags=["mod"], max_results=500)
    except Exception:
        mod_pages = []
    try:
        fn_pages = wiki.query("", tags=["fn"], max_results=500)
    except Exception:
        fn_pages = []

    # Deduplicate by slug
    seen_slugs: set[str] = set()
    coverage_pages: list[dict] = []
    for page in mod_pages + fn_pages:
        slug = page.get("slug", "")
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            coverage_pages.append(page)

    # Scan filesystem
    py_files = _scan_py_files(directory)

    covered: list[str] = []
    uncovered: list[str] = []
    for f in py_files:
        if _is_covered(f, coverage_pages):
            covered.append(f)
        else:
            uncovered.append(f)

    total = len(py_files)
    pct = len(covered) / total if total > 0 else 0.0

    return {
        "total_modules": total,
        "covered_modules": sorted(covered),
        "uncovered_modules": sorted(uncovered),
        "coverage_pct": round(pct, 4),
    }
