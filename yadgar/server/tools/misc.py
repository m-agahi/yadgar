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
import shlex
from pathlib import Path

import yadgar.server._state as _st
from yadgar import __version__
from yadgar.config import get_settings
from yadgar.file_queue import is_draining
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

    return replay.create_checkpoint(
        directory=directory,
        current_task=current_task,
        files_being_edited=files_being_edited,
        key_decisions=key_decisions,
        open_questions=open_questions,
        next_steps=next_steps,
        active_errors=active_errors,
        custom_context=enriched_context,
    )


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


@_tool()
def anchor(content: str, context: str, reason: str = "") -> dict:
    """Mark critical context as compaction-resistant.

    Anchored memories get max heat, max importance, and is_protected=True.
    They are ALWAYS included in post-compaction restoration regardless
    of other scoring. Use for decisions, constraints, and critical facts
    that must survive compaction.
    """
    for _field in (content, context, reason):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}

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
            _get_file_queue().enqueue(
                "anchor",
                {"content": content, "context": context, "reason": reason, "branch": _branch},
            )
            return {"queued": True, "status": "anchored", "is_protected": True, "reason": reason}
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync: %s", _fq_exc)

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    replay = _get_replay()
    tags = ["_anchor"]
    if reason:
        tags.append(f"anchor:{reason}")
    memory_id = replay.anchor_memory(content, context, tags, reason, branch=_branch)
    return {
        "memory_id": memory_id,
        "status": "anchored",
        "is_protected": True,
        "reason": reason,
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
    import shutil

    if scope not in ("project", "global"):
        return {
            "status": "error",
            "reason": f"Invalid scope '{scope}': must be 'project' or 'global'",
        }

    project_dir = Path(project_directory) if project_directory else Path.cwd()

    # Global paths (Stop hook always here; all hooks go here when scope=global)
    global_claude_dir = Path.home() / ".claude"
    global_hooks_dir = global_claude_dir / "hooks"
    global_hooks_dir.mkdir(parents=True, exist_ok=True)

    # Determine where hook scripts and settings are written based on scope
    if scope == "global":
        hooks_dir = global_hooks_dir
        settings_target_dir = global_claude_dir
    else:
        claude_dir = project_dir / ".claude"
        hooks_dir = claude_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        settings_target_dir = claude_dir

    # Copy hook scripts from package
    package_hooks = Path(__file__).parent.parent.parent / "hooks"

    hook_files = {
        "pre-compact-drain.sh": 0o755,
        "post-compact-rehydrate.sh": 0o755,
        "post-tool-capture.py": 0o755,
        "session-start-context.py": 0o755,
        "prompt-recall.py": 0o755,
    }

    for filename, mode in hook_files.items():
        src = package_hooks / filename
        dst = hooks_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            dst.chmod(mode)

    # Stop hook — always installed globally so it fires in every session
    stop_hook_src = package_hooks / "stop-memory-checkpoint.py"
    stop_hook_dst = global_hooks_dir / "yadgar-stop-memory-checkpoint.py"
    if stop_hook_src.exists():
        shutil.copy2(stop_hook_src, stop_hook_dst)
        stop_hook_dst.chmod(0o755)

    # hook_runner.py — the real script that all hooks delegate to.
    # Installed at an absolute path; project_directory passed as argv[1]
    # so no shell interpolation occurs.
    hook_runner_src = Path(__file__).parent.parent.parent / "scripts" / "hook_runner.py"
    hook_runner_dst = hooks_dir / "hook_runner.py"
    if hook_runner_src.exists():
        shutil.copy2(hook_runner_src, hook_runner_dst)
        hook_runner_dst.chmod(0o755)

    # Absolute path string — safe to embed in JSON command field because
    # it is passed as argv[0] to execve, not shell-interpolated.
    _runner = str(hook_runner_dst)

    # Auth env block: if YADGAR_MCP_AUTH_TOKEN is set, inject it into every hook
    # so hook scripts can authenticate to the daemon.
    _auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    _env_block: dict = {}
    if _auth_token:
        _env_block = {"YADGAR_MCP_AUTH_TOKEN": _auth_token}

    def _hook_entry(hook_type: str, matcher: str = "") -> dict:
        """Build a hook config entry using hook_runner.py."""
        entry: dict = {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {shlex.quote(_runner)} {hook_type}",
                }
            ],
        }
        if _env_block:
            entry["hooks"][0]["env"] = _env_block
        return entry

    # Write hooks configuration to the target settings file
    settings_path = settings_target_dir / "settings.json"
    settings_data: dict = {}
    if settings_path.exists():
        try:
            settings_data = json.loads(settings_path.read_text())
        except Exception:
            settings_data = {}

    hooks_config = settings_data.get("hooks", {})

    # PreCompact hook — drain context before compaction
    hooks_config["PreCompact"] = [_hook_entry("pre-compact-drain")]

    # SessionStart hooks — context on every session + full restore on compact
    hooks_config["SessionStart"] = [
        _hook_entry("session-start-context"),
        _hook_entry("post-compact-rehydrate", matcher="compact"),
    ]

    # PostToolUse hook — capture every tool action into action_log
    hooks_config["PostToolUse"] = [_hook_entry("post-tool-capture")]

    # UserPromptSubmit hook — auto-recall relevant memories on every user turn
    hooks_config["UserPromptSubmit"] = [_hook_entry("prompt-recall")]

    # PreToolUse hook — block direct docker exec into yadgar containers
    hooks_config["PreToolUse"] = [_hook_entry("db-lockdown-check", matcher="Bash")]

    settings_data["hooks"] = hooks_config
    # Atomic write: write to tmp file, then os.replace to avoid corrupt settings.json
    import tempfile

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=settings_target_dir, prefix=".settings_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(json.dumps(settings_data, indent=2))
        os.replace(tmp_path_str, settings_path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except Exception:
            pass
        raise

    # Register Stop hook in global ~/.claude/settings.json
    # (always global, regardless of scope — Stop must fire in every session)
    global_settings_path = global_claude_dir / "settings.json"
    global_settings: dict = {}
    if global_settings_path.exists():
        try:
            global_settings = json.loads(global_settings_path.read_text())
        except Exception:
            global_settings = {}

    global_hooks = global_settings.get("hooks", {})
    global_hooks["Stop"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python3 "{stop_hook_dst}"',
                }
            ],
        }
    ]
    global_settings["hooks"] = global_hooks
    # Atomic write for global settings too
    import tempfile

    tmp_fd2, tmp_path_str2 = tempfile.mkstemp(
        dir=global_claude_dir, prefix=".global_settings_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd2, "w") as f:
            f.write(json.dumps(global_settings, indent=2))
        os.replace(tmp_path_str2, global_settings_path)
    except Exception:
        try:
            os.unlink(tmp_path_str2)
        except Exception:
            pass
        raise

    return {
        "status": "installed",
        "scope": scope,
        "project_directory": str(project_dir),
        "hooks_directory": str(hooks_dir),
        "hooks_installed": [
            "PreCompact (drain)",
            "SessionStart (context)",
            "SessionStart (compact restore)",
            "PostToolUse (auto-capture)",
            "UserPromptSubmit (auto-recall)",
            "PreToolUse (DB lockdown)",
            "Stop (memory checkpoint — global)",
        ],
        "settings_file": str(settings_path),
        "global_settings_file": str(global_settings_path),
    }


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
- ALWAYS use the Yadgar MCP tools (memorize, recall, get_project_context) for memory operations
- On EVERY new session start, call `recall` with the current project name to load prior context
- NEVER rely on CLAUDE.md or built-in memory for cross-session context — use Yadgar
- Before starting any task, call `get_project_context` for the current working directory
- After completing any significant task, call `memorize` to store what was done, decisions made, and outcomes
- CRITICAL: The `context` parameter in `memorize` MUST be the actual working directory path (e.g., `/home/user/projects/myapp`), NEVER a description. `get_project_context` filters by exact directory path match — descriptive strings break it.
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
- `get_project_context(directory)` — Hot memories for directory
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
