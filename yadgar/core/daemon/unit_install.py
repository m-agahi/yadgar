"""Host-side half of rendering the nine units (task:0110 Stage C, ADR-0190).

``yadgar/core/daemon/units.py`` and ``maintenance_units.py`` are pure — every
value they need arrives on a spec, so a committed fixture can pin them. The
probes that produce those values, and the two side effects the shell renderer
performs while rendering, live here.

Ported from ``scripts/install/generate_systemd.sh`` (which still renders the
units until Stage D flips the wrapper):

* :func:`resolve_host_exec` — ``:130-156``. The maintenance units execute on the
  HOST, so ``@VACUUM_EXEC@`` / ``@NIGHTLY_EXEC@`` are resolved at RENDER time.
* :func:`fail_no_host_cli_message` / :class:`HostCliUnresolved` — ``:158-166``.
  Fail-loud: an unresolvable CLI aborts the install rather than baking a broken
  ``ExecStart`` into a unit that starts, fails, and is not looked at until
  consolidation has silently stopped for weeks.
* :func:`guard_nix_symlinks` — ``:98-111``, the DP5 defense-in-depth guard.
* :func:`ensure_trigger_dir` — ``:208``.
* :func:`seed_upgrade_env` — ``:236-244``, which must NOT overwrite an existing
  file: the upgrade orchestrator owns it after the first install.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from yadgar._shared.observability.observe import observe

__all__ = [
    "HostCliUnresolved",
    "InstallAborted",
    "NixManagedUnit",
    "ensure_trigger_dir",
    "fail_no_host_cli_message",
    "guard_nix_symlinks",
    "resolve_host_exec",
    "seed_upgrade_env",
]

# The DP5 guard's scope, verbatim from generate_systemd.sh:100 — the two units
# the nix flake also manages. Widening it to all nine would be a behaviour
# change, and Stage C is a port.
NIX_GUARDED_UNITS = ("yadgar.service", "yadgar-backend.service")


class InstallAborted(RuntimeError):
    """The render must not proceed. Both shell exits this module ports are these.

    Fail-loud is the recovery path (plan §9.2): the wrapper aborts BEFORE writing
    anything, so the previous units are still on disk and still running.
    """


class HostCliUnresolved(InstallAborted):
    """No host entry point resolved for a maintenance unit."""


class NixManagedUnit(InstallAborted):
    """An existing unit is a ``/nix/store`` symlink — the flake owns it (DP5)."""


@observe(tier="hot")
def fail_no_host_cli_message(unit: str, env_var: str, script: str, module: str) -> str:
    """``_fail_no_host_cli``'s text: name what was tried, name the fix.

    Kept as one string rather than prints so the caller decides the channel; the
    shape (tried-list, then an actionable install command) is what the shell
    version guarantees and what the version-skew abort in Stage D reuses.
    """
    return (
        f"ERROR: no host yadgar CLI found for the {unit} maintenance unit.\n"
        f"  Tried: ${env_var}, ~/.local/bin/{script}, 'command -v {script}', "
        f"'python3 -m {module}'.\n"
        "  Background maintenance (consolidation, heat decay, vacuum) runs on the\n"
        "  HOST, so a host CLI is required. Install one with:\n"
        "      pipx install yadgar\n"
        f"  ...then re-run setup. Or point ${env_var} at an existing install."
    )


@observe(tier="hot")
def _module_importable_isolated(module: str) -> bool:
    """``python3 -I -c 'import <module>'`` — the LAST resort probe.

    ``-I`` is NOT optional. Isolated mode drops the current directory from
    ``sys.path``; without it the probe succeeds from inside a repo checkout even
    with nothing installed, and the unit — which runs from a different working
    directory — then fails at 4am. Probe what the unit will actually experience.
    """
    python3 = shutil.which("python3")
    if not python3:
        return False
    return (
        subprocess.run(  # noqa: S603 — argv list, python3 from PATH, no shell
            [python3, "-I", "-c", f"import {module}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


@observe(tier="hot")
def resolve_host_exec(script: str, module: str, override: str | None = None) -> str | None:
    """Resolve a host entry point, or ``None``. Order is the shell version's.

    explicit override → ``~/.local/bin/<script>`` (the pipx shape ``flake.nix``
    installs) → ``shutil.which`` (brew, ``/usr/local``, other prefixes) →
    ``python3 -I -m <module>``.

    *script* and *module* are BOTH parameters because the two entry points are
    different binaries: ``yadgar-nightly-cycle`` is a console script and there
    is no ``yadgar nightly-cycle`` subcommand. Collapsing them into one code
    path is the tidy-up this signature exists to prevent.
    """
    if override:
        return override
    local = Path.home() / ".local" / "bin" / script
    if os.access(local, os.X_OK) and local.is_file():
        return str(local)
    found = shutil.which(script)
    if found:
        return found
    if _module_importable_isolated(module):
        return f"python3 -m {module}"
    return None


@observe(tier="hot")
def guard_nix_symlinks(output_dir: Path) -> None:
    """Refuse to render over nix-managed units (DP5 defense-in-depth).

    A ``/nix/store`` symlink means the flake owns these units; overwriting one
    replaces a read-only, declaratively-managed unit with an imperative copy that
    the next ``home-manager switch`` silently reverts.
    """
    for unit in NIX_GUARDED_UNITS:
        path = output_dir / unit
        if not path.is_symlink():
            continue
        target = os.readlink(path)
        if "/nix/store" in target:
            raise NixManagedUnit(
                f"ERROR: {unit} is managed by Nix (symlink → {target}).\n"
                "  Do not use 'make setup' on NixOS — use the nix flake (v5.46+).\n"
                "  See: https://github.com/m-agahi/yadgar#nixos-install"
            )


@observe(tier="hot")
def ensure_trigger_dir(state_dir: Path) -> Path:
    """Pre-create ``<state_dir>/triggers`` so the ``.path`` unit has a parent.

    Mirrors ``generate_launchd.sh`` (where launchd's ``WatchPaths`` genuinely
    needs the dir present at load); on systemd it removes the first-boot race.
    The core unit ALSO does this in its ``ExecStartPre`` — podman does not
    auto-create a missing ``-v`` source — so this is the render-time half of a
    two-sided guarantee, not a duplicate.
    """
    triggers = state_dir / "triggers"
    triggers.mkdir(parents=True, exist_ok=True)
    return triggers


@observe(tier="hot")
def seed_upgrade_env(state_dir: Path, core_image: str) -> tuple[Path, bool]:
    """Seed ``<state_dir>/upgrade.env`` with ``YADGAR_IMAGE_TAG``; never overwrite.

    ``yadgar.service`` reads the tag from here via ``EnvironmentFile=-``. The
    leading ``-`` makes a missing file non-fatal, but the upgrade orchestrator
    requires it to exist before the first upgrade, and it atomically REWRITES it
    on each routine upgrade — so an install that clobbered the file would roll
    every host back to the tag it was first installed with.

    Returns ``(path, seeded)``; ``seeded`` is False when the file already existed.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "upgrade.env"
    if path.exists():
        return path, False
    path.write_text(f"YADGAR_IMAGE_TAG={core_image}\n")
    return path, True
