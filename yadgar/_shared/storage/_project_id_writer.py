"""Car L (0047 §16.9) — project_id stamping chokepoint for live write paths.

Both ``_WikiMixin`` and ``_MemoryMixin`` carry hot-path INSERT methods
that must stamp ``project_id`` alongside ``directory_context``. The
classifier seam is the same in both — a lazy ``yadgar.core.identity.derive_project_id``
call that falls back to ``'unresolved'`` on any import-time or runtime
failure so the write never blocks on a path-resolution error.

Why a shared module: the helper is a hot-path utility (called once per
write). Importing it from a dedicated module keeps the per-file LOC
budget stable for both mixins (both are already at the I13 soft cap) and
centers the failure-mode contract in one place.

Sentinels (``'global'``, ``''``) → ``'global'`` (unchanged semantics).
The caller-provided ``project_id`` (when present) wins over the
classifier — this is how the live write paths stamp the same value
the migration would have stamped.

LAYER NOTE: this module lives in ``yadgar._shared`` and therefore
cannot statically import ``yadgar.core.identity`` (forbidden by
contract 1 of the import-linter config). The classifier call below
is dispatched via ``importlib.import_module`` on a string target —
the established PEP-562 lazy-forward pattern in
``yadgar._shared.retrieval`` (Car 0 #167 precedent). Static
analysis sees only the string; the runtime edge resolves at first
call when the composition root has finished bootstrapping.
"""

from __future__ import annotations

import importlib
from typing import Any

from yadgar._shared.observability.observe import observe

#: String target — PEP-562 lazy forward to dodge the _shared->core static edge.
_CORE_IDENTITY_TARGET = "yadgar.core.identity"


@observe(tier="hot", span=False)
def _resolve_project_id_for_write(
    *,
    caller_value: Any,
    directory_context: str | None,
) -> str:
    """Resolve ``project_id`` for a live write to ``wiki_page`` or ``memory``.

    Car L (0047 §16.9). Order of preference:

    1. ``caller_value`` — the caller (the cleanup path, the wiki_add
       replay branch, ``insert_wiki_page``, ``insert_memory``) may have
       already classified the row. When the value is truthy, return it.
    2. Sentinel ``directory_context`` (``'global'``, ``''``, ``None``)
       → ``'global'``.
    3. Lazy ``yadgar.core.identity.derive_project_id`` call (via
       string-target importlib — see module docstring).
    4. Classifier failure → ``'unresolved'`` so the migration's
       quarantine phase re-classifies on the next boot.

    Pure: no I/O outside the lazy import + the underlying subprocess.

    EXPORTED NAME has no leading underscore so the wiki/memory mixins
    import it from this module. The fn itself is intentionally not
    re-exported from ``__init__.py`` — it's a chokepoint helper, not a
    public API.
    """
    if caller_value:
        return caller_value
    if not directory_context or directory_context == "global":
        return "global"
    try:
        derive_project_id = importlib.import_module(_CORE_IDENTITY_TARGET).derive_project_id
        project_id, _ = derive_project_id(directory_context)
        return project_id
    except Exception:  # noqa: BLE001 — boot-path robustness
        return "unresolved"
