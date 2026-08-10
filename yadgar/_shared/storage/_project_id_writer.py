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
import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

#: String target — PEP-562 lazy forward to dodge the _shared->core static edge.
_CORE_IDENTITY_TARGET = "yadgar.core.identity"


@observe(tier="hot", span=False)
def _resolve_project_id_for_write(
    *,
    caller_value: Any,
    directory_context: str | None,
) -> str:
    """Resolve ``project_id`` for a live write to ``wiki_page`` or ``memory``.

    Car L (0047 §16.9); amended by C3 (0047 PR#40 §5.C3).

    ``caller_value`` is MANDATORY — keyword-required, and every caller is
    expected to pass a real, session-minted project_id. It is the ONLY
    honest input: this function runs inside the backend/core containers,
    which have no git binary and no host project mounts, so nothing here
    can derive an identity (§1.1 / ADR-0227). Passing ``None`` is a
    caller defect that C5 turns into a raise.

    Order of preference:

    1. ``caller_value`` — truthy → return it. THE path after C3: the core
       tool resolves in the process that can see the session and stamps the
       value onto the queue payload / page dict, so the drainer and the
       storage mixins carry it rather than compute one.
    2. Sentinel ``directory_context`` (``'global'``, ``''``, ``None``)
       → ``'global'``.
    3. **C5: DELETE** — lazy ``derive_project_id`` (via string-target
       importlib, see module docstring). Reachable only from a legacy
       payload enqueued before C3 or a caller that has not been converted.
    4. **C5: DELETE** — classifier failure → ``'unresolved'``.

    Tiers 3 and 4 log a WARNING naming the offending ``directory_context``,
    so a surviving caller is observable before C5 makes it fatal. C3's
    plan text called for ``warnings.warn``; measured, that is not
    survivable in this repo — ``filterwarnings = ["error"]`` in
    ``pyproject.toml`` turns it into a hard failure, and it reddened **167
    tests in ``yadgar/tests/_shared`` alone**. That is C5's semantics
    landing in C3, whose whole constraint is that nothing fails loud yet,
    and broadening the warnings filter to survive it would weaken a
    repo-wide gate for one car. A log line is the additive form.

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
    # C5: DELETE — everything below this line, both tiers.
    logger.warning(
        "project_id write reached the container-side derivation fallback: no "
        "caller_value for directory_context=%r. Neither container can derive an "
        "identity (no git binary, no host project mounts), so this yields "
        "local/<basename> or 'unresolved'. The caller must thread the "
        "session-minted project_id; C5 makes its absence raise.",
        directory_context,
    )
    try:
        derive_project_id = importlib.import_module(_CORE_IDENTITY_TARGET).derive_project_id
        project_id, _ = derive_project_id(directory_context)
        return project_id
    except Exception:  # noqa: BLE001 — boot-path robustness
        # C5: DELETE — a write that cannot name its project must fail, not
        # invent a sentinel the corpus then has to be swept for.
        logger.warning(
            "project_id classifier failed for directory_context=%r — stamping "
            "'unresolved'. C5 deletes this fallback.",
            directory_context,
        )
        return "unresolved"
