"""``yadgar.target`` plus the four maintenance units (task:0110 Stage C, ADR-0190).

The seven units ``scripts/install/*.in`` renders that the Python generator never
had: the target that ACTIVATES everything, the weekly vacuum (service + timer),
the MCP vacuum-trigger pair (``.path`` + ``.service``) and the nightly cycle
(service + timer). Stage C ports them into the unit model so all nine render
from this module. Stage D deleted the ``sed`` templates and turned the wrapper
into a delegating shim, so these builders are now what an install writes.

Three shapes here are the reason plan §4.4 chose an ordered directive model over
more f-strings, and each is pinned by a test in
``yadgar/tests/scripts/test_systemd_greenfield_units.py``:

* :func:`build_target_unit` writes ``Wants=`` on TWO lines. systemd unions
  repeated directives; a dict-keyed model keeps one, and the one it drops
  (``yadgar.target.in:19``) is the sole activation mechanism for both timers and
  the ``.path``. The unit still renders, still passes every "contains ``Wants=``"
  assertion, and background maintenance never starts.
* :func:`build_vacuum_trigger_service` writes ``ExecStart=`` TWICE — legal only
  under ``Type=oneshot`` — and the ORDER is load-bearing: the trigger file is
  removed BEFORE the vacuum starts, so a transient vacuum failure cannot pin the
  ``.path`` unit active (which would stop it firing again).
* three of the seven have NO ``[Install]`` section, because they are started by
  their timer/path and enabling them directly is not the activation mechanism.
  Omission is structural here, not a remembered ``if``. (Plan §4.1 says four of
  the nine; the templates say three — ``yadgar-vacuum-trigger.path`` does ship
  one. The parity fixtures are the authority.)

The two timers deliberately DISAGREE on timezone — vacuum local, nightly UTC —
matching ``flake.nix`` so the two systemd surfaces stay byte-comparable. A shared
"timer" helper that normalised them would be the bug, so they are built
separately and asserted literally.

Nothing here is runtime-conditional: ``diff -r`` over the committed fixtures
shows only ``yadgar.service`` and ``yadgar-backend.service`` differing between
the podman and docker arms, so these builders take no runtime at all. Nothing
reads the environment or the filesystem either — the host probes live in
``yadgar/core/daemon/unit_install.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.unit_model import (
    Blank,
    Comment,
    Directive,
    Entry,
    Section,
    UnitFile,
    comments,
)

__all__ = [
    "MAINTENANCE_UNIT_NAMES",
    "HostExecs",
    "build_maintenance_units",
    "build_nightly_service",
    "build_nightly_timer",
    "build_target_unit",
    "build_vacuum_service",
    "build_vacuum_timer",
    "build_vacuum_trigger_path",
    "build_vacuum_trigger_service",
]


@dataclass(frozen=True)
class HostExecs:
    """The two HOST entry points the maintenance units exec.

    Carried as a PAIR because the seven units are all-or-nothing: the target's
    second ``Wants=`` names the timers and the ``.path``, so an install with one
    exec resolved and not the other would enable units that do not exist. One
    optional field on the spec makes that structural rather than a runtime check.

    They are DIFFERENT binaries. ``yadgar-nightly-cycle`` is a console script;
    there is no ``yadgar nightly-cycle`` subcommand, and its ``main()`` has no
    argparse, so *nightly* is invoked bare while *vacuum* takes arguments.
    """

    vacuum: str
    nightly: str


MAINTENANCE_UNIT_NAMES = (
    "yadgar.target",
    "yadgar-vacuum.service",
    "yadgar-vacuum.timer",
    "yadgar-vacuum-trigger.path",
    "yadgar-vacuum-trigger.service",
    "yadgar-nightly-cycle.service",
    "yadgar-nightly-cycle.timer",
)

# ── Comment blocks ───────────────────────────────────────────────────────────
# Held as module constants for the same reason unit_docs.py exists: emitted
# verbatim into the units (the parity baseline carries every template comment),
# and inline they would push the builders past the function-LOC cap.

TARGET_ACTIVATION_DOC = (
    "ACTIVATION MECHANISM for the maintenance units (task:0077 D2). Every install",
    "entry point runs `systemctl --user enable [--now] yadgar.target` and nothing",
    "else — which installs ONLY yadgar.target's own WantedBy=default.target symlink.",
    "A .timer/.path carrying just `[Install] WantedBy=timers.target` therefore",
    "renders correctly, passes every render assertion, and NEVER ACTIVATES.",
    "",
    "Listing them here is one site that cannot drift. The alternative (an explicit",
    "`enable --now` per unit at each install entry point) is three sites, and those",
    "three have already diverged once — on macOS, where the Makefile bootstrapped",
    "two of six plists while yadgar-setup.sh bootstrapped all six.",
    "",
    "The units keep their own [Install] stanzas too, so `systemctl --user enable",
    "yadgar-vacuum.timer` still works for anyone who wants it. Note that",
    "`systemctl is-enabled` reports `disabled` for units pulled in this way — probe",
    "`is-active` / `list-timers` instead (yadgar-setup --doctor does).",
)

VACUUM_HOST_DOC = (
    "Runs on the HOST, not in a container: the vacuum flow interleaves phases",
    "requiring different daemon states (export → backend DOWN → reimport →",
    "backend UP) and the container image ships no systemctl, so",
    "--service-mode=systemd cannot work from inside one.",
)

# NEVER pass kwargs when rendering this block: `${cfg.secretsEnvFile}` is a
# literal brace pair and str.format would raise KeyError: 'cfg'.
VACUUM_SECRETS_DOC = (
    "Leading '-' — a missing secrets file must not wedge the timer into a",
    "permanent start failure. Mirrors flake.nix's `-${cfg.secretsEnvFile}`.",
)

VACUUM_NO_INSTALL_DOC = (
    "No [Install]: this unit is started by yadgar-vacuum.timer and by",
    "yadgar-vacuum-trigger.service, never enabled directly. Same shape as flake.nix.",
)

VACUUM_TIMER_TZ_DOC = (
    "LOCAL time, matching flake.nix — the two systemd surfaces stay byte-comparable.",
    "(The macOS plist is local too, since launchd's StartCalendarInterval has no UTC",
    "option, so the weekly vacuum agrees on all three surfaces.)",
)

VACUUM_TIMER_PERSISTENT_DOC = (
    "A missed window (machine asleep/off) fires at next activation. The stamp lives",
    "under ~/.local/share/systemd/timers/ and is timer machinery independent of the",
    "enablement symlink, so it still applies when the timer is pulled in by",
    "yadgar.target's Wants= rather than enabled. uninstall.sh clears it.",
)

VACUUM_TIMER_INSTALL_DOC = (
    "NOT the activation mechanism — see yadgar.target. Kept so a user who runs",
    "`systemctl --user enable yadgar-vacuum.timer` is not told the unit has no",
    "installation config.",
)

TRIGGER_PATH_DOC = (
    "MCP vacuum_now() (and the auto-vacuum backstop) write this file INSIDE the core",
    "container at YADGAR_VACUUM_TRIGGER_PATH; the `-v {state_dir}:/root/.local/state/yadgar`",
    "bind in yadgar.service projects that write to here on the host. Without this",
    "unit the file is written and never read, and vacuum_now() reports started=true",
    "into a void.",
    "",
    "{state_dir} is deliberately the SAME token that appears on the left of that",
    "`-v` bind, so the cross-generator test can compare the projected trigger dir",
    "against this watched dir as an exact string. Keep it that way.",
)

TRIGGER_SERVICE_DOC = (
    "Remove the trigger file BEFORE starting the vacuum, so a transient vacuum",
    "failure does not pin the .path unit in the active state (which would stop it",
    "firing again). If the vacuum itself fails, MCP can write the trigger again.",
    "",
    "Both commands are slash-free and resolved from the unit's $PATH — systemd has",
    "searched $PATH for a bare ExecStart command since v239. `systemctl` in",
    "particular MUST be the one belonging to the init system actually running the",
    "user bus, so pinning an absolute path would be wrong on some distros.",
)

NIGHTLY_HOST_DOC = (
    "Runs on the HOST for the same reason yadgar-vacuum.service does — the cycle",
    "includes a vacuum phase that stops and starts the backend.",
)

NIGHTLY_DB_URL_DOC = (
    "Consolidation opens StorageEngine in SERVER mode against this URL, reached via",
    "yadgar-backend.service's loopback publish.",
)

NIGHTLY_EMBED_DOC = (
    "Route embeddings through the backend's embed service rather than an in-process",
    "SentenceTransformer — a pipx-installed host yadgar has no [ml] extra.",
)

NIGHTLY_BARE_DOC = (
    "Invoked BARE. `yadgar-nightly-cycle` is a console script, NOT a `yadgar`",
    "subcommand — there is no `yadgar nightly-cycle`, and nightly_cycle.main()",
    "has no argparse at all (it reads the environment above). Any flag added here",
    "is either an argparse error or silently discarded.",
    "",
    "No LD_LIBRARY_PATH wrapper: numpy's .so problem is a nix-store artifact",
    "(flake.nix), not something an ordinary distro's pipx/system python has.",
)

NIGHTLY_TIMER_TZ_DOC = (
    "UTC, matching flake.nix exactly — the two systemd surfaces stay byte-comparable.",
    "(The macOS plist is unavoidably local time: launchd's StartCalendarInterval has",
    "no UTC option. That divergence is documented in the plist itself.)",
)

NIGHTLY_TIMER_PERSISTENT_DOC = (
    "A missed window fires at next activation. On a machine that was off overnight",
    "the first catch-up run may be long on a never-consolidated DB; TimeoutStartSec",
    "in the service bounds it at 1h. Mask with:",
    "  systemctl --user mask yadgar-nightly-cycle.timer",
)

_MANUAL_ENABLE_DOC = (
    "NOT the activation mechanism — see yadgar.target's Wants=. Kept for manual enable.",
)

_STACK = "yadgar.service yadgar-backend.service"


# ── yadgar.target ────────────────────────────────────────────────────────────


@observe(tier="hot")
def build_target_unit() -> UnitFile:
    """``yadgar.target`` — the full stack, and the ONLY thing installs enable.

    Two ``Wants=`` directives, not one: systemd unions them, and the second is
    what pulls in the timers and the ``.path``. They are separate lines rather
    than one merged list because the fifteen-line rationale sits between them in
    the template, and the parity baseline carries it.
    """
    return UnitFile(
        name="yadgar.target",
        sections=(
            Section(
                "Unit",
                (
                    Directive("Description", "Yadgar Memory Engine — full stack"),
                    Directive("Wants", _STACK),
                    *comments(TARGET_ACTIVATION_DOC),
                    Directive(
                        "Wants",
                        "yadgar-vacuum.timer yadgar-nightly-cycle.timer yadgar-vacuum-trigger.path",
                    ),
                    Directive("After", _STACK),
                ),
            ),
            Section("Install", (Directive("WantedBy", "default.target"),)),
        ),
    )


# ── Vacuum ───────────────────────────────────────────────────────────────────


@observe(tier="hot")
def build_vacuum_service(
    *, data_dir: str, secrets_env_file: str, surreal_port: int, vacuum_exec: str
) -> UnitFile:
    """``yadgar-vacuum.service`` — the weekly SurrealKV vacuum, on the host.

    No ``[Install]``: started by the timer and by the trigger service.
    ``TimeoutStartSec`` is a systemd time SPAN (``30min``), not an int.
    """
    return UnitFile(
        name="yadgar-vacuum.service",
        sections=(
            Section(
                "Unit",
                (
                    Directive(
                        "Description", "Yadgar SurrealKV vacuum (export → fresh DB → reimport)"
                    ),
                    Directive("After", _STACK),
                ),
            ),
            Section(
                "Service",
                (
                    *comments(VACUUM_HOST_DOC),
                    Directive("Type", "oneshot"),
                    *comments(VACUUM_SECRETS_DOC),  # no kwargs — see the block's note
                    Directive("EnvironmentFile", f"-{secrets_env_file}"),
                    Comment("SurrealDB over the loopback publish from yadgar-backend.service."),
                    Directive("Environment", f"YADGAR_DB_URL=http://127.0.0.1:{surreal_port}"),
                    Directive("Environment", f"YADGAR_DATA_DIR={data_dir}"),
                    Directive("ExecStart", f"{vacuum_exec} vacuum --service-mode=systemd --yes"),
                    Directive("TimeoutStartSec", "30min"),
                    Blank(),
                    *comments(VACUUM_NO_INSTALL_DOC),
                ),
            ),
        ),
    )


@observe(tier="hot")
def build_vacuum_timer() -> UnitFile:
    """``yadgar-vacuum.timer`` — Sunday 04:00 LOCAL, matching ``flake.nix``."""
    return UnitFile(
        name="yadgar-vacuum.timer",
        sections=(
            Section("Unit", (Directive("Description", "Weekly Yadgar vacuum"),)),
            Section(
                "Timer",
                (
                    *comments(VACUUM_TIMER_TZ_DOC),
                    Directive("OnCalendar", "Sun *-*-* 04:00:00"),
                    Directive("RandomizedDelaySec", "30min"),
                    *comments(VACUUM_TIMER_PERSISTENT_DOC),
                    Directive("Persistent", "true"),
                ),
            ),
            Section(
                "Install",
                (*comments(VACUUM_TIMER_INSTALL_DOC), Directive("WantedBy", "timers.target")),
            ),
        ),
    )


@observe(tier="hot")
def build_vacuum_trigger_path(*, state_dir: str) -> UnitFile:
    """``yadgar-vacuum-trigger.path`` — watches for MCP ``vacuum_now()``'s file.

    *state_dir* must be the SAME string the core unit puts on the left of its
    ``-v`` bind. Since both units now come from the same builder set that is a shared
    input rather than a cross-generator string comparison (plan §4.2).
    """
    return UnitFile(
        name="yadgar-vacuum-trigger.path",
        sections=(
            Section(
                "Unit",
                (Directive("Description", "Watch for vacuum trigger file from MCP vacuum_now()"),),
            ),
            Section(
                "Path",
                (
                    *comments(TRIGGER_PATH_DOC, state_dir=state_dir),
                    Directive("PathExists", f"{state_dir}/triggers/vacuum_requested"),
                ),
            ),
            Section(
                "Install", (*comments(_MANUAL_ENABLE_DOC), Directive("WantedBy", "paths.target"))
            ),
        ),
    )


@observe(tier="hot")
def build_vacuum_trigger_service(*, state_dir: str) -> UnitFile:
    """``yadgar-vacuum-trigger.service`` — TWO ``ExecStart=`` lines, in order.

    ``rm`` runs BEFORE ``systemctl start``: clearing the trigger first means a
    failing vacuum cannot leave the ``.path`` unit pinned active, which would
    stop it firing on the next request. Reversing these two lines is a silent
    behaviour change, so the order is asserted, not assumed.
    """
    return UnitFile(
        name="yadgar-vacuum-trigger.service",
        sections=(
            Section(
                "Unit",
                (
                    Directive(
                        "Description", "Handle vacuum trigger file (remove + start yadgar-vacuum)"
                    ),
                ),
            ),
            Section(
                "Service",
                (
                    Directive("Type", "oneshot"),
                    *comments(TRIGGER_SERVICE_DOC),
                    Directive("ExecStart", f"rm -f {state_dir}/triggers/vacuum_requested"),
                    Directive("ExecStart", "systemctl --user start yadgar-vacuum.service"),
                    Blank(),
                    Comment("No [Install]: started by yadgar-vacuum-trigger.path."),
                ),
            ),
        ),
    )


# ── Nightly cycle ────────────────────────────────────────────────────────────


def _nightly_service_entries(
    *, data_dir: str, secrets_env_file: str, surreal_port: int, nightly_exec: str
) -> tuple[Entry, ...]:
    """``[Service]`` for the nightly cycle, in the template's order."""
    return (
        *comments(NIGHTLY_HOST_DOC),
        Directive("Type", "oneshot"),
        Comment("Leading '-' — a missing secrets file must not wedge the timer."),
        Directive("EnvironmentFile", f"-{secrets_env_file}"),
        *comments(NIGHTLY_DB_URL_DOC),
        Directive("Environment", f"YADGAR_DB_URL=http://127.0.0.1:{surreal_port}"),
        *comments(NIGHTLY_EMBED_DOC),
        Directive("Environment", "YADGAR_EMBED_URL=http://127.0.0.1:8001"),
        Directive("Environment", f"YADGAR_DATA_DIR={data_dir}"),
        *comments(NIGHTLY_BARE_DOC),
        Directive("ExecStart", nightly_exec),
        Directive("TimeoutStartSec", "1h"),
        Blank(),
        Comment("No [Install]: started by yadgar-nightly-cycle.timer."),
    )


@observe(tier="hot")
def build_nightly_service(
    *, data_dir: str, secrets_env_file: str, surreal_port: int, nightly_exec: str
) -> UnitFile:
    """``yadgar-nightly-cycle.service`` — the bare-invoked console script.

    *nightly_exec* is a DIFFERENT binary from the vacuum's: ``yadgar-nightly-cycle``
    is a console script and there is no ``yadgar nightly-cycle`` subcommand.
    ``nightly_cycle.main()`` has no argparse, so the exec takes no arguments.
    """
    return UnitFile(
        name="yadgar-nightly-cycle.service",
        sections=(
            Section(
                "Unit",
                (
                    Directive(
                        "Description",
                        "Yadgar nightly cycle (backup → consolidate → vacuum → backup)",
                    ),
                    Directive("After", _STACK),
                ),
            ),
            Section(
                "Service",
                _nightly_service_entries(
                    data_dir=data_dir,
                    secrets_env_file=secrets_env_file,
                    surreal_port=surreal_port,
                    nightly_exec=nightly_exec,
                ),
            ),
        ),
    )


@observe(tier="hot")
def build_nightly_timer() -> UnitFile:
    """``yadgar-nightly-cycle.timer`` — 19:00 UTC, matching ``flake.nix``.

    UTC where the vacuum timer is local. That divergence is deliberate and
    cross-surface; do not factor the two timers through a shared helper.
    """
    return UnitFile(
        name="yadgar-nightly-cycle.timer",
        sections=(
            Section("Unit", (Directive("Description", "Nightly Yadgar cycle (19:00 UTC)"),)),
            Section(
                "Timer",
                (
                    *comments(NIGHTLY_TIMER_TZ_DOC),
                    Directive("OnCalendar", "*-*-* 19:00:00 UTC"),
                    *comments(NIGHTLY_TIMER_PERSISTENT_DOC),
                    Directive("Persistent", "true"),
                ),
            ),
            Section(
                "Install", (*comments(_MANUAL_ENABLE_DOC), Directive("WantedBy", "timers.target"))
            ),
        ),
    )


@observe(tier="boundary")
def build_maintenance_units(
    *,
    state_dir: str,
    data_dir: str,
    secrets_env_file: str,
    surreal_port: int,
    execs: HostExecs,
) -> dict[str, UnitFile]:
    """The seven, keyed by filename.

    Primitives rather than a ``UnitSpec`` on purpose: ``units.py`` imports this
    module, so taking its dataclass would be an import cycle — and these units
    genuinely need only five values.
    """
    units = (
        build_target_unit(),
        build_vacuum_service(
            data_dir=data_dir,
            secrets_env_file=secrets_env_file,
            surreal_port=surreal_port,
            vacuum_exec=execs.vacuum,
        ),
        build_vacuum_timer(),
        build_vacuum_trigger_path(state_dir=state_dir),
        build_vacuum_trigger_service(state_dir=state_dir),
        build_nightly_service(
            data_dir=data_dir,
            secrets_env_file=secrets_env_file,
            surreal_port=surreal_port,
            nightly_exec=execs.nightly,
        ),
        build_nightly_timer(),
    )
    return {u.name: u for u in units}
