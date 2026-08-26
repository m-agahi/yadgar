"""Wiki MCP tool registrations."""

from __future__ import annotations

import logging
from typing import Any

import yadgar._shared.runtime.state as _st
from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.security.secrets import gate_or_reject
from yadgar._shared.server_helpers import _has_unpaired_surrogate, _push_event
from yadgar._shared.storage.directory import RecallScope

# Car 5 (2026-08-20 train): the CREATE-path registry gate. Same
# ``UnknownProjectError`` class ``MariaStorageEngine.assert_project_registered``
# raises, so one ``except`` binds both halves of the guarantee.
from yadgar._shared.storage.sql.errors import UnknownProjectError
from yadgar._shared.wiki.policy import is_recall_visible
from yadgar._shared.wiki.store import WikiSimilarityGateUnavailable
from yadgar.core.forward import _forward_admin

# R2a Car D2: _get_file_queue moved to yadgar.core.lifecycle (core → core).
from yadgar.core.lifecycle import _get_file_queue
from yadgar.core.server._app import _tool

# Car M (0047 §7, §16.6): cross-project ``project=`` override on the wiki MCP
# tools. Resolves the effective project_id (override → session → directory →
# "global") and threads it through to the wiki enqueue / read path.
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    accept_project_param,
    resolve_effective_project,
)
from yadgar.core.server.tools._project_registry import (
    assert_project_registered_for_create,
)

logger = logging.getLogger(__name__)


# Page types allowed on the sanctioned canonical write path. This is a
# DEFENSE-IN-DEPTH assertion inside _wiki_write_canonical, NOT the gate — a model
# supplies page_type/tags, so it is spoofable. The real boundary is that the
# canonical path is reachable only via server-side sanctioned callers (never a
# model arg). Registered as an I32 capability-registry constant.
CANONICAL_PAGE_TYPES = frozenset({"task_list", "adr", "adr_superseded", "wiki_rollup"})
# Car G (0047 §7 D23): ``adr_superseded`` joins the allowlist so the retype
# mutator (``admin_exec.adr_seed.retype_page_type``) can re-write the wiki
# page's page_type as part of the sanctioned lifecycle transition
# (``adr`` → ``adr_superseded``, atomic with the row-side status flip).
# ``locked`` mutability blocks agent/tool edits but NOT sanctioned server-side
# transitions (D26) — otherwise the supersede retype would deadlock against
# its own guard.
# Car H (0047 §7 D29): ``wiki_rollup`` joins the allowlist so the per-subsystem
# rollup regen (admin_exec.rollup._regenerate_subsystem_rollup) can write the
# derived page via the sanctioned canonical write path. ``derived`` mutability
# (wiki/policy.py MUTABILITY_BY_TYPE["wiki_rollup"]="derived") blocks ALL
# agent/tool writes; the regen writer passes _sanctioned=True at the storage
# chokepoint so its lifecycle is the SOLE mutator.


@observe(tier="hot", metric="tools.wiki._check_wiki_add_context")
def _check_wiki_add_context(
    directory: str | None, *, project: str | None = None
) -> tuple[dict, str | None]:
    """Reject a ``wiki_add`` that names no scope at all.

    Returns ``({}, resolved_project_id)`` when the write may proceed, or
    ``(reject_envelope, None)`` to REJECT.

    ``project`` is the caller-supplied override (Car M, 0047 §7/§16.6). When
    ``directory`` is empty AND ``project`` is a non-empty string, the gate
    resolves the project BEFORE the directory check — this is what lets an
    MCP caller pass ``project=`` without also passing ``directory=`` and still
    land on the registry check below.

    ADR-0215/0217: this used to be Car 0's four-flow branch router — it read a
    trusted per-directory git fact and decided branch-scoped vs canonical. Branch
    scoping is gone, so the whole flow table went with it, and the git fact was
    deleted as redundant (ADR-0217).

    **C5 (0047 PR#40 §5): ``YADGAR_DIRECTORY_ENFORCEMENT`` is DELETED and so is
    ``_missing_directory_error``.** ADR-0225 set the knob's end condition as
    "until the registry check is actually wired"; C6 wires it in this same PR.
    A knob that turns a scoping guarantee OFF is incompatible with a system whose
    identity is fail-loud by construction — "relaxed enforcement" was precisely
    the mode in which unscoped rows entered the corpus. The rejection is now the
    structured error every other boundary raises, so an agent gets one shape of
    answer and one remedy sentence rather than two.

    ``is_draining()`` callers are exempt — this helper should only be called when
    not is_draining().

    C13: the returned envelope carries ``stored``/``ok`` alongside the payload.
    C5 replaced ``_missing_directory_error`` (which set ``stored=False``) with
    the bare ``.payload``, while the resolver rejection thirty lines below kept
    returning ``{"stored": False, "ok": False, **exc.payload}`` — so ONE tool
    answered ONE error class in TWO shapes, and a caller doing the documented
    ``result["stored"] is False`` check got ``None`` on this path and ``False``
    on the other. The whole point of the structured error is that an agent gets
    one shape of answer; shipping two defeats it.

    C0 (2026-08-22 train): ``project=`` now satisfies the directory gate.
    Pre-fix, the gate short-circuited on empty ``directory`` BEFORE the resolver
    ran, so a ``wiki_add(project=...)`` over MCP transport (which never sees
    ``directory``) returned the resolver's error envelope in a context where
    the resolver never got to look at the override. The gate now performs the
    resolution itself when ``project`` is supplied, and returns the resolved
    id so the call site below can skip a redundant resolver pass.

    Security: an unregistered project_id still gets rejected at the registry
    check in ``wiki_add`` (Car 5); the gate's resolve-before-reject only
    collapses the rejection envelope shape — it does NOT lower the bar.
    """
    if not (directory or "").strip():
        if project is None:
            # Missing identity — caller supplied neither ``directory=`` nor
            # ``project=``. Different defect from "I passed the wrong thing";
            # the missing-identity envelope is the right one.
            return {
                "stored": False,
                "ok": False,
                **UnresolvedProjectError("wiki_add").payload,
            }, None
        # PR #65 review finding #3: a non-string / empty / sentinel ``project=``
        # is an INVALID OVERRIDE — the resolver raises and the gate surfaces
        # the override's own ``invalid_project_override`` envelope, NOT
        # ``unresolved_project``. Bundling "I passed a non-string" into the
        # missing-identity branch above would tell the caller "you forgot to
        # pass a project" when in fact they passed the wrong thing — pointing
        # them at the wrong fix. Empty-string ``project=""`` falls into this
        # branch by the resolver's design: ``_project_param.py`` treats it as
        # a present-and-invalid override, not as an absent one. Sentinels
        # (``"global"`` / ``"unresolved"`` / ``"system"``) go through the same
        # ``_reject_sentinel`` raise.
        try:
            _resolved = resolve_effective_project(
                project=project,
                directory=None,
                session_project=None,
                tool="wiki_add",
            )
        except InvalidProjectOverrideError as exc:
            return {
                "stored": False,
                "ok": False,
                "error": "invalid_project_override",
                "tool": "wiki_add",
                "detail": str(exc),
            }, None
        else:
            return {}, _resolved
    return {}, None


@observe(tier="hot", metric="tools.wiki._wiki_write_canonical")
def _wiki_write_canonical(payload: dict, wait: bool = False) -> dict:
    """Sanctioned SERVER-SIDE canonical write.

    Sets ``_internal=True`` on the payload — the server-only token that marks the
    write as a sanctioned system write (the drainer honors it and strips it).
    Reachable ONLY from server-side sanctioned callers (``adr_add``,
    ``wiki_write_task_list``); never a ``wiki_add`` MCP param, so the model cannot
    invoke it.

    ADR-0215/0216: this also used to set ``branch=None`` to force the canonical
    slot. Branch scoping is gone — canonical is now the only slot — so the
    assignment went and this is a thin named passthrough. The seam is retained
    deliberately (plan §4 Q1) so the sanctioned callers keep a stable server-side
    entry point and ``_internal`` keeps its meaning for the drainer.

    Defense-in-depth: refuses to canonical-write a page whose ``page_type`` is not
    in ``CANONICAL_PAGE_TYPES``. Brutal-honesty — ``page_type`` is model-supplied
    and therefore spoofable; this is a soft accident-guard, NOT the security
    boundary (the boundary is server-side-only reachability).

    ``wait=False`` (default): enqueues on the file queue like the async ``wiki_add``
    path and returns ``{stored, queued, ...}`` — fine for a deterministic-slug page
    (e.g. a per-ADR ``<project>-adr-NNNN``) whose write does not feed a later ID.

    ``wait=True`` (read-your-writes): routes through ``_wiki_add_wait_path`` so the
    write is committed before returning ``{committed: True, ...}``. Car 2 needs this
    on the ADR INDEX create so a subsequent ``adr_add`` reads the just-written index
    when assigning the next sequential ID (the per-project lock is released between
    calls, so async enqueue would race the ID scan). Sanctioned callers must pass
    ``replace_slug`` (or ``force=True`` if the page_type's gate policy allows a
    non-canonical write path) when the payload warrants it. ``adr`` and ``task_list``
    canonical page_types now use ``gate_mode="identity"`` (Car C3, #0047 §7 D21),
    so the drainer sim gate is a pass-through for them — no bypass flag is
    required on the canonical ADR write.

    Raises ``ValueError`` on a non-allowlisted page_type (programmer error — a
    sanctioned caller must pass an allowlisted type).
    """
    page_type = payload.get("page_type")
    if page_type not in CANONICAL_PAGE_TYPES:
        raise ValueError(
            f"_wiki_write_canonical refuses page_type={page_type!r}; "
            f"canonical writes are restricted to {sorted(CANONICAL_PAGE_TYPES)}"
        )
    payload["_internal"] = True
    # C4 (0047 PR#40 §5): the canonical writers stamp an identity like every
    # other enqueue. Both callers (``adr_add``, ``wiki_write_task_list``) take a
    # required ``directory`` and run in the process that can see the session, so
    # they have no more excuse for an unnamed project than ``wiki_add`` does —
    # and the drainer's ``_validate_project_id`` DLQs an unstamped payload, so
    # leaving ``_internal`` as a hole would have taken the two canonical page
    # types out of service.
    #
    # C5 (0047 PR#40 §5): the resolve-with-fallback that stood here is deleted.
    # With no derivation tier left there is nothing for it to resolve FROM —
    # ``project=None`` plus a directory is exactly the case that now raises — so
    # the sanctioned callers must arrive with the stamp already on the payload.
    # ``adr_add`` does (it holds the value for the ledger row); C5 gave
    # ``wiki_write_task_list`` the same obligation.
    if not payload.get("project_id"):
        raise UnresolvedProjectError(
            "_wiki_write_canonical",
            detail=(
                f"(canonical {payload.get('page_type')!r} write for slug "
                f"{payload.get('slug')!r}: the sanctioned caller must stamp "
                "project_id — this process cannot derive one)"
            ),
        )
    # Car 5 (2026-08-20 train): the canonical path enqueues DIRECTLY (see the
    # ``_get_file_queue().enqueue`` below) — it does not pass through
    # ``wiki_add``, so the gate wired there does not cover it. Leaving it out
    # would make ``wiki_add``'s new docstring claim over-broad for exactly the
    # two page types (``adr``, ``task_list``) whose whole point is being
    # canonical, and an over-broad claim is the defect class this car deletes.
    assert_project_registered_for_create(payload["project_id"], tool="_wiki_write_canonical")
    # Canonical writes are server-side sanctioned (adr_add + wiki_write_task_list
    # are the SOLE writers of their page_types). Car J (0047 §7 D25/D26) marks
    # ``adr`` as mutability='locked' (decisions are immutable, Car G supersede
    # retype is the SOLE mutator of an existing row); adr_add's NEW insert path
    # must therefore carry the server-side sanctioned token so the storage-layer
    # gate (mutability_gate.enforce_mutability) does not reject the write.
    # ``task_list`` is mutability='free' — the token is a no-op there.
    payload["_sanctioned"] = True
    if wait:
        # RYW: enqueue then poll until the drainer commits. The sim gate is
        # bypassed via force=True in the payload OR the page_type's gate_mode
        # is "identity" (Car C3, #0047 §7 D21 — adr/task_list/agent-prompt
        # library). Sanctioned callers either set force or rely on the
        # identity gate.
        return _wiki_add_wait_path(payload, payload.get("slug"), payload.get("title"))
    _get_file_queue().enqueue("wiki_add", payload)
    return {
        "stored": True,
        "queued": True,
        "similarity_check": "deferred",
        "slug": payload.get("slug"),
        "title": payload.get("title"),
    }


@observe(tier="stage", metric="tools.wiki._task_list_guards")
def _task_list_guards(project_id: str | None, content: str, title: str) -> dict | None:
    """Return a rejection envelope for ``wiki_write_task_list``, or ``None``.

    Extracted at C5 (0047 PR#40 §5): the new ``project_id`` requirement pushed
    the tool over the I30 cyclomatic cap, and these are all "refuse before
    writing" predicates, so they belong together rather than baselined apart.

    The I26 secret gate deliberately STAYS in the tool body. Hiding
    ``gate_or_reject`` one frame down would satisfy the letter of the scan and
    defeat its point: the checker asserts that a write tool visibly gates its
    own content, and a security control that only a reader tracing call graphs
    can find is one refactor away from being dropped.
    """
    if not (project_id or "").strip():
        return dict(
            UnresolvedProjectError(
                "wiki_write_task_list",
                detail=(
                    "(the task-list page needs an owner; `project=` is the slug "
                    "key, not the identity)"
                ),
            ).payload
        )
    if len(content) > 65_536:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 65_536}
    for field in (content, title):
        if _has_unpaired_surrogate(field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}
    return None


@_tool()
def wiki_write_task_list(
    project: str,
    content: str,
    directory: str,
    wait: bool = True,
    *,
    project_id: str | None = None,
) -> dict:
    """Persist a Claude Code harness task list to the wiki — one call.

    The SANCTIONED task-list mirror writer. The stop-hook checkpoint protocol
    (step 4) calls this to save the harness task list so it survives ``/clear`` /
    session exit. The page resolves by directory alone, so the session-start
    restore-nudge finds it from any working tree and from a non-git project.

    Why a dedicated tool and NOT ``wiki_add(page_type="task_list", ...)``: this
    routes through the server-side ``_wiki_write_canonical`` path, which sets the
    server-only ``_internal`` token — ``page_type`` is deliberately NOT a gate
    (it is model-supplied and therefore forgeable). The sanction is STRUCTURAL —
    the tool is bounded to the ``{project}-task-list`` slug + ``task_list``
    page_type, so a model cannot use it to write an arbitrary page.

    Args:
        project: project NAME; the page is slug ``{project}-task-list``. This is
            a slug component, not an identity — it has never been ``owner/repo``
            and must not become one (a ``/`` would corrupt the slug).
        content: full page body (## Meta + one ## task:<id> section per task).
        directory: absolute project path (directory_context for the page).
        wait: block until the drainer commits (default True — read-your-writes so
            the caller can verify via wiki_history / wiki_read immediately).
        project_id: owning ``owner/repo`` key. **Required (C5 / ADR-0227.)**
            Keyword-only and deliberately NOT folded into ``project`` above,
            because the two are different things that happen to be adjacent: the
            slug key is a bare name, the identity is a namespaced path. C4 gave
            ``adr_add`` — the other sanctioned canonical writer — its stamp and
            missed this one; ``_wiki_write_canonical`` used to paper over that by
            resolving with a fallback, and with the fallback deleted the gap is
            visible. The stop-hook has the value: the SessionStart banner prints
            it.

    Returns the ``wiki_add``-shaped result: ``{stored, committed|queued, slug, ...}``.
    On wait-budget expiry returns ``{stored: False, committed: False,
    converging: True, reason: "wait_timeout", queued: True}`` — NOT a failure: the
    write is durably queued and converges on the next drain (treat converging=True
    as success-pending). Applies the same secret-gate / size / surrogate guards as
    ``wiki_add``.
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    title = f"{project} task list"
    _reject = _task_list_guards(project_id, content, title)
    if _reject is not None:
        return _reject

    _tags = ["task-list"]
    _gate = gate_or_reject(content, tags=_tags)
    if _gate is not None:
        return _gate
    if _st._rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _st._rules_engine.check_write_policy(
            content, "", _tags
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            content = wp_modified

    _effective_dir = (directory or "").strip() or None
    if _effective_dir and _effective_dir != "global":
        _effective_dir = _effective_dir.rstrip("/") or _effective_dir

    slug = f"{project}-task-list"
    payload = {
        "wiki_schema_version": 2,
        "slug": slug,
        "title": title,
        "content": content,
        "category": "reference",
        "tags": _tags,
        "source_memory_ids": None,
        "confidence": "medium",
        "append": False,
        # replace_slug: overwrite the existing canonical task-list page in place
        # (also skips the similarity gate — a task-list is intentionally self-similar
        # across checkpoints).
        "replace_slug": slug,
        "directory_context": _effective_dir,
        "page_type": "task_list",
        # C5: the canonical seam no longer resolves on a caller's behalf.
        "project_id": project_id.strip(),
    }
    return _wiki_write_canonical(payload, wait=wait)


@observe(tier="stage", metric="tools.wiki._wiki_add_wait_path")
def _wiki_add_wait_path(payload: dict, new_slug: str, title: str) -> dict:
    """Handle wiki_add(wait=True): enqueue then poll for the terminal file.

    R3 Car 1 (write-half): the sync write body lives in the backend drainer
    (yadgar.backend.write_exec.run_wiki_add_replay). This shell enqueues and polls
    the shared archive/dlq dirs for the job's terminal state (FileQueue.wait_for_job).
    The drainer runs the similarity gate; a rejection lands in the DLQ .error.json
    sidecar and is surfaced here synchronously.

    Returns:
      {"stored": True, "committed": True}          — archived (committed)
      {"stored": False, "reason": "duplicate_detected", "candidates": [...]} — gate rejected
      {"stored": False, "reason": "wait_timeout", "queued": True}  — drainer timeout
    """
    fq = _get_file_queue()
    job_id = fq.enqueue("wiki_add", payload)

    # Nudge the drainer to flush promptly so the caller does not wait a full drain
    # interval. Task #29 cold-drain fix: after the ADR-0078 split the live drainer
    # runs ONLY in the backend process — in-core ``_st._queue_drainer`` is None, so
    # the historical in-process nudge was a silent no-op in production. POST a
    # cross-process ``drain_now`` nudge to the backend first (synchronous, durable);
    # then keep the in-process nudge for single-process runs + existing tests.
    # Best-effort: if the backend POST fails (backend down / older backend without
    # the endpoint) swallow and fall through to the passive poll (mixed-version safe).
    try:
        _forward_admin("drain_now", {})
    except Exception as exc:  # noqa: BLE001 — non-fatal; passive poll still converges
        logger.warning("wiki_add wait: backend drain_now nudge failed (non-fatal): %s", exc)
    _drainer = _st._queue_drainer
    if _drainer is not None:
        try:
            _drainer.drain_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki_add wait: drain_now() failed (non-fatal): %s", exc)

    try:
        from yadgar._shared.config import get_settings as _get_settings

        timeout = getattr(_get_settings(), "WIKI_WRITE_WAIT_TIMEOUT_SECONDS", 15.0)
    except Exception:
        timeout = 15.0

    outcome = fq.wait_for_job(job_id, timeout=timeout)

    if outcome["status"] == "timeout":
        # Car 3 (contract clarity): wait_timeout is NOT a failure — the write is
        # durably queued and converges on the next drain. Signal that explicitly
        # (converging=True, committed=False) alongside the back-compat keys so
        # naive callers don't read wait_timeout as a hard error. stored/reason/
        # queued are UNCHANGED (adr._write_ok + the timeout tests depend on them).
        return {
            "stored": False,
            "committed": False,
            "converging": True,
            "reason": "wait_timeout",
            "queued": True,
            "slug": new_slug,
            "hint": "Write still queued — will commit on next drain or hit DLQ on repeated failure.",
        }

    if outcome["status"] == "rejected":
        rejection = outcome.get("result")
        if rejection is not None:
            # Gate fired in drainer — return rejection synchronously.
            return rejection
        return {
            "stored": False,
            "reason": "rejected",
            "queued": False,
            "slug": new_slug,
        }

    return {
        "stored": True,
        "queued": False,
        "committed": True,
        "slug": new_slug,
        "title": title,
    }


@_tool()
def wiki_add(
    title: str,
    content: str,
    category: str = "reference",
    tags: list[str] | None = None,
    source_memory_ids: list[int] | None = None,
    confidence: str = "medium",
    append: bool = False,
    force: bool = False,
    replace_slug: str | None = None,
    wait: bool = False,
    directory: str | None = None,
    page_type: str | None = None,
    slug: str | None = None,
    upsert: bool = True,
    *,
    project: str | None = None,
    allow_truncation: bool = False,
) -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references.

    Car M (0047 §7, §16.6): the ``project=`` override lets a caller address
    another project's wiki namespace. The validated project_id is stamped on
    the enqueued payload (``payload["project_id"]``) so the drainer routes
    the write to that project_id's pages; ``directory`` stays as the
    directory-context hint (the same shape ``directory_context`` already
    carries). Precedence: ``project`` (override) > ``session_project`` >
    ``directory``-derived > ``"global"``. Core enforces the shape guard
    (non-empty string, and NOT an ADR-0227 sentinel — Car 5).

    Car 5 also wires the REGISTRY check, which no writer of
    ``wiki_page.project_id`` had ever performed: an unregistered project_id is
    rejected before the enqueue by
    ``_project_registry.assert_project_registered_for_create``, which forwards
    ``list_project_rows`` to the backend and caches the key set in-process.
    When the registry cannot be consulted (engine #2 absent, backend
    unreachable) it WARNs and falls through to the shape guard rather than
    refusing the write — see that module's docstring. The claim covers the
    canonical writers too: ``_wiki_write_canonical`` enqueues directly rather
    than through this tool, so it carries its own call to the same gate.

    append=False (default): create a new page or overwrite an existing one.
    append=True: merge content into an existing page (appends with timestamp,
      merges tags and source_memory_ids). Use for accumulating knowledge over time.

    v5.39.0 similarity gate: wiki_add checks for near-duplicate pages before writing.
    v5.41.5 BREAKING CHANGE: gate moved from request thread to drainer (I9 fix).

    wait=False (default): gate check is DEFERRED — handler returns immediately.
      Response: {"stored": True, "queued": True, "similarity_check": "deferred", ...}
      Duplicate detection happens asynchronously in the drainer. If the gate fires,
      the job is archived (not inserted) and a rejection metric is emitted. Caller
      will NOT receive a sync rejection — use wait=True if rejection feedback needed.

    wait=True: gate runs in drainer, rejection surfaces synchronously.
      Gate fires  → {"stored": False, "reason": "duplicate_detected", "candidates": [...]}
      Gate passes → {"committed": True, "queued": False, "slug": ..., "title": ...}

    Use force=True to bypass the gate. Use replace_slug=<existing-slug> to overwrite
    an existing page by a different slug (gate is skipped for both).

    allow_truncation (task 271): an update whose new body is under 50% of the old
      one (old >= 1 KB) is REFUSED, reason ``wiki_size_collapse``. Usually that
      means the write is incomplete — re-read the page and write back the WHOLE
      intended body; ``wiki_restore(slug, version)`` undoes one that landed.
      Pass True only when the loss is deliberate. ``force`` does NOT open this
      gate: it bypasses duplicate detection on CREATE, and conflating the two
      would weaken this one for every dedup bypass. Threshold + evidence:
      ``_shared/storage/truncation_gate.py``.

    slug: optional — store at EXACTLY this slug (create-or-overwrite when upsert=True).
      When None (default), slug is derived from title (backward-compat).
      Required for structural pages whose crossrefs and stale-diff key on
      a caller-computed slug, not the title.
    upsert: controls collision behaviour when an explicit slug is given (default True).
      upsert=True  — create-or-overwrite at the slug (idempotent; use for
                     regeneration where the same slug is rewritten each cadence).
      upsert=False — reject if the slug already exists, returning
                     {"stored": False, "reason": "slug_exists"}.
      Only meaningful with an explicit slug; the legacy title-derived path always
      upserts by slug regardless.

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.
    Confidence: high, medium, low.
    page_type: optional — one of: function, module, service, architecture, decision, analysis.
      When provided, stored with wiki_schema_version=1. Omit to leave page untyped (backward-compat).
      Typed pages are format-checked by wiki_lint (missing required sections reported as warnings).
      wiki_add never rejects a write due to page_type/template mismatch — lint is advisory only.

    wait=False (default): async fast path — returns immediately with {"queued": True}.
    Only set wait=True when callers depend on next-call read-your-writes.
    wait=True: enqueues then blocks until the drainer commits, returning
      {"committed": True, "queued": False}. Preserves FIFO ordering. On timeout
      returns {"stored": False, "committed": False, "converging": True, "reason":
      "wait_timeout", "queued": True} — NOT a failure: the write is durably queued
      and converges on the next drain (treat converging=True as success-pending).
      I9 latency budget does NOT apply to wait=True.
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    if len(content) > 65_536:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 65_536}

    # v5.15.0: secret gate — use gate_or_reject() so allowlist tags= kwarg is forwarded.
    # Replaces direct check_secrets() call so v5.13.0 allowlist fires on real wiki_add() calls.
    _gate = gate_or_reject(content, tags=list(tags) if tags else [])
    if _gate is not None:
        return _gate
    if _st._rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _st._rules_engine.check_write_policy(
            content, "", tags or []
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            content = wp_modified

    for _field in (content, title):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Directory enforcement at the MCP boundary — error dict = REJECT.
    _decision, _gate_resolved_id = _check_wiki_add_context(directory, project=project)
    if "error" in _decision:
        return _decision

    _effective_dir: str | None = (directory or "").strip() or None
    # DP-3: strip trailing slash (preserve "global" sentinel as-is)
    if _effective_dir and _effective_dir != "global":
        _effective_dir = _effective_dir.rstrip("/") or _effective_dir

    # Car M (0047 §7, §16.6): resolve the effective project_id BEFORE the
    # enqueue so the wire payload can carry ``project_id`` (drainer-side
    # routing). Type-level guard runs here so a malformed ``project=``
    # surfaces as a tool error envelope, never as a raised exception.
    # C0 (2026-08-22 train): if the gate already resolved via ``project=``
    # alone (no ``directory``), reuse that id and skip the redundant call.
    try:
        if _gate_resolved_id is not None:
            _effective_project_id = _gate_resolved_id
        else:
            _effective_project_id = resolve_effective_project(
                project=project,
                directory=_effective_dir,
                session_project=None,
                tool="wiki_add",
            )
        # Car 5 (2026-08-20 train): the real registry check.
        # ``wiki_page.project_id`` had NO registry check on any writer — the
        # "backend-side" one the docstrings named had zero call sites.
        # Degrades to the shape gate when engine #2 is absent; see
        # ``_project_registry``'s module docstring.
        assert_project_registered_for_create(_effective_project_id, tool="wiki_add")
    except UnresolvedProjectError as exc:
        return {"stored": False, "ok": False, **exc.payload}
    except (InvalidProjectOverrideError, UnknownProjectError) as exc:
        return {
            "stored": False,
            "ok": False,
            "error": f"wiki_add: {exc}",
            "op_type": "wiki_add",
        }

    # v5.39.0 slug generation (O(1), needed for enqueue payload and wait path).
    import re as _re_slug

    _title_slug = (_re_slug.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]
    # Car C (#83): explicit slug overrides title derivation. The drainer
    # (wiki_add_impl.run_wiki_add_replay) reads payload["slug"] as explicit_slug
    # and passes it to WikiAddOptions.slug → WikiStore.add() stores at that slug.
    # When slug=None, fall back to the title-derived slug (unchanged backward compat).
    _effective_slug = slug if slug is not None else _title_slug

    # v5.41.5: similarity gate REMOVED from request path (I9 fix).
    # Gate now runs in the drainer pre-apply stage (_sim_gate_for_drainer).
    # wait=False callers get {queued: True, similarity_check: "deferred"} — see below.
    # wait=True callers get sync rejection via wait_for_job + get_job_result.
    # I26: secret-gate (above) still runs on request thread (cheap regex, stays here).
    # I6 no-double-pay: gate runs once in the drainer (backend write-exec).

    _payload = {
        "wiki_schema_version": 2,
        "slug": _effective_slug,
        "title": title,
        "content": content,
        "category": category or "reference",
        "tags": tags,
        "source_memory_ids": source_memory_ids,
        "confidence": confidence,
        "append": append,
        # v5.41.5: pass bypass flags so drainer can skip gate for these paths
        "force": force,
        "replace_slug": replace_slug,
        "directory_context": _effective_dir,
        "page_type": page_type,
        # Car C (#83): upsert semantics — drainer reads upsert from payload.
        "upsert": upsert,
        # Ledger task 271: the size-collapse gate's escape hatch.
        "allow_truncation": allow_truncation,
    }
    # C3 (0047 PR#40 §5.C3): stamp the resolved project_id UNCONDITIONALLY.
    # Car M stamped it only when the caller passed ``project=``, which left the
    # DEFAULT path relying on the drainer to infer one from directory_context —
    # inside a container with no git binary and no host project mounts, where
    # the classifier silently yields ``local/<basename>`` or ``unresolved``
    # (§1.1). This tool call is the only participant that can see the session,
    # so it is the only honest place to resolve — and Car 5 is why the
    # registry check is resolved here too, not "backend-side": the guard that
    # phrase named had zero call sites, and the drainer cannot reach engine #2.
    _payload["project_id"] = _effective_project_id

    # wait=True: enqueue first (preserves FIFO), then poll until the drainer commits.
    # The drainer runs the similarity gate; rejection surfaces synchronously via the
    # DLQ terminal-file poll inside _wiki_add_wait_path.
    if wait:
        return _wiki_add_wait_path(_payload, _effective_slug, title)

    # Async path (wait=False default): enqueue and return immediately.
    # v5.41.5: similarity gate is deferred to drainer — caller gets
    # {similarity_check: "deferred"} and must use wait=True for sync rejection.
    _get_file_queue().enqueue("wiki_add", _payload)
    return {
        "stored": True,
        "queued": True,
        "similarity_check": "deferred",
        "slug": _effective_slug,
        "title": title,
    }


# ── Car 2 (v5.113): wiki_read / wiki_query result caches ──────────────────────
#
# Both are query/slug-scoped read caches. Invalidation folds the structural wiki
# epoch (_current_epoch) into the key: ANY wiki write bumps the global epoch (via
# storage._bump_wiki_epoch → bump_epoch(None)), so a stale key becomes unreachable
# on the next read — the wiki-write-busts-read correctness guarantee. A short TTL
# backstops any non-write drift. deep_copy=True: callers mutate returned row-dicts
# (wiki_query bumps r["_retrieval_score"]; read dicts are handed out mutable).
_WIKI_READ_CACHE_TTL = 120.0
_WIKI_QUERY_CACHE_TTL = 60.0  # fuzzy search → shorter TTL acceptable

# Car C9 task 70 (paired with task 71): cap the content size returned by
# wiki_read. Below wiki_add's 65 536 ceiling (line 267, 554) so legacy
# over-size rows still get truncated; above wiki_find_similar_pages's
# 4000-byte search window (_shared/wiki/store.py:1239) so full-page reads
# stay useful. Task 71 sets the WRITE cap to the same value -- both must
# move in lockstep; if you bump one, bump the other.
_WIKI_READ_CONTENT_CAP_BYTES = 8_192


def _current_wiki_epoch() -> int:
    """Global structural epoch — bumped on every wiki write. Folded into cache keys
    so a wiki mutation busts every cached wiki read/query regardless of dir/branch
    normalization (the bump is global; see storage.wiki._bump_wiki_epoch)."""
    try:
        from yadgar._shared.runtime.cache_epoch import _current_epoch

        return _current_epoch(None)
    except Exception:
        return 0


@observe(tier="stage", metric="tools.wiki._make_wiki_read_cache")
def _make_wiki_read_cache():
    from yadgar.core.cache import (
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("wiki_read", total)
    return Cache(
        name="wiki_read",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget, #49)
        invalidation=TTL(_WIKI_READ_CACHE_TTL),  # epoch in key + TTL backstop
        deep_copy=True,  # returned page dict is mutable / caller-owned
        obs_tier="cold",  # low call rate → full tri-signal fine
    )


@observe(tier="stage", metric="tools.wiki._make_wiki_query_cache")
def _make_wiki_query_cache():
    from yadgar.core.cache import (
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("wiki_query", total)
    return Cache(
        name="wiki_query",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget, #49)
        invalidation=TTL(_WIKI_QUERY_CACHE_TTL),  # fuzzy search → short TTL
        deep_copy=True,  # results carry mutated _retrieval_score row-dicts
        obs_tier="cold",
    )


_wiki_read_cache = _make_wiki_read_cache()
_wiki_query_cache = _make_wiki_query_cache()


@observe(exempt="single resolve + ValueError mapping; no I/O — called at the wiki_query boundary")
def _resolve_wiki_query_project(*, project: str | None, directory: str | None) -> str:
    """Car M: resolve the effective project_id for ``wiki_query``.

    Raises ``ValueError`` on a malformed ``project=`` so the tool boundary
    surfaces a clean error envelope (read tools stay fail-loud at the
    boundary per the wiki_* pattern). The error string is prefixed with
    ``"wiki_query: "`` so callers see the tool name in any traceback.
    """
    try:
        return resolve_effective_project(
            project=project,
            directory=directory,
            session_project=None,
            tool="wiki_query",
        )
    except InvalidProjectOverrideError as exc:
        raise ValueError(f"wiki_query: {exc}") from exc


@observe(exempt="single resolve + ValueError mapping; no I/O — called at the wiki_read boundary")
def _resolve_wiki_read_project(*, project: str | None, directory: str | None) -> str:
    """Car M: resolve the effective project_id for ``wiki_read``.

    Raises ``ValueError`` on a malformed ``project=``; the caller wraps it
    in a dict-returning error envelope so the tool boundary stays clean.
    The error string is prefixed with ``"wiki_read: "`` so callers see the
    tool name in any traceback.

    Car W2: the return type was ``str | None`` and never could be. C5 deleted
    every tier that answered ``None``: ``resolve_effective_project`` returns a
    non-empty ``str`` or raises ``UnresolvedProjectError``, which is NOT a
    ``ValueError`` and so propagates past the caller's ``except``. The lie
    mattered once the value became the LOOKUP key — it invites a ``None``
    branch, and the only thing such a branch could do is widen to an unscoped
    slug match, i.e. rebuild the fallback ADR-0227 exists to delete.
    """
    try:
        return resolve_effective_project(
            project=project,
            directory=directory,
            session_project=None,
            tool="wiki_read",
        )
    except InvalidProjectOverrideError as exc:
        raise ValueError(f"wiki_read: {exc}") from exc


@observe(tier="stage", metric="tools.wiki._scope_wiki_results")
def _scope_wiki_results(
    results: list[dict],
    *,
    tags: list[str] | None,
    max_results: int,
) -> list[dict]:
    """Apply the row-level recall-visibility guard + trim to ``max_results``.

    Car C7 (0047 §5 C7) removed TWO things this helper used to do:

    * the ``is_directory_eligible`` post-filter — ``WikiStore.query`` now takes
      ``project_id`` and pushes the predicate into the stage-1 ``WHERE``, so the
      rows this dropped are no longer fetched (and no longer eat the LIMIT);
    * the C2 downweight multiply — retired outright. It scaled
      ``_retrieval_score`` by a factor in (0, 1) for ``task_list`` pages, whose
      disposition is now ``exclude``; and the identical multiply on the fusion
      path carried a sign bug (a negative cross-encoder logit is RAISED by a
      sub-1.0 factor). Nothing survives that used it.

    ``is_recall_visible`` stays. It is idempotent with the WHERE — both read
    ``recall_disposition`` + ``opt_in_tag`` from the same policy — and it is the
    guard for rows reaching this path any other way.
    """
    # Task 0134: wiki_query is a SEARCH path and used to bypass
    # recall_disposition entirely. See is_recall_visible for the shared rule.
    results = [r for r in results if is_recall_visible(r, tags)]
    return results[:max_results]


@_tool()
def wiki_query(
    query: str,
    tags: list[str] | None = None,
    category: str | None = None,
    max_results: int = 5,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> list[dict]:
    """Search wiki pages by keyword + semantic similarity.

    Returns matching pages with relevance scores. Use tags and category to filter.

    Car M (0047 §7, §16.6): the ``project=`` override lets a caller address
    another project's wiki namespace. The validated project_id is folded into
    the cache key AND the directory-eligibility check (a page whose
    ``directory_context`` resolves to that project_id's workspace is in-scope;
    pre-Car-L pages carry the canonical project_id via
    ``directory_context → derive_project_id`` round-trip). Precedence:
    ``project`` (override) > ``session_project`` > ``directory``-derived >
    ``"global"``. Wiki pages are not project-stamped at the row level (the
    directory_context remains the canonical key) — Car M's override is the
    FIRST step toward the per-project_id row stamp that lands with Car L's
    backfill.

    directory: Absolute project path for scoping results to caller directory + 'global'.
        Required (v5.65 Fix D): callers must supply the real host directory.
        Container-safe: daemon does NOT fall back to os.getcwd().

    DEPRECATION (Phase 2a): unified recall is now the only path — prefer
    ``recall(query, directory=..., type="wiki")`` which routes through the
    unified fan-out path with CE fusion and per-type quotas. This function
    remains fully functional for one release cycle as a thin alias.
    """
    import time as _time

    # v5.65 Fix D: hard-require directory — MUST be first check (before any store access).
    # Container-safe: do NOT fall back to os.getcwd().
    _dir_stripped = (directory or "").strip().rstrip("/")
    if not _dir_stripped:
        raise ValueError(
            "wiki_query: directory is required (caller must supply project dir; "
            "container cannot detect it via os.getcwd())"
        )

    # Car M (0047 §7, §16.6): resolve the effective project_id BEFORE the cache
    # key so the override stamps every cached lookup with the right scope. Type-
    # level guard runs here so a malformed ``project=`` raises ValueError,
    # matching the directory-required raise shape above (read tools stay fail-
    # loud at the boundary). When ``project`` is supplied, the resolve helper
    # logs-and-ignores ``directory`` (project wins — §9 [VERIFY]).
    _effective_project_id = _resolve_wiki_query_project(
        project=project,
        directory=_dir_stripped,
    )

    # Phase 2a: unified recall is now the ONLY path; emit deprecation unconditionally.
    try:
        logger.info(
            "wiki_query is deprecated. Use recall(query, directory=..., type='wiki') instead."
        )
    except Exception:
        pass

    # Car 2: cache the (embedding-computing) query by its inputs + wiki epoch.
    # A hit skips _st._wiki.query (which embeds the query text). A wiki write
    # bumps the epoch → the key moves → a stale result can never be served.
    # P11 / Car 2: start the duration clock BEFORE the cache lookup so the
    # yadgar_wiki_query_duration_ms histogram observes on EVERY wiki_query call
    # (cache hit AND miss) — obs total-visibility. The cache-hit early return
    # lives inside the try below so the finally still fires for hits.
    _wiki_query_t0 = _time.monotonic()
    # Car M: fold the resolved project_id into the cache key so the override
    # scopes every cached lookup — a stale cross-project read cannot leak.
    _q_key = (
        query,
        _dir_stripped,
        category,
        tuple(tags) if tags else None,
        max_results,
        _current_wiki_epoch(),
        _effective_project_id,
    )
    results: list[dict] = []

    try:
        _q_hit = _wiki_query_cache.get(_q_key)
        if _q_hit is not None:
            return _q_hit

        assert _st._wiki is not None, "WikiStore not initialized"
        # Car C7: the project scope + the policy-derived page_type exclusion now
        # ride in the query's WHERE clause, so the over-fetch that existed to
        # survive a post-filter is no longer needed — every row that comes back
        # is already in scope. The 3x is kept only as headroom for the row-level
        # visibility guard below.
        results = _st._wiki.query(
            query,
            tags,
            category,
            max_results * 3,
            scope=RecallScope(project_id=_effective_project_id, opt_in_tags=tags),
        )

        results = _scope_wiki_results(results, tags=tags, max_results=max_results)

        for r in results:
            r.pop("embedding", None)

        # Car 2: store the freshly-computed result under the epoch-folded key.
        # deep_copy=True → the cache holds an isolated copy; a caller mutating a
        # returned row (e.g. re-scoring) cannot corrupt the cached value.
        _wiki_query_cache.put(_q_key, results)
        return results

    finally:
        # P11: observe wiki_query total duration in finally so it fires on all paths.
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_wiki_query_duration_ms,
            )

            yadgar_wiki_query_duration_ms.observe((_time.monotonic() - _wiki_query_t0) * 1000)
        except Exception:
            pass


@_tool(power=True)
def wiki_read(
    slug: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Read a specific wiki page by slug.

    Car M (0047 §7, §16.6): the ``project=`` override lets a caller address
    another project's wiki namespace without leaving the current working
    tree. Precedence: ``project`` (override) > ``session_project`` >
    the hook-authored directory map > raise (ADR-0227 — never guessed).

    §25 Resolution order, keyed on the RESOLVED project_id (ADR-0233):
    1. project_id = $resolved   (the caller's own project)
    2. the Car C7 global reach tag   (the cross-project library)
    3. Not found → error dict.

    Car W2 (ledger task 219) re-pointed rungs 1-2 off ``directory``. They used
    to read ``directory_context = $caller_dir`` then ``= 'global'``, so
    ``project=`` reached only the validation and the cache key and the LOOKUP
    narrowed on the directory. Measured 2026-08-19: the SAME slug with the SAME
    correct ``project="quinyx/flux"`` resolved with no directory and returned
    "not found" with ``directory="/home/max/git/yadgar"`` — adding a correct
    scoping argument made the read fail. ADR-0233 makes project_id the sole
    scoping key; this shared read path had not been re-keyed, and every tool
    passing both arguments inherited the defect (car A6 worked around it in
    ``adr_get`` by dropping ``directory`` — a per-caller patch, not the fix).

    ``directory`` is therefore no longer a lookup input at all: it is accepted,
    logged-and-ignored when ``project`` also names an identity (§9 [VERIFY]),
    and otherwise consulted only by the resolver's hook-authored map tier.
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    # Car M (0047 §7, §16.6): resolve the effective project_id BEFORE the cache
    # key so the override scopes every cached lookup. Car 5: core enforces the
    # shape guard (sentinels included); no registry check runs on a READ — an
    # unregistered project_id simply resolves no page. _resolve_wiki_read_project
    # raises ValueError already prefixed with ``"wiki_read: "`` so we use
    # ``str(exc)`` rather than re-prefixing.
    try:
        _effective_project_id = _resolve_wiki_read_project(
            project=project,
            directory=directory,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    # Car 2: cache the resolved page by (slug, project_id) + wiki epoch.
    # A hit skips the WikiStore read. A wiki write to ANY page bumps the global
    # epoch → this key moves → a stale page can never be served (the
    # wiki-write-busts-read guarantee). Only found pages are cached; a not-found
    # result is cheap to recompute and a later create bumps the epoch anyway.
    # Car M: fold the resolved project_id into the cache key so a stale read
    # cannot leak across projects when the override path is exercised.
    #
    # Car W2 DROPPED ``_caller_dir`` from the key. It is no longer a lookup
    # input, so keying on it could only manufacture misses — two calls that
    # differ solely in ``directory=`` now resolve the same page and must share
    # the entry. Safe precisely BECAUSE the lookup stopped depending on it: the
    # cross-project guarantee rests on ``_effective_project_id``, which stays.
    _r_key = (slug, _current_wiki_epoch(), _effective_project_id)
    _r_hit = _wiki_read_cache.get(_r_key)
    if _r_hit is not None:
        return _r_hit

    # Car W2: the "no directory supplied — matching on slug alone" WARNING is
    # GONE, not moved. Post-re-key it described behaviour that no longer exists
    # (the lookup is project-keyed with or without a directory), and under
    # ADR-0233 a directory-less call is the CORRECT shape — warning on it told
    # callers to add the argument that caused the defect. It also fired once per
    # ``adr_get`` after car A6 dropped ``directory`` from that path.
    page = _st._wiki.read_by_project(slug, _effective_project_id)

    if page is None:
        return {"error": f"Wiki page '{slug}' not found"}
    page.pop("embedding", None)
    # Car C9 task 70: cap the returned `content` to a single-payload window
    # (task 71 caps the WRITE side to the same value). Mirrors the v5.7.x
    # uncapped-return class but on the single-page read path; keeps hot
    # MCP reads under the byte budget so a 50 KB rollup doesn't drag 50 KB
    # over the boundary. Truncation is applied BEFORE the cache put so a
    # warm hit returns the same view as the cold hit (consistent across
    # hits/misses); `content_truncated` lets callers re-fetch with a
    # version-pinned path if they need the full body.
    _content = page.get("content") or ""
    if isinstance(_content, str):
        # PR #65 review finding #2: cap on UTF-8 BYTES, not chars. A char slice
        # at 8192 chars on 3-byte CJK returns 24 576 bytes — 3× the promised
        # cap. Slice on encoded bytes, decode with errors="ignore" so a partial
        # trailing codepoint is dropped (NOT mid-codepoint garbage).
        _total = _content.encode("utf-8")
        if len(_total) > _WIKI_READ_CONTENT_CAP_BYTES:
            page["content"] = _total[:_WIKI_READ_CONTENT_CAP_BYTES].decode("utf-8", errors="ignore")
            page["content_truncated"] = True
            page["content_total_bytes"] = len(_total)
    # Car 2: store the resolved page. deep_copy=True → callers cannot corrupt the
    # cached value, and each hit returns its own isolated copy.
    _wiki_read_cache.put(_r_key, page)
    return page


@_tool(power=True)
def wiki_delete(slug: str) -> dict:
    """Delete a wiki page by slug."""
    # R3 Car 3c: the DB delete (+ epoch bump) forwards to the backend /admin op.
    # The SSE push_event and file-queue mirror cleanup are CORE-side side-effects
    # (core's SSE bus + the shared file-queue mirror) — they stay here, after the
    # forward reports the delete succeeded.
    _res = _forward_admin("wiki_delete", {"slug": slug})
    # Car C9 / task 223 + PR #65 review finding #5: wiki_delete's post-processing
    # guard must recognise a refusal envelope without swallowing the success
    # path. The LIVE refusal contract (yadgar/core/forward.py:115-120) keys
    # ONLY on ``refused`` — a present ``reason`` field is not a refusal marker
    # (it appears on audit-trail envelopes and on success-shaped responses).
    #
    # Two-step guard:
    #   1. Explicit ``refused=True`` marker → always short-circuit (live
    #      contract; covers the page-locked case task 223 closed).
    #   2. ``refused`` key ABSENT (live defence-in-depth: a future envelope
    #      drops the marker but keeps ``reason``) AND the envelope has no
    #      success markers AND ``reason`` is a non-empty string → short-circuit.
    #
    # The second arm does NOT swallow a success envelope that happens to carry
    # a ``reason`` field (e.g. ``{"deleted": True, "reason": "audit"}``) —
    # the ``deleted is True`` check rejects that shape before the guard
    # considers it a refusal.
    if isinstance(_res, dict):
        if _res.get("refused"):
            return _res
        if (
            "refused" not in _res
            and not _res.get("deleted")
            and isinstance(_res.get("reason"), str)
            and _res.get("reason")
        ):
            return _res
    if _res.get("deleted", False):
        _push_event({"event": "wiki_deleted", "slug": slug})
        try:
            _get_file_queue().delete_wiki(slug)
        except Exception as _fq_exc:
            logger.debug("File queue wiki mirror cleanup failed (non-fatal): %s", _fq_exc)
        # Car-N (ledger #341 / #365): cascade to ``wiki_bookmark`` so a deleted
        # page never leaves a dangling bookmark row. ``remove_bookmark`` deletes
        # the row AND compacts positions to keep the dense 0..N invariant the
        # next ``add_bookmark`` and ``list_bookmarks`` rely on. Idempotent on
        # missing slug → safe even if the storage DELETE in
        # ``delete_wiki_page`` already removed the row (defence-in-depth).
        # Calls ``_st._storage.remove_bookmark`` directly rather than forwarding
        # to the admin op — cascade is a core-side side-effect (sibling of
        # _push_event and the file-queue mirror cleanup), not a backend op.
        try:
            if _st._storage is not None:
                _st._storage.remove_bookmark(slug)
        except Exception as _bm_exc:
            logger.debug("Bookmark cascade failed (non-fatal): %s", _bm_exc)
        return {"deleted": True, "slug": slug}
    return {"deleted": False, "error": f"Wiki page '{slug}' not found"}


@_tool(power=True)
def wiki_list(
    category: str | None = None,
    limit: int = 100,
    slug_prefix: str | None = None,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> list[dict]:
    """List wiki pages by metadata only (no content). Use wiki_read(slug) for full content.

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.

    v5.42.5: when directory is supplied, results are scoped to that directory + 'global'.
    When absent (legacy call pattern), all pages are returned with a WARNING.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    assert _st._wiki is not None, "WikiStore not initialized"

    if directory is None:
        logger.warning(
            "wiki_list: no directory supplied — returning all pages (backward-compat mode). "
            "Pass directory= for project-scoped results (v5.42.5)."
        )

    # Push LIMIT, category, slug_prefix, and directory filters to the DB layer
    db_limit = limit if (limit is not None and limit > 0) else None
    pages = _st._wiki.list_pages(
        category=category,
        slug_prefix=slug_prefix,
        limit=db_limit,
        directory=directory,
    )
    out = []
    for p in pages:
        out.append(
            {
                "slug": p.get("slug"),
                "title": p.get("title"),
                "category": p.get("category"),
                "tags": p.get("tags", []),
                "confidence": p.get("confidence"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
                "source_count": len(p.get("source_memory_ids") or []),
            }
        )
    return out


@_tool(power=True)
def wiki_lint() -> dict:
    """Check wiki health: orphan pages, broken cross-refs, stale pages, low confidence.

    Returns issues list and summary stats.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    return _st._wiki.lint()


@_tool(power=True)
def wiki_autolink(
    directory: str | None = None,
    dry_run: bool = True,
    min_title_len: int = 6,
    max_links_per_page: int = 20,
    similarity_threshold: float = 0.70,
    semantic_guard: bool = True,
    *,
    project: str | None = None,
) -> dict:
    """Auto-insert [[slug]] cross-refs by matching other pages' titles in body text.

    SAFE BY DEFAULT — dry_run=True returns the proposed [[slug]] insertions
    WITHOUT mutating any page. Run dry-run first, review the proposals, then call
    again with dry_run=False to apply via the wiki upsert path (re-syncs
    crossrefs, bumps versions, tags changed pages 'auto-linked').

    Guards (all enforced, non-negotiable):
    - dry_run default (no accidental corpus mutation)
    - verbatim guard — never links inside code fences, inline code, existing
      [[...]], or URLs
    - length/specificity guard — min_title_len + word-boundary verbatim match
    - similarity guard — semantic_guard requires the target to clear
      similarity_threshold (kills coincidental title collisions)
    - idempotent — skips already-linked targets; a second run proposes nothing
    - no metadata clobber — each page keeps its own category/directory_context

    directory: absolute project path; scopes both the title map and the pages
        scanned to that dir + 'global'.

    Returns {applied, dry_run, proposals:[{page,target,title}], pages_changed,
             links_added}.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # R3 Car 3c: forward the whole tool — it writes when dry_run=False (upsert +
    # crossref re-sync + epoch bump). Forwarding the dry-run compute too keeps a
    # single path (harmless — no write on dry_run).
    _dir = (directory or "").strip().rstrip("/") or None
    return _forward_admin(
        "wiki_autolink",
        {
            "directory": _dir,
            "dry_run": dry_run,
            "min_title_len": min_title_len,
            "max_links_per_page": max_links_per_page,
            "similarity_threshold": similarity_threshold,
            "semantic_guard": semantic_guard,
        },
    )


@_tool()
def wiki_check_duplicate(  # secret-gate: skip — read-only dry-run, never writes to DB
    title: str,
    content: str,
    threshold: float | None = None,
    top_k: int = 5,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Dry-run similarity check: returns candidate duplicate pages without writing anything.

    Use before wiki_add to detect near-duplicates and decide whether to proceed.
    Returns candidates sorted by descending similarity score.

    Args:
        title: Title of the proposed new page.
        content: Content of the proposed new page.
        threshold: Minimum cosine similarity (0-1). Defaults to WIKI_SIM_CONTENT_THRESHOLD.
        top_k: Maximum candidates to return (default 5).
        directory: Caller project dir for the ADR-0158 directory-scoped candidate
            filter (None = no directory filter).

    Returns:
        {"candidates": [...], "threshold_used": float}
        Each candidate: {slug, title, similarity}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    assert _st._wiki is not None, "WikiStore not initialized"

    from yadgar._shared.config import get_settings

    cfg = get_settings()
    effective_threshold = (
        threshold if threshold is not None else getattr(cfg, "WIKI_SIM_CONTENT_THRESHOLD", 0.80)
    )

    try:
        candidates = _st._wiki.find_similar_wiki_pages(
            title=title,
            content=content,
            threshold=effective_threshold,
            top_k=top_k,
            directory_context=(directory.strip().rstrip("/") or None) if directory else None,
        )
    except WikiSimilarityGateUnavailable as exc:
        # Car C10 (task 312): read-side stays fail-OPEN — a degraded embedder
        # surfaces as an empty candidate list with a hint so the caller knows
        # the query could not evaluate. Distinct from the WRITE gate, which
        # fails CLOSED on the same exception.
        return {
            "candidates": [],
            "threshold_used": effective_threshold,
            "warning": (
                f"similarity check unavailable: {exc}. The dry-run result is "
                "indeterminate — duplicate-detection requires the embedder."
            ),
        }
    return {
        "candidates": candidates,
        "threshold_used": effective_threshold,
    }


# ── v5.41.0: Versioning + section-patching tools ──────────────────────────────


@observe(
    exempt=(
        "single resolve + UnresolvedProjectError mapping; no I/O — called at the "
        "slug-resolution family's tool boundary"
    )
)
def _resolve_slug_scope_project(
    *, project: str | None, directory: str | None, tool: str
) -> str | None:
    """Car W4: resolve the scoping project_id for the slug-resolution family.

    TOLERANT ON PURPOSE, and this is the one place the car differs from
    ``_resolve_wiki_read_project``. That resolver is strict — an unresolved
    identity raises ``UnresolvedProjectError``. It can afford to be: it landed
    with car M, BEFORE the value became a lookup key, and ``wiki_read``'s
    browser counterpart (``api_wiki_read``, ``http.py``) bypasses the tool and
    reads the store directly.

    This family has no such escape. ``api_wiki_history``,
    ``api_wiki_read_version``, ``api_wiki_diff`` and ``api_wiki_restore``
    (``http_wiki_versioning.py``) call the TOOLS positionally with a slug and
    nothing else — no directory, no project. A raise here would take the viz's
    version-history, diff and restore surfaces offline to fix a defect that is
    entirely about ``project=`` being ignored WHEN SUPPLIED. So an unresolved
    identity degrades to ``None`` and ``_resolve_page_id_by_slug`` keeps its
    directory rung. ADR-0233's residue on this path is therefore reduced, not
    yet zero; making it fail loud needs the four endpoints re-plumbed first.

    ``InvalidProjectOverrideError`` still propagates — a MALFORMED override is a
    caller bug, and that is exactly what ``accept_project_param`` (the call this
    replaces) did at every one of these sites.
    """
    try:
        return resolve_effective_project(
            project=project,
            directory=directory,
            session_project=None,
            tool=tool,
        )
    except UnresolvedProjectError:
        return None


# ── Car M (0047 §7 row M) — WRITE-SIDE cross-project scope gate ──────────────
#
# ``wiki_query`` (the read path) filters by ``project_id``; every wiki write
# path also takes ``page_id`` but until this car did NOT consult the page's
# stored ``project_id``. The defect: a caller holding auth for project A could
# read any page's ``page_id`` (e.g. via ``wiki_read`` on a global-reach slug)
# and pass it to ``wiki_update`` / ``wiki_replace_text`` / ``wiki_delete`` /
# etc. — those funnels trusted the integer. Today the page's ``project_id``
# is compared against the caller's resolved identity and a mismatch is
# REFUSED, the same shape Car J used for the mutability lock and Car 5 used
# for the registry check. HTTP 409 on the wire (``REFUSAL_STATUS``).
#
# The two helpers below are called from EVERY wiki write shell — the page_id-
# keyed family (10 tools) and the slug-keyed all-rows family (2 tools). The
# restamp carve-out on ``wiki_set_metadata(field="project_id")`` is the only
# hole punched, and it is a deliberate one (Car 9 retired every other restamp
# route, so this tool is now the SOLE mechanism by which a mis-stamped page
# is corrected — blocking it on the very field it sets would freeze the drift).
#
# Car C7's global-reach tag is the OTHER carve-out: pages carrying ``"global"``
# in their ``tags`` are explicitly cross-project-writable, per ADR-0171/0159
# (the agent-prompt library surface).


@observe(tier="hot", metric="tools.wiki._cross_project_page_refusal")
def _cross_project_page_refusal(
    *,
    tool: str,
    slug: str,
    caller_project_id: str | None,
    page: dict[str, Any] | None,
) -> dict | None:
    """Return a refusal envelope if a write to *slug* would cross project.

    Returns ``None`` when the write may proceed. Returns the structured refusal
    envelope (suitable for direct return from a tool shell) otherwise.

    ``page`` is the resolved page from ``_resolve_page_id_by_slug`` (may be
    None when the caller's project / directory rung does not match). When
    ``page`` is None, the helper looks up the unscoped row and checks THAT
    page's ``project_id`` against the caller's declared identity — that is
    the discovery path for the explicit-attack shape (caller A, page B).

    THE CALLER-DECLARES-PRINCIPLE. The gate runs only when the caller has
    declared a project identity (the ``project=`` kwarg, a session project, or
    a directory-derived one). An UNRESOLVED caller identity (``None``) is the
    browser-endpoint form (the four viz endpoints in ``http_wiki_versioning``)
    and the integration test form (``test_wiki_edit_primitives``, which
    pre-dates car M and never named a project). Failing loud there would take
    the viz's history / diff / restore surfaces offline to fix a defect that
    is structurally about ``project=`` being IGNORED WHEN SUPPLIED — the same
    tolerance the slug-scope resolver ships with (car W4 docstring).

    Once the caller declares, the page's stored ``project_id`` MUST match —
    otherwise the explicit-attack shape (caller A, page B) reaches the seam.
    The global-reach tag is the documented carve-out: agent-prompt library
    pages carry ``"global"`` in their ``tags`` and are explicitly writable
    from every project, per ADR-0171 / Car C7.
    """
    if page is None:
        # The slug→page_id resolver returned None for the caller's project —
        # the page either does not exist OR belongs to another project. Try
        # the unscoped lookup to discover which.
        gate_page = _resolve_page_unscoped(slug)
        if gate_page is None:
            return None  # truly does not exist — let the tool return not-found
        page = gate_page
    page_project_id = page.get("project_id")
    if caller_project_id is None:
        return None  # unscoped caller — see docstring; out of scope for this car
    if page_project_id == caller_project_id:
        return None
    # Cross-project. The reach tag is the ONLY sanctioned cross-project write.
    if "global" in (page.get("tags") or []):
        return None
    return {
        "ok": False,
        "refused": True,
        "reason": "cross_project_write_refused",
        "error": (
            f"{tool}: caller project_id={caller_project_id!r} cannot write to "
            f"page stamped project_id={page_project_id!r} (slug {slug!r})"
        ),
        "tool": tool,
        "caller_project_id": caller_project_id,
        "page_project_id": page_project_id,
    }


@observe(tier="hot", metric="tools.wiki._cross_project_slug_refusal")
def _cross_project_slug_refusal(
    *,
    tool: str,
    caller_project_id: str | None,
    slug: str,
    field: str,
    restamp_carve_out: bool = False,
) -> dict | None:
    """Slug-keyed write gate: refuse if the slug's row set spans a foreign project.

    ``wiki_set_metadata`` and ``wiki_set_mutability`` reach EVERY row for a
    slug (BC-G10, Car 9) — including rows that may belong to another project.
    A caller whose identity does not own every row in the set cannot write
    any of them. ``restamp_carve_out=True`` allows ``project_id`` restamps
    (the only sanctioned cross-project write on these tools, because the
    restamp IS the documented correction path for a mis-stamped page).

    Returns ``None`` when the write may proceed; the structured refusal envelope
    otherwise.
    """
    if restamp_carve_out and field == "project_id":
        return None
    # The caller-declares principle (same as the page-keyed helper): an
    # UNRESOLVED caller identity is the browser-endpoint / pre-Car-M
    # integration-test form; failing loud there would break existing flows.
    # The defect this car closes is the EXPLICIT attack where a caller
    # declaring A would mutate rows owned by B. A follow-up car that closes
    # the browser-endpoint rung would refuse here.
    if caller_project_id is None:
        return None
    if _st._wiki is None:
        # Mirror the storage-down behaviour every other wiki tool ships: refuse
        # loud rather than fall through to a permissive default. The same
        # reason as Car 5's registry-unavailable branch.
        return {
            "ok": False,
            "refused": True,
            "reason": "cross_project_write_refused",
            "error": f"{tool}: WikiStore not initialised; cannot scope slug {slug!r}",
            "tool": tool,
        }
    rows = _st._wiki._storage.get_wiki_page_ids_by_slug(slug)  # type: ignore[attr-defined]
    if not rows:
        return None  # slug not found — let the impl return its own not-found
    storage = _st._wiki._storage
    # Every row's project_id must be either None (legacy), "global" reach, or
    # owned by the caller. ANY foreign row blocks the write — a single slug
    # touching N projects is no one's to rewrite unilaterally.
    for row_id in rows:
        row = storage.get_wiki_page(int(row_id))
        if row is None:
            continue
        row_project = row.get("project_id")
        if row_project is None:
            continue
        if "global" in (row.get("tags") or []):
            continue
        if caller_project_id is not None and row_project == caller_project_id:
            continue
        return {
            "ok": False,
            "refused": True,
            "reason": "cross_project_write_refused",
            "error": (
                f"{tool}: slug {slug!r} spans a foreign project "
                f"(row project_id={row_project!r}, caller "
                f"project_id={caller_project_id!r})"
            ),
            "tool": tool,
            "row_project_id": row_project,
            "caller_project_id": caller_project_id,
        }
    return None


@observe(tier="stage", metric="tools.wiki._resolve_page_id_by_slug")
def _resolve_page_id_by_slug(
    slug: str,
    directory: str | None = None,
    *,
    project_id: str | None = None,
) -> tuple[int | None, dict | None]:
    """Resolve slug → (page_id, page), or (None, None). Project-keyed first.

    Car W4 (ledger task 226) — the ADR-0233 re-key of the shared resolver behind
    the whole section-patch / versioning family. It used to call
    ``read_by_directory(slug, directory)`` unconditionally, so a caller's
    ``project=`` reached ``accept_project_param``'s validation and nothing else:
    thirteen tools each dropped the resolved value on the floor. Same shape as
    the ``wiki_read`` defect car W2 fixed one layer up, and the same shape car 3
    found on the prelude path.

    THREE RUNGS, in this order:

    1. ``project_id`` supplied → ``read_by_project`` (own project, then the Car
       C7 global reach tag). This is the fix.
    2. no project, ``directory`` supplied → ``read_by_directory`` (exact
       directory, then ``'global'``). KEPT as the back-compat floor — see
       ``_resolve_slug_scope_project`` for why this family cannot fail loud yet.
       It is also what stops an unresolved directory WIDENING: without it a
       caller in tree A falls through to an unscoped ``LIMIT 1`` slug match and
       can be served tree B's row.
    3. neither → unscoped slug match (``read_by_directory(slug, None)``,
       unchanged). What the four browser endpoints do.

    Measured 2026-08-19 on the live corpus (2523 rows): ``project_id`` ↔
    ``directory_context`` is NOT 1:1 — ``quinyx/qwfm`` owns rows at six distinct
    directories, and ~57 rows corpus-wide sit at subdirectory paths that the
    exact-match directory ladder cannot reach from their own project root. Slugs
    are unique corpus-wide (2523 distinct), so rung 1 cannot change WHICH row
    resolves — only whether one resolves at all.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    if project_id:
        page = _st._wiki.read_by_project(slug, project_id)
    else:
        page = _st._wiki.read_by_directory(slug, directory)
    if page is None:
        return None, None
    return page.get("id"), page


@observe(tier="stage", metric="tools.wiki._resolve_page_unscoped")
def _resolve_page_unscoped(slug: str) -> dict | None:
    """Resolve *slug* WITHOUT caller scoping — used only by the cross-project gate.

    Car M (0047 §7 row M). The write-side gate needs to inspect a page that
    may not belong to the caller's project. The public
    ``_resolve_page_id_by_slug`` would return None in that case (rung 1
    filters on the caller's project), and the gate would never see the row
    it's supposed to refuse — the attacker just gets a "not found" envelope,
    which leaks no signal but also writes nothing.

    This helper returns the page dict the slug resolves to under the
    UNSCOPED rung (which, on a unique-corpus slug, is the only row for that
    slug). The gate then reads its ``project_id`` and compares to the caller's
    declared identity. The public resolver keeps its same-project behaviour
    so the ``if page_id is None: not-found`` semantics are unchanged for
    everything else.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    return _st._wiki.read_by_directory(slug, None)


@_tool()
def wiki_history(
    slug: str, limit: int = 20, directory: str | None = None, *, project: str | None = None
) -> dict:
    """List version history for a wiki page, newest first.

    Returns metadata for each version (no content — use wiki_read_version for that).
    Each entry includes: version, created_at, change_summary, size_bytes, provenance_agent.

    Note: wiki_add uses an async file queue by default. Calling wiki_history immediately
    after wiki_add(wait=False) may return a stale list until the queue drains (typically
    within 30s). Use wiki_add(wait=True) on the preceding write to guarantee
    read-your-writes consistency without sleep — wait=True writes synchronously so the
    version row is visible immediately.

    Args:
        slug: Wiki page slug.
        limit: Max versions to return (default 20).
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(project=project, directory=directory, tool="wiki_history")
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    versions = _st._wiki.history(page_id, limit=limit)
    total = _get_storage().get_max_version_for_page(page_id)
    return {"slug": slug, "page_id": page_id, "versions": versions, "total_versions": total}


@_tool()
def wiki_read_version(
    slug: str, version: int, directory: str | None = None, *, project: str | None = None
) -> dict:
    """Read a specific historical version of a wiki page (full content + snapshot fields).

    Args:
        slug: Wiki page slug.
        version: Version number (1-based; use wiki_history to find version numbers).
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns the full snapshot including: version, title, content, category, tags,
    confidence, source_memory_ids, change_summary, created_at.

    C12 (ADR-0226): ``branch`` is no longer in that list. Migration 032 dropped the
    column from ``wiki_page_version`` and every snapshot writer was silenced, so a
    field list still naming it was a false contract in the surface a model reads.

    Error: {"error": "...", "max_version": N} if version not found.
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_read_version"
    )
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.read_version(page_id, version)
    result["slug"] = slug
    return result


@_tool()
def wiki_diff(
    slug: str,
    v1: int,
    v2: int,
    fmt: str = "unified",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Diff two versions of a wiki page.

    Args:
        slug: Wiki page slug.
        v1: First (older) version number.
        v2: Second (newer) version number.
        fmt: "unified" (default, human-readable text diff) or "json" (structured).
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    unified format returns: {"diff": "<unified diff text>", "v1": N, "v2": M, ...}
    json format returns: {"hunks": [...], "added_lines": N, "removed_lines": M,
                          "sections_changed": [...], ...}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(project=project, directory=directory, tool="wiki_diff")
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.diff(page_id, v1, v2, fmt=fmt)
    result["slug"] = slug
    return result


@_tool(power=True)
def wiki_restore(
    slug: str,
    version: int,
    wait: bool = False,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Restore a wiki page to a previous version by creating a new version.

    Creates a NEW version (N+1) whose content matches the specified historical version.
    Intervening versions are preserved — restore does not delete history.
    Rebuilds embedding, crossrefs, and all snapshot fields (title, tags, category,
    confidence) from the restored version.

    I26 secret-gate: NOT applied on restore. The content being restored was
    already secret-gated when first stored — re-gating would incorrectly reject
    your own previously approved content. This is intentional, not an oversight.

    Bypasses the v5.39 similarity gate: restore is explicit user intent (recovery
    from corruption), not a new duplicate page.

    Use wiki_history to see version numbers; use wiki_diff to confirm the content
    before restoring.

    Args:
        slug: Wiki page slug.
        version: Version number to restore from (use wiki_history to list).
        wait: Accepted for API symmetry with wiki_add. This tool writes
            synchronously (no queue) — wait=True is a no-op.
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {"page_id": N, "restored_from_version": V, "new_version": N+1, "note": "..."}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(project=project, directory=directory, tool="wiki_restore")
    # R3 Car 3c: slug→page_id resolution stays CORE (backend has a different cwd,
    # so backend-side resolution would resolve the wrong row); the restore write
    # forwards keyed by page_id.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_restore", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    return _forward_admin("wiki_restore", {"page_id": page_id, "version": version, "slug": slug})


@_tool(power=True)
def wiki_append_section(
    slug: str,
    section_heading: str,
    content: str,
    position: str = "end_of_section",
    wait: bool = False,
    directory: str | None = None,
    heading_type: str = "h2",
    *,
    project: str | None = None,
) -> dict:
    """Section-atomic wiki write: patch a specific section without replacing entire content.

    Prevents the 2026-05-31 corruption pattern where agents replaced full wiki content
    with only their section patch (destroying everything else). Use this instead of
    wiki_update(fields={"content": <short patch>}) for targeted edits.

    Heading detection (controlled by heading_type, default 'h2'):
      h2 (default) — matches ## or ### at column 0. Case-insensitive. Ignores
        ## inside fenced code blocks. Use "Pipeline#2" syntax for 2nd occurrence.
      h3 — same as h2 (## and ### both matched by default).
      bold — matches **Bold Header** first-line patterns outside code fences.
      blockquote — matches "> text" first-line patterns.

    Positions:
      end_of_section    (default) — append content before next heading
      start_of_section  — insert immediately after heading line
      replace_section   — replace section body (heading preserved)
      new_section_top   — create new section at top (error if heading exists)
      new_section_bottom — create new section at bottom (error if heading exists)

    Error responses:
      {"error": "section_not_found", "available_sections": [...]}
      {"error": "section_exists"} — heading already present + new_section_* position
      {"error": "ambiguous_section"} — multiple headings + non-replace position
        (use "Heading#2" syntax to address 2nd occurrence)
      {"error": "invalid_heading_type"} — heading_type not in {h2, h3, bold, blockquote}

    wait: Accepted for API symmetry with wiki_add. This tool writes
        synchronously (no queue) — wait=True is a no-op.

    project: Cross-project override. When it resolves, it REPLACES ``directory``
        as the scope key (ADR-0233, car W4); the directory rung is reached only
        when no identity resolves.

    Returns: {"page_id": N, "new_version": M, "section_heading": "...",
              "action": "appended", "size_before": X, "size_after": Y}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_append_section"
    )
    # I26: secret-gate on written content (STAYS core)
    _gate = gate_or_reject(content, tags=[])
    if _gate is not None:
        return _gate

    # Car C9 task 71 (paired with task 70): cap the section body at the
    # same 8 192-byte window the read path now uses. Without this, a single
    # append of a 50 KB dump would have grown the row past the read cap,
    # so the truncation marker would fire on EVERY subsequent read —
    # the append "succeeded" but the page was made unreadable by it. The
    # constant is shared with the read cap (line 700) so the two stay in
    # lockstep; bump both together. Length is bytes on the UTF-8 encode
    # to match what crosses the wire — char length would let a 4-byte
    # payload slip past a 1 KB intended cap.
    _content_bytes = len((content or "").encode("utf-8"))
    if _content_bytes > _WIKI_READ_CONTENT_CAP_BYTES:
        return {
            "error": "section_content_too_large",
            "content_bytes": _content_bytes,
            "max_bytes": _WIKI_READ_CONTENT_CAP_BYTES,
        }

    # R3 Car 3c: slug→page_id resolution stays core (backend has no git/cwd); the
    # section write forwards keyed by page_id. Car M reads the resolved page
    # dict (second tuple slot) so the cross-project gate needs no extra
    # storage round-trip.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_append_section", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_append_section",
        {
            "page_id": page_id,
            "section_heading": section_heading,
            "content": content,
            "position": position,
            "heading_type": heading_type,
            "slug": slug,
        },
    )


# ── v5.61.0: Layer 4 — Metadata primitives ───────────────────────────────────


@_tool(power=True)
def wiki_set_metadata(
    slug: str,
    field: str,
    value: str | None,
    directory: str | None = None,
    *,
    project: str | None = None,  # noqa: ARG001 — kept for API back-compat
) -> dict:
    """Set ``project_id`` / ``directory_context`` on ALL rows sharing a slug.

    Reaches every row for the slug — including 'global' stragglers — not just
    the single row returned by §25 resolution (BC-G10). This fixes the bug where
    wiki_set_metadata reported changed=False even though straggler rows were
    never touched (only one row was resolved via LIMIT 1 resolution).

    ``field`` is one of:

    * ``"project_id"`` — CURRENT. ADR-0233 made it the sole scoping key, and
      this is the ONLY path that restamps it on an existing page: ``wiki_add``
      with ``replace_slug`` / ``force`` / ``upsert`` all update the row WITHOUT
      restamping ``project_id`` (measured live on ``wiki_page:7710``,
      2026-08-19), which used to leave delete-and-recreate as the only option
      for a mis-stamped page.
    * ``"directory_context"`` — LEGACY, retained for back-compat. It is not a
      scoping key any more (ADR-0233) but it is still the field a wrongly-keyed
      directory is corrected through.

    ADR-0215 removed branch scoping, so 'branch' is not settable; any other
    field is rejected.

    Validation:
    - ``project_id``: non-empty string, and NOT one of the manufactured
      identities ADR-0227 deletes (``global`` / ``unresolved`` / ``system`` /
      empty). Global REACH travels as the Car C7 tag, never as a project_id.
      ``None`` is rejected too — nulling the column makes the page unreachable
      from every project-scoped read.
    - ``directory_context``: ``'global'`` or an absolute path (starts with '/').

    NO REGISTRY CHECK runs here, still — but Car 5 (2026-08-20) removed the
    premise this paragraph used to rest on. It said "no writer of
    ``wiki_page.project_id`` performs one", which was true and was the defect:
    ``wiki_add`` now gates on
    ``_project_registry.assert_project_registered_for_create``, so the CREATION
    path is no longer looser than this CORRECTION path. ``wiki_page`` is still
    a SurrealDB row with no FK to ``project``, and ``assert_project_registered``
    still fires only on the engine-#2 ledger tables — the check reaches
    ``wiki_add`` over the forwarded ``list_project_rows`` seam, which degrades
    to the shape guard rather than hard-failing with
    ``ProjectRegistryUnavailableError`` where engine #2 is not composed. What
    stays absent here is that forwarded lookup on a single-row correction whose
    caller has just been told which project the page belongs to.

    Idempotent per row: no version row created when the value already matches.
    On real change per row: creates a wiki_page_version row (v5.41 versioning).
    Logs old + new value per row for audit trail.

    Bypasses v5.39 similarity gate (metadata revision, not a new page).

    Args:
        slug: Wiki page slug.
        field: ``'project_id'`` (current) or ``'directory_context'`` (legacy).
        value: New value. This is what stamps the page — the ``project``
            keyword below is an ignored back-compat parameter, not the stamp.
        directory: Kept for API back-compat (unused — all-rows path needs no §25 resolution).

    Returns: {ok, slug, rows_updated, page_ids} or {ok: False, error}.
    Preserved keys for back-compat callers that inspect {ok, slug}.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # ADR-0215: 'branch' left the allowed-field set with the rest of branch
    # scoping. Rejected here at the MCP boundary; Car 9 also removed it from
    # WikiStore._METADATA_FIELDS, which closes the privileged POST /admin path
    # that reaches set_metadata_by_slug without passing through this shell.
    #
    # The message is composed here rather than rendered from the allowlist:
    # ``sorted(_METADATA_FIELDS)`` cannot say which member is current and which
    # is retained legacy, and this is the message a caller actually reads (the
    # shell returns before forwarding). The old text read
    # ``allowed: ['directory_context']`` — so a caller who asked for the
    # ADR-0233 scoping key was told by the tool to use the concept ADR-0233
    # retired. Naming the retired key as THE allowed one is the defect.
    if field not in ("project_id", "directory_context"):
        return {
            "ok": False,
            "error": (
                f"invalid field '{field}' — allowed: 'project_id' (current scoping "
                "key, ADR-0233) and 'directory_context' (legacy, retained for "
                "back-compat)"
            ),
        }
    # R3 Car 3c: slug-keyed all-rows metadata write forwards to backend /admin.
    # No §25 page_id resolution needed (impl reaches every row for the slug).
    #
    # Car M (0047 §7 row M): slug-keyed cross-project scope gate. The all-rows
    # pattern means a single slug can touch N projects; if ANY row is foreign
    # the caller cannot rewrite any of them. The ``project_id`` field is the
    # restamp carve-out (Car 9) — the documented correction for a mis-stamped
    # page must not be blocked by the same gate that exists to enforce it.
    _pid = accept_project_param(project, directory)
    _slug_refusal = _cross_project_slug_refusal(
        tool="wiki_set_metadata",
        caller_project_id=_pid,
        slug=slug,
        field=field,
        restamp_carve_out=True,
    )
    if _slug_refusal is not None:
        return _slug_refusal
    return _forward_admin("wiki_set_metadata", {"slug": slug, "field": field, "value": value})


@_tool(power=True)
def wiki_set_mutability(
    slug: str,
    value: str | None,
    reason: str,
    directory: str | None = None,
    *,
    project: str | None = None,  # noqa: ARG001 — kept for API back-compat
) -> dict:
    """Set ``mutability_override`` on ALL rows sharing *slug* (Car J).

    Power-gated, logged, SOLE escape hatch for changing a page's mutability.
    Mirrors ``wiki_set_metadata``'s all-rows pattern — every row sharing the
    slug (including 'global' stragglers) gets the new override.

    Validation:
    - ``value`` must be ``"free"`` | ``"locked"`` | ``"derived"`` | ``None``.
      ``None`` clears the override back to the per-type default.
    - ``reason`` is REQUIRED — non-empty, logged for audit (D26).

    Bypasses the storage-layer mutability gate (this tool is the privileged
    writer; the gate would deadlock its own purpose). Sanctioned server-side
    lifecycle transitions (Car G supersede retype, Car K nightly sweep) use a
    separate ``_sanctioned=True`` path on ``storage.update_wiki_page`` — they
    do NOT call this tool.

    Args:
        slug: Wiki page slug.
        value: New mutability value, or None to clear the override.
        reason: Required audit reason (logged per row).
        directory: Kept for API back-compat (unused — all-rows path).

    Returns: ``{ok, slug, rows_updated, page_ids}`` or ``{ok: False, error}``.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    _pid = accept_project_param(project, directory)
    if not reason or not reason.strip():
        return {
            "ok": False,
            "error": "reason is required for mutability_override audit log",
        }
    # Car M (0047 §7 row M): slug-keyed cross-project scope gate.
    _slug_refusal = _cross_project_slug_refusal(
        tool="wiki_set_mutability",
        caller_project_id=_pid,
        slug=slug,
        field="mutability_override",
        restamp_carve_out=False,
    )
    if _slug_refusal is not None:
        return _slug_refusal
    return _forward_admin(
        "wiki_set_mutability",
        {"slug": slug, "value": value, "reason": reason},
    )


# ── v5.61.0: Layer 1 — Anchor-text primitives ────────────────────────────────


@_tool(power=True)
def wiki_replace_text(
    slug: str,
    old_text: str,
    new_text: str,
    occurrences: int | str = 1,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Replace old_text with new_text in a wiki page (surgical anchor-text edit).

    Caller never computes line/col. Server finds the text and applies the replacement.

    occurrences controls matching:
      1 (default) — require exactly one match (unique text). Reject if 0 or >1.
      N (int)     — require exactly N matches. Reject if count != N.
      'all'       — replace every occurrence (≥1 required, else reject).

    No-op (ok:True, replaced_count=0) when old_text == new_text.
    Reject (ok:False) when found-count != occurrences.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        old_text: Text to find (exact match, case-sensitive).
        new_text: Replacement text.
        occurrences: Expected match count, or 'all'.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_replace_text"
    )
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_replace_text", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_replace_text",
        {
            "page_id": page_id,
            "old_text": old_text,
            "new_text": new_text,
            "occurrences": occurrences,
            "slug": slug,
        },
    )


@_tool(power=True)
def wiki_delete_text(
    slug: str,
    text: str,
    occurrences: int | str = 1,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Delete text from a wiki page (surgical anchor-text edit).

    Absent text is a no-op (ok:True, replaced_count=0) — not an error.
    Reject (ok:False) when text IS present but found-count != occurrences.
    occurrences='all' deletes every match (≥1 required for present text).

    No secret gate: nothing new is written.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        text: Text to remove (exact match, case-sensitive).
        occurrences: Expected match count when text present, or 'all'.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_delete_text"
    )
    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    # No secret gate (nothing new is written).
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_delete_text", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_delete_text",
        {"page_id": page_id, "text": text, "occurrences": occurrences, "slug": slug},
    )


@_tool(power=True)
def wiki_insert_after(
    slug: str,
    anchor_text: str,
    new_text: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Insert new_text immediately after anchor_text in a wiki page.

    anchor_text must be unique (exactly one occurrence). Reject if absent or non-unique.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        anchor_text: Unique text to locate (exact, case-sensitive).
        new_text: Content to insert immediately after anchor_text.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_insert_after"
    )
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_insert_after", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_insert_after",
        {"page_id": page_id, "anchor_text": anchor_text, "new_text": new_text, "slug": slug},
    )


@_tool(power=True)
def wiki_insert_before(
    slug: str,
    anchor_text: str,
    new_text: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Insert new_text immediately before anchor_text in a wiki page.

    anchor_text must be unique (exactly one occurrence). Reject if absent or non-unique.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        anchor_text: Unique text to locate (exact, case-sensitive).
        new_text: Content to insert immediately before anchor_text.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_insert_before"
    )
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_insert_before", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_insert_before",
        {"page_id": page_id, "anchor_text": anchor_text, "new_text": new_text, "slug": slug},
    )


# ── v5.61.0: Layer 2 — Positional primitives ─────────────────────────────────


@_tool(power=True)
def wiki_replace_at(
    slug: str,
    line: int,
    col: int,
    length: int,
    new_text: str,
    anchor_hint: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Replace `length` chars at (line, col) in a wiki page (positional escape hatch).

    anchor_hint MUST be ≥20 chars. The actual text at (line, col) must start with
    anchor_hint — guards against caller off-by-one arithmetic bugs.

    line/col are 1-indexed. length is in chars (not bytes).

    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        line: 1-indexed line number.
        col: 1-indexed column number.
        length: Number of chars to replace.
        new_text: Replacement text.
        anchor_hint: Expected text at (line, col). Must be ≥20 chars.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(project=project, directory=directory, tool="wiki_replace_at")
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_replace_at", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_replace_at",
        {
            "page_id": page_id,
            "line": line,
            "col": col,
            "length": length,
            "new_text": new_text,
            "anchor_hint": anchor_hint,
            "slug": slug,
        },
    )


@_tool(power=True)
def wiki_delete_at(
    slug: str,
    line: int,
    col: int,
    length: int,
    anchor_hint: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Delete `length` chars at (line, col) in a wiki page (positional escape hatch).

    anchor_hint MUST be ≥20 chars. The actual text at (line, col) must start with
    anchor_hint — guards against caller off-by-one arithmetic bugs.

    line/col are 1-indexed. length is in chars (not bytes).

    No secret gate: nothing new is written.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        line: 1-indexed line number.
        col: 1-indexed column number.
        length: Number of chars to delete.
        anchor_hint: Expected text at (line, col). Must be ≥20 chars.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(project=project, directory=directory, tool="wiki_delete_at")
    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    # No secret gate (nothing new is written).
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_delete_at", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_delete_at",
        {
            "page_id": page_id,
            "line": line,
            "col": col,
            "length": length,
            "anchor_hint": anchor_hint,
            "slug": slug,
        },
    )


@_tool(power=True)
def wiki_insert_at(
    slug: str,
    line: int,
    col: int,
    new_text: str,
    anchor_hint: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Insert new_text at (line, col) in a wiki page (positional escape hatch).

    anchor_hint MUST be ≥20 chars. The text immediately BEFORE the insertion
    point must end with anchor_hint — guards against off-by-one bugs.

    line/col are 1-indexed.

    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        line: 1-indexed line number.
        col: 1-indexed column (1 = start of line, len+1 = after end of line).
        new_text: Text to insert at position.
        anchor_hint: Expected text immediately before insertion point. Must be ≥20 chars.
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(project=project, directory=directory, tool="wiki_insert_at")
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_insert_at", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_insert_at",
        {
            "page_id": page_id,
            "line": line,
            "col": col,
            "new_text": new_text,
            "anchor_hint": anchor_hint,
            "slug": slug,
        },
    )


# ── v5.61.0: Layer 3 — Structural primitives ─────────────────────────────────


@_tool(power=True)
def wiki_replace_markdown_block(
    slug: str,
    block_type: str,
    block_index: int,
    new_content: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Replace the Nth block of block_type in a wiki page (structural edit).

    Parses the markdown structure, locates the Nth block of the given type,
    and replaces the entire block span (including fence markers, >, #, etc.)
    with new_content.

    block_type must be one of: paragraph, heading, code_fence, blockquote, list, table.
    block_index is 0-based within the given block_type.

    Useful for: replace the 3rd code fence, swap a heading, rewrite a blockquote.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        block_type: Type of markdown block to target.
        block_index: 0-based index within that block_type.
        new_content: Replacement content for the block (whole span including markers).
        directory: Caller directory for §25 resolution.
        project: Cross-project override. When it resolves, it REPLACES
            ``directory`` as the scope key (ADR-0233, car W4); the
            directory rung is reached only when no identity resolves.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # Car W4 (ledger task 226): the resolved project_id is the SCOPE KEY
    # (ADR-0233), not just a validated argument. This was a bare
    # ``accept_project_param(project, directory)`` whose return value was
    # discarded, so ``project=`` never reached the lookup.
    _pid = _resolve_slug_scope_project(
        project=project, directory=directory, tool="wiki_replace_markdown_block"
    )
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_content, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _page = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
    # Car M (0047 §7 row M): write-side cross-project scope gate. Runs BEFORE
    # the not-found branch so the gate can fall back to the unscoped slug
    # resolver — the explicit-attack shape (caller A, page B) would otherwise
    # get a generic "not found" and the gate would never see the row.
    _refusal = _cross_project_page_refusal(
        tool="wiki_replace_markdown_block", slug=slug, caller_project_id=_pid, page=_page
    )
    if _refusal is not None:
        return _refusal
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_replace_markdown_block",
        {
            "page_id": page_id,
            "block_type": block_type,
            "block_index": block_index,
            "new_content": new_content,
            "slug": slug,
        },
    )
