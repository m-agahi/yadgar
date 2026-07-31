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
import os
from pathlib import Path

import yadgar._shared.runtime.state as _st
from yadgar import __version__
from yadgar._shared.config import get_settings
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


@observe(tier="stage", metric="tools.misc._resolve_checkpoint_branch")
def _resolve_checkpoint_branch(
    directory: str, branch_hint: str | None
) -> tuple[str | None, dict | None]:
    """Resolve branch for checkpoint at the MCP boundary.

    Resolution order: _detect_branch(directory) → branch_hint → YADGAR_CI_BRANCH env.
    Returns (branch, None) on success, (None, error_dict) when branch is absent
    and not draining (hard-reject path).
    """
    # Capture branch at API boundary for payload tagging and future filter use.
    # v5.46.7: resolution order: _detect_branch(directory) → branch_hint
    #           → YADGAR_CI_BRANCH env → hard-reject.
    _branch = None
    try:
        import yadgar.core.server as _srv

        _branch = _srv._detect_branch(directory)
    except Exception:
        pass  # non-fatal — fall through to branch_hint

    # v5.42.3: branch_hint fallback
    if not _branch and branch_hint:
        _branch = branch_hint

    # v5.46.7: YADGAR_CI_BRANCH env fallback — CI runner sets this when git is unavailable.
    if not _branch:
        _branch = os.environ.get("YADGAR_CI_BRANCH") or None

    # v5.42.3: hard-reject at MCP boundary when branch context is absent.
    # Enqueue-only shell: always runs on the request thread (never draining).
    if not _branch:
        return None, {
            "error": "missing_branch",
            "stored": False,
            "message": (
                "Branch context required. Supply branch_hint=<current-branch-name> or ensure "
                "the working directory is a git repo accessible to the yadgar daemon."
            ),
            "field": "branch_hint",
            "op_type": "checkpoint",
        }

    return _branch, None


@_tool(always_load=True)
def checkpoint(  # noqa: PLR0913 — v5.42.3 added branch_hint param; pre-existing 8-param fn
    directory: str,
    current_task: str = "",
    files_being_edited: list[str] = None,
    key_decisions: list[str] = None,
    open_questions: list[str] = None,
    next_steps: list[str] = None,
    active_errors: list[str] = None,
    custom_context: str = "",
    branch_hint: str | None = None,
) -> dict:
    """Snapshot your current working state for post-compaction recovery.

    Call this periodically during long sessions. After context compaction,
    the restore tool uses this checkpoint to reconstruct what you were doing.
    Checkpoints auto-supersede — only the latest one matters.

    branch_hint: host-supplied branch name (v5.42.3). Used when daemon-side
      _detect_branch() cannot reach the host .git directory.
    """
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

    _branch, _branch_err = _resolve_checkpoint_branch(directory, branch_hint)
    if _branch_err is not None:
        return _branch_err

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root so checkpoints stay restorable from the canonical repo.
    directory, _branch = normalize_write_context(directory, _branch)

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
            "branch": _branch,
        },
    )
    return {"queued": True, "directory": directory}


@_tool(always_load=True)
def restore(directory: str = "") -> dict:
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
    """
    return _forward_restore(directory)


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


@observe(tier="stage", metric="tools.misc._resolve_anchor_branch")
def _resolve_anchor_branch(context: str, branch_hint: str | None) -> tuple[str | None, dict | None]:
    """Resolve branch for anchor at the MCP boundary.

    Resolution order: _detect_branch(context) → branch_hint → YADGAR_CI_BRANCH env.
    Returns (branch, None) on success, (None, error_dict) when branch is absent
    and not draining (hard-reject path).
    """
    _branch = None
    try:
        import yadgar.core.server as _srv

        _branch = _srv._detect_branch(context)
    except Exception:
        pass  # non-fatal — fall through to branch_hint

    # v5.42.3: branch_hint fallback
    if not _branch and branch_hint:
        _branch = branch_hint

    # v5.46.7: YADGAR_CI_BRANCH env fallback — CI runner sets this when git is unavailable.
    if not _branch:
        _branch = os.environ.get("YADGAR_CI_BRANCH") or None

    # v5.42.3: hard-reject at MCP boundary when branch context is absent.
    # Enqueue-only shell: always runs on the request thread (never draining).
    if not _branch:
        return None, {
            "error": "missing_branch",
            "stored": False,
            "message": (
                "Branch context required. Supply branch_hint=<current-branch-name> or ensure "
                "the working directory is a git repo accessible to the yadgar daemon."
            ),
            "field": "branch_hint",
            "op_type": "anchor",
        }

    return _branch, None


@_tool(always_load=True)
def anchor(
    content: str,
    context: str,
    reason: str = "",
    tier: str | None = None,
    valid_until: str | None = None,
    ttl_days: int | None = None,
    branch_hint: str | None = None,
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

    branch_hint: host-supplied branch name (v5.42.3). Used when daemon-side
      _detect_branch() cannot reach the host .git directory. Agents should pass
      branch_hint=<current-branch> from SessionStart hook context.
    """
    # secret-gate: skip — gate_or_reject() is called inside _validate_anchor_inputs()
    _tier, _computed_valid_until, _err = _validate_anchor_inputs(
        content, context, reason, tier, valid_until, ttl_days
    )
    if _err is not None:
        return _err

    # Capture branch at API boundary — enqueue-time value used by drainer.
    # v5.46.7: resolution order: _detect_branch(context) → branch_hint
    #           → YADGAR_CI_BRANCH env → hard-reject.
    _branch, _branch_err = _resolve_anchor_branch(context, branch_hint)
    if _branch_err is not None:
        return _branch_err

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root so anchors stay visible to canonical-repo recall.
    context, _branch = normalize_write_context(context, _branch)

    # Enqueue-only: the sync write runs in the backend drainer (R3 Car 1).
    _enqueue_payload: dict = {
        "content": content,
        "context": context,
        "reason": reason,
        "branch": _branch,
        "tier": _tier,
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


@mcp_server.resource("memory://hot")
@observe(tier="boundary", metric="resource.hot")
def resource_hot() -> str:
    """All memories with heat >= HOT_THRESHOLD."""
    storage = _get_storage()
    memories = storage.get_memories_by_heat(settings.HOT_THRESHOLD)
    for m in memories:
        m.pop("embedding", None)

    return json.dumps(memories, default=str)


@mcp_server.resource("memory://stale")
@observe(tier="boundary", metric="resource.stale")
def resource_stale() -> str:
    """All stale memories."""
    storage = _get_storage()
    memories = storage.get_stale_memories()
    for m in memories:
        m.pop("embedding", None)

    return json.dumps(memories, default=str)


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
def seed_project(directory: str, dry_run: bool = False) -> dict:
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
    from yadgar.core.seed import seed_project as _seed

    resolved = str(Path(directory).resolve())
    # T2 Car E1: the store phase forwards to the backend seed_store /admin op —
    # no core engine handles needed (scan + generate run host-side inside _seed).
    result = _seed(directory=resolved, dry_run=dry_run)
    # §4: Register this directory as a known project root for file-hash whitelist.
    if not dry_run:
        _st._project_roots.add(resolved)
    return result
