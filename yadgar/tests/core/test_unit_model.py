"""The ordered unit model's load-bearing properties (task:0110 Stage A, ADR-0190).

The model exists because two of the nine units cannot survive a dict-keyed
representation. Those two traps are asserted here directly — nothing else tests
the ordered-pairs choice until Stage C ports the units that need it, and by then
the wrong model would already be load-bearing.
"""

from __future__ import annotations

from yadgar.core.daemon.unit_model import (
    Blank,
    Comment,
    Directive,
    Section,
    UnitFile,
    comments,
    render_unit,
)
from yadgar.core.daemon.units import UnitSpec


def minimal_spec(**overrides) -> UnitSpec:
    """A fully-pinned spec for model tests — no host probes, no env reads."""
    base = {
        "runtime": "podman",
        "network": "yadgar-net",
        "secrets_env_file": "/etc/yadgar/secrets.env",
        "upgrade_env_file": "/state/upgrade.env",
        "state_dir": "/state",
        "data_dir": "/data-host",
        "backend_container": "yadgar-backend",
        "backend_image": "img:1",
        "backend_data_mount": "/data-host",
        "backend_embed_port": 8001,
        "backend_surreal_port": 8000,
        "backend_cpus": "2",
        "backend_memory": "4g",
        "core_data_mount": "/data-host",
    }
    base.update(overrides)
    return UnitSpec(**base)  # type: ignore[arg-type]


def test_duplicate_wants_directives_both_render_in_order():
    """``yadgar.target`` writes ``Wants=`` TWICE and systemd unions them.

    A dict-keyed model keeps one. The one it drops (``yadgar.target.in:19``) is
    the sole activation mechanism for the vacuum timer, the nightly timer and the
    vacuum-trigger path — the unit still renders and still passes every "contains
    Wants=" assertion while background maintenance never starts.
    """
    section = Section(
        "Unit",
        (
            Directive("Wants", "yadgar.service yadgar-backend.service"),
            Comment("activation mechanism for the maintenance units"),
            Directive("Wants", "yadgar-vacuum.timer yadgar-nightly-cycle.timer"),
        ),
    )
    text = render_unit(UnitFile("yadgar.target", (section,)))
    assert text.count("Wants=") == 2, f"a Wants= line was collapsed:\n{text}"
    assert section.values("Wants") == [
        "yadgar.service yadgar-backend.service",
        "yadgar-vacuum.timer yadgar-nightly-cycle.timer",
    ]
    assert text.index("yadgar.service yadgar-backend") < text.index("yadgar-vacuum.timer")


def test_duplicate_execstart_directives_preserve_order():
    """``yadgar-vacuum-trigger.service`` has two ``ExecStart=`` lines and the ORDER matters.

    The trigger file is removed BEFORE the vacuum starts, so a transient vacuum
    failure cannot pin the ``.path`` unit active and stop it firing again.
    """
    section = Section(
        "Service",
        (
            Directive("Type", "oneshot"),
            Directive("ExecStart", "rm -f /state/triggers/vacuum_requested"),
            Directive("ExecStart", "systemctl --user start yadgar-vacuum.service"),
        ),
    )
    lines = render_unit(UnitFile("x.service", (section,))).splitlines()
    execs = [line for line in lines if line.startswith("ExecStart=")]
    assert execs == [
        "ExecStart=rm -f /state/triggers/vacuum_requested",
        "ExecStart=systemctl --user start yadgar-vacuum.service",
    ]


def test_sections_separated_by_exactly_one_blank_line_and_file_ends_with_newline():
    """The shape ``sed`` produces from the templates — parity depends on it."""
    unit = UnitFile(
        "x.service",
        (
            Section("Unit", (Directive("Description", "d"),)),
            Section("Install", (Directive("WantedBy", "default.target"),)),
        ),
    )
    assert render_unit(unit) == "[Unit]\nDescription=d\n\n[Install]\nWantedBy=default.target\n"


def test_install_section_is_omitted_structurally():
    """Four of the nine units have NO ``[Install]`` by design (they are timer/path started)."""
    unit = UnitFile("x.service", (Section("Service", (Directive("Type", "oneshot"),)),))
    assert "[Install]" not in render_unit(unit)


def test_trailing_comment_after_blank_survives():
    """``yadgar-vacuum.service.in`` ends with a blank line then a ``# No [Install]`` note."""
    unit = UnitFile(
        "x.service",
        (
            Section(
                "Service",
                (Directive("Type", "oneshot"), Blank(), Comment("No [Install]: timer-started.")),
            ),
        ),
    )
    assert render_unit(unit).endswith("Type=oneshot\n\n# No [Install]: timer-started.\n")


def test_comments_helper_formats_and_renders_empty_as_bare_hash():
    block = comments(("state dir is {state_dir}", ""), state_dir="/state")
    assert [c.line() for c in block] == ["# state dir is /state", "#"]
