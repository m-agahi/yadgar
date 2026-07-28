"""settings.json hook-entry assembly for install_hooks (Car C5 split).

Builds and merges the Claude Code hook-entry structures written into
settings.json — the core (replace-always) hooks, the append-if-absent hooks,
the global Stop/SessionEnd entries — plus scope-path resolution, the auth env
block, settings load, and the atomic write.

Imports interpreter resolution from ``_interpreter`` and script copy from
``_hook_scripts``; the canonical ``install_hooks_lib`` re-exports this surface
and threads ``install_hooks_impl`` on top.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import tempfile
from pathlib import Path

from yadgar._shared.observability.observe import observe

from ._hook_scripts import _copy_hook
from ._interpreter import _stable_python

logger = logging.getLogger(__name__)

# BUG C — known test-fixture auth tokens that must never reach settings.json.
# (`a-valid-32-char-token-here!!` lives in tests/server/test_security_headers.py.)
_TEST_FIXTURE_TOKENS = frozenset({"a-valid-32-char-token-here!!"})


# ── Container detection ────────────────────────────────────────────────────


@observe(tier="hot")
def is_running_in_container() -> bool:
    """True iff YADGAR_IN_CONTAINER=1 is set.

    Explicit opt-in avoids false positives in CI/runner environments that
    happen to have /.dockerenv present (e.g. Forgejo Actions container
    images).  Only the yadgar core service sets this env var at launch.
    """
    return os.environ.get("YADGAR_IN_CONTAINER", "") == "1"


# ── Hook-entry builders ────────────────────────────────────────────────────


@observe(tier="hot")
def _make_hook_entry(cmd: str, matcher: str, env_block: dict, async_: bool = False) -> dict:
    """Build a single hook entry dict.

    When *async_* is True, sets ``entry["hooks"][0]["async"] = True`` — the
    Claude Code fire-and-forget flag (non-blocking, harness does not wait for
    the hook).  When False (default), the ``"async"`` key is omitted entirely
    so blocking hooks carry no spurious field.
    """
    entry: dict = {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": cmd}],
    }
    if env_block:
        entry["hooks"][0]["env"] = env_block
    if async_:
        entry["hooks"][0]["async"] = True
    return entry


@observe(tier="stage")
def _entry_command(entry: object) -> str:
    """Return the first hook command string of an entry, or ''."""
    if not isinstance(entry, dict):
        return ""
    hooks = entry.get("hooks")
    if not hooks or not isinstance(hooks[0], dict):
        return ""
    return hooks[0].get("command", "")


@observe(tier="stage")
def _append_if_absent(
    hooks_config: dict,
    event: str,
    cmd: str,
    env_block: dict,
    matcher: str = "",
    managed_basename: str | None = None,
) -> None:
    """Register a hook entry under *event*, keyed on the managed script basename.

    BUG A fix — dedup + migration-sweep. When *managed_basename* is given, the
    identity of a yadgar-managed entry is the presence of that basename in its
    command string (NOT the full command). This survives interpreter drift: a
    prior install that baked ``python3 …/hook.py`` and a new one that resolves
    ``/venv/bin/python3 …/hook.py`` are the SAME managed hook.

    Migration-sweep: every pre-existing entry whose command contains
    *managed_basename* is stripped, then the single fresh entry is appended —
    collapsing accumulated dupes and refreshing a stale interpreter. Foreign
    hooks (including a ``yadgar-``-substring path that is NOT this basename) are
    never touched: the strip predicate is the exact basename only.

    When *managed_basename* is None the legacy full-command dedup is used
    (direct callers with no managed identity).
    """
    existing = hooks_config.get(event, [])
    if managed_basename is not None:
        existing = [entry for entry in existing if managed_basename not in _entry_command(entry)]
        existing.append(_make_hook_entry(cmd, matcher, env_block))
    else:
        already = any(_entry_command(entry) == cmd for entry in existing)
        if not already:
            existing.append(_make_hook_entry(cmd, matcher, env_block))
    hooks_config[event] = existing


@observe(tier="stage")
def _replace_managed_entries(
    hooks_config: dict,
    event: str,
    managed_basename: str,
    entries: list[dict],
) -> None:
    """Foreign-preserving replace of yadgar's own entries under *event*.

    ADR-0161: the core hook events used to be hard-replaced
    (``hooks_config[event] = [...]``), which silently discarded ANY pre-existing
    entry — including a foreign hook another tool wrote under the same key (e.g.
    nix's ``plugins/cache/caveman`` SessionStart hook). This strips ONLY the
    yadgar-managed entries (identity = *managed_basename* substring in the
    command, mirroring ``_append_if_absent``'s script-basename identity — drift
    resilient across interpreter changes) and appends the fresh *entries*,
    leaving every foreign entry in place.

    Handles MULTIPLE yadgar entries under one key (e.g. SessionStart's
    context + rehydrate pair): every stale yadgar entry shares the same managed
    basename, so one strip predicate collapses them and the full fresh list is
    re-appended. Idempotent: a second install strips the entries this one added
    (same basename) and re-appends them — no duplication, foreign survives.
    """
    existing = hooks_config.get(event, [])
    existing = [entry for entry in existing if managed_basename not in _entry_command(entry)]
    existing.extend(entries)
    hooks_config[event] = existing


@observe(tier="stage")
def _install_global_scripts(
    package_hooks: Path,
    global_hooks_dir: Path,
    dry_run: bool,
    shebang_python: str | None = None,
) -> tuple[Path, Path, Path]:
    """Copy always-global hook scripts; return (stop_dst, session_end_dst, router_dst)."""
    stop_dst = global_hooks_dir / "yadgar-stop-memory-checkpoint.py"
    _copy_hook(package_hooks / "stop-memory-checkpoint.py", stop_dst, dry_run, shebang_python)

    session_end_dst = global_hooks_dir / "yadgar-session-end-capture.py"
    _copy_hook(package_hooks / "session-end-capture.py", session_end_dst, dry_run, shebang_python)

    # HOOKS train Car 1: standalone PreToolUse router-guard — subsumes the old
    # db-lockdown-check.py and adds the git-commit-bypass / terraform-family /
    # git-push-to-default guards. Not routed through hook_runner.py (keeps the
    # deny path dependency-free + crash-isolated), same install-path class.
    router_dst = global_hooks_dir / "yadgar-pretooluse-router.py"
    _copy_hook(package_hooks / "pretooluse-router.py", router_dst, dry_run, shebang_python)

    if not dry_run:
        # Seed the exceptions config create-if-absent — NEVER clobber (a
        # reinstall must preserve user-added push_default_allowlist entries).
        exceptions_dst = global_hooks_dir.parent / "yadgar-hook-exceptions.json"
        if not exceptions_dst.exists():
            exceptions_dst.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "push_default_allowlist": ["nix", "ledger", "ostad"],
                        "disabled_guards": [],
                    },
                    indent=2,
                )
                + "\n"
            )
        # #64: the db-lockdown orphan unlink + the non-prefixed vestige sweep now
        # live in the single `_sweep_stale_hook_scripts` helper, called from
        # `install_hooks_impl` after the global scripts are copied.

    return stop_dst, session_end_dst, router_dst


@observe(tier="stage")
def _build_core_hooks(
    hooks_config: dict,
    runner: str,
    env_block: dict,
    router_dst: Path,
    python: str | None = None,
) -> None:
    """Register the five core hook events, foreign-preserving (ADR-0161).

    The five events (PreCompact, SessionStart, PostToolUse, UserPromptSubmit,
    PreToolUse) used to be hard-replaced (``hooks_config[event] = [...]``), which
    discarded any foreign entry a different tool had written under the same key.
    This now strips ONLY yadgar's own entries per event (via
    ``_replace_managed_entries``, keyed on the managed script basename) and
    appends the fresh yadgar entries, so foreign hooks (e.g. nix's
    ``plugins/cache/caveman`` SessionStart hook) survive. Idempotent across
    reinstalls.
    """

    # Pin the python interpreter to a DURABLE path — settings.json carries
    # literal command strings, so `python3` would resolve on Claude Code's
    # PATH at hook-fire time (often a system python without yadgar
    # importable, e.g. on NixOS). *python* threads the once-resolved
    # durable interpreter from install_hooks_impl; None falls back to
    # `_stable_python()` for direct callers.
    _python = shlex.quote(python or _stable_python())

    def _runner_entry(hook_type: str, matcher: str = "", async_: bool = False) -> dict:
        cmd = f"{_python} {shlex.quote(runner)} {hook_type}"
        return _make_hook_entry(cmd, matcher, env_block, async_=async_)

    # yadgar-managed identity for the 4 runner-dispatched events is the runner
    # script basename (e.g. hook_runner.py) — present in every runner command,
    # so one strip predicate collapses all yadgar entries under a key (including
    # SessionStart / PostToolUse's two-entry pairs) while foreign entries, which
    # never contain it, are preserved.
    _runner_basename = Path(runner).name

    _replace_managed_entries(
        hooks_config,
        "PreCompact",
        _runner_basename,
        [_runner_entry("pre-compact-drain", async_=True)],
    )
    _replace_managed_entries(
        hooks_config,
        "SessionStart",
        _runner_basename,
        [
            _runner_entry("session-start-context"),
            _runner_entry("post-compact-rehydrate", matcher="compact"),
        ],
    )
    # PostToolUse: two entries — (1) generic capture, (2) block-reflect on block_* writes.
    # block-reflect matcher: any of the five block write tools (v5.35.1).
    _block_reflect_matcher = "mcp__yadgar__block_(create|update|delete|replace|append)"
    _replace_managed_entries(
        hooks_config,
        "PostToolUse",
        _runner_basename,
        [
            _runner_entry("post-tool-capture"),
            _runner_entry("block-reflect", matcher=_block_reflect_matcher),
        ],
    )
    _replace_managed_entries(
        hooks_config,
        "UserPromptSubmit",
        _runner_basename,
        [_runner_entry("prompt-recall")],
    )

    # HOOKS train Car 1: direct-command entry (router-guard) so hookEventName is
    # always emitted. Matcher stays "Bash" — all four guards are Bash-string guards.
    # yadgar-managed identity here is the router script basename (NOT the runner),
    # since PreToolUse dispatches the standalone router, not hook_runner.py.
    router_cmd = f"{_python} {shlex.quote(str(router_dst))}"
    _replace_managed_entries(
        hooks_config,
        "PreToolUse",
        router_dst.name,
        [_make_hook_entry(router_cmd, "Bash", env_block)],
    )


@observe(tier="stage")
def _install_append_hooks(
    package_hooks: Path,
    hooks_dir: Path,
    hooks_config: dict,
    env_block: dict,
    dry_run: bool,
    python: str | None = None,
) -> None:
    """Install and register the append-if-absent hook scripts."""
    # ADR-0156: the SubagentStop append hook was removed with the auto-store path
    # (the legacy subagent-stop.py script is gone). Subagent findings are now
    # curated via the Stop-hook checkpoint prompt, not a SubagentStop endpoint POST.
    _append_specs = [
        ("instructions-loaded.py", "yadgar-instructions-loaded.py", "InstructionsLoaded", ""),
        ("subagent-start.py", "yadgar-subagent-start.py", "SubagentStart", ""),
        ("file-changed.py", "yadgar-file-changed.py", "FileChanged", ""),
    ]
    _resolved = python or _stable_python()
    _python = shlex.quote(_resolved)
    for src_name, dst_name, event, matcher in _append_specs:
        dst = hooks_dir / dst_name
        _copy_hook(package_hooks / src_name, dst, dry_run, _resolved)
        _append_if_absent(
            hooks_config,
            event,
            f"{_python} {shlex.quote(str(dst))}",
            env_block,
            matcher,
            managed_basename=dst_name,
        )


@observe(tier="stage")
def _write_global_stop_hooks(
    global_claude_dir: Path,
    stop_entry: list,
    session_end_entry: list,
) -> None:
    """Merge Stop + SessionEnd into the global settings.json (scope=project path)."""
    global_settings_path = global_claude_dir / "settings.json"
    global_settings: dict = {}
    if global_settings_path.exists():
        try:
            global_settings = json.loads(global_settings_path.read_text())
        except Exception:
            global_settings = {}
    global_hooks = global_settings.get("hooks", {})
    global_hooks["Stop"] = stop_entry
    global_hooks["SessionEnd"] = session_end_entry
    global_settings["hooks"] = global_hooks
    _atomic_write(global_claude_dir, global_settings_path, global_settings)


@observe(tier="stage")
def _resolve_scope_paths(
    home_dir: Path,
    scope: str,
    project_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    """Return (global_claude_dir, global_hooks_dir, hooks_dir, settings_target_dir)."""
    global_claude_dir = home_dir / ".claude"
    global_hooks_dir = global_claude_dir / "hooks"
    if scope == "global":
        return global_claude_dir, global_hooks_dir, global_hooks_dir, global_claude_dir
    claude_dir = project_dir / ".claude"
    return global_claude_dir, global_hooks_dir, claude_dir / "hooks", claude_dir


@observe(tier="stage")
def _load_settings(settings_path: Path) -> dict:
    """Read existing settings.json; return empty dict on missing or parse error."""
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text())
    except Exception:
        return {}


@observe(tier="stage")
def _resolve_env_block() -> dict:
    """Return the hook-entry ``env`` block (BUG B/C fix).

    BUG B (omit-env default): do NOT bake the token value into settings.json.
    A literal token is a secret at rest and silently overrides the ambient env
    after rotation. Hooks inherit the ambient ``YADGAR_MCP_AUTH_TOKEN`` at fire
    time — the same env the daemon authenticates from — so an empty env block is
    provably correct. (``${VAR}`` indirection is the documented alternative,
    gated on a live Claude-Code hook-``env`` interpolation probe; unverified, so
    not adopted here.)

    BUG C (test-fixture guard): the token is still read to warn on a known test
    fixture, but no value — literal or otherwise — is written.
    """
    auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    if auth_token in _TEST_FIXTURE_TOKENS:
        logger.warning(
            "install_hooks: refusing to use a known test-fixture auth token "
            "(check YADGAR_MCP_AUTH_TOKEN); not baking any token value into settings.json"
        )
    return {}


@observe(tier="stage")
def _atomic_write(directory: Path, target: Path, data: dict) -> None:
    """Write *data* as JSON to *target* atomically via a temp file."""
    directory.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=directory, prefix=".settings_tmp_", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp_path_str, target)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except Exception:
            pass
        raise
