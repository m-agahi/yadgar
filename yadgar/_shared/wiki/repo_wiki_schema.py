"""Schema constants and validation for repo_wiki wiki pages.

Car A of #83 (repo-wiki page-type).

A ``repo_wiki`` page documents a single Python module in a project.  Its slug
is deterministic: ``{project}-mod-{slugify(module_name)}``.  This scheme is:

  - Cross-project distinct:  ``proj_a-mod-logging`` ≠ ``proj_b-mod-logging``.
  - TOC-consistent:  all module slugs share the ``{project}-mod-`` prefix.
  - Stable:  same inputs → same slug every call (no randomness, no timestamps).
  - 64-char-capped:  via the shared ``slugify`` function.

Required fields per page
------------------------
source_file
    Absolute path to the source file on disk.  Relative paths are rejected —
    the path must be rooted so that stale-diff and hash verification are
    unambiguous across machines.

hash
    SHA-256 hex digest of the source file at generation time.  Exactly 64
    lowercase hex characters.  Enables stale-diff to detect file changes
    without re-reading content.

slug
    Must contain ``"-mod-"`` to confirm it was built via ``repo_wiki_slug``.

All validation is pure and non-raising: ``validate_repo_wiki_page`` returns a
list of human-readable error strings.  An empty list means valid.  Callers
decide whether to raise, reject, or DLQ.
"""

from __future__ import annotations

import re as _re

from yadgar._shared.observability.observe import observe
from yadgar._shared.wiki.slug import slugify

# ── Constant ─────────────────────────────────────────────────────────────────

REPO_WIKI_PAGE_TYPE = "repo_wiki"
"""The canonical page_type string for auto-generated module documentation."""

# ── Slug builder ─────────────────────────────────────────────────────────────

_HEX_RE = _re.compile(r"^[0-9a-f]{64}$")


@observe(tier="stage")
def repo_wiki_slug(project: str, module_name: str) -> str:
    """Return the deterministic wiki slug for a module page.

    Args:
        project:     Project identifier (e.g. ``"yadgar"``).  Must be a short
                     ASCII identifier — spaces / dots will be slugified away.
        module_name: Dotted module path (e.g. ``"yadgar._shared.embeddings"``).

    Returns:
        A slug of the form ``"{project}-mod-{slugify(module_name)}"``,
        capped at 64 characters by the shared ``slugify`` function.

    Example::

        >>> repo_wiki_slug("yadgar", "yadgar._shared.embeddings")
        'yadgar-mod-yadgar-shared-embeddings'
    """
    return slugify(f"{project}-mod-{module_name}")


# ── Validator ────────────────────────────────────────────────────────────────


@observe(tier="stage")
def validate_repo_wiki_page(
    *,
    slug: str | None,
    source_file: str | None,
    hash: str | None,  # noqa: A002 — parameter name matches the wiki field contract
) -> list[str]:
    """Validate a repo_wiki page's required fields.

    All arguments are keyword-only.

    Args:
        slug:        The wiki slug.  Must contain ``"-mod-"``.
        source_file: Absolute path to the source file.  Must start with ``"/"``.
        hash:        SHA-256 hex digest.  Must be exactly 64 lowercase hex chars.

    Returns:
        A (possibly empty) list of human-readable error strings.
        Empty list ↔ valid.
    """
    errors: list[str] = []

    # -- source_file --
    if source_file is None:
        errors.append("source_file is required for repo_wiki pages")
    elif not str(source_file).startswith("/"):
        errors.append(
            f"source_file must be an absolute path (starts with '/'); got: {source_file!r}"
        )

    # -- hash --
    if hash is None:
        errors.append("hash is required for repo_wiki pages")
    elif not _HEX_RE.fullmatch(str(hash)):
        errors.append(f"hash must be exactly 64 lowercase hex characters; got: {hash!r}")

    # -- slug shape --
    if slug is None:
        errors.append("slug is required for repo_wiki pages")
    elif "-mod-" not in str(slug):
        errors.append(
            f"slug must contain '-mod-' (expected '{{project}}-mod-...' shape); got: {slug!r}"
        )

    return errors
