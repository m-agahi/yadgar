"""Miscellaneous MCP tool registrations: anchor, checkpoint, restore, install_hooks,
sync_instructions, seed_project, and MCP resource endpoints.

# Module size justified: cross-cutting lifecycle tools that share a single dependency
# pattern — all depend on the _get_file_queue() and _get_storage() singletons
# from server.lifecycle (restore forwards to the backend since T2 Car B). The tools are heterogeneous by purpose but cohesive by their
# shared lifecycle coupling. Splitting (e.g. hooks vs. replay vs. resources) would
# produce tiny files that each re-import the same lifecycle singletons, with no
# architectural benefit. The module has a clear, bounded scope: everything that isn't
# memorize/recall/wiki/admin/project belongs here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yadgar._shared.runtime.state as _st
from yadgar import __version__
from yadgar._shared.config import get_settings
from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import (
    _get_consolidation,
    _get_storage,
)
from yadgar._shared.security.secrets import gate_or_reject
from yadgar._shared.server_helpers import _has_unpaired_surrogate, normalize_write_context
from yadgar.core.forward import _forward_restore

# R2a Car D2: _get_file_queue moved to yadgar.core.lifecycle (core → core).
from yadgar.core.lifecycle import _get_file_queue
from yadgar.core.server._app import _tool, mcp_server
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    accept_project_param,
    resolve_effective_project,
)

logger = logging.getLogger(__name__)

settings = get_settings()


@observe(tier="stage", metric="tools.misc._validate_checkpoint_surrogates")
def _validate_checkpoint_surrogates(  # noqa: PLR0913
    current_task: str,
    custom_context: str,
    key_decisions: list[str] | None,
    open_questions: list[str] | None,
    next_steps: list[str] | None,
    active_errors: list[str] | None,
    files_being_edited: list[str] | None,
) -> dict | None:
    """Return an error dict if any free-text field contains unpaired surrogates, else None."""
    for _field in (current_task, custom_context):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}
    for _lst in (
        key_decisions or [],
        open_questions or [],
        next_steps or [],
        active_errors or [],
        files_being_edited or [],
    ):
        for _item in _lst:
            if isinstance(_item, str) and _has_unpaired_surrogate(_item):
                return {"stored": False, "reason": "invalid_unicode_surrogates"}
    return None


def _gate_checkpoint_text(
    current_task: str,
    custom_context: str,
    key_decisions: list[str] | None,
    next_steps: list[str] | None,
    open_questions: list[str] | None,
    active_errors: list[str] | None,
) -> dict | None:
    """Run the secret gate over all checkpoint free-text fields.

    Returns a gate-rejection dict if a secret is detected, else None.
    """
    # v5.10.2: secret gate — scan all free-text fields before enqueue
    _list_text = " ".join(
        item
        for lst in (
            key_decisions or [],
            next_steps or [],
            open_questions or [],
            active_errors or [],
        )
        for item in lst
        if isinstance(item, str)
    )
    return gate_or_reject(current_task, custom_context, _list_text)


@_tool(always_load=True)
def checkpoint(  # noqa: PLR0913 — pre-existing 8-param fn
    directory: str,
    current_task: str = "",
    files_being_edited: list[str] = None,
    key_decisions: list[str] = None,
    open_questions: list[str] = None,
    next_steps: list[str] = None,
    active_errors: list[str] = None,
    custom_context: str = "",
    *,
    project: str | None = None,
) -> dict:
    """Snapshot your current working state for post-compaction recovery.

    Call this periodically during long sessions. After context compaction,
    the restore tool uses this checkpoint to reconstruct what you were doing.
    Checkpoints auto-supersede — only the latest one matters.

    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    #
    # C13: the validated value is KEPT and stamped on the enqueue payload
    # below. C4b gave memorize / anchor / agent_prompt_save their enqueue-time
    # stamp and did not reach checkpoint; C5 then widened the drainer's
    # ``_validate_project_id`` gate from wiki_add to EVERY op type. Together
    # those meant checkpoint validated an identity it had been handed and then
    # threw it away, so every checkpoint job was permanently DLQ'd as
    # ``missing_project_id`` -- including the ones from the stop-hook protocol,
    # which C5 had just taught to pass ``project="{project}"``. The highest-
    # volume recovery path in the system, discarding the one value that would
    # have let it through.
    #
    # The checkpoint TABLE still has no project_id column -- that is C11's
    # work. The PAYLOAD carrying one is what the drainer's gate requires.
    # (Found independently by two C13 sweeps, tests/core and tests/scripts.)
    _project_id = accept_project_param(project, directory)
    # secret-gate: skip — gate_or_reject() is called inside _gate_checkpoint_text()
    _surrogate_err = _validate_checkpoint_surrogates(
        current_task,
        custom_context,
        key_decisions,
        open_questions,
        next_steps,
        active_errors,
        files_being_edited,
    )
    if _surrogate_err is not None:
        return _surrogate_err

    # v5.10.2: secret gate — scan all free-text fields before enqueue
    _gate = _gate_checkpoint_text(
        current_task,
        custom_context,
        key_decisions,
        next_steps,
        open_questions,
        active_errors,
    )
    if _gate is not None:
        return _gate

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root so checkpoints stay restorable from the canonical repo.
    # ADR-0215: the branch half of the pair is discarded — nothing reads it now.
    directory = normalize_write_context(directory)

    # Enqueue-only: the sync write runs in the backend drainer (R3 Car 1).
    _get_file_queue().enqueue(
        "checkpoint",
        {
            "directory": directory,
            "current_task": current_task,
            "files_being_edited": files_being_edited,
            "key_decisions": key_decisions,
            "open_questions": open_questions,
            "next_steps": next_steps,
            "active_errors": active_errors,
            "custom_context": custom_context,
            # C13: the enqueue-time stamp the drainer's gate requires. Set from
            # the value the caller named, never derived — the drainer runs in a
            # container that cannot mint one (ADR-0227). A caller that named
            # none still lands in the DLQ, which is the DECLARED failure path
            # for a queued write (recoverable, requeueable once the caller
            # passes project=), not a silent drop.
            "project_id": _project_id,
        },
    )
    return {"queued": True, "directory": directory}


@_tool(always_load=True)
def restore(directory: str = "", *, project: str | None = None) -> dict:
    """Restore context after compaction using Hippocampal Replay.

    Reconstructs your working context from:
    - Latest checkpoint (what you were doing)
    - Anchored memories (critical facts)
    - Hot project memories (thermodynamic ranking)
    - Predicted context (SR cognitive map navigation)
    - Detected knowledge gaps

    Call this after context compaction, or it will be called
    automatically via the post-compact hook.

    T2 Car B: thin forwarder — the restore compute (CheckpointRestore +
    CognitiveMap SR navigation) runs backend-side behind POST /restore.

    project: C10g (0047 PR#40 §5) — REQUIRED in effect. The anchor bucket, the
      hot-memories bucket and gap detection are all keyed on the resolved
      project_id now, so a call that names no project gets the checkpoint and
      memory blocks (both still path-keyed) and empty memory buckets. When
      neither ``project=`` nor a session identity names one, this returns the
      structured unresolved-project envelope rather than guessing (ADR-0227).
    """
    # C10g (0047 PR#40 §5): PROMOTED from ``accept_project_param`` to a real
    # resolution, the same promotion C5b made for ``bootstrap_project``. That
    # helper only validates the override; restore's memory-backed sinks are now
    # keyed on the resolved project_id, so boundary-validation-only would leave
    # the anchor and hot buckets permanently empty.
    #
    # Both values go down the wire: restore fans out to five sinks and they key
    # on different columns (see ``CheckpointRestore.restore``). ``directory``
    # still keys the checkpoint and memory-block sinks.
    #
    # Failure returns the tool's error envelope rather than raising — the MCP
    # boundary never raises, matching ``anchor`` above.
    try:
        _effective_project_id = resolve_effective_project(
            project=project,
            directory=directory,
            session_project=None,
            tool="restore",
        )
    except UnresolvedProjectError as exc:
        return {"ok": False, **exc.payload}
    except InvalidProjectOverrideError as exc:
        return {"ok": False, "reason": f"restore: {exc}"}
    return _forward_restore(directory, project_id=_effective_project_id)


_VALID_ANCHOR_TIERS = frozenset({"semantic_immortal", "conditional", "ephemeral"})


@observe(tier="stage", metric="tools.misc._validate_anchor_inputs")
def _validate_anchor_inputs(
    content: str,
    context: str,
    reason: str,
    tier: str | None,
    valid_until: str | None,
    ttl_days: int | None,
) -> tuple[str, str | None, dict | None]:
    """Validate anchor inputs and compute expiry.

    Returns (resolved_tier, computed_valid_until, error_dict_or_None).
    On error, the third element is the error dict to return to the caller.
    """
    for _field in (content, context, reason):
        if _has_unpaired_surrogate(_field):
            return "", None, {"stored": False, "reason": "invalid_unicode_surrogates"}

    # v5.15.0: secret gate — pass _anchor tag so allowlist can fire for anchor() calls.
    # anchor() always writes with ["_anchor"] tag; forward that to gate so allowlist
    # entries keyed on "_anchor" become effective.
    _gate = gate_or_reject(content, reason, tags=["_anchor"])
    if _gate is not None:
        return "", None, _gate

    # v5.8.0: tier validation
    _tier = tier if tier is not None else "conditional"
    if _tier not in _VALID_ANCHOR_TIERS:
        return (
            "",
            None,
            {
                "stored": False,
                "reason": f"invalid tier: {tier!r}. Must be one of {sorted(_VALID_ANCHOR_TIERS)}",
            },
        )

    # v5.8.0: semantic_immortal requires non-empty reason
    if _tier == "semantic_immortal" and settings.ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON:
        if not reason or not reason.strip():
            return (
                "",
                None,
                {
                    "stored": False,
                    "reason": "anchor tier=semantic_immortal requires a non-empty reason explaining why this anchor is truly immortal",
                },
            )

    # v5.8.0: conflicting valid_until + ttl_days
    if valid_until is not None and ttl_days is not None:
        return (
            "",
            None,
            {
                "stored": False,
                "reason": "conflict: both valid_until and ttl_days provided — choose one",
            },
        )

    # v5.8.0: compute valid_until at API boundary
    from yadgar._shared.server_helpers import _compute_valid_until

    _computed_valid_until: str | None = None
    try:
        _computed_valid_until = _compute_valid_until(_tier, valid_until, ttl_days, settings)
    except ValueError as _vu_exc:
        return "", None, {"stored": False, "reason": str(_vu_exc)}

    return _tier, _computed_valid_until, None


@_tool(always_load=True)
def anchor(  # noqa: PLR0913 — MCP tool signature; ``project`` is keyword-only
    content: str,
    context: str,
    reason: str = "",
    tier: str | None = None,
    valid_until: str | None = None,
    ttl_days: int | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Mark critical context as compaction-resistant.

    Anchored memories get max heat, max importance, and is_protected=True.
    They are ALWAYS included in post-compaction restoration regardless
    of other scoring. Use for decisions, constraints, and critical facts
    that must survive compaction.

    tier: "semantic_immortal" | "conditional" (default) | "ephemeral".
      semantic_immortal → no expiry, requires non-empty reason.
      conditional → expires in ANCHOR_CONDITIONAL_TTL_DAYS (default 90d).
      ephemeral → expires in ANCHOR_EPHEMERAL_TTL_DAYS (default 14d).

    valid_until: ISO-8601 UTC explicit expiry. Mutually exclusive with ttl_days.
    ttl_days: shorthand valid_until = now() + ttl_days. Mutually exclusive with valid_until.

    project: C4b (0047 PR#40 §5) — the OPTIONAL cross-project override, same
      contract as ``memorize``/``wiki_add``. ``anchor`` had no such parameter
      before this car: C3 measured its 42-tool surface over the ``@_tool``
      functions taking ``directory``, and ``anchor`` names that argument
      ``context``. The RESOLVED project_id is stamped on the enqueued payload
      on EVERY call; ``project=`` changes which project is named, never
      whether one is.

    """
    # secret-gate: skip — gate_or_reject() is called inside _validate_anchor_inputs()
    _tier, _computed_valid_until, _err = _validate_anchor_inputs(
        content, context, reason, tier, valid_until, ttl_days
    )
    if _err is not None:
        return _err

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root so anchors stay visible to canonical-repo recall.
    # ADR-0215: the branch half of the pair is discarded — nothing reads it now.
    context = normalize_write_context(context)

    # C4b (0047 PR#40 §5): resolve the effective project_id BEFORE the enqueue
    # so the wire payload carries it. This tool call is the only participant
    # that can see the session; the drainer runs in a container with no git
    # binary and no host project mounts (ADR-0227 §1.1). A malformed override
    # surfaces as the tool's error envelope so the MCP boundary never raises.
    try:
        _effective_project_id = resolve_effective_project(
            project=project,
            directory=context,
            session_project=None,
            tool="anchor",
        )
    except UnresolvedProjectError as exc:
        return {"queued": False, "stored": False, "ok": False, **exc.payload}
    except InvalidProjectOverrideError as exc:
        return {"queued": False, "stored": False, "ok": False, "reason": f"anchor: {exc}"}

    # Enqueue-only: the sync write runs in the backend drainer (R3 Car 1).
    _enqueue_payload: dict = {
        "content": content,
        "context": context,
        "reason": reason,
        "tier": _tier,
        "project_id": _effective_project_id,
    }
    if _computed_valid_until is not None:
        _enqueue_payload["valid_until"] = _computed_valid_until
    _get_file_queue().enqueue("anchor", _enqueue_payload)
    return {
        "queued": True,
        "status": "anchored",
        "is_protected": True,
        "reason": reason,
        "tier": _tier,
    }


@_tool(power=True)
def install_hooks(project_directory: str = "", scope: str = "project") -> dict:
    """Install Claude Code hooks for automatic memory capture and replay.

    Car 7 (2026-07-26): this MCP tool now delegates to the unified
    ``install_client`` orchestrator (`yadgar install --client claude-code --hooks`).
    The legacy `yadgar install-hooks` CLI command has been hard-removed
    (Car 7 of the opencode port train); see the migration message in
    yadgar/core/cli/install_hooks.py.

    Behaviour: container-refuse still applies (the container's
    filesystem is throwaway); when allowed, the tool delegates to
    install_client with mcp=False, rules=False, hooks=True, scope,
    project_directory — i.e. ONLY the hooks surface, no MCP/rules
    re-write (matches the legacy install_hooks surface exactly).

    Installs five hook types via the per-kind emitter in
    hooks_render.register_hooks -> _emit_claude_json:
      - PreCompact: drain context before compaction
      - SessionStart (compact): restore context after compaction
      - SessionStart (all): inject project context on every new session
      - PostToolUse: capture every tool action into action_log
      - UserPromptSubmit: auto-recall relevant memories on every user turn

    Works in both stdio and HTTP transport modes.

    project_directory: The project root. Defaults to cwd.
    scope: "project" (default) writes hooks to project .claude/settings.json;
           "global" writes SessionStart, PreCompact, PostToolUse, UserPromptSubmit,
           and PreToolUse hooks to ~/.claude/settings.json and scripts to ~/.claude/hooks/.
           Stop hook is always global regardless of scope.
    """
    from yadgar.core.install.install_hooks_lib import is_running_in_container

    # Refuse when running inside a container: the container's filesystem is
    # throwaway and $HOME resolves to /root, not the host user's home dir.
    if is_running_in_container():
        return {
            "status": "refused",
            "reason": "running_in_container",
            "detail": (
                "install_hooks must run on the host (the container's filesystem is throwaway). "
                "Run `yadgar install --client claude-code --hooks --scope=global` on the host machine."
            ),
            "host_command": "yadgar install --client claude-code --hooks --scope=global",
        }

    # Delegate to the unified orchestrator. Surface = hooks ONLY (matches
    # the legacy install_hooks contract); MCP and rules are explicitly off
    # so this tool doesn't accidentally re-write those configs when a caller
    # only wanted hooks.
    from yadgar.core.install.clients.install import InstallOptions, install_client

    result = install_client(
        "claude-code",
        opts=InstallOptions(
            mcp=False,
            rules=False,
            hooks=True,
            scope=scope,
            project_dir=Path(project_directory) if project_directory else None,
            # home_dir=Path.home() so the per-kind emitter resolves
            # ~/.claude/ from the host user's home (NOT the container's
            # /root). Matches the legacy install_hooks contract exactly.
            home_dir=Path.home(),
            dry_run=False,
        ),
    )
    return {
        "status": "installed",
        "scope": scope,
        "result": result,
    }


@_tool(power=True)
def sync_instructions(
    claude_md_path: str = "",
    target_path: str = "",
    section_header: str = "",
    client: str = "",
) -> dict:
    """Sync Yadgar instructions into a rules file (CLAUDE.md or AGENTS.md).

    Finds or creates the section identified by *section_header* in the target
    file and updates it with the latest tools, capabilities, and rules.
    Idempotent — re-running replaces only the Yadgar section; surrounding
    content is preserved.  Call this on session start or after Yadgar updates.

    Generalisation (Car 2): the find/replace-section mechanic is now
    client-agnostic.  Delegates to
    ``yadgar.core.install.clients.rules_render.section_replace`` + atomic write
    so the same safety property holds for any rules file.

    Arguments
    ---------
    claude_md_path:
        Path to CLAUDE.md (legacy parameter, kept for back-compat).
        When supplied, takes precedence over *target_path*.
        Defaults to ``~/.claude/CLAUDE.md``.
    target_path:
        Explicit target path for the rules file.  Ignored when
        *claude_md_path* is set.  Useful for writing to AGENTS.md or other
        per-client rules files.
    section_header:
        The ``## …`` delimiter that marks the start of the Yadgar section.
        Defaults to ``"## Memory System — Yadgar"`` (CC compat).
    client:
        Client name from ``CLIENT_REGISTRY`` (e.g. ``"codex"``,
        ``"opencode"``).  When supplied, the descriptor's
        ``rules_header`` and rendered body are used; *section_header* is
        ignored.  Useful for session-time sync from the MCP tool for
        non-CC clients.
    """
    from yadgar.core.install.clients.rules_render import (  # noqa: PLC0415
        _atomic_write_text,
        render_body,
        section_replace,
    )

    # ── Resolve target path ──────────────────────────────────────────────────
    if claude_md_path:
        md_path = Path(claude_md_path)
    elif target_path:
        md_path = Path(target_path)
    else:
        md_path = Path.home() / ".claude" / "CLAUDE.md"

    if not md_path.parent.is_dir():
        return {
            "status": "skipped",
            "reason": f"Directory {md_path.parent} does not exist",
        }

    # ── Resolve section header + body ────────────────────────────────────────
    if client:
        from yadgar.core.install.clients.registry import CLIENT_REGISTRY  # noqa: PLC0415

        descriptor = CLIENT_REGISTRY.get(client)
        if descriptor is None:
            return {
                "status": "error",
                "reason": f"Unknown client {client!r}; known: {sorted(CLIENT_REGISTRY)}",
            }
        resolved_header = descriptor.rules_header
        body = render_body(descriptor, __version__)
    else:
        resolved_header = section_header or "## Memory System — Yadgar"
        # For CC back-compat, use the CC descriptor so the rendered body
        # includes the compaction_shield + auto_capture addenda.
        from yadgar.core.install.clients.registry import CLIENT_REGISTRY  # noqa: PLC0415

        cc_descriptor = CLIENT_REGISTRY["claude-code"]
        # NOTE: CC addenda (compaction_shield, auto_capture) always included
        # because bare sync_instructions() exclusively targets CC's CLAUDE.md.
        # Non-CC callers should pass client= to get the correct descriptor.
        body = render_body(cc_descriptor, __version__)

    existing = md_path.read_text() if md_path.exists() else ""
    new_content = section_replace(existing, resolved_header, body)
    if not md_path.exists():
        new_content = "# Global Rules\n\n" + new_content

    _atomic_write_text(md_path, new_content)

    return {
        "status": "synced",
        "path": str(md_path),
        "version": __version__,
        "section_length": len(body),
    }


# ── MCP Resources ──────────────────────────────────────────────────────


@mcp_server.resource("memory://stats")
def resource_stats() -> str:
    """Live memory statistics."""
    storage = _get_storage()
    return json.dumps(storage.get_memory_stats())


@observe(tier="stage", metric="tools.misc._resource_project_or_none")
def _resource_project_or_none(resource: str) -> str | None:
    """Resolve the project for a parameterless MCP resource, or ``None``.

    Car 3 — the two resources below read the whole ``memory`` table. An MCP
    resource takes NO parameters, and ``session_project`` is hardcoded ``None``
    at every core call site today, so this raises for every real read: there is
    no identity source at this boundary to resolve FROM. That is the finding,
    not an omission — and it is why both resources fail CLOSED rather than
    keep returning the corpus.

    Deliberately written as a resolution rather than a bare ``return None``: the
    rule the resources follow is "scope or nothing", and when an identity source
    IS wired to this boundary they start working without another edit. Restoring
    the capability sooner needs a parameterised ``memory://hot/{project}``
    template — new API surface, queued, not built here.
    """
    try:
        return resolve_effective_project(
            project=None,
            directory=None,
            session_project=None,
            tool=resource,
        )
    except (UnresolvedProjectError, InvalidProjectOverrideError) as exc:
        logger.warning(
            "%s: no project identity is resolvable at this boundary — returning "
            "no rows rather than the whole corpus (%s)",
            resource,
            exc,
        )
        return None


def _unresolved_resource_payload(resource: str) -> str:
    """The fail-closed body: no rows, and a machine-readable reason."""
    return json.dumps(
        {
            "memories": [],
            "reason": "unresolved_project",
            "detail": (
                f"{resource} takes no parameters and no session identity is "
                "available, so it cannot be scoped to a project. Returning no "
                "rows: an unscoped read here returns every project's memories. "
                "Use recall(project=...) or restore(project=...) instead."
            ),
        }
    )


@mcp_server.resource("memory://hot")
@observe(tier="boundary", metric="resource.hot")
def resource_hot() -> str:
    """This project's memories with heat >= HOT_THRESHOLD.

    Car 3: was ``get_memories_by_heat(settings.HOT_THRESHOLD)`` with zero
    scoping — and ``HOT_THRESHOLD`` defaults to ``0.0``, so the resource served
    every memory row in the database to whoever read it.
    """
    project_id = _resource_project_or_none("memory://hot")
    if not project_id:
        return _unresolved_resource_payload("memory://hot")

    storage = _get_storage()
    memories = storage.get_memories_for_directory(project_id, min_heat=settings.HOT_THRESHOLD)
    for m in memories:
        m.pop("embedding", None)

    return json.dumps({"memories": memories, "project_id": project_id}, default=str)


@mcp_server.resource("memory://stale")
@observe(tier="boundary", metric="resource.stale")
def resource_stale() -> str:
    """This project's stale memories.

    Car 3: was ``get_stale_memories()`` — a bare ``WHERE is_stale = true`` over
    the whole corpus, with no project predicate of any kind.
    """
    project_id = _resource_project_or_none("memory://stale")
    if not project_id:
        return _unresolved_resource_payload("memory://stale")

    storage = _get_storage()
    memories = storage.get_stale_memories(project_id=project_id)
    for m in memories:
        m.pop("embedding", None)

    return json.dumps({"memories": memories, "project_id": project_id}, default=str)


@mcp_server.resource("memory://processes")
@observe(tier="boundary", metric="resource.processes")
def resource_processes() -> str:
    """List of astrocyte process stats."""
    consolidation = _get_consolidation()
    pool = consolidation.pool
    if pool is None:
        return json.dumps([])
    return json.dumps(pool.get_process_stats(), default=str)


# ── Project Seeding ────────────────────────────────────────────────


@_tool(power=True)
def seed_project(directory: str, dry_run: bool = False, *, project: str | None = None) -> dict:
    """Bootstrap Yadgar memory for an existing project in one call.

    Scans the project directory and creates foundational memories from:
    - Project structure and layout
    - Config files (package.json, pyproject.toml, Cargo.toml, go.mod, etc.)
    - Documentation (README, ARCHITECTURE, CONTRIBUTING, etc.)
    - CI/CD configuration
    - Entry points and key source files
    - Per-component summaries (monorepo-aware via config file boundaries)

    All seeded memories are tagged with '_seed' for identification.
    Re-running is safe — old seed memories are replaced, not appended to.

    directory: Project root directory to scan (absolute path).
    dry_run: If True, scan and show what would be stored without actually storing.
    """
    # C4 (0047 PR#40 §5): seed_project WRITES rows, so its ``project`` is
    # threaded for real rather than merely validated — the backend's
    # ``seed_store`` op stamps the value instead of deriving one it cannot
    # derive (ADR-0227). ``accept_project_param`` still runs first so a
    # malformed override raises at the MCP boundary.
    accept_project_param(project, directory)
    from yadgar.core.seed import seed_project as _seed

    resolved = str(Path(directory).resolve())
    _effective_project_id = resolve_effective_project(
        project=project,
        directory=resolved,
        session_project=None,
        tool="seed_project",
    )
    # T2 Car E1: the store phase forwards to the backend seed_store /admin op —
    # no core engine handles needed (scan + generate run host-side inside _seed).
    result = _seed(directory=resolved, dry_run=dry_run, project_id=_effective_project_id)
    # §4: Register this directory as a known project root for file-hash whitelist.
    if not dry_run:
        _st._project_roots.add(resolved)
    return result


# ── Project Registry Seeding (Car A, 2026-08-14 train) ──────────────────────


@_tool(power=True)
def project_seed(
    *,
    map_path: str | None = None,
) -> dict:
    """Seed the engine-#2 ``project`` registry from a map TSV.

    Car A (2026-08-14 identity train, plan §2). Closes the gap where
    ``backend.admin_exec.ledger.create_project_row`` is registered but
    had no MCP / CLI path; engine-#2 ledger writes were blocked by
    ADR-0078's ``_ensure_project_exists_sync`` guard with no way to
    prime the registry first.

    Reads the TSV at ``map_path`` (default: ``<cwd>/.yadgar/
    project-id-map.tsv``, gitignored), calls ``create_project_row`` per
    row over the backend ``/admin`` route, and returns a per-row
    ``created`` / ``skipped`` / ``failed`` tally. Idempotent — a second
    call is a no-op for already-present rows (backend raises
    ``DuplicateProjectError`` → returns ``skipped``). Drop / review
    rows are skipped (not registry rows — operator decisions).

    The guard at ``_ensure_project_exists_sync`` is NOT relaxed by
    this tool. This is the SEED that lets the guard ever succeed;
    subsequent writes still hit the registry check.

    ADR-0225: this tool takes NO ``directory`` parameter. The TSV's
    first column (``source_directory``) is a host-side origin hint
    captured at mint time and is NOT a scoping key. The registry keys
    on ``project_id`` alone.

    Args:
        map_path: Optional absolute path to the map TSV. Overrides the
            default location (``<cwd>/.yadgar/project-id-map.tsv``).
            When the operator staged the map outside the working tree,
            pass it here.

    Returns:
        ``{"ok": True, "counts": {seed, drop, review, created, skipped,
        failed}, "map_path": <path>}`` on completion. Backend errors on
        individual rows are reported in ``counts["failed"]``; structural
        map errors (file not found, malformed row) return
        ``{"ok": False, "error": ...}``.
    """
    from yadgar.core.cli.project import (
        DEFAULT_MAP_PATH,
        classify_row,
        parse_map,
        read_auth_token,
        seed_row,
    )

    # Resolve the map path. ``map_path`` is the ONLY positional contract
    # the caller has — no directory fallback, by design (ADR-0225).
    if map_path:
        resolved_map = Path(map_path)
    else:
        resolved_map = DEFAULT_MAP_PATH

    try:
        rows = parse_map(resolved_map)
    except SystemExit:
        # parse_map raises SystemExit(2) on structural errors. The
        # MCP boundary does not want a SystemExit — rewrap as an
        # error envelope so the client gets the same shape every
        # other failure here returns.
        return {
            "ok": False,
            "error": f"map file malformed or missing: {resolved_map}",
            "map_path": str(resolved_map),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"map read failed: {exc}",
            "map_path": str(resolved_map),
        }

    auth_token = read_auth_token()
    counts = {"seed": 0, "drop": 0, "review": 0, "created": 0, "skipped": 0, "failed": 0}
    for row in rows:
        kind = classify_row(row)
        counts[kind] += 1
        if kind != "seed":
            continue
        outcome = seed_row(row, auth_token=auth_token)
        counts[outcome] += 1

    return {"ok": True, "counts": counts, "map_path": str(resolved_map)}
