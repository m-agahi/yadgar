"""Hook-script copy + stale-orphan sweep for install_hooks (Car C5 split).

Copies yadgar-bundled hook scripts into the hooks dir (rewriting their shebang
to a durable interpreter) and sweeps yadgar-installed orphan scripts left by
pre-#64 global installs. Imports ``_resolve_python_shebang`` from ``_interpreter``;
imported by the canonical ``install_hooks_lib`` (which re-exports the surface)
and by ``_settings``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from yadgar._shared.observability.observe import observe

from ._interpreter import _resolve_python_shebang


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


# The non-prefixed hook basenames that PRE-#64 installs copied verbatim into
# hooks_dir via the old ``_copy_scope_scripts._files`` dict. Nothing dispatches
# to these on disk — the 4 runner-dispatched ones remaining here (post-tool-capture,
# session-start-context, pre-compact-drain, post-compact-rehydrate) are executed
# via ``hook_runner.py <type>``'s internal ``_HOOKS`` dict, and the 3 append
# hooks (subagent-start, instructions-loaded, file-changed) are installed under
# ``yadgar-`` names by ``_install_append_hooks``. The non-prefixed copies are
# pure vestige. #64 stops emitting them AND sweeps existing orphans.
# ADR-0156 removed the subagent-stop.py entry with the auto-store path.
#
# Car 8 (bug train): "prompt-recall" DROPPED from this set. It named a real,
# second implementation at ``core/hooks/prompt-recall.py`` — its own raw FTS
# query, its own HTTP forward that (unlike the wired path) never sent
# ``?project=`` — which nothing dispatched to and Car 8 deleted outright as
# vestige. The sweep below proves provenance by content-hashing an on-disk
# orphan AGAINST the packaged source (``_sha256_file(package_hooks / name)``);
# with the source gone that hash is permanently unobtainable, so leaving the
# name in this set would only leave it forever on the "cannot prove
# provenance → conservative, leave it" no-op branch. A pre-#64 install's
# leftover ``prompt-recall.py`` orphan is now permanently un-swept — accepted:
# it is inert dead bytes nothing ever executes, not the two-competing-
# implementations hazard the file itself represented while it shipped.
_MANAGED_NONPREFIXED: frozenset[str] = frozenset(
    {
        "pre-compact-drain.sh",
        "post-compact-rehydrate.sh",
        "post-tool-capture.py",
        "session-start-context.py",
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
