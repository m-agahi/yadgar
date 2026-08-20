"""Effective project_id resolution for cross-project MCP tool overrides (Car M).

§16.6 / §16.11 (0047 spine train, Car M):

The MCP tool surface gains an OPTIONAL ``project: str | None = None`` parameter
that lets a caller address another project without leaving the current
working tree. Default = the derived current project (from SessionStart
context — Car E extends ``yadgar/core/hooks/session-start-context.py``).
Override = the registry-validated caller-supplied ``project``.

PREVALENCE RULE — **two tiers, and no third (C5 / ADR-0227):**

    1. ``project`` supplied → use it (override). Non-empty string only;
       backends reject unknown keys (FAIL-LOUD per ADR-0202 amendment,
       backend-side via ``_ensure_project_exists_sync``; core NEVER
       touches the DB — §15).
    2. ``session_project`` (SessionStart context) → use it.
    3. Neither → ``UnresolvedProjectError``.

C5 deleted the two tiers that used to sit under those: a ``directory``
derivation through ``derive_project_id``, and a final ``return "global"``.
ADR-0227's rationale, in one sentence: *"A fallback that cannot fail is worse
than an error, because it manufactures a plausible-looking wrong answer."*
Both deleted tiers ran inside containers with no git binary and no host
project mounts, so neither could produce a correct answer even in principle —
``local/<basename>`` and ``"global"`` are well-formed keys that pass every type
check and are indistinguishable at read time from a real one.

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

from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import _NON_IDENTIFYING_PROJECT_IDS
from yadgar._shared.storage.directory import GLOBAL_REACH_TAG

logger = logging.getLogger(__name__)


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
    tool: str = "yadgar tool",
) -> str:
    """Resolve the effective project_id for a tool call, or raise.

    Args:
        project: Caller-supplied override. When ``None`` falls through to
            ``session_project``, and then to a raise.
        directory: Host-side project directory. **No longer a resolution
            source** (C5 deleted the derivation tier); it is retained only so a
            caller that supplies BOTH gets the ignore logged, which is how a
            stale directory stays observable.
        session_project: Value the SessionStart hook surfaces to the session.
        tool: Name of the calling tool, used to build the structured error. The
            reader of a raise is an agent that has to correct its own call, so
            an error that does not name the tool is only marginally more useful
            than the fallback it replaced.

    Returns:
        The validated project_id string. Never empty, never a sentinel.

    Raises:
        InvalidProjectOverrideError: when ``project`` is supplied but not a
            non-empty string (type-level guard; deep registry validation
            is backend-side per §15).
        UnresolvedProjectError: when neither ``project`` nor ``session_project``
            names an identity. ADR-0227: never defaulted, never inferred, never
            silently substituted.
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

    # ── 2. Per-request ContextVar (Car B §3.4) ─────────────────────────────
    # The tool wrapper in ``yadgar/core/server/_app.py:_instrumented_async``
    # reads the Mcp-Session-Id off the inbound request, looks up the
    # binding registered by /session_bind, and stamps this ContextVar.
    # Stdio / stateless_http callers have no Mcp-Session-Id; the
    # ContextVar stays unbound and we fall through to tier 3.
    from yadgar._shared.runtime.session_project import (  # noqa: PLC0415
        get_current_session_project,
    )

    _ctx_session_project = get_current_session_project()
    if _ctx_session_project:
        return _ctx_session_project

    # ── 3. SessionStart legacy parameter (fallback when ContextVar unbound) ──
    # Plan §3.4: "If unbound, fall back to explicit ``project=``" — re-read
    # as: if the ContextVar did not resolve the identity, do NOT add a new
    # tier; honour the legacy ``session_project`` keyword as a fallback
    # (the SessionStart hook still passes it through, and tests assert
    # it as the tier-3 path). Honouring the explicit ``project=`` was
    # already done in tier 1; the one line that flips the fallback is
    # the ``return session_project`` below.
    if session_project is not None and session_project:
        return session_project

    # ── 3b. Hook-authored directory map (the global-config tier) ───────────
    # With ONE global ``mcpServers`` entry — the common setup — every request
    # is identical on the wire, and the daemon runs ``stateless_http=True`` so
    # there is no Mcp-Session-Id either. The only per-call signal that varies
    # is ``directory``.
    #
    # This is a LOOKUP, not a derivation, and the difference is the whole of
    # ADR-0227: the SessionStart hook mints host-side (where the working tree
    # exists) and registers the pair; this asks only "has the hook told me
    # about this exact directory?". An unregistered directory returns None and
    # falls through to the raise below — no key is ever manufactured from a
    # path, which is the failure mode the ADR deletes.
    from yadgar._shared.runtime.session_map import (  # noqa: PLC0415
        lookup_project_for_directory,
    )

    _mapped = lookup_project_for_directory(directory)
    if _mapped:
        return _mapped

    # ── 4. Nothing named an identity — FAIL LOUD (C5 / ADR-0227) ───────────
    #
    # What used to be here: a ``derive_project_id(cwd=directory)`` tier and a
    # ``return GLOBAL_FALLBACK``. Both are deleted. A directory is not an
    # identity — it is a filesystem hint that happened to be adjacent to one,
    # and the process reading it cannot see the tree it names.
    raise UnresolvedProjectError(
        tool,
        detail=(
            f"(directory={directory!r} is a filesystem hint, not an identity; "
            "the SessionStart banner prints the value to pass)"
        ),
    )


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

    What it deliberately does NOT do: resolve when ``project`` is ``None``.
    Before C5 that was a latency argument (the derivation tier shelled out to
    ``git`` twice, uncached, to compute a value nothing read yet). After C5 the
    tier is gone, so it is a semantics argument instead: these tools' scope key
    is still ``directory`` until C7 re-keys it, and raising here would fail a
    call that does not yet need an identity.

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
        tool="accept_project_param",
    )


@observe(exempt="pure single-value shape predicate on one string field; no I/O, no storage access")
def project_id_value_error(value: object) -> str | None:
    """Return the rejection message for a ``project_id`` WRITE, or ``None``.

    Ledger task 262. Sibling of ``resolve_effective_project`` above, and
    deliberately NOT folded into it: that function resolves which project a
    CALL addresses (override → session → raise); this one validates a
    project_id being written INTO a row as data. ``memory_update`` is the
    first caller — it patches the column, so it never goes through the
    resolver at all.

    Lives here rather than in ``admin_other.py`` for the same reason
    ``_shared/storage/_project_id_writer.py`` gives for its own existence:
    that file is at the I13 per-file LOC budget, and this module is the
    tools-layer home for project_id validation.

    SHAPE ONLY — the same two rules the WIKI half of this fix applies (ledger
    task 246, branch ``fix/wiki-set-metadata-project-id`` @ ``6fa99512``,
    which adds ``WikiStore._metadata_value_error``). **That branch is NOT on
    master yet**, so do not expect to grep the symbol. The RULE stays
    duplicated even once it lands: that helper dispatches on WIKI field names
    and sits in the module defining ``WikiStore``, so importing it would drag
    the wiki store into this import graph and let a wiki-side dispatch edit
    silently change memory validation. What IS shared is the AUTHORITY — both
    halves read the one ``_NON_IDENTIFYING_PROJECT_IDS`` frozenset (Car C4, on
    master), so the sentinel set cannot drift.

    NO REGISTRY CHECK (ADR-0078 ``_ensure_project_exists_sync``), deliberately,
    and for a stronger reason than the wiki half had: that function has ZERO
    production call sites (definition + ``__all__`` + docstrings that claim it
    enforces something it never runs for), and the CREATE paths are LOOSER
    than this gate — ``resolve_effective_project`` validates non-empty-string
    and nothing else, so it would happily stamp ``'global'`` on a new row.
    The correction being stricter than the creation is therefore the OPPOSITE
    of the asymmetry task 246 argued against; a real registry check belongs on
    the create path first.

    What is rejected, and why each matters:
      * non-string / empty — ``update_memory_fields`` writes the bare ``NONE``
        literal for any falsy ``project_id`` (``option<string>``, migration
        033), so an empty string does not fail loudly, it NULLS the column.
      * ``None`` — same NULL, and nulling reproduces exactly the
        unreachability the restamp path exists to repair.
      * the ADR-0227 manufactured identities — global REACH travels as the
        ``GLOBAL_REACH_TAG`` tag inside ``build_project_scope_clause``, never
        as ``project_id='global'``; writing the sentinel mints the phantom
        namespace the registry exists to prevent.
    """
    if not isinstance(value, str) or not value:
        return f"project_id must be a non-empty string; got {value!r}"
    if value in _NON_IDENTIFYING_PROJECT_IDS:
        return (
            f"project_id {value!r} names no project — it is a manufactured "
            "identity ADR-0227 deletes. Global reach is carried by the "
            f"{GLOBAL_REACH_TAG!r} tag, not by project_id"
        )
    return None


__all__ = [
    "InvalidProjectOverrideError",
    "accept_project_param",
    "project_id_value_error",
    "resolve_effective_project",
]
