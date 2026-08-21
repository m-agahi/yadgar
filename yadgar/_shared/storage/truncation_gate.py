"""Wiki size-collapse gate — ledger task 271.

WHY THIS EXISTS
---------------
A wiki update recorded ``"+43 -199 lines | ... | size: 15902 → 3717 bytes"``
**in its own change_summary** and shipped. ``compute_change_summary`` had
already measured the 77% loss, written it into the version row, and handed it
back to a write path that did nothing with it. The signal was computed and then
ignored — this train's defect class pointed at data rather than at an operator.

Nothing at HEAD detected a truncating wiki write. The similarity gate refuses
duplicate CREATES; Car J's mutability gate refuses writes to locked/derived
pages; the ADR-0208 discipline guard refuses rule removals on
``agent_discipline`` pages only. A generic page losing three quarters of its
body passed all three.

Enforcement sits at the STORAGE chokepoint (``_WikiMixin.update_wiki_page``),
beside ``mutability_gate.enforce_mutability``, for the same reason Car J chose
it: it is below ``WikiStore.add``'s upsert branch (the path the incident used),
below ``_apply_text_edit``'s eight anchor-text/positional ops, and below the
``admin_exec.wiki_update`` op that bypasses ``WikiStore`` entirely. A guard the
same instance can walk around is not a guard.

THE THRESHOLD IS MEASURED, NOT GUESSED
--------------------------------------
Ratio buckets over the live corpus (2 857 ``wiki_page_version`` rows with
``version > 1`` carrying a parseable ``size: X → Y``, measured 2026-08-21),
bucketed by ``floor(new/old * 10)``::

    <0.1   16 |  0.4    6 |  0.7   12 |  1.0  2302 (grow/same)
     0.1    1 |  0.5    3 |  0.8   21 |  >1.0  331
     0.2    1 |  0.6   11 |  0.9  151
     0.3    1

A trigger at 0.7 would fire on 39 writes, at 0.6 on 28 — and the 0.9 and 0.8
bands (151 + 21) sit immediately above, i.e. ordinary prunes crowd the region
just past the boundary. At **0.5** the guard fires on 25 rows, and with the
**1 024-byte floor** on 24: 0.84% of all updates ever made. The incident sits
at 0.234, well inside with margin.

Of those 24, fourteen are a single 22-second batch on 2026-07-14T10:49 — a
one-off campaign that replaced deprecated ``agent-prompt-*`` bodies with
``"DEPRECATED → renamed to [[…]]"`` stubs (those pages have since been
deleted). Excluding that campaign the steady-state rate is ten firings across
ten weeks — roughly one a week, each one a write worth a second look.

The FLOOR is what keeps the guard about content loss rather than about
arithmetic. The smallest real shrink in the corpus is 268 → 119 bytes: a
"55% collapse" of something the size of this paragraph. Small pages halve
routinely and losing 149 bytes is not the failure this exists for.

REFUSE, NOT WARN
----------------
A warning here would be a computed signal that gets ignored — which is the
defect, restated. ``WikiSizeCollapseError`` is an ``AdminRefusal`` so the
``/admin`` route renders it as a structured 409 naming its reason rather than a
bare HTTP 500 (see ``_shared/refusal.py``), and so an automated caller can tell
a deliberate rejection from a backend fault.

The ``reason`` is deliberately NOT folded into Car J's
``_REASON_BY_MUTABILITY`` values: the operator fix differs. A locked page needs
``wiki_set_mutability``; a collapsing write needs the author to either restore
the missing body or assert that the loss is intended.

NOT COUPLED TO ``_sanctioned``
------------------------------
``_allow_truncation`` is its own kwarg. Folding it into Car J's sanctioned
token would blind the guard to a REGENERATOR emitting a gutted page — the case
it is best placed to catch, since a regenerator writes a whole body from
scratch and has no author reading the result. Sanctioned writers that
legitimately shrink opt in one call site at a time:

* ``WikiStore.restore_version`` — the recovery path. Restoring an earlier,
  shorter version is the fix for an over-eager growth; it already bypasses the
  v5.39 similarity gate for this reason and ``_reject_if_discipline_weakening``
  exempts it by name.
* ``WikiStore.add`` when the canonical write seam marks the write sanctioned —
  full-body regeneration (task-list mirrors shrink as tasks complete, ADR
  bodies and agent-prompt pages are rewritten whole).

Everything else stays gated, including the metadata-only writers
(``adr_retype``, the Car K nightly sweep, migration 028), which write
``page_type`` alone and therefore never reach the ratio at all, and
``admin_exec.rollup``, whose regeneration IS gated on purpose: a rollup that
collapses is a generator bug, and its failure mode when refused is a stale
rollup plus a logged refusal, not data loss.

RESIDUAL
--------
``_allow_truncation=True`` disables the check outright for that write,
including a write to an empty body. No separate empty-page floor is enforced:
a second, unescapable threshold would contradict the requirement that a
deliberate rewrite stays possible, and ``wiki_delete`` is the tool for removing
a page.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.refusal import AdminRefusal

__all__ = [
    "TRUNCATION_MIN_OLD_BYTES",
    "TRUNCATION_RATIO_THRESHOLD",
    "WikiSizeCollapseError",
    "enforce_no_size_collapse",
]

#: A write is refused when the new body is under this fraction of the old one.
#: Measured against the live corpus — see the module docstring's bucket table.
TRUNCATION_RATIO_THRESHOLD = 0.5

#: …and only when the OLD body was at least this many bytes. Below the floor a
#: halving is noise, not content loss (the corpus's smallest real case is
#: 268 → 119 bytes).
TRUNCATION_MIN_OLD_BYTES = 1024


class WikiSizeCollapseError(AdminRefusal, ValueError):
    """A wiki update would drop most of the page's body. A DECISION, not a fault.

    Subclasses ``ValueError`` alongside ``AdminRefusal`` for the same reason
    ``WikiImmutableError`` subclasses ``PermissionError``: the storage layer's
    existing vocabulary for a rejected wiki write is ``ValueError``
    (``admin_exec.wiki_update`` raises it for a missing page), so a caller with
    a broad ``except`` keeps behaving as before, while the ``AdminRefusal``
    half re-files it as a structured 409.

    That second base is safe only if nothing SHADOWS the refusal, since Python
    matches ``except`` clauses in written order rather than by specificity —
    checked, not assumed: the ``/admin`` route's arms are ``KeyError`` then
    ``AdminRefusal``, with no ``ValueError`` arm, and neither ``except
    ValueError`` left in the wiki tree sits on the update path (one parses an
    int out of a ``"Heading#2"`` spec, one guards read-side project
    resolution). If a ``ValueError`` arm is ever added ahead of the refusal
    arm, this base has to go —
    ``RestoreVerificationError(AdminRefusal, RuntimeError)`` is the in-tree
    precedent for picking a neutral builtin instead.
    """

    reason = "wiki_size_collapse"

    def __init__(self, page: dict, new_content: str, op: str) -> None:
        self._page = page
        self.old_bytes = len((page.get("content") or "").encode())
        self.new_bytes = len((new_content or "").encode())
        self._op = op
        # The message is composed HERE, from the same numbers
        # ``refusal_report`` returns, rather than passed in by the raise site —
        # so the prose and the structured evidence cannot drift apart. The
        # incident this gate exists for was precisely a report and a write that
        # disagreed. It also has to NAME the way forward: a refusal that leaves
        # the operator with no route is the same defect wearing a hat.
        pct = 100.0 * (self.old_bytes - self.new_bytes) / (self.old_bytes or 1)
        super().__init__(
            f"wiki_size_collapse: {op} would drop {pct:.0f}% of the page body "
            f"(size: {self.old_bytes} \u2192 {self.new_bytes} bytes, "
            f"page_id={page.get('id')} slug={page.get('slug')!r}). Refused "
            f"because the new body is under {TRUNCATION_RATIO_THRESHOLD:.0%} of "
            f"the old one and the old one was at least "
            f"{TRUNCATION_MIN_OLD_BYTES} bytes. If the page really did lose "
            f"that much, re-read it and write back the WHOLE intended body. "
            f"If the loss is intended, say so: "
            f"wiki_add(..., allow_truncation=True) on the whole-page write "
            f"path, or _allow_truncation=True at the storage call for a "
            f"server-side regeneration. To undo a truncation that already "
            f"landed, use wiki_restore(slug, version)."
        )

    @observe(tier="hot", span=False)
    def refusal_report(self) -> dict:
        """The measurement itself — the thing the incident wrote down and dropped."""
        return {
            "page_id": self._page.get("id"),
            "slug": self._page.get("slug"),
            "page_type": self._page.get("page_type"),
            "old_bytes": self.old_bytes,
            "new_bytes": self.new_bytes,
            "removed_bytes": self.old_bytes - self.new_bytes,
            "ratio": (self.new_bytes / self.old_bytes) if self.old_bytes else 1.0,
            "ratio_threshold": TRUNCATION_RATIO_THRESHOLD,
            "min_old_bytes": TRUNCATION_MIN_OLD_BYTES,
            # NOT ``op``: the envelope's ``op`` is the /admin op name, and the
            # storage method this gate guards is a different thing.
            "wiki_op": self._op,
        }


@observe(tier="hot", span=False)
def enforce_no_size_collapse(
    page: dict,
    new_content: str,
    *,
    op: str,
    allowed: bool,
) -> None:
    """Raise ``WikiSizeCollapseError`` when *new_content* collapses *page*'s body.

    *allowed=True* bypasses the gate. Like Car J's ``sanctioned``, it is a
    kwarg on the storage method rather than a separate signal, so a caller that
    has not been given it physically cannot pass it.

    Byte length, not character length: it is what ``compute_change_summary``
    already reports in the version row, so the refusal's numbers and the
    change_summary's numbers are the same numbers.
    """
    if allowed:
        return
    old_bytes = len((page.get("content") or "").encode())
    if old_bytes < TRUNCATION_MIN_OLD_BYTES:
        return
    if len((new_content or "").encode()) >= old_bytes * TRUNCATION_RATIO_THRESHOLD:
        return
    raise WikiSizeCollapseError(page, new_content, op)
