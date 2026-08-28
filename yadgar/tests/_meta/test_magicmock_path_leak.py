"""Task 412 — a stringified MagicMock must never become a real DB path.

The suite was creating a live SurrealKV store INSIDE the repo working tree::

    MagicMock/Settings().DB_PATH/125435063995424/{clog,manifest}/...

Mechanism, in two steps:

  1. ``unittest.mock`` implements ``__fspath__``, and its default return value
     is NOT the ``<MagicMock name=... id=...>`` repr — it is
     ``"MagicMock/<mock name>/<id(mock)>"``.  So ``Path(some_magicmock)`` is a
     silent success that yields a three-segment RELATIVE path, and the numeric
     leaf is ``id()`` of the mock object.
  2. ``yadgar/core/cli/stats.py`` does ``settings = Settings()`` and then
     ``Path(args.db_path or settings.DB_PATH)``, and opens
     ``Surreal(f"surrealkv://{db_path}")``.  A test that patches ``Settings``
     but fails to intercept ``Surreal`` therefore opens a real store at that
     relative path — i.e. under whatever cwd pytest was started from.

The producing test patched ``yadgar._shared.storage.Surreal`` and a
``sys.modules`` entry for that module, but ``_run_db_path`` imports the client
as ``from surrealdb import Surreal``, so neither patch was on the path.

Adding ``MagicMock/`` to ``.gitignore`` hid the symptom; this pins the cause.

Shape note: the check runs a CHILD pytest with ``cwd`` set to a scratch dir
rather than asserting on the real repo root.  Two reasons — a repo-root
``exists()`` assertion goes red for pre-existing debris that nobody just
created, and in-process it would be order-dependent (green forever if it
collects before the leaking test).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

# _meta/ → tests/ → yadgar/ → repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The node ids that actually produced the store. Targeted rather than
# whole-suite so this test cannot recurse into itself and stays seconds-cheap.
_LEAK_SUSPECTS = ("yadgar/tests/core/test_cli_stats_module.py::TestCmdStatsResolvesIdentityFirst",)


def _run_child_pytest(target: str, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("PYTEST_XDIST_WORKER", None)
    env["TMPDIR"] = str(cwd)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_REPO_ROOT / target.split("::")[0]) + "::" + target.split("::", 1)[1],
            "--no-header",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--override-ini=addopts=",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(cwd),
        env=env,
    )


class TestMockFspathMechanism:
    """Pins the surprise that makes this class of bug possible at all."""

    def test_fspath_of_a_magicmock_is_a_relative_three_segment_path(self):
        m = MagicMock(name="Settings")
        fs = os.fspath(m().DB_PATH)

        assert fs.startswith("MagicMock/Settings().DB_PATH/"), fs
        assert not Path(fs).is_absolute()
        # str() is the repr and contains no separator — which is exactly why a
        # reviewer reading `str(settings.DB_PATH)` does not see a path here.
        assert "/" not in str(m().DB_PATH)


class TestNoMagicMockStoreEscapesIntoTheWorkingTree:
    def test_stats_cli_tests_create_no_magicmock_path_under_cwd(self, tmp_path):
        result = _run_child_pytest(_LEAK_SUSPECTS[0], tmp_path)

        assert result.returncode == 0, f"child pytest failed:\n{result.stdout}\n{result.stderr}"
        leaked = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.glob("MagicMock*"))
        assert not leaked, (
            "a stringified MagicMock reached a real DB path — the child pytest "
            f"created {leaked} under its working directory"
        )

    def test_repo_root_is_clean_of_committed_magicmock_paths(self):
        """The tree itself must never carry one of these under version control."""
        tracked = subprocess.run(
            ["git", "ls-files", "MagicMock*"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert tracked.stdout.strip() == "", (
            f"MagicMock debris is tracked in git: {tracked.stdout.strip()!r}"
        )
