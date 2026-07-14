"""Shared install_hooks implementation.

Both the MCP tool (yadgar/server/tools/misc.py) and the CLI subcommand
(yadgar/cli/install_hooks.py) call install_hooks_impl() here.

Container detection:
  is_running_in_container() returns True only when YADGAR_IN_CONTAINER=1
  is set in the environment.  Explicit opt-in avoids false positives in CI
  runners (e.g. Forgejo Actions) that have /.dockerenv present but are NOT
  the yadgar core service container.

  To enable container-mode refusal, callers must set:
    YADGAR_IN_CONTAINER=1
  in the environment before launching the process.  The nix module and
  docker-compose config set this on the yadgar core service ExecStart.
  CI pipelines must NOT set it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

_WORKTREE_MARKER = f"{os.sep}.claude{os.sep}worktrees{os.sep}"

# BUG C — known test-fixture auth tokens that must never reach settings.json.
# (`a-valid-32-char-token-here!!` lives in tests/server/test_security_headers.py.)
_TEST_FIXTURE_TOKENS = frozenset({"a-valid-32-char-token-here!!"})


@observe(tier="stage")
def _is_git_worktree_path(path: str) -> bool:
    """True when *path* sits inside a LINKED git worktree.

    Detection: ``git rev-parse --git-dir --git-common-dir`` from the path's
    directory — the two differ only inside a linked worktree. Missing
    directory, no git, or not-a-repo all return False (cannot prove
    ephemerality → don't claim it).
    """
    probe_dir = os.path.dirname(path)
    if not os.path.isdir(probe_dir):
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", probe_dir, "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        return False
    if proc.returncode != 0:
        return False
    lines = proc.stdout.splitlines()
    if len(lines) != 2:
        return False
    git_dir = os.path.realpath(os.path.join(probe_dir, lines[0]))
    common_dir = os.path.realpath(os.path.join(probe_dir, lines[1]))
    return git_dir != common_dir


@observe(tier="stage")
def _is_durable_interpreter(exe: str) -> bool:
    """True when *exe* is safe to bake into persistent settings/shebangs.

    NON-durable (task #38, 3rd occurrence of hook-interpreter poisoning):
    - any path under ``.claude/worktrees/`` (EnterWorktree agent worktrees),
    - any path under the system temp dir (``/tmp/<worktree>/.venv`` agents),
    - any path inside a linked git worktree elsewhere.

    The RAW path string is what gets baked into settings — symlink targets are
    deliberately NOT resolved (a /tmp symlink to a durable python is still a
    doomed registration once the /tmp dir is cleaned).
    """
    if not os.path.isabs(exe):
        return True  # PATH-resolved name — never a doomed absolute path
    if _WORKTREE_MARKER in exe:
        return False
    tmp_roots = {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir()), f"{os.sep}tmp"}
    if any(exe.startswith(root + os.sep) for root in tmp_roots):
        return False
    return not _is_git_worktree_path(exe)


@observe(tier="stage")
def _existing_registration_ok(existing: str | None) -> str | None:
    """Return *existing* when it still resolves to a real, durable interpreter.

    Durability is required on top of mere existence: a still-alive worktree/tmp
    python that a PREVIOUS session poisoned into settings would otherwise be
    kept forever — this is exactly the state the fix must heal.
    """
    if not existing:
        return None
    if not _is_durable_interpreter(existing):
        return None
    if os.path.isabs(existing):
        return existing if os.path.exists(existing) else None
    return existing if shutil.which(existing) else None


@observe(tier="stage")
def _pipx_python(home_dir: Path) -> str | None:
    """Return the pipx-venv yadgar python if installed (durable by contract)."""
    bin_dir = home_dir / ".local" / "pipx" / "venvs" / "yadgar" / "bin"
    for name in ("python", "python3"):
        candidate = bin_dir / name
        if candidate.exists():
            return str(candidate)
    return None


@observe(tier="stage")
def _main_repo_root(path: str) -> str | None:
    """Return the MAIN checkout root for *path* via ``--git-common-dir``."""
    probe_dir = os.path.dirname(path)
    if not os.path.isdir(probe_dir):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", probe_dir, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        return None
    if proc.returncode != 0:
        return None
    common_dir = os.path.realpath(os.path.join(probe_dir, proc.stdout.strip()))
    if os.path.basename(common_dir) != ".git":
        return None  # bare/odd layout — no canonical venv derivable
    return os.path.dirname(common_dir)


@observe(tier="stage")
def _canonical_repo_python(exe: str) -> str | None:
    """Derive ``<main-repo>/.venv/bin/python3`` from a worktree interpreter."""
    if _WORKTREE_MARKER in exe:
        repo_root: str | None = exe.split(_WORKTREE_MARKER, 1)[0]
    else:
        repo_root = _main_repo_root(exe)
    if not repo_root:
        return None
    candidate = os.path.join(repo_root, ".venv", "bin", "python3")
    return candidate if os.path.exists(candidate) else None


@observe(tier="stage")
def _stable_python(existing: str | None = None, home_dir: Path | None = None) -> str:
    """Return a DURABLE interpreter path safe to bake into persistent settings.

    ``sys.executable`` inside an agent git worktree (``.claude/worktrees/…`` or
    ``/tmp/<worktree>/.venv/bin/python``) is EPHEMERAL: when the worktree is
    removed, every hook command / shebang pinned to it breaks with "No such
    file or directory" (observed corrupting the global Stop/SessionEnd hooks —
    3rd user-facing occurrence, task #38).

    Non-durable interpreter → substitute, in order:
      (a) the interpreter already registered in the existing settings, IF it
          still exists on disk AND is itself durable (heals prior poisoning);
      (b) the pipx venv (``~/.local/pipx/venvs/yadgar/bin/python``);
      (c) the canonical repo venv (``<main-repo>/.venv/bin/python3``);
      (d) the existing registration UNCHANGED (warn) — never write a NEW
          doomed path over whatever is already there;
      (e) PATH ``python3`` as last resort when nothing was registered.
    """
    exe = sys.executable
    if _is_durable_interpreter(exe):
        return exe
    home = home_dir if home_dir is not None else Path.home()
    substitute = (
        _existing_registration_ok(existing) or _pipx_python(home) or _canonical_repo_python(exe)
    )
    if substitute:
        return substitute
    if existing:
        logger.warning(
            "install_hooks: no durable python found; keeping existing registration %r "
            "instead of baking non-durable %r",
            existing,
            exe,
        )
        return existing
    logger.warning(
        "install_hooks: no durable python found for non-durable %r; falling back to PATH 'python3'",
        exe,
    )
    return "python3"


@observe(tier="stage")
def _registered_python(settings: dict) -> str | None:
    """Extract the interpreter pinned in existing yadgar hook commands, if any."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            interpreter = _entry_interpreter(entry)
            if interpreter:
                return interpreter
    return None


@observe(tier="stage")
def _entry_interpreter(entry: object) -> str | None:
    """Return the leading python token of a yadgar-managed hook command."""
    if not isinstance(entry, dict):
        return None
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "") if isinstance(hook, dict) else ""
        if "hook_runner.py" not in cmd and "yadgar-" not in cmd:
            continue
        try:
            parts = shlex.split(cmd)
        except ValueError:
            continue
        if len(parts) >= 2 and "python" in os.path.basename(parts[0]):
            return parts[0]
    return None


@observe(tier="stage")
def _resolve_python_shebang(python: str | None = None) -> str:
    """Return the shebang line to pin yadgar-bundled hooks at install time.

    Hooks that `import yadgar.paths` (stop-memory-checkpoint, session-end-capture,
    post-tool-capture, prompt-recall) need a Python that has yadgar on its path.
    `#!/usr/bin/env python3` resolves to whichever python3 is first on PATH,
    which on many systems (notably NixOS with a pipx-installed yadgar) is a
    system python that does NOT have yadgar importable.

    Strategy: at copy time, pin the shebang to the durable interpreter resolved
    by `_stable_python()` (callers thread the once-resolved value in). A
    PATH-relative fallback (e.g. ``python3``) uses the `/usr/bin/env` form —
    a relative absolute-shebang is invalid.

    Returns the literal shebang line including the leading `#!` and trailing
    newline.
    """
    resolved = python or _stable_python()
    if not os.path.isabs(resolved):
        return f"#!/usr/bin/env {resolved}\n"
    return f"#!{resolved}\n"


# ── Container detection ────────────────────────────────────────────────────


@observe(tier="hot")
def is_running_in_container() -> bool:
    """True iff YADGAR_IN_CONTAINER=1 is set.

    Explicit opt-in avoids false positives in CI/runner environments that
    happen to have /.dockerenv present (e.g. Forgejo Actions container
    images).  Only the yadgar core service sets this env var at launch.
    """
    return os.environ.get("YADGAR_IN_CONTAINER", "") == "1"


# ── Internal helpers ───────────────────────────────────────────────────────


@observe(tier="stage")
def _copy_hook(src: Path, dst: Path, dry_run: bool, shebang_python: str | None = None) -> None:
    """Copy a hook script, rewrite its shebang to a durable python,
    and mark it executable. No-op on dry_run.

    Shebang rewrite: any `#!/usr/bin/env python3` (or `#!/usr/bin/env python`)
    first line is replaced with `#!<durable python>` so yadgar-bundled hooks
    that `import yadgar.paths` find a python that has yadgar on its path.
    Other shebang forms are preserved. *shebang_python* threads the
    once-resolved durable interpreter; None falls back to `_stable_python()`.
    """
    if dry_run:
        return
    if not src.exists():
        return
    text = src.read_text()
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#!") and "python" in lines[0]:
        first = lines[0].strip()
        if first in ("#!/usr/bin/env python3", "#!/usr/bin/env python"):
            lines[0] = _resolve_python_shebang(shebang_python)
            text = "".join(lines)
    dst.write_text(text)
    dst.chmod(0o755)


@observe(tier="hot")
def _make_hook_entry(cmd: str, matcher: str, env_block: dict) -> dict:
    """Build a single hook entry dict."""
    entry: dict = {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": cmd}],
    }
    if env_block:
        entry["hooks"][0]["env"] = env_block
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
    """Populate the four core (replace-always) hook event entries."""

    # Pin the python interpreter to a DURABLE path — settings.json carries
    # literal command strings, so `python3` would resolve on Claude Code's
    # PATH at hook-fire time (often a system python without yadgar
    # importable, e.g. on NixOS). *python* threads the once-resolved
    # durable interpreter from install_hooks_impl; None falls back to
    # `_stable_python()` for direct callers.
    _python = shlex.quote(python or _stable_python())

    def _runner_entry(hook_type: str, matcher: str = "") -> dict:
        cmd = f"{_python} {shlex.quote(runner)} {hook_type}"
        return _make_hook_entry(cmd, matcher, env_block)

    hooks_config["PreCompact"] = [_runner_entry("pre-compact-drain")]
    hooks_config["SessionStart"] = [
        _runner_entry("session-start-context"),
        _runner_entry("post-compact-rehydrate", matcher="compact"),
    ]
    # PostToolUse: two entries — (1) generic capture, (2) block-reflect on block_* writes.
    # block-reflect matcher: any of the five block write tools (v5.35.1).
    _block_reflect_matcher = "mcp__yadgar__block_(create|update|delete|replace|append)"
    hooks_config["PostToolUse"] = [
        _runner_entry("post-tool-capture"),
        _runner_entry("block-reflect", matcher=_block_reflect_matcher),
    ]
    hooks_config["UserPromptSubmit"] = [_runner_entry("prompt-recall")]

    # HOOKS train Car 1: direct-command entry (router-guard) so hookEventName is
    # always emitted. Matcher stays "Bash" — all four guards are Bash-string guards.
    router_cmd = f"{_python} {shlex.quote(str(router_dst))}"
    hooks_config["PreToolUse"] = [_make_hook_entry(router_cmd, "Bash", env_block)]


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
    _append_specs = [
        ("subagent-stop.py", "yadgar-subagent-stop.py", "SubagentStop", ""),
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


# The 9 non-prefixed hook basenames that PRE-#64 installs copied verbatim into
# hooks_dir via the old ``_copy_scope_scripts._files`` dict. Nothing dispatches
# to these on disk — the 5 runner-dispatched ones (post-tool-capture,
# session-start-context, prompt-recall, pre-compact-drain, post-compact-rehydrate)
# are executed via ``hook_runner.py <type>``'s internal ``_HOOKS`` dict, and the
# 4 append hooks (subagent-{start,stop}, instructions-loaded, file-changed) are
# installed under ``yadgar-`` names by ``_install_append_hooks``. The non-prefixed
# copies are pure vestige. #64 stops emitting them AND sweeps existing orphans.
_MANAGED_NONPREFIXED: frozenset[str] = frozenset(
    {
        "pre-compact-drain.sh",
        "post-compact-rehydrate.sh",
        "post-tool-capture.py",
        "session-start-context.py",
        "prompt-recall.py",
        "subagent-stop.py",
        "instructions-loaded.py",
        "subagent-start.py",
        "file-changed.py",
    }
)


@observe(tier="stage")
def _is_nix_symlink(path: Path) -> bool:
    """True when *path* is a symlink whose target lives in the nix store.

    Per-file provenance signal (NOT the system-level ``is_nix_managed()``, which
    returns True on any NixOS box and would make the sweep a no-op on the very
    machine that has the orphans). Uses ``os.readlink`` string-compare so a
    DANGLING nix symlink is detected without a real ``/nix/store`` present.
    """
    try:
        if not path.is_symlink():
            return False
        return os.readlink(path).startswith("/nix/store")
    except OSError:
        return False


@observe(tier="stage")
def _sha256_file(path: Path) -> str | None:
    """Return the hex sha256 of *path*'s bytes, or None on any read error."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@observe(tier="stage")
def _sweep_stale_hook_scripts(
    package_hooks: Path,
    global_hooks_dir: Path,
    dry_run: bool,
) -> None:
    """Remove yadgar-installed orphan hook scripts from *global_hooks_dir*.

    Two classes of orphan:

    1. Non-prefixed vestigial copies (``post-tool-capture.py`` et al.) that
       pre-#64 global installs wrote and nothing dispatches to. Predicate is
       **content-hash equality against the packaged source** — the only signal
       that works for the 5 runner-dispatched names (which have NO ``yadgar-``
       on-disk sibling) AND preserves a user's coincidentally-named file (its
       bytes differ → survives). A nix-store SYMLINK is skipped (deleting it
       would fight home-manager).

    2. The ``yadgar-db-lockdown-check.py`` orphan, superseded by the PreToolUse
       router — an unconditional unlink (a ``yadgar-`` name, always ours;
       settings.json no longer references it).

    Best-effort: any OSError per unlink is swallowed — a missing file or a perms
    error must never fail an install. No-op on dry_run.
    """
    if dry_run:
        return
    for name in _MANAGED_NONPREFIXED:
        candidate = global_hooks_dir / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if _is_nix_symlink(candidate):
            continue  # nix-deployed — never touch (would fight home-manager)
        packaged = package_hooks / name
        on_disk_hash = _sha256_file(candidate)
        packaged_hash = _sha256_file(packaged)
        if on_disk_hash is None or packaged_hash is None:
            continue  # cannot prove provenance → conservative, leave it
        if on_disk_hash != packaged_hash:
            continue  # user's own file (different content) → survives
        try:
            candidate.unlink()
        except OSError:
            pass
    # db-lockdown orphan — unconditional (yadgar- name, router subsumed it).
    orphan = global_hooks_dir / "yadgar-db-lockdown-check.py"
    try:
        orphan.unlink()
    except OSError:
        pass


# NOTE (#64): the old ``_copy_scope_scripts`` helper — which bulk-copied the 9
# non-prefixed hook basenames into hooks_dir — was REMOVED. Nothing dispatched to
# those copies (hook_runner uses its internal ``_HOOKS`` dict; the append hooks
# are installed under ``yadgar-`` names), so they were pure vestige that a global
# install duplicated alongside the managed set. Root cause of the orphan dupes.
# Existing orphans are cleaned by ``_sweep_stale_hook_scripts``.


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


# ── Shared install logic ───────────────────────────────────────────────────


@observe(tier="boundary")
def install_hooks_impl(
    home_dir: Path,
    scope: str,
    project_directory: str | None,
    dry_run: bool = False,
) -> dict:
    """Write Claude Code hook config to the appropriate settings.json.

    Parameters
    ----------
    home_dir:
        The user's home directory.  Callers pass ``Path.home()`` for real
        usage or a temp dir in tests.
    scope:
        ``"project"`` — write to <project_directory>/.claude/settings.json
        ``"global"``  — write to ~/.claude/settings.json
    project_directory:
        Project root string.  ``None`` / empty → ``Path.cwd()``.
    dry_run:
        When True, compute but do NOT write changes; return the would-be
        settings dict under ``"preview"`` and print a compact preview to
        stdout.
    """
    if scope not in ("project", "global"):
        return {
            "status": "error",
            "reason": f"Invalid scope '{scope}': must be 'project' or 'global'",
        }

    project_dir = Path(project_directory) if project_directory else Path.cwd()
    global_claude_dir, global_hooks_dir, hooks_dir, settings_target_dir = _resolve_scope_paths(
        home_dir, scope, project_dir
    )

    # Resolve the durable interpreter ONCE, before any copy, so hook commands
    # and script shebangs all agree. The existing registration (target
    # settings first, then global) is the preferred substitute when the
    # running interpreter is non-durable (agent worktree / tmp venv).
    settings_path = settings_target_dir / "settings.json"
    settings_data = _load_settings(settings_path)
    _existing_python = _registered_python(settings_data)
    if _existing_python is None and scope == "project":
        _existing_python = _registered_python(_load_settings(global_claude_dir / "settings.json"))
    _python_path = _stable_python(existing=_existing_python, home_dir=home_dir)

    if not dry_run:
        global_hooks_dir.mkdir(parents=True, exist_ok=True)
        if scope != "global":
            hooks_dir.mkdir(parents=True, exist_ok=True)

    package_hooks = Path(__file__).parents[1] / "hooks"

    # Always-global scripts
    stop_dst, session_end_dst, router_dst = _install_global_scripts(
        package_hooks, global_hooks_dir, dry_run, _python_path
    )

    # #64: sweep yadgar-installed orphan hook scripts from the GLOBAL hooks dir —
    # the non-prefixed vestige copies prior installs emitted (content-hash-gated,
    # nix-symlink-skipped) + the superseded db-lockdown orphan. Global-dir only:
    # the orphans only ever landed in ~/.claude/hooks (scope=global writes there).
    _sweep_stale_hook_scripts(package_hooks, global_hooks_dir, dry_run)

    # hook_runner.py (dispatcher for core hooks)
    hook_runner_dst = hooks_dir / "hook_runner.py"
    _copy_hook(
        Path(__file__).parents[1] / "scripts" / "hook_runner.py",
        hook_runner_dst,
        dry_run,
        _python_path,
    )
    _runner = str(hook_runner_dst)

    # Auth env block (BUG B/C fix) — see _resolve_env_block.
    _env_block: dict = _resolve_env_block()

    hooks_config = settings_data.get("hooks", {})

    # Core hooks (always replaced)
    _build_core_hooks(hooks_config, _runner, _env_block, router_dst, _python_path)

    # Append-if-absent hooks
    _install_append_hooks(package_hooks, hooks_dir, hooks_config, _env_block, dry_run, _python_path)

    settings_data["hooks"] = hooks_config

    _python = shlex.quote(_python_path)
    _stop_entry = [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{_python} {shlex.quote(str(stop_dst))}"}],
        }
    ]
    _session_end_entry = [
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": f"{_python} {shlex.quote(str(session_end_dst))}"}
            ],
        }
    ]

    if scope == "global":
        hooks_config["Stop"] = _stop_entry
        hooks_config["SessionEnd"] = _session_end_entry
        settings_data["hooks"] = hooks_config

    if dry_run:
        preview = json.dumps(settings_data, indent=2)
        print(f"[dry-run] Would write to: {settings_path}")
        print(preview)
        return {
            "status": "dry_run",
            "scope": scope,
            "project_directory": str(project_dir),
            "settings_file": str(settings_path),
            "preview": settings_data,
        }

    _atomic_write(settings_target_dir, settings_path, settings_data)

    if scope == "project":
        _write_global_stop_hooks(global_claude_dir, _stop_entry, _session_end_entry)

    global_settings_file = (
        str(settings_path) if scope == "global" else str(global_claude_dir / "settings.json")
    )
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
            "PreToolUse (router-guard)",
            "Stop (memory checkpoint — global)",
            "SessionEnd (sentinel capture — global)",
            "SubagentStop (findings capture — append-if-absent)",
            "InstructionsLoaded (recall on CLAUDE.md load — append-if-absent)",
            "SubagentStart (context injection at dispatch — append-if-absent)",
            "FileChanged (team_inbox + PLAN_*.md — append-if-absent)",
        ],
        "settings_file": str(settings_path),
        "global_settings_file": global_settings_file,
    }


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
