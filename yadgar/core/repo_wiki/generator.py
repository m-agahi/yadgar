"""Repo-wiki page generator — convert ModuleRecords into wiki page dicts.

Each module gets one wiki page (per-module granularity; the 364-fn-page era is
over — per-function is too granular and produces noisy, low-signal corpus).

Output dict shape:
  {
    "slug": "mod-yadgar-retrieval-core",
    "title": "yadgar.retrieval.core",
    "content": "<markdown>",
    "tags": ["code-structure", "module", ...],
    "category": "code",
    "page_type": "code",
    "directory_context": "/abs/path/to/repo",   # correct stamp (fixes the 364-page leak)
  }

The caller (CLI or MCP tool) submits these dicts via wiki_add / wiki_update.
directory_context is always the repo root absolute path, never 'global'.
"""

from __future__ import annotations

import hashlib as _hashlib
from pathlib import Path as _Path

from yadgar._shared.observability.observe import observe
from yadgar.core.repo_wiki.scanner import ClassRecord, FunctionRecord, ModuleRecord

# Slug prefix for module pages.  Choose "mod-" to match existing wiki conventions.
_MOD_SLUG_PREFIX = "mod-"

# Max docstring length shown in a page (avoid bloating pages with huge module-docs)
_MAX_DOCSTRING = 800

# Max signature length before truncation
_MAX_SIG = 160


@observe(tier="stage")
def _slugify(module_name: str) -> str:
    """Convert dotted module name to a slug: yadgar.retrieval.core → mod-yadgar-retrieval-core."""
    slug = module_name.replace(".", "-").replace("_", "-")
    # Collapse repeated hyphens
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"{_MOD_SLUG_PREFIX}{slug}".lower()


@observe(tier="stage")
def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _render_signature(sig: str) -> str:
    return f"```python\n{_truncate(sig, _MAX_SIG)}\n```"


@observe(tier="stage")
def _render_function(fn: FunctionRecord, level: int = 3) -> str:
    """Render a function/method as a markdown section."""
    hashes = "#" * level
    heading = fn.qualname if fn.is_method else fn.name
    parts = [f"{hashes} `{heading}`"]
    parts.append("")
    parts.append(_render_signature(fn.signature))
    if fn.docstring:
        parts.append("")
        parts.append(_truncate(fn.docstring, _MAX_DOCSTRING))
    return "\n".join(parts)


@observe(tier="stage")
def _render_class(cls: ClassRecord) -> str:
    """Render a class as a markdown section with nested methods."""
    bases_str = f"({', '.join(cls.bases)})" if cls.bases else ""
    parts = [f"## class `{cls.name}{bases_str}`"]
    parts.append("")
    if cls.docstring:
        parts.append(_truncate(cls.docstring, _MAX_DOCSTRING))
        parts.append("")
    for method in cls.methods:
        # Skip private methods (single-underscore prefix) — keep pages navigable
        if method.name.startswith("_") and not method.name.startswith("__"):
            continue
        # Skip dunder methods except __init__ and __call__
        if method.name.startswith("__") and method.name not in ("__init__", "__call__"):
            continue
        parts.append(_render_function(method, level=3))
        parts.append("")
    return "\n".join(parts)


@observe(tier="stage")
def generate_module_page(rec: ModuleRecord, directory_context: str) -> dict:
    """Generate a wiki page dict for one ModuleRecord.

    directory_context: absolute path to the repo root — used as the
    directory_context stamp so recall scopes pages to the correct project.
    This is the key fix vs. the external skill which defaulted to 'global'.

    v5.85.0 (car #36): each page dict now carries ``hash`` = SHA256(file bytes)
    and ``source_file`` = absolute path to the module file.  These fields allow
    the staleness checker to compare against live file contents via DB lookup
    instead of only scanning .local-review/wiki/*.md on disk.
    """
    slug = _slugify(rec.module_name)
    title = rec.module_name

    # Compute source hash — same bytes path as checker's _compute_source_hash file branch.
    abs_path = str((_Path(directory_context) / rec.module_path).resolve())
    try:
        file_bytes = _Path(abs_path).read_bytes()
        file_hash = _hashlib.sha256(file_bytes).hexdigest()
    except OSError:
        file_hash = ""

    # --- Build content ---
    sections: list[str] = []

    # Header
    sections.append(f"# {title}")
    sections.append(f"\n**File:** `{rec.module_path}`")

    if rec.parse_error:
        sections.append(f"\n> **Parse error:** {rec.parse_error}")
        content = "\n".join(sections)
        return {
            "slug": slug,
            "title": title,
            "content": content,
            "tags": ["code-structure", "module", "parse-error"],
            "category": "code",
            "page_type": "code",
            "directory_context": directory_context,
            "hash": file_hash,
            "source_file": abs_path,
        }

    # Module docstring
    if rec.docstring:
        sections.append("")
        sections.append(_truncate(rec.docstring, _MAX_DOCSTRING))

    # Imports (lightweight ref context — first 10 only)
    if rec.imports:
        sections.append("")
        shown = rec.imports[:10]
        sections.append("**Imports:** " + ", ".join(f"`{i}`" for i in shown))
        if len(rec.imports) > 10:
            sections.append(f"*(…and {len(rec.imports) - 10} more)*")

    # Module-level functions
    if rec.functions:
        sections.append("")
        sections.append("## Functions")
        for fn in rec.functions:
            if fn.name.startswith("_") and not fn.name.startswith("__"):
                # Skip private helpers at module level — keep pages navigable
                continue
            sections.append("")
            sections.append(_render_function(fn, level=3))

    # Classes
    if rec.classes:
        sections.append("")
        sections.append("## Classes")
        for cls in rec.classes:
            sections.append("")
            sections.append(_render_class(cls))

    content = "\n".join(sections)

    # Build tags
    tags = ["code-structure", "module"]
    # Tag module by top-level package
    pkg = rec.module_name.split(".")[0] if "." in rec.module_name else rec.module_name
    tags.append(f"pkg-{pkg}")

    return {
        "slug": slug,
        "title": title,
        "content": content,
        "tags": tags,
        "category": "code",
        "page_type": "code",
        "directory_context": directory_context,
        "hash": file_hash,
        "source_file": abs_path,
    }


@observe(tier="boundary")
def generate_wiki_pages(
    records: list[ModuleRecord],
    directory_context: str,
    skip_parse_errors: bool = False,
) -> list[dict]:
    """Convert a list of ModuleRecords into wiki page dicts.

    directory_context: absolute path to repo root (directory stamp, not 'global').
    skip_parse_errors: if True, omit records with parse_error from output.

    Returns sorted by slug for deterministic output.
    """
    pages: list[dict] = []
    for rec in records:
        if skip_parse_errors and rec.parse_error:
            continue
        page = generate_module_page(rec, directory_context)
        pages.append(page)
    pages.sort(key=lambda p: p["slug"])
    return pages
