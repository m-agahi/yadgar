"""Wiki page mutability gate — Car J (0047 §7 D25/D26).

Sole chokepoint for wiki write-permission enforcement. Called at the entry
of ``_WikiMixin.update_wiki_page``, ``insert_wiki_page``, and
``delete_wiki_page``. Per-page ``mutability_override`` wins over the per-type
default via ``get_effective_mutability``.

Extracted from ``wiki.py`` (file_loc cap I13): keeps the gate logic
self-contained and unit-testable in isolation.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.wiki.policy import get_effective_mutability


@observe(tier="hot", span=False)
def enforce_mutability(
    page: dict,
    *,
    op: str,
    sanctioned: bool,
) -> None:
    """Raise ``PermissionError`` if *page*'s effective mutability forbids *op*.

    *sanctioned=True* bypasses the gate (server-side lifecycle transitions:
    Car G supersede retype, Car K nightly sweep). Sanctioned is a kwarg on
    the storage method — no separate signal — so unsanctioned callers
    physically cannot pass True.

    Values:
    - ``"free"`` — never raised.
    - ``"locked"`` — raise PermissionError unless *sanctioned*.
    - ``"derived"`` — raise PermissionError unless *sanctioned* (regenerated,
      not hand-edited).
    """
    if sanctioned:
        return
    eff = get_effective_mutability(page)
    if eff in ("locked", "derived"):
        raise PermissionError(
            f"wiki page mutability={eff!r} forbids {op} "
            f"(page_id={page.get('id')} slug={page.get('slug')!r} "
            f"page_type={page.get('page_type')!r}). "
            f"Pass _sanctioned=True for server-side lifecycle transitions, "
            f"or use wiki_set_mutability to override."
        )
