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
from yadgar._shared.refusal import AdminRefusal
from yadgar._shared.wiki.policy import get_effective_mutability

# Per effective-mutability rejection codes. NOT collapsed into one: the operator
# fix differs — a ``locked`` page needs ``wiki_set_mutability`` (or a sanctioned
# lifecycle transition), a ``derived`` page needs its GENERATOR re-run, because
# hand-edits there are overwritten by the next regeneration.
_REASON_BY_MUTABILITY = {
    "locked": "wiki_page_locked",
    "derived": "wiki_page_derived",
}


class WikiImmutableError(AdminRefusal, PermissionError):
    """Car J's lock refused a write. A DECISION, not a fault.

    Subclasses ``PermissionError`` so every pre-existing catcher keeps working —
    and so the builtin stays distinguishable from this. A filesystem EACCES is a
    genuine fault and must keep surfacing as one; only this type is the
    intentional outcome the ``/admin`` route re-files as a structured refusal
    (``_shared/refusal.py``).
    """

    def __init__(self, message: str, *, mutability: str, page: dict, op: str) -> None:
        super().__init__(message)
        self.reason = _REASON_BY_MUTABILITY.get(mutability, "wiki_page_immutable")
        self.mutability = mutability
        self._page = page
        self._op = op

    @observe(tier="hot", span=False)
    def refusal_report(self) -> dict:
        return {
            "mutability": self.mutability,
            "page_id": self._page.get("id"),
            "slug": self._page.get("slug"),
            "page_type": self._page.get("page_type"),
            # NOT ``op``: the envelope's ``op`` is the /admin op name, and the
            # storage method this gate guards is a different thing.
            "wiki_op": self._op,
        }


@observe(tier="hot", span=False)
def enforce_mutability(
    page: dict,
    *,
    op: str,
    sanctioned: bool,
) -> None:
    """Raise ``WikiImmutableError`` if *page*'s effective mutability forbids *op*.

    *sanctioned=True* bypasses the gate (server-side lifecycle transitions:
    Car G supersede retype, Car K nightly sweep). Sanctioned is a kwarg on
    the storage method — no separate signal — so unsanctioned callers
    physically cannot pass True.

    Values:
    - ``"free"`` — never raised.
    - ``"locked"`` — raise ``WikiImmutableError`` unless *sanctioned*.
    - ``"derived"`` — raise ``WikiImmutableError`` unless *sanctioned*
      (regenerated, not hand-edited).

    The raised type is a ``PermissionError`` AND an ``AdminRefusal``, so the
    ``/admin`` route renders it as a structured rejection naming its reason
    rather than as a bare HTTP 500 (ledger task 294).
    """
    if sanctioned:
        return
    eff = get_effective_mutability(page)
    if eff in ("locked", "derived"):
        raise WikiImmutableError(
            f"wiki page mutability={eff!r} forbids {op} "
            f"(page_id={page.get('id')} slug={page.get('slug')!r} "
            f"page_type={page.get('page_type')!r}). "
            f"Pass _sanctioned=True for server-side lifecycle transitions, "
            f"or use wiki_set_mutability to override.",
            mutability=eff,
            page=page,
            op=op,
        )
