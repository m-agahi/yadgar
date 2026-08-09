"""ADR slug helpers — the per-ADR page slug is the legacy fallback path.

Car G (0047 §7): the parser/serializer/index-render machinery that lived
here pre-G is DELETED. The seeded ``adr`` ledger rows + ``MariaStorageEngine``
CRUD (``create_adr_row`` / ``list_adr_rows`` / ``set_adr_body_slug``) own
ID allocation, body-slug linkage, and the index view. The Car L
``{project_id}_adr-NNNN`` re-slug is shipped (D32 ③) but the operator runs
it dry-run by default; the legacy ``<project>-adr-NNNN`` slug persists
for the 194 not-yet-reslugged pages — ``adr_page_slug`` MUST stay until
the reslug op runs end-to-end so the ``adr_get`` body-fetch fallback
(``adr.py::_fetch_adr_body_page``) keeps resolving legacy pages.
"""

from __future__ import annotations

import os

from yadgar._shared.observability.observe import observe


@observe(
    exempt=(
        "pure slug formatter: no I/O, no storage side effect, no error branch; "
        "observability would add a per-call span with zero diagnostic value"
    )
)
def adr_page_slug(resolved: str, adr_id: str) -> str:
    """The legacy per-ADR page slug ``<project>-adr-NNNN``.

    ``adr_id`` is an "ADR-NNNN" string; the slug lowercases it to ``adr-NNNN``
    (slugify maps ``ADR-0001`` → ``adr-0001``), so the stored wiki title must
    equal this slug string to make ``_slugify(title)`` deterministic.

    Retained post-Car-G so ``adr_get`` can fall back to the legacy slug when
    Car L's reslug op has not yet rewritten a page (D32 ③ ships the op dry-run
    by default). The canonical post-reslug slug is
    ``{project_id}_adr-NNNN`` (with ``/`` → ``_``); ``adr_add`` writes that
    shape directly (``adr.py::_write_adr_body_page``).
    """
    return f"{os.path.basename(resolved)}-{adr_id.lower()}"
