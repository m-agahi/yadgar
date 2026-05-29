"""Miscellaneous MCP tool registrations: anchor, checkpoint, restore, install_hooks,
sync_instructions, seed_project, and MCP resource endpoints.

# Module size justified: cross-cutting lifecycle tools that share a single dependency
# pattern — all depend on _get_replay(), _get_file_queue(), and _get_storage() singletons
# from server.lifecycle. The tools are heterogeneous by purpose but cohesive by their
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

import yadgar.server._state as _st
from yadgar import __version__
from yadgar.config import get_settings
from yadgar.file_queue import is_draining
from yadgar.restoration import CheckpointContext
from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool, mcp_server
from yadgar.server._helpers import _has_unpaired_surrogate
from yadgar.server.lifecycle import _get_consolidation, _get_file_queue, _get_replay, _get_storage

logger = logging.getLogger(__name__)

settings = get_settings()


@_tool()
def checkpoint(
    directory: str,
    current_task: str = "",
    files_being_edited: list[str] = None,
    key_decisions: list[str] = None,
    open_questions: list[str] = None,
    next_steps: list[str] = None,
    active_errors: list[str] = None,
    custom_context: str = "",
) -> dict:
    """Snapshot your current working state for post-compaction recovery.

    Call this periodically during long sessions. After context compaction,
    the restore tool uses this checkpoint to reconstruct what you were doing.
    Checkpoints auto-supersede — only the latest one matters.
    """
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
    _gate = gate_or_reject(current_task, custom_context, _list_text)
    if _gate is not None:
        return _gate

    # Capture branch at API boundary for payload tagging and future filter use.
    _branch = None
    try:
        import yadgar.server as _srv

        _branch = _srv._detect_branch(directory)
    except Exception:
        pass  # non-fatal — checkpoint proceeds without branch context

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
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
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync: %s", _fq_exc)

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    replay = _get_replay()

    # Enrich checkpoint with action stream summary if available
    enriched_context = custom_context
    buffer = _st._buffer
    if buffer is not None:
        action_summary = buffer.get_action_summary()
        if action_summary:
            enriched_context = (
                f"{custom_context}\n\n{action_summary}" if custom_context else action_summary
            )

    ctx = CheckpointContext(
        current_task=current_task,
        files_being_edited=files_being_edited or [],
        key_decisions=key_decisions or [],
        open_questions=open_questions or [],
        next_steps=next_steps or [],
        active_errors=active_errors or [],
        custom_context=enriched_context,
    )
    return replay.create_checkpoint(directory, ctx)


@_tool()
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
    """
    replay = _get_replay()
    return replay.restore(directory=directory)


_VALID_ANCHOR_TIERS = frozenset({"semantic_immortal", "conditional", "ephemeral"})


@_tool()
def anchor(
    content: str,
    context: str,
    reason: str = "",
    tier: str | None = None,
    valid_until: str | None = None,
    ttl_days: int | None = None,
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
    """
    for _field in (content, context, reason):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # v5.10.2: secret gate — scan content + reason before any state mutation
    _gate = gate_or_reject(content, reason)
    if _gate is not None:
        return _gate

    # v5.8.0: tier validation
    _tier = tier if tier is not None else "conditional"
    if _tier not in _VALID_ANCHOR_TIERS:
        return {
            "stored": False,
            "reason": f"invalid tier: {tier!r}. Must be one of {sorted(_VALID_ANCHOR_TIERS)}",
        }

    # v5.8.0: semantic_immortal requires non-empty reason
    if _tier == "semantic_immortal" and settings.ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON:
        if not reason or not reason.strip():
            return {
                "stored": False,
                "reason": "anchor tier=semantic_immortal requires a non-empty reason explaining why this anchor is truly immortal",
            }

    # v5.8.0: conflicting valid_until + ttl_days
    if valid_until is not None and ttl_days is not None:
        return {
            "stored": False,
            "reason": "conflict: both valid_until and ttl_days provided — choose one",
        }

    # v5.8.0: compute valid_until at API boundary
    from yadgar.server.tools.memorize import _compute_valid_until

    _computed_valid_until: str | None = None
    try:
        _computed_valid_until = _compute_valid_until(_tier, valid_until, ttl_days, settings)
    except ValueError as _vu_exc:
        return {"stored": False, "reason": str(_vu_exc)}

    # Capture branch at API boundary — enqueue-time value used by drainer.
    _branch = None
    try:
        import yadgar.server as _srv

        _branch = _srv._detect_branch(context)
    except Exception:
        pass  # non-fatal — anchor inserts with branch=NONE

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
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
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync: %s", _fq_exc)

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    replay = _get_replay()
    tags = ["_anchor"]
    if reason:
        tags.append(f"anchor:{reason}")
    memory_id = replay.anchor_memory(
        content,
        context,
        tags,
        reason,
        branch=_branch,
        tier=_tier,
        valid_until=_computed_valid_until,
    )
    return {
        "memory_id": memory_id,
        "status": "anchored",
        "is_protected": True,
        "reason": reason,
        "tier": _tier,
    }


@_tool(power=True)
def install_hooks(project_directory: str = "", scope: str = "project") -> dict:
    """Install Claude Code hooks for automatic memory capture and replay.

    Installs five hook types:
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
    from yadgar.install_hooks_lib import install_hooks_impl, is_running_in_container

    # Refuse when running inside a container: the container's filesystem is
    # throwaway and $HOME resolves to /root, not the host user's home dir.
    if is_running_in_container():
        return {
            "status": "refused",
            "reason": "running_in_container",
            "detail": (
                "install_hooks must run on the host (the container's filesystem is throwaway). "
                "Run `yadgar install-hooks --scope=global` on the host machine, or POST "
                "/hooks/install-bootstrap for the settings.json snippet to write manually."
            ),
            "host_command": "yadgar install-hooks --scope=global",
            "host_command_fallback": (
                "# manual: read host_command_fallback_response from POST /hooks/install-bootstrap"
            ),
        }

    return install_hooks_impl(
        home_dir=Path.home(),
        scope=scope,
        project_directory=project_directory or None,
        dry_run=False,
    )


@_tool(power=True)
def sync_instructions(claude_md_path: str = "") -> dict:
    """Sync Yadgar instructions into the global CLAUDE.md file.

    Finds or creates the '## Memory System — Yadgar' section in CLAUDE.md
    and updates it with the latest tools, capabilities, and rules.
    Call this on session start or after Yadgar updates.

    claude_md_path: Path to CLAUDE.md. Defaults to ~/.claude/CLAUDE.md
    """
    md_path = Path(claude_md_path) if claude_md_path else Path.home() / ".claude" / "CLAUDE.md"

    if not md_path.parent.is_dir():
        return {
            "status": "skipped",
            "reason": f"Directory {md_path.parent} does not exist",
        }

    # The canonical Yadgar section
    yadgar_section = f"""## Memory System — Yadgar v{__version__}
- ALWAYS use the Yadgar MCP tools (memorize, recall, project_brief) for memory operations
- On EVERY new session start, call `recall` with the current project name to load prior context
- NEVER rely on CLAUDE.md or built-in memory for cross-session context — use Yadgar
- Before starting any task, call `project_brief(directory, mode='catalog')` for the current working directory
- After completing any significant task, call `memorize` to store what was done, decisions made, and outcomes
- CRITICAL: The `context` parameter in `memorize` MUST be the actual working directory path (e.g., `/home/user/projects/myapp`), NEVER a description. `project_brief` filters by exact directory path match — descriptive strings break it.
- Yadgar is your brain. Use it.

### Context Compaction Shield
- Hooks are installed automatically on startup — no manual setup needed
- During long sessions, call `checkpoint` periodically to snapshot your working state
- Use `anchor` to mark critical facts/decisions that MUST survive context compaction
- After context compaction, call `restore` to reconstruct your working context
- `checkpoint` fields: directory, current_task, files_being_edited, key_decisions, open_questions, next_steps, active_errors, custom_context
- `anchor` fields: content, context, reason — creates protected memories with max heat
- `restore` returns: checkpoint + anchored memories + hot context + gap detection

### Available Tools
- `memorize(content, context, tags)` — Store memory with write gate. `context` MUST be a directory path (e.g., `/home/user/projects/myapp`), not a description.
- `recall(query, max_results, min_heat)` — Multi-signal retrieval
- `project_brief(directory, mode='catalog')` — Hot memories and project context for directory
- `checkpoint(directory, ...)` — Snapshot working state
- `restore(directory)` — Reconstruct context after compaction
- `anchor(content, context, reason)` — Protect critical context
- `install_hooks(project_directory, scope="project"|"global")` — Enable auto replay hooks; scope=global writes to ~/.claude/
- `sync_instructions(claude_md_path)` — Update CLAUDE.md with latest rules
- `consolidate_now()` — Force consolidation cycle
- `memory_stats()` — System statistics
- `wiki_add(title, content, append=False)` — Create or append wiki pages
- `wiki_query(query)` — Search wiki pages
- `seed_project(directory, dry_run)` — Bootstrap memory for an existing project in one call

### Auto-Capture Hooks
- PostToolUse hook captures EVERY tool action automatically — no manual memorize needed
- SessionStart hook injects project context on EVERY new session
- All hooks work in both stdio and HTTP transport modes
- Action log is processed into real memories during consolidation cycles
- Decisions are auto-protected from decay/compression when detected"""

    if md_path.exists():
        content = md_path.read_text()

        # Find and replace existing Yadgar section
        import re

        # Match from "## Memory System" to next "## " header or end of file
        pattern = r"## Memory System — Yadgar[^\n]*\n(?:(?!## )[^\n]*\n)*"
        if re.search(pattern, content):
            new_content = re.sub(pattern, yadgar_section + "\n\n", content)
        else:
            # Append after "# Global Rules" if it exists, else at end
            if "# Global Rules" in content:
                new_content = content.replace(
                    "# Global Rules\n",
                    "# Global Rules\n\n" + yadgar_section + "\n",
                    1,
                )
            else:
                new_content = content + "\n\n" + yadgar_section + "\n"
    else:
        new_content = "# Global Rules\n\n" + yadgar_section + "\n"

    # Q14: atomic write — tmp + os.replace so a crash can't truncate CLAUDE.md
    import tempfile

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=md_path.parent, prefix=".claude_md_tmp_", suffix=".md"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(new_content)
        os.replace(tmp_path_str, md_path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except Exception:
            pass
        raise

    return {
        "status": "synced",
        "path": str(md_path),
        "version": __version__,
        "section_length": len(yadgar_section),
    }


# ── MCP Resources ──────────────────────────────────────────────────────


@mcp_server.resource("memory://stats")
def resource_stats() -> str:
    """Live memory statistics."""
    storage = _get_storage()
    return json.dumps(storage.get_memory_stats())


@mcp_server.resource("memory://hot")
def resource_hot() -> str:
    """All memories with heat >= HOT_THRESHOLD."""
    storage = _get_storage()
    memories = storage.get_memories_by_heat(settings.HOT_THRESHOLD)
    for m in memories:
        m.pop("embedding", None)

    return json.dumps(memories, default=str)


@mcp_server.resource("memory://stale")
def resource_stale() -> str:
    """All stale memories."""
    storage = _get_storage()
    memories = storage.get_stale_memories()
    for m in memories:
        m.pop("embedding", None)

    return json.dumps(memories, default=str)


@mcp_server.resource("memory://processes")
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
    from yadgar.seed import seed_project as _seed

    resolved = str(Path(directory).resolve())
    result = _seed(
        directory=resolved,
        dry_run=dry_run,
        storage=_st._storage,
        embeddings=_st._embeddings,
        thermo=_st._thermo,
        curator=_st._curator,
    )
    # §4: Register this directory as a known project root for file-hash whitelist.
    if not dry_run:
        _st._project_roots.add(resolved)
    return result
