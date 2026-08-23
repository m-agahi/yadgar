"""Env-independent `surreal` binary resolution + VACUUM_SIDE_LAUNCHER knob (task 0107).

The vacuum unit inherits the systemd user-manager PATH, which on a fresh
Debian VM is ``/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`` — it
excludes ``~/.local/bin``, where pipx installs `surreal`. Before this car,
which side-build branch a host took (`HostBinaryLauncher` vs
`ContainerLauncher` vs the SKIP) was decided by three independent
``shutil.which("surreal")``/bare-``Popen(["surreal", ...])`` call sites
against whatever PATH happened to be inherited — so a host with a perfectly
usable `surreal` could silently take the container branch (or the SKIP) under
the timer, and could flip branches across reboots.

This module proves the fix: ``_resolve_surreal_binary`` is a single,
env-independent resolver (env override → PATH → fixed candidate dirs) that
``select_side_launcher``, the preflight (`_has_surreal_binary`), and the
actual spawn (`HostBinaryLauncher.start` → `spawn_surreal`) all go through —
and ``VACUUM_SIDE_LAUNCHER`` lets an operator pin the branch explicitly
instead of selecting it by absence.

ADR-0186 is the binding neighbour: its consequences section names this task
by number as the open question ("which branch a given host takes is still
decided by inherited systemd environment (task 0107)").
"""

from __future__ import annotations

import stat
import tempfile
import types as _types
from pathlib import Path

import pytest

from yadgar.core.vacuum import launcher as launcher_mod


def _make_exec(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _neutralise_candidate_dirs(monkeypatch) -> None:
    """Make the candidate-dir fallback resolve nothing, for tests whose whole
    premise is "no binary is obtainable".

    Emptying ``PATH`` and pinning ``HOME`` is NOT sufficient: three of the four
    entries in ``_SURREAL_BIN_CANDIDATES`` (``/usr/local/bin/surreal``,
    ``/opt/homebrew/bin/surreal``, ``/usr/bin/surreal``) are ABSOLUTE paths
    that no environment variable can redirect.  The CI image installs a real
    `surreal` into ``/usr/local/bin``, so on CI the resolver kept returning it
    and every "nothing resolves" assertion in this module failed against a host
    where a binary genuinely was resolvable (PR #22).  The product behaviour is
    correct and deliberate — ADR-0191 chose the candidate-dir fallback over
    PATH-luck precisely so a pipx-installed binary is found under the systemd
    user-manager PATH — so the fix belongs here, in the precondition.

    Redirects to ONE ``HOME``-anchored entry rather than ``()``: an empty tuple
    would leave these tests green even if the candidate-dir loop were deleted
    from the resolver entirely.  The caller must also pin ``HOME`` at a
    directory with no ``.local/bin/surreal`` in it.
    """
    monkeypatch.setattr(launcher_mod, "_SURREAL_BIN_CANDIDATES", ("~/.local/bin/surreal",))


# ---------------------------------------------------------------------------
# 4.1 — RED first: env-independent resolution
# ---------------------------------------------------------------------------


class TestResolveSurrealBinary:
    def test_resolves_from_local_bin_when_path_excludes_it(self, monkeypatch):
        """The Debian VM failure, reproduced without a VM.

        PATH excludes `~/.local/bin` (the systemd user-manager default); a
        pipx-style install still resolves via the candidate-dir fallback.
        """
        from yadgar.core.vacuum.launcher import _resolve_surreal_binary

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            surreal = _make_exec(local_bin / "surreal")

            monkeypatch.setenv("HOME", str(home))
            monkeypatch.setenv("PATH", "/usr/bin:/bin")
            monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)

            resolved = _resolve_surreal_binary()

        assert resolved == str(surreal), (
            f"expected the ~/.local/bin candidate {surreal}, got {resolved!r}"
        )

    def test_env_override_wins_over_path(self, monkeypatch):
        from yadgar.core.vacuum.launcher import _resolve_surreal_binary

        with tempfile.TemporaryDirectory() as td:
            on_path = Path(td) / "on-path"
            on_path.mkdir()
            _make_exec(on_path / "surreal")

            override_dir = Path(td) / "override"
            override_dir.mkdir()
            override = _make_exec(override_dir / "surreal-override")

            monkeypatch.setenv("PATH", str(on_path))
            monkeypatch.setenv("YADGAR_SURREAL_BIN", str(override))

            resolved = _resolve_surreal_binary()

        assert resolved == str(override), (
            "the explicit operator override must win over a binary found on PATH"
        )

    def test_returns_none_when_nothing_resolves(self, monkeypatch):
        """No override, empty PATH, no candidates → None; the caller falls to
        the container branch.

        "no candidates" needs the candidate LIST redirected, not just HOME and
        PATH emptied: three of the four defaults (``/usr/local/bin``,
        ``/opt/homebrew/bin``, ``/usr/bin``) are ABSOLUTE, so no env var
        reaches them.  The CI image carries a real `surreal` in
        ``/usr/local/bin`` and this test resolved it, asserting ``None``
        against a host where a binary genuinely WAS resolvable (PR #22).  See
        :func:`_neutralise_candidate_dirs`.
        """
        from yadgar.core.vacuum.launcher import (
            ContainerLauncher,
            _resolve_surreal_binary,
            select_side_launcher,
        )

        with tempfile.TemporaryDirectory() as td:
            empty_bin = Path(td) / "empty-bin"
            empty_bin.mkdir()
            home = Path(td) / "home"
            home.mkdir()

            monkeypatch.setenv("HOME", str(home))
            monkeypatch.setenv("PATH", str(empty_bin))
            monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
            monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)
            _neutralise_candidate_dirs(monkeypatch)

            assert _resolve_surreal_binary() is None

            fake_runtime = _make_exec(
                Path(td) / "fake-podman",
                '#!/bin/sh\ncase "$1" in image) exit 0;; *) exit 0;; esac\n',
            )
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))

            launcher = select_side_launcher()

        assert isinstance(launcher, ContainerLauncher), (
            f"no binary resolvable + an available image must fall to the "
            f"container branch; got {launcher!r}"
        )

    def test_branch_is_identical_with_and_without_path(self, monkeypatch):
        """THE determinism assertion the car is named for.

        With YADGAR_SURREAL_BIN set, select_side_launcher() returns the same
        launcher class whether PATH contains `surreal` or is empty — the
        branch decision must not depend on inherited environment.
        """
        from yadgar.core.vacuum.launcher import HostBinaryLauncher, select_side_launcher

        with tempfile.TemporaryDirectory() as td:
            override_dir = Path(td) / "override"
            override_dir.mkdir()
            override = _make_exec(override_dir / "surreal")

            on_path_dir = Path(td) / "on-path"
            on_path_dir.mkdir()
            _make_exec(on_path_dir / "surreal")

            empty_bin = Path(td) / "empty-bin"
            empty_bin.mkdir()

            monkeypatch.setenv("YADGAR_SURREAL_BIN", str(override))
            monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)

            monkeypatch.setenv("PATH", str(on_path_dir))
            with_path = select_side_launcher()

            monkeypatch.setenv("PATH", str(empty_bin))
            without_path = select_side_launcher()

        assert type(with_path) is type(without_path) is HostBinaryLauncher, (
            f"branch must be identical regardless of PATH; got "
            f"{type(with_path).__name__} vs {type(without_path).__name__}"
        )

    def test_spawn_uses_the_resolved_absolute_path(self, monkeypatch, tmp_path):
        """Closes the third (previously independent) resolution point: the
        actual Popen argv must carry the RESOLVED absolute path, not the bare
        string "surreal" re-looked-up against PATH.
        """
        from yadgar.core._surreal_runner import _surreal_runner as runner_mod
        from yadgar.core.vacuum.launcher import HostBinaryLauncher

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        surreal = _make_exec(fake_bin / "surreal")
        monkeypatch.setenv("PATH", str(fake_bin))
        monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)

        captured: dict[str, list[str]] = {}

        class _FakeProc:
            pid = 424242

        def _fake_popen(argv, **_kwargs):
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(runner_mod.subprocess, "Popen", _fake_popen)

        side_path = tmp_path / "side"
        side_path.mkdir()
        HostBinaryLauncher().start(side_path=side_path, port=1, user="root", password="root")

        assert captured["argv"][0] == str(surreal), (
            f"Popen argv[0] must be the resolved absolute path; got {captured['argv'][0]!r}"
        )


# ---------------------------------------------------------------------------
# 4.2 — RED first: VACUUM_SIDE_LAUNCHER knob behaviour
# ---------------------------------------------------------------------------


class TestLauncherKnob:
    def test_launcher_knob_container_ignores_a_present_host_binary(self, monkeypatch):
        from yadgar.core.vacuum.launcher import ContainerLauncher, select_side_launcher

        with tempfile.TemporaryDirectory() as td:
            on_path_dir = Path(td) / "on-path"
            on_path_dir.mkdir()
            _make_exec(on_path_dir / "surreal")
            monkeypatch.setenv("PATH", str(on_path_dir))
            monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)

            fake_runtime = _make_exec(
                Path(td) / "fake-podman",
                '#!/bin/sh\ncase "$1" in image) exit 0;; *) exit 0;; esac\n',
            )
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))
            monkeypatch.setenv("YADGAR_VACUUM_SIDE_LAUNCHER", "container")

            launcher = select_side_launcher()

        assert isinstance(launcher, ContainerLauncher), (
            f"VACUUM_SIDE_LAUNCHER=container must ignore a resolvable host binary; got {launcher!r}"
        )

    def test_launcher_knob_host_fails_loudly_when_unresolvable(self, monkeypatch, capsys):
        """Must NOT silently fall through to the container; the SKIP reason
        must name the pin so an operator who typo'd the path learns it from
        the log.
        """
        from yadgar.core.vacuum import _has_side_build_launcher

        with tempfile.TemporaryDirectory() as td:
            empty_bin = Path(td) / "empty-bin"
            empty_bin.mkdir()
            home = Path(td) / "home"
            home.mkdir()

            monkeypatch.setenv("HOME", str(home))
            monkeypatch.setenv("PATH", str(empty_bin))
            monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
            monkeypatch.setenv("YADGAR_VACUUM_SIDE_LAUNCHER", "host")
            # "unresolvable" must mean unresolvable — the absolute candidate
            # dirs are not reachable by any env var (PR #22).
            _neutralise_candidate_dirs(monkeypatch)
            # A present, working container runtime + image — if the pin were
            # not honoured, this would silently succeed via the container.
            fake_runtime = _make_exec(
                Path(td) / "fake-podman",
                '#!/bin/sh\ncase "$1" in image) exit 0;; *) exit 0;; esac\n',
            )
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))
            monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "example.invalid/backend:test")

            ok, detail = _has_side_build_launcher()

        err = capsys.readouterr().err
        assert ok is False, "a pinned-but-unresolvable host mode must SKIP, not fall through"
        assert "VACUUM_SIDE_LAUNCHER=host" in err, (
            f"the SKIP reason must name the pin; stderr was:\n{err}"
        )
        assert "container" not in err.lower() or "fall" in err.lower(), (
            f"the message must not suggest the container branch silently ran; stderr:\n{err}"
        )
        # task 65 / C1: the returned detail IS the source of truth that gets
        # propagated into the consolidation-log row, so it must carry the pin.
        assert "VACUUM_SIDE_LAUNCHER=host" in detail, (
            f"the returned detail must carry the pin (consolidation row mirror); detail={detail!r}"
        )

    def test_default_knob_is_auto_and_preserves_host_first(self, monkeypatch):
        """Behaviour-preservation guard for nix/dev hosts: unset (or 'auto')
        keeps today's host-binary-first semantics.
        """
        from yadgar.core.vacuum.launcher import HostBinaryLauncher, select_side_launcher

        with tempfile.TemporaryDirectory() as td:
            on_path_dir = Path(td) / "on-path"
            on_path_dir.mkdir()
            _make_exec(on_path_dir / "surreal")
            monkeypatch.setenv("PATH", str(on_path_dir))
            monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
            monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)

            fake_runtime = _make_exec(
                Path(td) / "fake-podman",
                '#!/bin/sh\ncase "$1" in image) exit 0;; *) exit 0;; esac\n',
            )
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))

            launcher = select_side_launcher()

        assert isinstance(launcher, HostBinaryLauncher), (
            f"default (unset) knob must keep host-first behaviour; got {launcher!r}"
        )


@pytest.mark.parametrize("mode", ["auto", "host", "container", "AUTO", "  container  ", "bogus"])
def test_launcher_mode_never_raises_on_any_input(monkeypatch, mode):
    """An unrecognised or oddly-cased value falls back to auto rather than
    raising — a typo'd pin must not crash the vacuum unit's startup.
    """
    from yadgar.core.vacuum.launcher import _launcher_mode

    monkeypatch.setenv("YADGAR_VACUUM_SIDE_LAUNCHER", mode)
    result = _launcher_mode()
    assert result in {"auto", "host", "container"}


# ---------------------------------------------------------------------------
# The knob must not be a PHANTOM: config.yaml has to be authoritative.
# ---------------------------------------------------------------------------


class TestLauncherKnobIsNotEnvOnly:
    """``VACUUM_SIDE_LAUNCHER`` is registered in ``FIELD_META``, so it is shown
    and written by config.yaml and the config UI.  It was read with a bare
    ``os.environ.get``, so a value set THERE was silently discarded — the exact
    phantom-knob class ``test_no_phantom_knobs.py`` ratchets against.  The knob
    exists so an operator can PIN a branch (ADR-0191), which is worthless if
    the sanctioned place to write the pin is ignored.
    """

    def test_settings_value_is_honoured_when_env_is_unset(self, monkeypatch):
        import yadgar._shared.config.config as config_mod
        from yadgar.core.vacuum.launcher import _launcher_mode

        monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)
        # Patch where resolve_knob LOOKS get_settings up, not at a re-export —
        # patching the re-export leaves resolve_knob reading the real (cached)
        # settings and the test passes for the wrong reason.
        monkeypatch.setattr(
            config_mod,
            "get_settings",
            lambda: _types.SimpleNamespace(VACUUM_SIDE_LAUNCHER="container"),
        )

        assert _launcher_mode() == "container", (
            "a VACUUM_SIDE_LAUNCHER pin written to config.yaml must take effect; "
            "reading it via os.environ only makes the yaml/UI value a no-op"
        )

    def test_live_env_still_outranks_settings(self, monkeypatch):
        """Precedence guard: env FIRST keeps today's semantics (and every other
        test in this module, which pins the knob via monkeypatch.setenv).
        """
        import yadgar._shared.config.config as config_mod
        from yadgar.core.vacuum.launcher import _launcher_mode

        monkeypatch.setattr(
            config_mod,
            "get_settings",
            lambda: _types.SimpleNamespace(VACUUM_SIDE_LAUNCHER="container"),
        )
        monkeypatch.setenv("YADGAR_VACUUM_SIDE_LAUNCHER", "host")

        assert _launcher_mode() == "host"

    def test_settings_value_is_normalised_like_an_env_value(self, monkeypatch):
        """Case/whitespace folding happens AFTER resolution, so a yaml value is
        normalised identically to an env one — normalising inside ``parse``
        would reach only the env string.
        """
        import yadgar._shared.config.config as config_mod
        from yadgar.core.vacuum.launcher import _launcher_mode

        monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)
        monkeypatch.setattr(
            config_mod,
            "get_settings",
            lambda: _types.SimpleNamespace(VACUUM_SIDE_LAUNCHER="  Container  "),
        )

        assert _launcher_mode() == "container"
