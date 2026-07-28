"""Shared pure slugify function for the yadgar wiki subsystem.

Car A of #83 (repo-wiki page-type).

``slugify`` is the single source of truth for converting a title string into
a URL-safe wiki slug.  It was hoisted from ``WikiStore._slugify`` in
``yadgar/_shared/wiki/store.py``; the method now delegates here so all
producers (WikiStore, schema builders, and formerly the now-decommissioned
repo_wiki generator, #33/ADR-0162) produce identical slugs from the same input.

Behaviour (byte-for-byte identical to the old inline):
  1. HTML entities are decoded first (``html.unescape``), so ``&amp;`` → ``&``.
  2. Lowercase.
  3. Any run of characters outside ``[a-z0-9]`` (including spaces, dots, dashes,
     underscores, unicode) collapses to a single ``-``.
  4. Leading/trailing hyphens stripped.
  5. Hard cap at 64 characters.
  6. Empty result (e.g. whitespace-only input) → ``"untitled"``.

Dependencies: stdlib only (``html``, ``re``).  No imports from this package —
dependency direction is ``store → slug``, never the reverse.
"""

from __future__ import annotations

import html as _html
import re as _re

_NON_ALNUM = _re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 64


def slugify(title: str) -> str:
    """Convert *title* to a URL-safe wiki slug.

    Args:
        title: Raw title string.  May contain HTML entities, unicode, spaces.

    Returns:
        A lowercase alphanumeric-plus-hyphens string of at most 64 characters,
        or ``"untitled"`` when *title* is empty or whitespace-only.
    """
    slug = _NON_ALNUM.sub("-", _html.unescape(title).lower()).strip("-")
    return slug[:_MAX_SLUG_LEN] if slug else "untitled"
