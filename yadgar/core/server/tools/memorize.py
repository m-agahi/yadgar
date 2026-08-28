"""memorize MCP tool registration."""

from __future__ import annotations

import logging

from yadgar._shared.config import get_settings
from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.security.secrets import (
    gate_or_reject,  # noqa: F401 — required by I26 secret-gate check
)
from yadgar._shared.server_helpers import normalize_write_context

# Car 5 (2026-08-20 train): the CREATE-path registry gate. Same
# ``UnknownProjectError`` class ``MariaStorageEngine.assert_project_registered``
# raises, so one ``except`` binds both halves of the guarantee.
from yadgar._shared.storage.sql.errors import UnknownProjectError
from yadgar._shared.write_exec import (
    MemorizeContext,
    phase_validate,
)
from yadgar.core.forward import _forward_admin

# R2a Car D2: _get_file_queue lives in yadgar.core.lifecycle (core → core).
from yadgar.core.lifecycle import _get_file_queue
from yadgar.core.server._app import _tool

# Car M (0047 §7, §16.6): cross-project ``project=`` override. Resolves the
# effective project_id (override → session → directory → "global").
# C4b (0047 PR#40 §5): the resolved value is stamped on EVERY enqueued
# payload, not only when the caller supplied ``project=`` — see ``_enqueue``
# for why the conditional form left the default path broken.
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    resolve_effective_project,
)
from yadgar.core.server.tools._project_registry import (
    assert_project_registered_for_create,
)

logger = logging.getLogger(__name__)

settings = get_settings()

_VALID_TIERS = frozenset({"semantic_immortal", "conditional", "ephemeral"})


@_tool(always_load=True)
def memorize(  # noqa: PLR0913 — MCP tool with frozen 11-arg signature
    content: str,
    context: str | None = None,
    tags: list[str] | None = None,
    is_protected: bool = False,
    provenance_agent: str | None = None,
    tier: str | None = None,
    valid_until: str | None = None,
    ttl_days: int | None = None,
    reason: str = "",
    wait: bool = False,
    *,
    project: str | None = None,
) -> dict:
    """Store a new memory with embedding.

    project is the scope key. ``project`` (or the SessionStart identity) decides
    which project the memory belongs to, and it is the ONLY thing the stored
    ``directory_context`` is stamped from.

    context is an OPTIONAL REAL FILE PATH, used for nothing but staleness
    detection. Supply it when the memory is *about* a specific file and you want
    the staleness detector to notice when that file changes; omit it otherwise.
    Omitted (or not a readable file) → no hash is recorded, which is the
    long-standing best-effort contract. **It is NOT a description and NOT a
    scope key.**

    C10 (f) (0047 PR#40 §5) split those two roles apart. ``context`` used to be
    both — the stamp AND the hash input — and its own docstring used to insist
    it "MUST be the actual working directory path". The live corpus shows that
    instruction losing: 18 distinct ``directory_context`` values on ``memory``
    are free-text prose rather than paths (``db_inspect``, 2026-08-10), e.g.
    ``"debugging opsecrets nixos-quinyx"``. Callers were treating ``context`` as
    a description because the parameter's name invites it. Splitting the roles
    is what stops the class; a stricter docstring demonstrably did not. It also
    removes the two-keys-for-one-concept state ADR-0225 exists to delete —
    ``memorize`` no longer takes ``project`` AND a directory under another name.

    Car M (0047 §7, §16.6): the OPTIONAL ``project=`` parameter is the
    cross-project override. Precedence: ``project`` (override) >
    ``session_project`` (Car E) > raise (C5 deleted the derivation and the
    ``"global"`` fallback). ``context`` is NOT and never was a resolution
    source — see ``resolve_effective_project``, where it is accepted only so a
    supplied-and-ignored path stays observable in the log. Car 5 (2026-08-20)
    added the two gates this docstring used to attribute to an uncalled backend
    guard: a sentinel guard (``global`` / ``unresolved`` / ``system`` refused)
    and the REGISTRY check no writer of ``memory.project_id`` had ever
    performed — see ``_project_registry.assert_project_registered_for_create``.

    tags carries a ``None`` default purely as a consequence of ``context``
    becoming optional in front of it: Python forbids a required parameter
    behind a defaulted one, and reordering the frozen MCP signature would break
    every positional caller. ``None`` is normalised to ``[]`` — the same value
    an explicit empty list produces.

    C4b (0047 PR#40 §5): the RESOLVED project_id — override or not — is
    stamped on the enqueued payload (``payload["project_id"]``) on every
    call. ``project=`` changes WHICH project is named, never WHETHER one is.

    Persistence options:
    - is_protected=True: memory is exempt from heat decay and will never be aged out.
      Use this for facts that must persist indefinitely (credentials locations,
      permanent constraints). A DECISION does not belong here at all — file it
      with adr_add, which validates the schema and gives it an ADR number.
      Equivalent to calling anchor() but inline.
    - Alternatively, include "_anchor" in tags for the same effect.
    - Without either flag, memories decay naturally based on heat and last-access time.

    tier: anchor tier — "semantic_immortal" | "conditional" | "ephemeral".
      Setting tier auto-sets is_protected=True.
      Defaults: conditional → 90d TTL; ephemeral → 14d TTL; semantic_immortal → no expiry.

    valid_until: ISO-8601 UTC datetime string. Explicit expiry. Mutually exclusive with ttl_days.

    ttl_days: Shorthand for valid_until = now() + ttl_days. Mutually exclusive with valid_until.

    provenance_agent: identifies the agent or subagent type that stored this memory.
      Defaults to "default". Must be ASCII alphanumeric/hyphen/underscore, ≤64 chars.
      Used for provenance tracking across multi-agent workflows.

    reason: human-readable justification for why this memory is protected.
      Only meaningful when is_protected=True. Adds 'anchor:<reason>' tag.
      Required when tier='semantic_immortal' and ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True.


    wait: read-your-writes surface (mirrors wiki_add's wait semantics).
      wait=False (default): enqueue and return {stored, queued, queue_id}
        immediately — the drainer commits asynchronously.
      wait=True: enqueue, nudge the drainer, and block until the write drains
        or WIKI_WRITE_WAIT_TIMEOUT_SECONDS elapses, returning:
          {"stored": True, "committed": True, "queued": False, ...} — committed
          {"stored": False, "reason": "wait_timeout", "queued": True, ...} — still queued
          {"stored": False, "reason": "rejected", "queued": False, ...} — DLQ'd
    """
    # secret-gate: skip — gate_or_reject() called in phase_validate() (see _memorize_phases/_phase_validate.py)
    ctx = MemorizeContext(
        content=content,
        context=context,
        tags=list(tags or []),
        is_protected=is_protected,
        provenance_agent=provenance_agent,
        tier=tier,
        valid_until=valid_until,
        ttl_days=ttl_days,
        reason=reason,
    )

    # Validate + compute valid_until / provenance (still on the request thread:
    # secret gate + policy live here). The sync write pipeline (embed →
    # contradiction → store → post_write) runs ONLY in the backend drainer.
    result = phase_validate(ctx, settings)
    if result is not None:
        return result

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root so rows stay visible to canonical-repo recall. Covers
    # the SubagentStop footer path too (it calls this same tool).
    # ADR-0215: the branch half of the pair is discarded — nothing downstream
    # reads it any more.
    # C10 (f): guarded on truthiness now that ``context`` is optional. Behaviour
    # for a supplied context is byte-identical; ``None`` skips the seam rather
    # than pushing it through a normaliser that has nothing to normalise.
    if ctx.context:
        ctx.context = normalize_write_context(ctx.context)

    # Car M (0047 §7, §16.6): resolve the effective project_id BEFORE the
    # enqueue so the wire payload can carry it as ``project_id`` (drainer-side
    # routing). Type-level guard runs here so a malformed ``project=`` surfaces
    # as InvalidProjectOverrideError, mapped to the tool's error envelope so
    # the MCP boundary never raises.
    #
    # C10 (f): ``directory=`` is still handed the context so that a caller who
    # supplies BOTH gets the ignore logged — it is NOT a resolution tier (C5
    # deleted that), so passing it confers no scoping role on ``context``.
    try:
        effective_project_id = resolve_effective_project(
            project=project,
            directory=ctx.context,
            session_project=None,
            tool="memorize",
        )
        # Car 5: the registry check ~12 docstrings claimed ran "backend-side".
        # It never did; see ``_project_registry`` for why it must run here.
        assert_project_registered_for_create(effective_project_id, tool="memorize")
    except UnresolvedProjectError as exc:
        return {"stored": False, "ok": False, **exc.payload}
    except (InvalidProjectOverrideError, UnknownProjectError) as exc:
        return {"stored": False, "ok": False, "error": f"memorize: {exc}"}

    return _enqueue(ctx, wait=wait, project_id=effective_project_id)


@observe(tier="stage")
def _enqueue(ctx: MemorizeContext, wait: bool = False, *, project_id: str | None = None) -> dict:
    """Enqueue a memorize job. Returns the queued result.

    wait=True routes through _memorize_wait_path for read-your-writes (mirrors
    wiki_add). wait=False returns the async {stored, queued, queue_id} shape.

    C4b (0047 PR#40 §5): ``project_id`` is stamped on the wire payload
    UNCONDITIONALLY. Car M stamped it only when the caller passed
    ``project=``, which left the DEFAULT path — i.e. nearly every call to the
    highest-volume write path in the system — arriving at the drainer
    unattributed, to be re-derived inside a container with no git binary and
    no host project mounts (§1.1 / ADR-0227). The MCP tool call is the only
    participant that can see the session, so it is the only honest place to
    resolve. Car 5 (2026-08-20): the registry check that this comment used to
    attribute to an uncalled backend guard runs in ``memorize`` itself,
    ABOVE this function, via ``assert_project_registered_for_create``. It has
    to: that guard had zero call sites, and the drainer — the only other place
    a queued memorize write passes through — is a bare thread that cannot
    drive engine #2's pooled async engine. Core still touches no DB; the
    lookup is a forwarded ``list_project_rows``, cached in-process.
    """
    payload: dict = {
        "content": ctx.content,
        "context": ctx.context,
        "tags": list(ctx.tags),
        "is_protected": ctx.is_protected,
        "provenance_agent": ctx.provenance_agent_resolved,
    }
    if ctx.tier is not None:
        payload["tier"] = ctx.tier
    if ctx.computed_valid_until is not None:
        payload["valid_until"] = ctx.computed_valid_until
    # reason is required for semantic_immortal tier; include in payload so
    # run_memorize_replay can re-validate on the drainer side (R3 write-path).
    if ctx.reason:
        payload["reason"] = ctx.reason
    if project_id is not None:
        payload["project_id"] = project_id

    if wait:
        return _memorize_wait_path(payload)

    job_id = _get_file_queue().enqueue("memorize", payload)
    return {"stored": True, "queued": True, "queue_id": job_id}


@observe(tier="stage", metric="tools.memorize._memorize_wait_path")
def _memorize_wait_path(payload: dict) -> dict:
    """Handle memorize(wait=True): enqueue then poll for the terminal file.

    Mirrors wiki._wiki_add_wait_path exactly: enqueue, nudge the background
    drainer (drain_now) so the caller doesn't wait a full drain interval, then
    poll the shared archive/dlq dirs for the job's terminal state
    (FileQueue.wait_for_job). Reuses the same wait/drain plumbing wiki_add uses —
    no new machinery.

    Returns:
      {"stored": True, "committed": True, "queued": False, "queue_id": ...} — archived
      {"stored": False, "reason": "wait_timeout", "queued": True, ...} — drainer timeout
      {"stored": False, "reason": "rejected", "queued": False, ...} — DLQ'd
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    fq = _get_file_queue()
    job_id = fq.enqueue("memorize", payload)

    # Nudge the drainer to flush promptly, matching wiki_add's wait path. Task #29
    # cold-drain fix: the live drainer runs ONLY in the backend after the ADR-0078
    # split (in-core ``_st._queue_drainer`` is None → the in-process nudge is a
    # silent no-op in production). POST a cross-process ``drain_now`` nudge first
    # (synchronous, durable); keep the in-process nudge for single-process runs +
    # existing tests. Best-effort: a failed POST (backend down / older backend) is
    # swallowed and we fall through to the passive poll (mixed-version safe).
    try:
        _forward_admin("drain_now", {})
    except Exception as exc:  # noqa: BLE001 — non-fatal; passive poll still converges
        logger.warning("memorize wait: backend drain_now nudge failed (non-fatal): %s", exc)
    _drainer = _st._queue_drainer
    if _drainer is not None:
        try:
            _drainer.drain_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("memorize wait: drain_now() failed (non-fatal): %s", exc)

    # Reuse wiki_add's wait-timeout knob (WIKI_WRITE_WAIT_TIMEOUT_SECONDS) rather
    # than inventing a memorize-specific one — the shared file-queue wait budget.
    # (Sibling car #26 owns this knob's default; do not change it here.)
    try:
        timeout = getattr(settings, "WIKI_WRITE_WAIT_TIMEOUT_SECONDS", 5.0)
    except Exception:  # noqa: BLE001
        timeout = 5.0

    outcome = fq.wait_for_job(job_id, timeout=timeout)

    if outcome["status"] == "timeout":
        # Car 3 (contract clarity): wait_timeout is convergence-pending, not a
        # failure. Signal it explicitly (converging=True, committed=False) while
        # keeping stored/reason/queued unchanged for back-compat.
        return {
            "stored": False,
            "committed": False,
            "converging": True,
            "reason": "wait_timeout",
            "queued": True,
            "queue_id": job_id,
            "hint": "Write still queued — will commit on next drain or hit DLQ on repeated failure.",
        }

    if outcome["status"] == "rejected":
        rejection = outcome.get("result")
        if rejection is not None:
            return rejection
        return {
            "stored": False,
            "reason": "rejected",
            "queued": False,
            "queue_id": job_id,
        }

    return {
        "stored": True,
        "committed": True,
        "queued": False,
        "queue_id": job_id,
    }
