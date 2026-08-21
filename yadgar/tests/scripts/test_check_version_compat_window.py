"""Unit tests for scripts/check_version_compat_window.py.

Task 173. Pins three things:

1. The pure ``check()`` function against injected server.json / version_compat.json
   text — a permanent regression suite independent of whatever the real
   sidecar says today.
2. Agreement between the hook's independent, stdlib-only comparator and the
   production ``yadgar._shared.version_compat`` module the ``/health``
   handshake actually calls, over a shared table of cases — the hook is
   deliberately a separate stdlib-only copy (see the script's module
   docstring for why it doesn't import the yadgar package), so nothing else
   catches the two silently drifting apart.
3. ``check()`` wired to the REAL ``server.json`` + the REAL
   ``yadgar/_shared/version_compat.json`` on disk. This is the guard the
   pre-commit hook actually runs — it must currently PASS (the JSON gets
   bumped as part of this same car so the tree it ships on is green), and
   its whole purpose is to fail loudly the next time these two files drift
   apart, exactly as they did for 5 releases before task 173.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the hook from scripts/ — not a package, use direct path injection.
# Mirrors yadgar/tests/scripts/test_check_backend_bump.py.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_version_compat_window import _in_range, check  # noqa: E402

from yadgar._shared.version_compat import (  # noqa: E402
    _in_range as _production_in_range,
)
from yadgar._shared.version_compat import (  # noqa: E402
    backend_compatible,
    core_compatible,
)

_REPO_ROOT = Path(__file__).parent.parent.parent.parent

_COMPAT_JSON = json.dumps(
    {
        "core": {"min": "5.170.0", "max": "5.183.1"},
        "backend": {"min": "5.65.0", "max": "5.75.0"},
    }
)


def _server_json(core_version: str, backend_version: str) -> str:
    return json.dumps({"version": core_version, "backend_version": backend_version})


# ---------------------------------------------------------------------------
# Pure check() against injected server.json / version_compat.json text
# ---------------------------------------------------------------------------


class TestWithinWindow:
    def test_both_tracks_in_range(self) -> None:
        ok, msg = check(_server_json("5.180.0", "5.70.0"), _COMPAT_JSON)
        assert ok is True
        assert "5.180.0" in msg
        assert "5.70.0" in msg

    def test_exactly_at_max_boundary(self) -> None:
        ok, _msg = check(_server_json("5.183.1", "5.75.0"), _COMPAT_JSON)
        assert ok is True

    def test_exactly_at_min_boundary(self) -> None:
        ok, _msg = check(_server_json("5.170.0", "5.65.0"), _COMPAT_JSON)
        assert ok is True


class TestOutsideWindow:
    def test_core_too_new_reproduces_task_173(self) -> None:
        """THE task-173 bug: core shipped past the declared max."""
        ok, msg = check(_server_json("5.188.0", "5.75.0"), _COMPAT_JSON)
        assert ok is False
        assert "5.188.0" in msg
        assert "version_compat.json" in msg

    def test_backend_too_new_reproduces_task_173(self) -> None:
        ok, msg = check(_server_json("5.183.1", "5.80.0"), _COMPAT_JSON)
        assert ok is False
        assert "5.80.0" in msg

    def test_both_too_new(self) -> None:
        """Exactly today's real-world state before the bump: both tracks stale."""
        ok, msg = check(_server_json("5.188.0", "5.80.0"), _COMPAT_JSON)
        assert ok is False
        assert "5.188.0" in msg
        assert "5.80.0" in msg

    def test_core_too_old(self) -> None:
        ok, msg = check(_server_json("5.100.0", "5.70.0"), _COMPAT_JSON)
        assert ok is False
        assert "5.100.0" in msg


class TestMalformedInput:
    def test_invalid_server_json(self) -> None:
        ok, msg = check("{not json", _COMPAT_JSON)
        assert ok is False
        assert "server.json" in msg
        assert "not valid JSON" in msg

    def test_invalid_compat_json(self) -> None:
        ok, msg = check(_server_json("5.180.0", "5.70.0"), "{not json")
        assert ok is False
        assert "version_compat.json" in msg
        assert "not valid JSON" in msg

    def test_missing_version_fields_are_unverifiable_not_a_crash(self) -> None:
        # Matches version_compat.py's own contract: an empty/missing version
        # is "unverifiable", not "incompatible" — a fresh install with no
        # version fields yet must not be refused. This guard must not crash
        # on that shape either.
        ok, _msg = check("{}", _COMPAT_JSON)
        assert ok is True


# ---------------------------------------------------------------------------
# Agreement with the production comparator (yadgar._shared.version_compat).
#
# The hook is a deliberate, independent stdlib-only copy of the small
# parse/range logic (see the script's module docstring for why it can't
# just import the production module). Nothing else would catch the two
# silently diverging, so pin agreement directly over a shared table.
# ---------------------------------------------------------------------------

_AGREEMENT_CASES: list[tuple[str, dict[str, str]]] = [
    ("5.180.0", {"min": "5.170.0", "max": "5.183.1"}),
    ("5.183.1", {"min": "5.170.0", "max": "5.183.1"}),  # exact max
    ("5.170.0", {"min": "5.170.0", "max": "5.183.1"}),  # exact min
    ("5.188.0", {"min": "5.170.0", "max": "5.183.1"}),  # too new
    ("5.100.0", {"min": "5.170.0", "max": "5.183.1"}),  # too old
    ("5.183.2", {"min": "5.170.0", "max": "5.183.1"}),  # patch past max
    ("unknown", {"min": "5.170.0", "max": "5.183.1"}),  # unverifiable
    ("", {"min": "5.170.0", "max": "5.183.1"}),  # unverifiable
    ("5.75.0", {"min": "5.65.0", "max": "5.75.0"}),
    ("5.80.0", {"min": "5.65.0", "max": "5.75.0"}),
]


class TestAgreesWithProductionComparator:
    def test_in_range_matches_production_over_case_table(self) -> None:
        for version, bounds in _AGREEMENT_CASES:
            hook_result = _in_range(version, bounds)
            prod_result = _production_in_range(version, bounds)
            assert hook_result == prod_result, (
                f"disagreement for version={version!r} bounds={bounds!r}: "
                f"hook={hook_result} production={prod_result}"
            )

    def test_agrees_with_core_compatible_and_backend_compatible(self) -> None:
        # core_compatible/backend_compatible read the REAL sidecar's _BOUNDS
        # at import time, so compare against the hook using that same
        # real-file bounds (via check()'s own real-file wiring below) rather
        # than an injected table here — see TestWiredToRealFiles.
        real_compat_text = (_REPO_ROOT / "yadgar" / "_shared" / "version_compat.json").read_text()
        real_compat_data = json.loads(real_compat_text)
        real_server_text = (_REPO_ROOT / "server.json").read_text()
        real_server_data = json.loads(real_server_text)

        core_version = real_server_data["version"]
        backend_version = real_server_data["backend_version"]

        assert _in_range(core_version, real_compat_data["core"]) == core_compatible(core_version)
        assert _in_range(backend_version, real_compat_data["backend"]) == backend_compatible(
            backend_version
        )


# ---------------------------------------------------------------------------
# Wired to reality: the REAL server.json + REAL version_compat.json.
#
# This is the actual pre-commit gate. It must pass on the committed tree —
# task 173 bumps version_compat.json's bounds in this same car precisely so
# this test (and the hook it mirrors) goes green. Its job going forward is
# to catch the NEXT time server.json's shipped pair drifts outside the
# declared window without version_compat.json being bumped alongside it.
# ---------------------------------------------------------------------------


class TestWiredToRealFiles:
    def test_real_server_json_within_real_declared_window(self) -> None:
        server_json_text = (_REPO_ROOT / "server.json").read_text()
        compat_json_text = (_REPO_ROOT / "yadgar" / "_shared" / "version_compat.json").read_text()
        ok, msg = check(server_json_text, compat_json_text)
        assert ok is True, msg
