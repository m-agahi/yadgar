"""Cross-generator regression: no ``ExecStartPre=`` any generator emits invokes ``cp``.

THE CLAIM THAT WAS NEVER TRUE (task 0121 item 2)
-----------------------------------------------
``entrypoint-backend.sh`` asserted, from 2026-05-12 (``267a45c3``) until this
car, that

    DB snapshots are handled outside the container by the systemd
    ExecStartPre `cp -r` of the surrealkv data dir.

No such directive has ever existed. ``git log -S 'ExecStartPre=cp -r' --all``
and ``-S 'ExecStartPre=/bin/cp' --all`` both return nothing. A reader believed
it, concluded pre-migration backups existed, and task 0115 exists to close the
gap that belief hid.

The comment rewrite has no runtime behaviour, so this is the one thing about it
that IS mechanically checkable: pin the FACT the comment got wrong, in the place
where a future generator change would make the corrected comment false again.
Deliberately ONE assertion — the general "prose names a real systemd directive"
guard was rejected on false-positive grounds (0121 §5.2): ``ExecStartPre``
appears in explanatory comments describing OTHER units, so a naive matcher
fails on correct text.

WHY THESE TWO SURFACES, ASSERTED THIS WAY
-----------------------------------------
(a) ``yadgar/core/daemon/systemd.py``'s RENDERED unit text and (b) ``flake.nix``'s
``ExecStartPre`` lists. Both survive task 0110, which makes ``systemd.py`` the
sole renderer and deletes the ``scripts/install/*.in`` templates — the templates
are therefore covered transitively once it lands, and asserting them directly
here would make this test a Stage-D casualty.

For the same reason this does NOT use ``yadgar/tests/_unit_render.py``'s
``render_systemd``: it shells out to ``GENERATE_SYSTEMD_SH``, which is
template-sourced, and 0110's plan lists ``_unit_render.py`` by name as a
Stage-D casualty.

Snapshot backups DO exist — they are simply not ExecStartPre:

* pre-vacuum physical snapshot — ``yadgar/core/vacuum/phases.py`` quiesces the
  service then ``shutil.copytree``s the data dir (host-side core process).
* nightly logical snapshots — ``yadgar/core/backup/backup.py`` ``GET /export``
  against a live backend, labelled ``nightly-pre`` / ``nightly-post``.
* in-container wiki snapshot — ``entrypoint-backend.sh``'s ``_wiki_backup_loop``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile
from yadgar.tests._paths import REPO_ROOT

FLAKE_NIX = REPO_ROOT / "flake.nix"

# `cp` as the invoked program: at the start of the directive value, or straight
# after the systemd `-` failure-tolerance prefix, or as an absolute path. Matching
# a bare `cp` anywhere in the line would false-fail on `--cpus`, `cp` inside a
# container name, and any word ending in "cp".
_CP_INVOCATION = re.compile(r"(?:^|[\s'\"])-?(?:/[\w/]*/)?cp(?:\s|$)")


def _execstartpre_values(unit_text: str) -> list[str]:
    return [
        line.split("=", 1)[1].strip()
        for line in unit_text.splitlines()
        if line.strip().startswith("ExecStartPre=")
    ]


def _render_python_systemd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    profile = _prod_profile(8765)
    result = systemd_mod.install_systemd_service(profile, dev=False)
    key = "backend_service" if role == "backend" else "core_service"
    return Path(result[key]).read_text()


@pytest.mark.parametrize("role", ["backend", "core"])
def test_rendered_systemd_unit_has_no_cp_execstartpre(role, tmp_path, monkeypatch):
    unit = _render_python_systemd(tmp_path, monkeypatch, role)
    directives = _execstartpre_values(unit)
    assert directives, f"{role}: expected at least one ExecStartPre — renderer changed shape?"
    offenders = [d for d in directives if _CP_INVOCATION.search(d)]
    assert not offenders, (
        f"{role} unit gained a `cp` ExecStartPre: {offenders}. No generator has ever "
        "emitted one; entrypoint-backend.sh claimed otherwise for ~3 months and that "
        "false claim is why task 0115 exists. If this is a deliberate new backup "
        "mechanism, update entrypoint-backend.sh's comment in the SAME commit."
    )


def test_flake_nix_execstartpre_lists_have_no_cp():
    """flake.nix declares ExecStartPre as a nix list of strings, not unit text."""
    text = FLAKE_NIX.read_text(encoding="utf-8")
    blocks = re.findall(r"ExecStartPre\s*=\s*\[(.*?)\];", text, re.DOTALL)
    assert blocks, "flake.nix declares no ExecStartPre lists — did the module move?"
    offenders = [
        entry
        for block in blocks
        for entry in re.findall(r'"([^"]*)"', block)
        if _CP_INVOCATION.search(entry)
    ]
    assert not offenders, (
        f"flake.nix gained a `cp` ExecStartPre: {offenders}. See the module docstring — "
        "the DB-snapshot mechanisms are phases.py's copytree, backup.py's /export, and "
        "entrypoint-backend.sh's wiki loop, none of which is an ExecStartPre."
    )
