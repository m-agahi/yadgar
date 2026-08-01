"""Cross-generator: vacuum unit ExecStart must be an absolute path (task 0107).

Companion to ``yadgar/tests/core/test_vacuum_binary_resolution.py``. That
module proves the CODE-side fix — `surreal` binary resolution is
env-independent (env override → PATH → fixed candidate dirs), so which
side-build launcher a host takes no longer depends on inherited PATH.

This module guards a narrower, adjacent invariant on the three unit-rendering
surfaces (`generate_systemd.sh`, `flake.nix`, `generate_launchd.sh`): the
`yadgar` binary each surface's vacuum unit execs is resolved to an ABSOLUTE
path at render time (`@VACUUM_EXEC@` / `${homeDir}/.local/bin/yadgar` /
`@YADGAR_SCRIPTS_DIR@/...`), never a bare command relying on the unit's own
(unset) PATH. That is true TODAY — this guards the regression where someone
"simplifies" `@VACUUM_EXEC@` to a bare `yadgar` and reintroduces a
PATH-dependent unit.

This is a WEAKER guard than the code-side fix and the plan says so plainly:
once `_resolve_surreal_binary` (task 0107) is env-independent, the units
legitimately carry no PATH for `surreal` at all, so there is nothing positive
left to assert about the `surreal` resolution on these surfaces specifically
— that property is proven by the RED tests in
`test_vacuum_binary_resolution.py`, not here.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import render_launchd, render_systemd

FLAKE_NIX = REPO_ROOT / "flake.nix"

_ATTR_ANCHOR = "\n            systemd.user."


def _flake_attr_block(attr: str) -> str | None:
    """Return the flake.nix source of `systemd.user.<attr> = { ... }`, or None.

    Mirrors ``test_vacuum_trigger_cross_generator.py``'s slicer: anchored on
    newline+indent (not the bare substring) so prose in a comment cannot
    truncate the block.
    """
    text = FLAKE_NIX.read_text()
    marker = f"{_ATTR_ANCHOR}{attr} = {{"
    if marker not in text:
        return None
    start = text.index(marker) + len(_ATTR_ANCHOR)
    rest = text[start:]
    end = rest.find(_ATTR_ANCHOR)
    return rest[:end] if end != -1 else rest


def test_vacuum_units_do_not_rely_on_inherited_path(tmp_path: Path) -> None:
    """Render each surface's vacuum unit; ExecStart must be an absolute path."""
    render_systemd(tmp_path)
    systemd_unit = (tmp_path / "units" / "yadgar-vacuum.service").read_text()
    exec_line = next(line for line in systemd_unit.splitlines() if line.startswith("ExecStart="))
    exec_cmd = exec_line.removeprefix("ExecStart=").split()[0]
    assert exec_cmd.startswith("/"), (
        f"generate_systemd.sh: yadgar-vacuum.service ExecStart must resolve to "
        f"an absolute path, not rely on the unit's (unset) PATH; got {exec_cmd!r}"
    )

    flake_block = _flake_attr_block("services.yadgar-vacuum")
    assert flake_block is not None, "flake.nix has no systemd.user.services.yadgar-vacuum unit"
    flake_exec_line = next(line for line in flake_block.splitlines() if "ExecStart" in line)
    # `ExecStart = "${homeDir}/.local/bin/yadgar vacuum ...";` — the Nix
    # interpolation always expands to something rooted at a home dir, so a
    # literal `/` immediately after the opening quote is what "absolute"
    # means here (this is asserted as source text, not evaluated Nix).
    quoted = flake_exec_line.split('"', 2)[1]
    assert quoted.startswith("${") and "/.local/bin/yadgar" in quoted.split()[0], (
        f"flake.nix: yadgar-vacuum ExecStart must be rooted at an absolute "
        f"home-dir path, not a bare `yadgar`; got {quoted!r}"
    )

    render_launchd(tmp_path)
    plist = plistlib.loads(
        (tmp_path / "units" / "com.openfantasy.yadgar-vacuum.plist").read_bytes()
    )
    program = plist["ProgramArguments"][0]
    assert program.startswith("/"), (
        f"generate_launchd.sh: com.openfantasy.yadgar-vacuum.plist ProgramArguments[0] "
        f"must be an absolute path; got {program!r}"
    )
