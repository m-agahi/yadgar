"""Task 65 / Car C1: when the side-build launcher is pinned to a mode that
cannot resolve (e.g. host pinned, no surreal on PATH), ``_preflight_skip_reason``
must surface the PIN-specific detail — not the generic auto fallback.

Before the fix, the inner :func:`yadgar.core.vacuum._has_side_build_launcher`
correctly printed ``VACUUM_SIDE_LAUNCHER=host is pinned but no usable
``surreal`` binary resolved`` to stderr, but the outer
:func:`yadgar.core.vacuum._preflight_skip_reason` overwrote the detail with
the generic ``no usable `surreal` — none on the host PATH and no backend image
locally``. An operator reading the consolidation log saw a reason that
contradicted the pin they explicitly set.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def _make_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _neutralise_candidate_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher probes several absolute paths (PR #22); neuter ALL of them
    so this test reproduces an unresolvable host environment regardless of what
    is actually installed on the developer machine. Specifically:

    * HOME points at an empty tempdir so ``~/.local/bin/surreal`` cannot resolve.
    * PATH points at an empty dir so ``shutil.which('surreal')`` finds nothing.
    * YADGAR_SURREAL_BIN env override is unset.
    * The hard-coded absolute candidates (``/usr/local/bin/surreal``,
      ``/opt/homebrew/bin/surreal``, ``/usr/bin/surreal``) are not on the PATH
      either — they would only resolve via the candidate-dir loop, which uses
      ``expanduser`` on ``~/.local/bin/surreal`` plus absolute paths. The
      absolute paths are NOT touched because writing to /usr/bin would need
      root; the test relies on those binaries not existing on the dev box
      (they do not on this fixture host).
    """
    empty = Path(tempfile.mkdtemp())
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)


def test_preflight_preserves_host_pin_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When VACUUM_SIDE_LAUNCHER=host is pinned and unresolvable, the
    preflight SKIP detail MUST name the pin and the binary search; the
    generic auto-fallback message must NOT appear."""
    from yadgar.core.vacuum import _SKIP_NO_SURREAL, _preflight_skip_reason

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        home.mkdir()
        yadgar_home = home / ".yadgar"
        yadgar_home.mkdir()

        _neutralise_candidate_dirs(monkeypatch)
        monkeypatch.setenv("YADGAR_VACUUM_SIDE_LAUNCHER", "host")
        # A working fake container runtime + image: if the pin were not honoured,
        # the function would silently succeed via the container branch.
        fake_runtime = _make_exec(
            Path(td) / "fake-podman",
            '#!/bin/sh\ncase "$1" in image) exit 0;; *) exit 0;; esac\n',
        )
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))
        monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "example.invalid/backend:test")

        skip_reason, detail = _preflight_skip_reason(yadgar_home, before_bytes=10 * 1024 * 1024)

    assert skip_reason == _SKIP_NO_SURREAL, (
        f"host-pinned-unresolvable is still a no-launcher SKIP, got {skip_reason!r}"
    )
    assert "VACUUM_SIDE_LAUNCHER=host" in detail, (
        "the SKIP detail must name the operator's pin so the log matches the pin they set; "
        f"got detail={detail!r}"
    )
    assert "pinned" in detail.lower(), (
        f"the SKIP detail must say the pin is the blocker; got detail={detail!r}"
    )
    # Sanity: the AUTO fallback wording must NOT be the whole story when the
    # pin is host. The pre-fix message opened with 'no usable `surreal` — none on
    # the host PATH and no backend image locally' which contradicted the pin.
    assert "no backend image locally" not in detail, (
        "the auto-fallback message overwrites the pin-specific cause; the pin "
        "is the actual blocker, not the absence of the image. detail="
        f"{detail!r}"
    )


def test_preflight_preserves_container_pin_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """When VACUUM_SIDE_LAUNCHER=container is pinned and the image is absent,
    the preflight SKIP detail MUST name the pin and the missing image, not the
    auto-fallback message."""
    from yadgar.core.vacuum import _SKIP_NO_SURREAL, _preflight_skip_reason

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        home.mkdir()
        yadgar_home = home / ".yadgar"
        yadgar_home.mkdir()
        on_path = Path(td) / "on-path"
        on_path.mkdir()
        # No `surreal` binary on PATH either — eliminates host-branch fallthrough.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(on_path))
        monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
        monkeypatch.setenv("YADGAR_VACUUM_SIDE_LAUNCHER", "container")
        # Image not present: any `image ls` returns empty.
        fake_runtime = _make_exec(
            Path(td) / "fake-podman",
            "#!/bin/sh\nexit 1\n",
        )
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))
        monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "example.invalid/backend:test")

        skip_reason, detail = _preflight_skip_reason(yadgar_home, before_bytes=10 * 1024 * 1024)

    assert skip_reason == _SKIP_NO_SURREAL
    assert "VACUUM_SIDE_LAUNCHER=container" in detail, (
        f"the SKIP detail must name the operator's pin; got detail={detail!r}"
    )
    assert "image" in detail.lower(), (
        f"the SKIP detail must identify the missing image; got detail={detail!r}"
    )


def test_preflight_auto_branch_keeps_generic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When launcher mode is AUTO (the default) and NEITHER branch resolves,
    the preflight detail should still mention both the host-PATH search AND
    the backend-image search — this is the un-pinned case where the generic
    wording is correct, and the test pins that down so a future 'always
    mention the pin' tweak does not regress the auto path."""
    from yadgar.core.vacuum import _preflight_skip_reason

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        home.mkdir()
        yadgar_home = home / ".yadgar"
        yadgar_home.mkdir()
        empty = Path(td) / "empty"
        empty.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
        monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)
        # No container runtime either — auto cannot pick a branch.
        fake_runtime = _make_exec(Path(td) / "fake-podman", "#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))
        monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "example.invalid/backend:test")

        skip_reason, detail = _preflight_skip_reason(yadgar_home, before_bytes=10 * 1024 * 1024)

    assert skip_reason is not None
    assert "PATH" in detail, "auto branch must mention the host PATH search"
    assert "image" in detail.lower(), "auto branch must mention the backend image"


def test_preflight_skip_reason_return_contract_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The return type is ``tuple[str, str] | None``. A caller destructures
    straight into two names:

        skip_reason, detail = _preflight_skip_reason(home, before_bytes)

    so on the SKIP path the call MUST return a 2-tuple — never a bare string,
    never a tuple-of-1, never ``None`` where two names were promised.
    Regression guard for a flake observed on the C5 PR where one runner
    surfaced ``cannot unpack non-iterable NoneType object`` from this site.
    """
    from yadgar.core.vacuum import _preflight_skip_reason

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        home.mkdir()
        yadgar_home = home / ".yadgar"
        yadgar_home.mkdir()
        empty = Path(td) / "empty"
        empty.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
        monkeypatch.delenv("YADGAR_VACUUM_SIDE_LAUNCHER", raising=False)
        # Force the SKIP path: no launcher resolvable, free space irrelevant.
        fake_runtime = _make_exec(Path(td) / "fake-podman", "#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(fake_runtime))
        monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "example.invalid/backend:test")

        result = _preflight_skip_reason(yadgar_home, before_bytes=10 * 1024 * 1024)

    # Contract: SKIP path returns a 2-tuple of non-empty strings.
    # (Passing path returns None — that is the other valid arm.)
    if result is None:
        pytest.fail("preflight should SKIP in this fixture, got None")
    assert isinstance(result, tuple), f"expected tuple, got {type(result).__name__}: {result!r}"
    assert len(result) == 2, f"expected (reason, detail) 2-tuple, got len={len(result)}"
    skip_reason, detail = result
    assert isinstance(skip_reason, str) and skip_reason, (
        f"skip_reason must be a non-empty string, got {skip_reason!r}"
    )
    assert isinstance(detail, str) and detail, f"detail must be a non-empty string, got {detail!r}"


def test_preflight_skip_reason_defends_against_malformed_inner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C5 follow-up: even if a future refactor of ``_has_side_build_launcher``
    returns a non-tuple, a tuple-of-1, ``None``, or any other payload the
    caller cannot destructure into two names, ``_preflight_skip_reason`` MUST
    still return a real ``(reason, detail)`` tuple on the SKIP path. This is
    the belt for the ``cannot unpack non-iterable NoneType`` flake observed on
    the C5 PR's CI run.
    """
    import yadgar.core.vacuum as vacuum_mod

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        home.mkdir()
        yadgar_home = home / ".yadgar"
        yadgar_home.mkdir()

        # Each malformed payload simulates one shape a sloppy refactor could
        # emit. The outer function must normalise every one of them into a
        # valid (reason, detail) tuple.
        for malformed in (None, ("only_one",), (False, "", ""), ("truthy", "detail-ok")):
            monkeypatch.setattr(vacuum_mod, "_has_side_build_launcher", lambda m=malformed: m)
            result = vacuum_mod._preflight_skip_reason(yadgar_home, before_bytes=10 * 1024 * 1024)
            assert result is not None, f"got None for malformed inner payload {malformed!r}"
            assert isinstance(result, tuple) and len(result) == 2, (
                f"expected 2-tuple for inner={malformed!r}, got {result!r}"
            )
            reason, detail = result
            assert isinstance(reason, str) and reason, (
                f"reason must be non-empty str for inner={malformed!r}, got {reason!r}"
            )
            assert isinstance(detail, str) and detail, (
                f"detail must be non-empty str for inner={malformed!r}, got {detail!r}"
            )
