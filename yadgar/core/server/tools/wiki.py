"""Wiki MCP tool registrations."""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.security.secrets import gate_or_reject
from yadgar._shared.server_helpers import _has_unpaired_surrogate, _push_event
from yadgar._shared.storage.directory import is_directory_eligible
from yadgar._shared.wiki.policy import is_recall_visible
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
def _check_wiki_add_context(directory: str | None) -> dict:
    """Reject a ``wiki_add`` that names no scope at all.

    Returns ``{}`` when the write may proceed, or the ``UnresolvedProjectError``
    payload to REJECT.

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
    """
    if not (directory or "").strip():
        return {"stored": False, "ok": False, **UnresolvedProjectError("wiki_add").payload}
    return {}


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
        from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415

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
) -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references.

    Car M (0047 §7, §16.6): the ``project=`` override lets a caller address
    another project's wiki namespace. The validated project_id is stamped on
    the enqueued payload (``payload["project_id"]``) so the drainer routes
    the write to that project_id's pages; ``directory`` stays as the
    directory-context hint (the same shape ``directory_context`` already
    carries). Precedence: ``project`` (override) > ``session_project`` >
    ``directory``-derived > ``"global"``. The deep "is this project_id in the
    registry?" check is backend-side (Car A0 `_ensure_project_exists_sync`,
    §15 / ADR-0078); core enforces the type-level guard.

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
    _decision = _check_wiki_add_context(directory)
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
    try:
        _effective_project_id = resolve_effective_project(
            project=project,
            directory=_effective_dir,
            session_project=None,
            tool="wiki_add",
        )
    except UnresolvedProjectError as exc:
        return {"stored": False, "ok": False, **exc.payload}
    except InvalidProjectOverrideError as exc:
        return {
            "stored": False,
            "ok": False,
            "error": f"wiki_add: {exc}",
            "op_type": "wiki_add",
        }

    # v5.39.0 slug generation (O(1), needed for enqueue payload and wait path).
    import re as _re_slug  # noqa: PLC0415

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
    }
    # C3 (0047 PR#40 §5.C3): stamp the resolved project_id UNCONDITIONALLY.
    # Car M stamped it only when the caller passed ``project=``, which left the
    # DEFAULT path relying on the drainer to infer one from directory_context —
    # inside a container with no git binary and no host project mounts, where
    # the classifier silently yields ``local/<basename>`` or ``unresolved``
    # (§1.1). This tool call is the only participant that can see the session,
    # so it is the only honest place to resolve. The deep registry check stays
    # backend-side (`_ensure_project_exists_sync`, §15 / ADR-0078).
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


def _current_wiki_epoch() -> int:
    """Global structural epoch — bumped on every wiki write. Folded into cache keys
    so a wiki mutation busts every cached wiki read/query regardless of dir/branch
    normalization (the bump is global; see storage.wiki._bump_wiki_epoch)."""
    try:
        from yadgar._shared.runtime.cache_epoch import _current_epoch  # noqa: PLC0415

        return _current_epoch(None)
    except Exception:
        return 0


@observe(tier="stage", metric="tools.wiki._make_wiki_read_cache")
def _make_wiki_read_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
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
    from yadgar.core.cache import (  # noqa: PLC0415
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
def _resolve_wiki_read_project(*, project: str | None, directory: str | None) -> str | None:
    """Car M: resolve the effective project_id for ``wiki_read``.

    Raises ``ValueError`` on a malformed ``project=``; the caller wraps it
    in a dict-returning error envelope so the tool boundary stays clean.
    The error string is prefixed with ``"wiki_read: "`` so callers see the
    tool name in any traceback.
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


@observe(tier="stage", metric="tools.wiki._scope_and_downweight_wiki_results")
def _scope_and_downweight_wiki_results(
    results: list[dict],
    *,
    directory: str,
    tags: list[str] | None,
    max_results: int,
) -> list[dict]:
    """Apply Car C2 downweight + directory-eligibility filter + trim to ``max_results``.

    Pulled out of ``wiki_query`` for complexity governance (fn_loc cap) — the
    block has its own substrate (downweight policy + is_directory_eligible +
    re-sort) and no tool-boundary state, so extraction is safe.
    """
    # Task 0134: wiki_query is a SEARCH path and used to bypass
    # recall_disposition entirely. See is_recall_visible for the shared rule.
    results = [r for r in results if is_recall_visible(r, tags)]

    # v5.43.0 / v5.62.0: directory scoping — scope to caller directory.
    # v5.62.0: replaces hand-rolled predicate with is_directory_eligible() from
    # storage/directory.py — single source of truth for the eligible-set rule.
    # Applied as Python-side post-filter (mirrors recall directory filter from v5.42.5).
    results = [r for r in results if is_directory_eligible(r.get("directory_context"), directory)]

    # Car C2 (0047 §7 3b): downweight penalty — a ``task_list`` page (D22
    # `task → downweight`) sinks below include-disposition pages of
    # comparable relevance without being filtered out. The legacy
    # ``wiki_query`` path has no fusion / CE — ``_retrieval_score`` IS
    # the ranking key. Apply the multiplier IN PLACE and re-sort BEFORE
    # the cache so the reordering takes effect on this call AND on any
    # cache hit (the cached copy holds the post-penalty scores; the
    # penalty is deterministic per ``page_type`` so this is correct).
    # Guarded on factor < 1.0 to skip the re-sort cost when the
    # operator has disabled the penalty.
    from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415
    from yadgar._shared.wiki.policy import downweight_multiplier  # noqa: PLC0415

    _dw_factor = float(_get_settings().RECALL_DOWNWEIGHT_FACTOR)
    if _dw_factor < 1.0:
        for r in results:
            r["_retrieval_score"] = float(r.get("_retrieval_score", 0.0)) * downweight_multiplier(
                r, _dw_factor
            )
        results.sort(key=lambda r: r.get("_retrieval_score", 0.0), reverse=True)

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
    import time as _time  # noqa: PLC0415

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
        # Fetch extra results before the directory filter so we still return
        # max_results after pruning.
        results = _st._wiki.query(query, tags, category, max_results * 3)

        # Car C2 downweight + v5.62 directory scoping + trim — extracted for
        # complexity governance (fn_loc cap).
        results = _scope_and_downweight_wiki_results(
            results,
            directory=_dir_stripped,
            tags=tags,
            max_results=max_results,
        )

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
                yadgar_wiki_query_duration_ms,  # noqa: PLC0415
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
    ``directory``-derived > ``"global"``. The resolved project_id is folded
    into the cache key so a stale read cannot leak across projects. When
    BOTH ``project`` and ``directory`` are supplied, ``project`` wins and
    ``directory`` is logged-and-ignored (§9 [VERIFY]).

    §25 Resolution order (directory-aware; ADR-0215 removed the branch axis):
    1. directory=$caller_dir  (project-scoped)
    2. directory='global'     (global fallback)
    3. Not found → error dict.


    When directory is not supplied, the slug is matched on its own
    (backward-compat mode; WARNING logged).
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    # Car M (0047 §7, §16.6): resolve the effective project_id BEFORE the cache
    # key so the override scopes every cached lookup. The deep registry check
    # is backend-side (Car A0 `_ensure_project_exists_sync`, §15 / ADR-0078);
    # core enforces the type-level guard only. _resolve_wiki_read_project
    # raises ValueError already prefixed with ``"wiki_read: "`` so we use
    # ``str(exc)`` rather than re-prefixing.
    try:
        _effective_project_id = _resolve_wiki_read_project(
            project=project,
            directory=directory,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    # Car 2: cache the resolved page by (slug, dir) + wiki epoch.
    # A hit skips the WikiStore read. A wiki write to ANY page bumps the global
    # epoch → this key moves → a stale page can never be served (the
    # wiki-write-busts-read guarantee). Only found pages are cached; a not-found
    # result is cheap to recompute and a later create bumps the epoch anyway.
    # Car M: fold the resolved project_id into the cache key so a stale read
    # cannot leak across projects when the override path is exercised.
    _caller_dir = directory.strip().rstrip("/") if directory is not None else None
    _r_key = (slug, _caller_dir, _current_wiki_epoch(), _effective_project_id)
    _r_hit = _wiki_read_cache.get(_r_key)
    if _r_hit is not None:
        return _r_hit

    if directory is None:
        # Legacy fallback — no directory supplied; backward-compat mode.
        logger.warning(
            "wiki_read('%s'): no directory supplied — matching on slug alone. "
            "Pass directory= for project-scoped results (v5.42.5).",
            slug,
        )
    page = _st._wiki.read_by_directory(slug, _caller_dir or None)

    if page is None:
        return {"error": f"Wiki page '{slug}' not found"}
    page.pop("embedding", None)
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
    deleted = _forward_admin("wiki_delete", {"slug": slug}).get("deleted", False)
    if deleted:
        _push_event({"event": "wiki_deleted", "slug": slug})
        try:
            _get_file_queue().delete_wiki(slug)
        except Exception as _fq_exc:
            logger.debug("File queue wiki mirror cleanup failed (non-fatal): %s", _fq_exc)
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

    from yadgar._shared.config import get_settings  # noqa: PLC0415

    cfg = get_settings()
    effective_threshold = (
        threshold if threshold is not None else getattr(cfg, "WIKI_SIM_CONTENT_THRESHOLD", 0.80)
    )

    candidates = _st._wiki.find_similar_wiki_pages(
        title=title,
        content=content,
        threshold=effective_threshold,
        top_k=top_k,
        directory_context=(directory.strip().rstrip("/") or None) if directory else None,
    )
    return {
        "candidates": candidates,
        "threshold_used": effective_threshold,
    }


# ── v5.41.0: Versioning + section-patching tools ──────────────────────────────


@observe(tier="stage", metric="tools.wiki._resolve_page_id_by_slug")
def _resolve_page_id_by_slug(
    slug: str,
    directory: str | None = None,
) -> tuple[int | None, dict | None]:
    """Directory-resolve slug → page dict. Returns (page_id, page) or (None, None).

    v5.42.5 (F1 fix): accepts directory from the caller so resolution uses caller
    context instead of daemon os.getcwd(). ADR-0215 removed the branch axis.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page = _st._wiki.read_by_directory(slug, directory)
    if page is None:
        return None, None
    return page.get("id"), page


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
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, page = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns the full snapshot including: version, title, content, category, tags,
    confidence, source_memory_ids, branch, change_summary, created_at.

    Error: {"error": "...", "max_version": N} if version not found.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    unified format returns: {"diff": "<unified diff text>", "v1": N, "v2": M, ...}
    json format returns: {"hunks": [...], "added_lines": N, "removed_lines": M,
                          "sections_changed": [...], ...}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {"page_id": N, "restored_from_version": V, "new_version": N+1, "note": "..."}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # R3 Car 3c: slug→page_id resolution stays CORE (backend has a different cwd,
    # so backend-side resolution would resolve the wrong row); the restore write
    # forwards keyed by page_id.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {"page_id": N, "new_version": M, "section_heading": "...",
              "action": "appended", "size_before": X, "size_after": Y}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret-gate on written content (STAYS core)
    _gate = gate_or_reject(content, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: slug→page_id resolution stays core (backend has no git/cwd); the
    # section write forwards keyed by page_id.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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
    """Set directory_context on ALL rows sharing a slug (BC-G10 fix).

    Reaches every row for the slug — including 'global' stragglers — not just
    the single row returned by §25 resolution. This fixes the bug where
    wiki_set_metadata reported changed=False even though straggler rows were
    never touched (only one row was resolved via LIMIT 1 resolution).

    field must be 'directory_context'. Other fields are rejected. ADR-0215
    removed branch scoping, so 'branch' is no longer a settable field.

    Validation: directory_context must be 'global' or an absolute path
    (starts with '/').

    Idempotent per row: no version row created when the value already matches.
    On real change per row: creates a wiki_page_version row (v5.41 versioning).
    Logs old + new value per row for audit trail.

    Bypasses v5.39 similarity gate (metadata revision, not a new page).

    Args:
        slug: Wiki page slug.
        field: Metadata field to set. Must be 'directory_context'.
        value: New value.
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
    if field != "directory_context":
        return {
            "ok": False,
            "error": f"invalid field '{field}' — allowed: ['directory_context']",
        }
    # R3 Car 3c: slug-keyed all-rows metadata write forwards to backend /admin.
    # No §25 page_id resolution needed (impl reaches every row for the slug).
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
    accept_project_param(project, directory)
    if not reason or not reason.strip():
        return {
            "ok": False,
            "error": "reason is required for mutability_override audit log",
        }
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

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    # No secret gate (nothing new is written).
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    # No secret gate (nothing new is written).
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_content, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory)
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
