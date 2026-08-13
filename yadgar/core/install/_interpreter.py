"""Durable-interpreter resolution for install_hooks (Car C5 split from install_hooks_lib).

``sys.executable`` inside an agent git worktree (``.claude/worktrees/…`` or
``/tmp/<worktree>/.venv/bin/python``) is EPHEMERAL: baking it into persistent
settings/shebangs breaks every hook once the worktree is cleaned. These helpers
resolve a DURABLE interpreter path safe to persist (task #38, 3rd occurrence of
hook-interpreter poisoning).

Leaf module: no intra-package dependencies. Imported by ``_hook_scripts`` and
``_settings``; the canonical ``install_hooks_lib`` re-exports the public surface.
"""

from __future__ import annotations

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
    post-tool-capture) need a Python that has yadgar on its path.
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
