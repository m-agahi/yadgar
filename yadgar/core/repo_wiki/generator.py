"""Repo-wiki page generator — convert ModuleRecords into wiki page dicts.

Each module gets one wiki page (per-module granularity; the 364-fn-page era is
over — per-function is too granular and produces noisy, low-signal corpus).

Output dict shape (module page):
  {
    "slug": "{project}-mod-yadgar-retrieval-core",
    "title": "yadgar.retrieval.core",
    "content": "<markdown>",
    "tags": ["code-structure", "module", ...],
    "category": "reference",
    "page_type": "repo_wiki",
    "directory_context": "/abs/path/to/repo",
    "hash": "<sha256-hex>",
    "source_file": "/abs/path/to/module.py",
  }

Slug scheme: ``{project}-mod-{slugify(module_name)}`` — project-namespaced to
avoid cross-project collisions.  Built via ``repo_wiki_slug(project, module_name)``
from ``yadgar._shared.wiki.repo_wiki_schema``.  The TOC page uses a separate
slug ``{project}-repo-wiki-index`` and carries ``page_type="module"`` (not
``repo_wiki``) because it has no source_file/hash and its slug contains no
``-mod-``.

The caller (CLI or MCP tool) submits these dicts via wiki_add / wiki_update.
directory_context is always the repo root absolute path, never 'global'.
"""

from __future__ import annotations

import hashlib as _hashlib
from pathlib import Path as _Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.wiki.repo_wiki_schema import REPO_WIKI_PAGE_TYPE, repo_wiki_slug
from yadgar.core.repo_wiki.scanner import ClassRecord, FunctionRecord, ModuleRecord

# Max docstring length shown in a page (avoid bloating pages with huge module-docs)
_MAX_DOCSTRING = 800

# Max signature length before truncation
_MAX_SIG = 160


@observe(tier="stage")
def _resolve_import(import_name: str, first_party: set[str]) -> str | None:
    """Map an import dotted-path to the in-repo MODULE it resolves to, or None.

    Longest dot-boundary prefix match against the scanned module-name SET.
    For ``from x.y import z`` (extractor emits ``x.y.z``) this links the module
    ``x.y``, never the symbol ``z``.  Resolution is via the module-name SET, NOT
    a slug round-trip — a round-trip re-triggers the ``_``/``.`` collapse and
    mis-resolves.  stdlib/third-party imports return None (stay plain backtick).
    """
    best: str | None = None
    for mod in first_party:
        if import_name == mod or import_name.startswith(mod + "."):
            if best is None or len(mod) > len(best):
                best = mod
    return best


@observe(tier="stage")
def _render_import(import_name: str, first_party: set[str] | None, project: str) -> str:
    """Render one import: [[{project}-mod-slug]] if it resolves in-repo, else plain backtick."""
    if first_party:
        matched = _resolve_import(import_name, first_party)
        if matched is not None:
            return f"[[{repo_wiki_slug(project, matched)}]]"
    return f"`{import_name}`"


# Cap on the plain-backtick (stdlib/third-party) import remainder shown per page.
_MAX_PLAIN_IMPORTS = 10


@observe(tier="stage")
def _render_imports_section(imports: list[str], first_party: set[str] | None, project: str) -> str:
    """Render the **Imports:** section.

    First-party imports (crossref edges) are ALWAYS shown as [[{project}-mod-<slug>]]
    links, deduped, order-preserving — never truncated.  The plain-backtick remainder
    (stdlib/third-party) is capped at _MAX_PLAIN_IMPORTS to bound page noise.
    """
    links: list[str] = []
    seen_links: set[str] = set()
    plain: list[str] = []
    for imp in imports:
        matched = _resolve_import(imp, first_party) if first_party else None
        if matched is not None:
            slug = repo_wiki_slug(project, matched)
            if slug not in seen_links:
                seen_links.add(slug)
                links.append(f"[[{slug}]]")
        else:
            plain.append(f"`{imp}`")

    shown_plain = plain[:_MAX_PLAIN_IMPORTS]
    rendered = links + shown_plain
    line = "\n**Imports:** " + ", ".join(rendered)
    hidden = len(plain) - len(shown_plain)
    if hidden > 0:
        line += f"\n*(…and {hidden} more)*"
    return line


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
def generate_module_page(
    rec: ModuleRecord,
    directory_context: str,
    first_party: set[str] | None = None,
    project: str | None = None,
) -> dict:
    """Generate a wiki page dict for one ModuleRecord.

    directory_context: absolute path to the repo root — used as the
    directory_context stamp so recall scopes pages to the correct project.
    This is the key fix vs. the external skill which defaulted to 'global'.

    first_party: the scanned in-repo module-name SET.  When supplied, imports
    resolving to one of these modules render as ``[[{project}-mod-<slug>]]``
    crossref links (wiki_add's crossref-sync then builds the import graph as wiki
    edges).  stdlib/third-party imports stay plain backtick.  Default None → all
    imports stay plain backtick (back-compat).

    project: project identifier (e.g. ``"yadgar"``), used to namespace slugs so
    two projects with a module of the same name don't collide.  When None,
    falls back to ``Path(directory_context).name``.

    v5.85.0 (car #36): each page dict now carries ``hash`` = SHA256(file bytes)
    and ``source_file`` = absolute path to the module file.  These fields allow
    the staleness checker to compare against live file contents via DB lookup
    instead of only scanning .local-review/wiki/*.md on disk.

    Car D (#83): slug is ``repo_wiki_slug(project, module_name)`` =
    ``{project}-mod-{slugify(module_name)}``.  page_type = REPO_WIKI_PAGE_TYPE.
    """
    resolved_project = project or _Path(directory_context).name
    slug = repo_wiki_slug(resolved_project, rec.module_name)
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
            "category": "reference",
            "page_type": REPO_WIKI_PAGE_TYPE,
            "directory_context": directory_context,
            "hash": file_hash,
            "source_file": abs_path,
        }

    # Module docstring
    if rec.docstring:
        sections.append("")
        sections.append(_truncate(rec.docstring, _MAX_DOCSTRING))

    # Imports.  In-repo imports render as [[{project}-mod-<slug>]] crossref links
    # and are NEVER truncated — they are the graph edges (the whole point of the
    # crossref car).  isort orders first-party imports LAST, so a naive head-slice
    # would drop exactly the edges we want.  Partition: all first-party links always
    # shown; the 10-cap applies only to the plain-backtick (stdlib/third-party)
    # remainder to bound noise.
    if rec.imports:
        sections.append(_render_imports_section(rec.imports, first_party, resolved_project))

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
        "category": "reference",
        "page_type": REPO_WIKI_PAGE_TYPE,
        "directory_context": directory_context,
        "hash": file_hash,
        "source_file": abs_path,
    }


@observe(tier="stage")
def generate_toc_page(
    records: list[ModuleRecord],
    directory_context: str,
    project: str,
) -> dict:
    """Build the single navigable repo-wiki index page (the code map).

    slug = ``<project>-repo-wiki-index``.  Content is the package/module tree with
    ``[[{project}-mod-<slug>]]`` links — one wiki_read → the whole code map → drill
    via crossrefs.  No source_file/hash (it is not a source module).

    page_type is ``"module"`` (not ``REPO_WIKI_PAGE_TYPE``) because the TOC has no
    ``-mod-`` in its slug and carries no source_file/hash — it would fail the
    ``validate_repo_wiki_page`` schema check.  The TOC is structural metadata, not
    a module documentation page.
    """
    slug = f"{project}-repo-wiki-index"
    title = f"{project} — repo-wiki index"

    # Group modules by top-level package for a tree-ish TOC.
    by_pkg: dict[str, list[ModuleRecord]] = {}
    for rec in records:
        pkg = rec.module_name.split(".")[0] if "." in rec.module_name else rec.module_name
        by_pkg.setdefault(pkg, []).append(rec)

    sections: list[str] = [
        f"# {title}",
        "",
        "Navigable index of the repository's code-structure wiki pages "
        "(AST signatures + docstrings, auto-refreshed). Drill into a module via "
        "its crossref link; each page links onward to the modules it imports.",
    ]
    for pkg in sorted(by_pkg):
        sections.append("")
        sections.append(f"## {pkg}")
        sections.append("")
        for rec in sorted(by_pkg[pkg], key=lambda r: r.module_name):
            sections.append(
                f"- [[{repo_wiki_slug(project, rec.module_name)}]] — `{rec.module_path}`"
            )

    content = "\n".join(sections)
    return {
        "slug": slug,
        "title": title,
        "content": content,
        "tags": ["code-structure", "module", "repo-wiki-index", f"pkg-{project}"],
        "category": "reference",
        "page_type": "module",
        "directory_context": directory_context,
    }


@observe(tier="boundary")
def generate_wiki_pages(
    records: list[ModuleRecord],
    directory_context: str,
    skip_parse_errors: bool = False,
    project: str | None = None,
) -> list[dict]:
    """Convert a list of ModuleRecords into wiki page dicts.

    directory_context: absolute path to repo root (directory stamp, not 'global').
    skip_parse_errors: if True, omit records with parse_error from output.
    project: project identifier (repo basename).  When supplied, also emit the
        ``<project>-repo-wiki-index`` TOC page (the navigable entry point) and
        include it in the sorted output.  When None, falls back to
        ``Path(directory_context).name`` so module page slugs are still namespaced.

    The first-party module-name SET is built from ``records`` automatically so
    in-repo imports resolve to ``[[{project}-mod-<slug>]]`` crossref links.

    Returns sorted by slug for deterministic output.
    """
    # Resolve project once; thread it into all slug/crossref builders.
    resolved_project = project or _Path(directory_context).name
    first_party = {rec.module_name for rec in records}
    pages: list[dict] = []
    for rec in records:
        if skip_parse_errors and rec.parse_error:
            continue
        page = generate_module_page(
            rec, directory_context, first_party=first_party, project=resolved_project
        )
        pages.append(page)
    if project is not None:
        pages.append(generate_toc_page(records, directory_context, resolved_project))
    pages.sort(key=lambda p: p["slug"])
    return pages
