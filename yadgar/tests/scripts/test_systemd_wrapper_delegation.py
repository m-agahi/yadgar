"""``generate_systemd.sh`` after Stage D: it renders nothing and delegates (ADR-0190).

The wrapper's whole job is now resolve → assert schema → delegate, and every one
of those three has a failure mode that is silent if it is got wrong:

* **resolution** must prefer the CO-SHIPPED renderer over ``command -v``, or a
  curl-piped installer happily renders units with whatever ``yadgar`` happens to
  be on ``PATH`` (plan §7 mitigation 1).
* **the schema assertion** must treat a renderer that does not IMPLEMENT
  ``--print-schema`` as too old. A genuinely old CLI does not answer with a low
  number — it exits non-zero on an argparse error, so a naive
  ``if schema < N: abort`` lets exactly the case the check exists for fall
  straight through (plan §7 "bootstrap gap"). Three arms are tested here: a
  renderer reporting ``N-1``, one that rejects the flag, and one that prints
  something unparseable.
* **fail-loud is the recovery path** (plan §9.2). Every abort must leave the
  units already on disk untouched, because those are the ones currently running.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from yadgar.core.daemon.unit_install import UNIT_SCHEMA_VERSION
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import BASH, RENDERER_CLI

INSTALL_DIR = REPO_ROOT / "scripts" / "install"
GENERATE_SYSTEMD_SH = INSTALL_DIR / "generate_systemd.sh"

SENTINEL = "[Unit]\nDescription=the unit that was already installed\n"


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


def _run(tmp_path: Path, renderer: str, out_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "YADGAR_RUNTIME": "podman",
            "YADGAR_SYSTEMD_OUTPUT_DIR": str(out_dir),
            "YADGAR_STATE_DIR": str(tmp_path / "state"),
            "YADGAR_RENDERER_CLI": renderer,
            # Only meaningful for the real-interpreter arm below; harmless for
            # the bash stubs. See RENDERER_ENV's rationale in _unit_render.py.
            "PYTHONPATH": str(REPO_ROOT),
            "YADGAR_HOST_CLI": "/usr/bin/true",
            "YADGAR_HOST_NIGHTLY_CLI": "/usr/bin/true",
        }
    )
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    return subprocess.run([BASH, str(GENERATE_SYSTEMD_SH)], capture_output=True, text=True, env=env)


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # A renderer one shape behind: it answers, with a number that is too low.
        ("reports_n_minus_1", f'[[ "$*" == *--print-schema* ]] && echo {UNIT_SCHEMA_VERSION - 1}'),
        # THE BOOTSTRAP GAP: an old CLI has no such flag at all. argparse exits 2.
        ("rejects_the_flag", 'echo "unrecognized arguments: --print-schema" >&2; exit 2'),
        # Answers, but not with a number — a help banner, a deprecation notice.
        ("prints_garbage", 'echo "yadgar (see --help)"'),
        # Answers with nothing on stdout and exit 0 — the emptiest possible pass.
        ("prints_nothing", "exit 0"),
    ],
)
def test_wrapper_refuses_a_renderer_that_cannot_prove_its_schema(tmp_path, label, body):
    """All four arms abort, and none of them touches the units already on disk."""
    out_dir = tmp_path / "units"
    out_dir.mkdir()
    (out_dir / "yadgar.service").write_text(SENTINEL)
    renderer = _stub(tmp_path / "bin" / f"yadgar-{label}", body)

    result = _run(tmp_path, str(renderer), out_dir)

    assert result.returncode != 0, f"{label}: the wrapper rendered anyway\n{result.stdout}"
    combined = result.stdout + result.stderr
    assert "unit schema" in combined, f"{label}: the abort does not name the schema\n{combined}"
    assert "pipx install" in combined, f"{label}: the abort names no fix\n{combined}"
    assert (out_dir / "yadgar.service").read_text() == SENTINEL, (
        f"{label}: an abort rewrote a unit that was already installed — fail-loud "
        f"is only a recovery path if the previous units survive it"
    )
    assert {p.name for p in out_dir.iterdir()} == {"yadgar.service"}, (
        f"{label}: the wrapper left files behind after aborting"
    )


def test_wrapper_refuses_when_no_renderer_resolves(tmp_path):
    """No renderer at all — same actionable shape, still nothing written."""
    out_dir = tmp_path / "units"
    out_dir.mkdir()
    env = dict(os.environ)
    # Strip only the PATH entries that carry a `yadgar`; emptying PATH outright
    # would take `dirname` and `cd` with it and the script would fail earlier,
    # for a reason that has nothing to do with renderer resolution.
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join(
                d for d in env["PATH"].split(os.pathsep) if d and not os.path.exists(f"{d}/yadgar")
            ),
            "YADGAR_RUNTIME": "podman",
            "YADGAR_SYSTEMD_OUTPUT_DIR": str(out_dir),
        }
    )
    env.pop("YADGAR_RENDERER_CLI", None)
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        [BASH, str(GENERATE_SYSTEMD_SH)], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "no yadgar renderer found" in result.stderr
    assert not list(out_dir.iterdir()), "nothing may be written when no renderer resolves"


def test_wrapper_prefers_the_co_shipped_renderer_over_path(tmp_path):
    """``<prefix>/bin/yadgar`` beside the wrapper wins over ``command -v yadgar``.

    This is plan §7's first mitigation and the reason skew usually cannot arise:
    the wheel ships this script to ``<prefix>/share/yadgar/scripts/``, so the CLI
    from the SAME install is three levels up in ``bin/``. Proven by putting a
    DIFFERENT renderer on ``PATH`` and asserting the co-shipped one ran.
    """
    prefix = tmp_path / "prefix"
    scripts = prefix / "share" / "yadgar" / "scripts"
    shutil.copytree(INSTALL_DIR, scripts)
    marker = tmp_path / "co-shipped-ran"
    _stub(
        prefix / "bin" / "yadgar",
        f'[[ "$*" == *--print-schema* ]] && {{ echo {UNIT_SCHEMA_VERSION}; exit 0; }}\n'
        f'touch "{marker}"',
    )
    path_bin = tmp_path / "path-bin"
    _stub(path_bin / "yadgar", 'echo "PATH renderer must not be chosen" >&2; exit 3')

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{path_bin}:{env['PATH']}",
            "YADGAR_RUNTIME": "podman",
            "YADGAR_SYSTEMD_OUTPUT_DIR": str(tmp_path / "units"),
        }
    )
    env.pop("YADGAR_RENDERER_CLI", None)
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        [BASH, str(scripts / "generate_systemd.sh")], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"co-shipped renderer was not used\n{result.stderr}"
    assert marker.exists(), "the wrapper delegated to the PATH renderer, not the co-shipped one"


def test_renderer_cli_may_carry_arguments(tmp_path):
    """``YADGAR_RENDERER_CLI`` is word-split, so ``python3 -m yadgar`` is valid.

    The tests pin the renderer at the interpreter running them (``RENDERER_CLI``),
    which is only possible because the wrapper reads the variable with
    ``read -ra`` rather than treating it as a single path.
    """
    out_dir = tmp_path / "units"
    out_dir.mkdir()
    result = _run(tmp_path, RENDERER_CLI, out_dir)
    assert result.returncode == 0, f"multi-word renderer failed\n{result.stderr}"
    assert (out_dir / "yadgar.target").exists()
    assert " " in RENDERER_CLI, "this test is only meaningful for a multi-word command"
