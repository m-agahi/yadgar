"""Effective project_id resolution for cross-project MCP tool overrides (Car M).

§16.6 / §16.11 (0047 spine train, Car M):

The MCP tool surface gains an OPTIONAL ``project: str | None = None`` parameter
that lets a caller address another project without leaving the current
working tree. Default = the derived current project (from SessionStart
context — Car E extends ``yadgar/core/hooks/session-start-context.py``).
Override = the registry-validated caller-supplied ``project``.

PREVALENCE RULE (§9 [VERIFY] — applied as the proposed default):

    1. ``project`` supplied → use it (override). Non-empty string only;
       backends reject unknown keys (FAIL-LOUD per ADR-0202 amendment,
       backend-side via ``_ensure_project_exists_sync``; core NEVER
       touches the DB — §15).
    2. ``session_project`` (Car E SessionStart context) → use it.
       May still be ``None`` until Car E lands.
    3. ``directory``-derived via ``yadgar.core.identity.derive_project_id``
       (Car A0): resolves the canonical ``owner/repo`` (host excluded) or
       the ``local/<basename>`` fallback.
    4. None of the above → ``"global"`` (the cross-project sentinel;
       consistent with the WikiStore ``unresolved``/``local`` paths).

When BOTH ``project`` AND ``directory`` are supplied, ``project`` wins
(``directory`` is logged-and-ignored). A caller that supplies a stale
``directory`` from another project still gets the right project_id
scoping — the parameter that ACTUALLY identifies the project takes
precedence. The warning is logged at INFO so a misuse is observable
without raising.

§15 / ADR-0078 — core NEVER touches the DB. Car M does NOT register a
``project_exists`` admin op here; the cheap path (string-level guard +
backend-side FAIL-LOUD at write time) matches Car D's task-tool pattern
(see ``task.py:_validate_project_id`` + ADR-0202). The PTC-cached
read-side validation is §15.1's scope (not yet built).
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar.core.identity import derive_project_id

logger = logging.getLogger(__name__)


# Sentinel for "no project resolution at all" — same shape the wiki/memory
# paths use for the cross-project fallback (see WikiStore.unresolved,
# memory-stamp ``local/<basename>`` paths).
GLOBAL_FALLBACK: str = "global"


class InvalidProjectOverrideError(ValueError):
    """The supplied ``project=`` is malformed at the type level.

    Car M performs the cheap type-level guard (non-empty string) here in
    core. The deep "project_id exists in the registry?" check is delegated
    to the backend's ``_ensure_project_exists_sync`` (Car A0, §16.5) so
    core stays free of DB calls (§15). This exception is ONLY for caller
    errors that prevent the request from reaching the wire at all.
    """


@observe(tier="hot", span=False)
def resolve_effective_project(
    *,
    project: str | None,
    directory: str | None,
    session_project: str | None,
) -> str:
    """Resolve the effective project_id for a tool call.

    Args:
        project: Caller-supplied override. When ``None`` falls through to
            ``session_project`` → ``directory``-derived → ``GLOBAL_FALLBACK``.
        directory: Host-side project directory, used only as the LAST-RESORT
            derivation source. When ``project`` is also supplied,
            ``directory`` is logged-and-ignored (project wins — §9 [VERIFY]).
        session_project: Value the SessionStart hook surfaces to the session
            (Car E). ``None`` until Car E lands; until then the fallback
            chain skips it.

    Returns:
        The validated project_id string. Never empty — the chain ends at
        ``GLOBAL_FALLBACK`` (= ``"global"``) when nothing resolves.

    Raises:
        InvalidProjectOverrideError: when ``project`` is supplied but not a
            non-empty string (type-level guard; deep registry validation
            is backend-side per §15).
    """
    # ── 1. Override path (caller-supplied) ─────────────────────────────────
    if project is not None:
        if not isinstance(project, str):
            raise InvalidProjectOverrideError(
                f"project must be a string, got {type(project).__name__}"
            )
        if not project:
            raise InvalidProjectOverrideError("project must be non-empty when supplied")
        # Plan §9 [VERIFY]: "project wins, directory ignored with a warning".
        # Defensive: a caller that supplies BOTH clearly intended the project
        # to win (it is the newer, more-precise key). The directory is logged
        # so a misuse is observable without raising.
        if directory is not None and (directory or "").strip():
            logger.info(
                "project_param: project=%r overrides supplied directory=%r",
                project,
                directory,
            )
        return project

    # ── 2. SessionStart context (Car E) ────────────────────────────────────
    if session_project is not None and session_project:
        return session_project

    # ── 3. directory-derived (Car A0 identity.derive_project_id) ────────────
    if directory is not None and (directory or "").strip():
        try:
            _derived = derive_project_id(cwd=directory.strip())
            project_id: str = _derived[0]
        except Exception as exc:  # noqa: BLE001
            # Identity derivation is best-effort: a non-git dir, a missing
            # remote, or a corrupt config must not crash a tool call.
            logger.warning(
                "project_param: derive_project_id failed for directory=%r: %s",
                directory,
                exc,
            )
            return GLOBAL_FALLBACK
        if project_id:
            return project_id

    # ── 4. No resolution — global fallback (consistent with WikiStore) ─────
    return GLOBAL_FALLBACK


@observe(tier="hot", span=False)
def accept_project_param(project: str | None, directory: str | None) -> str | None:
    """C3 boundary guard for a tool whose scope key is still ``directory``.

    C3 (0047 PR#40 remediation §5.C3) adds ``project`` to every scoped MCP
    tool. For the tools whose read/write path already has a ``project_id``
    sink (``recall``, ``memorize``, ``wiki_add``, ``adr_*``, ``task_*``) the
    resolved value is threaded for real. For the rest, the scope key does not
    become ``project_id`` until **C7** re-keys the WHERE clause (and C11 adds
    the missing per-table columns) — so this helper is what the parameter
    reaches in the meantime, and its call sites are the exact list of
    signatures C7 has to revisit::

        git grep -n 'accept_project_param' -- yadgar/core/server/tools

    What it DOES do, today: validate the caller's override at the MCP
    boundary, so a malformed ``project=`` (empty string, non-string) raises
    ``InvalidProjectOverrideError`` at the edge instead of being ignored.

    What it deliberately does NOT do: run the resolver's derivation tiers
    when ``project`` is ``None``. Those tiers call ``derive_project_id``,
    which shells out to ``git`` twice and is not cached; paying that on every
    call of every scoped tool to compute a value nothing reads yet would be a
    straight latency regression. The session-side resolve belongs on the
    paths that actually stamp a row.

    KNOWN, ACCEPTED GAP UNTIL C7: a caller who passes ``project=`` to one of
    these tools gets their CURRENT project's rows back, not the named
    project's. That is inherent to the additive ordering §2 chose (C3 adds
    the parameter, C5 makes absence fail loud, C7 re-keys the scope) and all
    three land in the same PR.

    Returns:
        The validated project_id when the caller supplied one, else ``None``.
    """
    if project is None:
        return None
    return resolve_effective_project(
        project=project,
        directory=directory,
        session_project=None,
    )


__all__ = [
    "GLOBAL_FALLBACK",
    "InvalidProjectOverrideError",
    "accept_project_param",
    "resolve_effective_project",
]
