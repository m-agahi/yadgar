"""Car L (0047 §16.9) — project_id stamping chokepoint for live write paths.

Both ``_WikiMixin`` and ``_MemoryMixin`` carry hot-path INSERT methods
that must stamp ``project_id`` alongside ``directory_context``. This module is
the one seam both go through, so the failure-mode contract is stated once.

**C5 made that contract "the caller's value, or a raise" — there is no other
branch left.** What used to be here: a ``'global'`` return for a sentinel
``directory_context``, a lazy ``yadgar.core.identity.derive_project_id``
classifier reached through ``importlib``, and an ``'unresolved'`` catch around
it. All three are deleted (ADR-0227). The lazy-import layer note that justified
the ``importlib`` string target went with the import it justified: nothing here
reaches ``yadgar.core`` any more, so contract 1 of the import-linter config is
satisfied structurally rather than by indirection.

Why a shared module: the helper is a hot-path utility (called once per
write). Importing it from a dedicated module keeps the per-file LOC
budget stable for both mixins (both are already at the I13 soft cap).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

#: Values that name NO project. ``'global'`` and ``'unresolved'`` are the two
#: manufactured identities ADR-0227 deletes; ``'system'`` is the pre-v5.64
#: mis-stamp sink. A source row carrying one of these cannot tell a derived
#: write which project it belongs to, so it does not get a vote.
_NON_IDENTIFYING_PROJECT_IDS: frozenset[str] = frozenset({"", "global", "unresolved", "system"})


@observe(tier="hot", span=False)
def resolve_project_id_from_rows(rows: Iterable[dict]) -> str | None:
    """Return the single ``project_id`` shared by *rows*, or ``None``.

    C4 (0047 PR#40 §5) — the sessionless-writer counterpart to
    ``dominant_directory``. Same voting shape, opposite failure mode: where
    ``dominant_directory`` collapses "0 or ≥2 distinct" to the literal
    ``"global"``, this returns ``None`` so the caller can take its declared
    skip-and-count path. ADR-0227: an identity is never inferred and never
    substituted, and a derived memory spanning two projects belongs to
    neither.

    This is NOT a derivation. Each row's ``project_id`` was stamped by the
    session that wrote it; reading it back is inheritance, which is exactly
    what "travels as an explicit caller parameter" (ADR-0202) means for a
    write whose inputs are other rows.

    Args:
        rows: source rows (memory dicts) that the derived write is built from.

    Returns:
        The one distinct identifying ``project_id``, or ``None`` when the
        inputs name zero projects or more than one.
    """
    real: set[str] = set()
    for row in rows:
        candidate = row.get("project_id")
        if isinstance(candidate, str) and candidate not in _NON_IDENTIFYING_PROJECT_IDS:
            real.add(candidate)
    if len(real) == 1:
        return next(iter(real))
    return None


@observe(tier="hot", span=False)
def observe_project_id_skip(writer: str, count: int = 1) -> None:
    """Count *count* writes skipped by *writer* for want of a nameable project.

    Skip-and-count is the fail-loud form for the nightly cycle: loud in
    metrics, non-fatal to the cycle. ADR-0227 predicts ``cleanup.py``'s
    ``"unknown"`` bucket becoming "an unhandled raise inside the nightly
    cycle" — it must not, because a sweep that dies on one bad row is worse
    than one that reports it.

    Never raises: a metrics backend that is absent or misconfigured must not
    turn a skipped row into a failed cycle.
    """
    try:
        from yadgar._shared.observability.metrics import (  # noqa: PLC0415
            yadgar_project_id_skipped_total,
        )

        yadgar_project_id_skipped_total.labels(writer=writer).inc(count)
    except ImportError:
        logger.debug("project_id skip metric unavailable (non-fatal)", exc_info=True)


@observe(tier="hot", span=False)
def project_id_set_fragment(
    project_id: str | None, *, param: str = "project_id"
) -> tuple[str, dict]:
    """Return the ``SET project_id = …`` fragment + params for an INSERT.

    C11 (0047 PR#40 §5). This exists because of a real SurrealDB behaviour that
    a bound ``None`` does NOT satisfy: ``project_id`` is declared
    ``option<string>`` by migration 033, and binding Python ``None`` sends
    ``NULL``, which the coercer rejects —

        Couldn't coerce value for field `project_id` of `memory_block:1`:
        Expected `none | string` but found `NULL`

    ``NONE`` must therefore appear as a LITERAL in the statement, exactly as the
    surrounding writers already do for ``directory = NONE``. Centralised here so
    the four C11 writers cannot each rediscover it — and so the ADR-0227 rule is
    expressed once: **an absent identity writes NONE. It is never substituted
    with a path, a basename, or a sentinel.**

    Args:
        project_id: The caller's resolved identity, or falsy for "none named".
        param: Bind-parameter name, so the fragment can join a statement that
            already uses ``$project_id`` for something else.

    Returns:
        ``(sql_fragment, params_dict)`` — the fragment never has a trailing
        comma, so the caller controls the join.
    """
    if project_id:
        return f"{param} = ${param}", {param: project_id}
    return f"{param} = NONE", {}


@observe(tier="hot", span=False)
def _resolve_project_id_for_write(
    *,
    caller_value: Any,
    directory_context: str | None,
) -> str:
    """Return ``caller_value``, or raise. There is no second branch.

    Car L (0047 §16.9); amended by C3, finished by C5 (0047 PR#40 §5).

    ``caller_value`` is the ONLY honest input: this function runs inside the
    backend/core containers, which have no git binary and no host project
    mounts, so nothing here can derive an identity (§1.1 / ADR-0227). A falsy
    ``caller_value`` is a caller defect, and C5 is where it stops being logged
    and starts being fatal.

    ``directory_context`` is no longer a resolution source. It is accepted only
    so the raise can name the write that failed — a filesystem path is a useful
    thing to see in an error and a catastrophic thing to derive an identity
    from, which is the whole distinction this car draws.

    Deleted here, listed so the next reader does not reinvent one:

    * ``if not directory_context or directory_context == "global": return
      "global"`` — the single line that MINTED the sentinel §1.4 forbids, and
      the one a ``GLOBAL_FALLBACK`` / ``"unresolved"`` / ``local/`` grep would
      not have caught.
    * the lazy ``derive_project_id`` classifier reached via ``importlib``.
    * the ``except → 'unresolved'`` catch around it.

    Pure: no I/O at all now.

    EXPORTED NAME has no leading underscore so the wiki/memory mixins
    import it from this module. The fn itself is intentionally not
    re-exported from ``__init__.py`` — it's a chokepoint helper, not a
    public API.

    Raises:
        UnresolvedProjectError: ``caller_value`` is falsy.
    """
    if caller_value:
        return caller_value
    raise UnresolvedProjectError(
        "storage write",
        detail=(
            f"(directory_context={directory_context!r}; the enqueueing tool must "
            "stamp project_id — the container that executes the write has no git "
            "binary and no host project mounts)"
        ),
    )
