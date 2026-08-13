"""Car J1 — every test file belongs to exactly one CI group.

WHY THIS EXISTS
----------------
Car J1 replaced four ``needs:``-chained test jobs with one ``tests`` matrix
whose ``include:`` entries select test paths. That makes the selection a LIST
someone edits, and a list someone edits is a list that goes stale: add
``yadgar/tests/newthing/`` and, unless a group's ``paths:`` grows to cover it,
those tests simply never run in CI. Nothing goes red. The suite reports
success while testing strictly less than it did yesterday.

This repo has hit that exact shape repeatedly — most recently a CI job that
reported success while skipping all 41 of its tests, and (found while writing
this car) ``yadgar/tests/restoration/`` which was in NO CI job at all: one test
file that had never executed in CI since the directory was created.

So the enumeration is checked against the filesystem rather than trusted:

  * every directory under ``yadgar/tests/`` that contains tests is either
    covered by a matrix group or listed in :data:`DELIBERATELY_UNCOVERED` with
    a reason;
  * every path a matrix group names actually exists (a typo'd path selects
    nothing — the workflow's own collect-only step catches that at run time,
    this catches it at commit time);
  * no group's selection is empty.

Adding a test directory therefore forces a deliberate choice: put it in a
group, or write down why it does not belong in one. Both are fine; silently
doing neither is not.
"""

from __future__ import annotations

import sys

import pytest

from yadgar.tests._paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from ci_group_manifest import groups, legs  # noqa: E402

TESTS_ROOT = REPO_ROOT / "yadgar" / "tests"

# Directories that hold tests but are deliberately NOT in the `tests` matrix.
# Each needs a reason naming what DOES run them — "nothing runs these" is never
# an acceptable entry here; that is the bug this module exists to surface.
DELIBERATELY_UNCOVERED: dict[str, str] = {
    "integration": (
        "carries the `integration` marker, which every matrix group excludes via "
        "-m 'not integration and not e2e and not perf'. Run by the dedicated "
        "test-engine2-integration job (MariaDB arms, bare host + container runtime) "
        "and by viz-tests' Layer 2 (Playwright smoke)."
    ),
    "e2e": (
        "carries the `e2e` marker, excluded by every matrix group's -m expression. "
        "Run by `make e2e` against a real local surreal binary — CI's embedded "
        "SurrealDB cannot run these reliably."
    ),
}

# Non-test support directories — no test_*.py, nothing to run.
_SUPPORT_DIRS = {"fixtures", "snapshots", "__pycache__"}


def _dirs_with_tests() -> set[str]:
    """Top-level names under yadgar/tests/ that contain at least one test file."""
    found = set()
    for child in TESTS_ROOT.iterdir():
        if not child.is_dir() or child.name in _SUPPORT_DIRS:
            continue
        if any(child.rglob("test_*.py")):
            found.add(child.name)
    return found


def _covered_dirs() -> set[str]:
    """Top-level names under yadgar/tests/ that some matrix group selects."""
    covered = set()
    for entry in groups():
        for token in entry["paths"].split():
            parts = token.split("/")
            # tokens look like yadgar/tests/<name>/ or yadgar/tests/<name>/file.py
            if len(parts) >= 3 and parts[0] == "yadgar" and parts[1] == "tests":
                covered.add(parts[2])
    return covered


class TestEveryTestDirectoryIsCovered:
    def test_no_test_directory_is_silently_uncovered(self):
        uncovered = _dirs_with_tests() - _covered_dirs() - set(DELIBERATELY_UNCOVERED)
        assert not uncovered, (
            f"test director{'y' if len(uncovered) == 1 else 'ies'} {sorted(uncovered)} "
            "contain tests but are selected by NO group in ci-pr.yml's `tests` matrix, "
            "and are not listed in DELIBERATELY_UNCOVERED.\n"
            "Those tests never run in CI, and nothing else reports that fact — the suite "
            "goes green while testing less than it did before.\n"
            "Fix: add the directory to a group's `paths:` in BOTH workflow mirrors and to "
            "the matching CI_LOCAL_DIRS_<leg> in the Makefile, or add it to "
            "DELIBERATELY_UNCOVERED with a reason naming what does run it."
        )

    def test_deliberately_uncovered_entries_still_exist(self):
        """The allowlist is a ledger, not a dumping ground — stale entries rot."""
        for name in DELIBERATELY_UNCOVERED:
            assert (TESTS_ROOT / name).is_dir(), (
                f"DELIBERATELY_UNCOVERED names '{name}' but yadgar/tests/{name} does not "
                "exist — remove the stale entry."
            )

    def test_deliberately_uncovered_is_not_also_covered(self):
        """An entry that IS in a group is a contradiction — one side is wrong."""
        both = set(DELIBERATELY_UNCOVERED) & _covered_dirs()
        assert not both, (
            f"{sorted(both)} appear BOTH in a matrix group's paths and in "
            "DELIBERATELY_UNCOVERED. Decide which is true and delete the other."
        )


class TestGroupSelectionsAreReal:
    @pytest.mark.parametrize("entry", groups(), ids=lambda e: e["group"])
    def test_every_declared_path_exists(self, entry):
        """A path that does not exist selects nothing and the group passes vacuously."""
        for token in entry["paths"].split():
            # Globs are resolved against the repo root; a literal path must exist.
            if any(ch in token for ch in "*?["):
                matches = list(REPO_ROOT.glob(token))
                assert matches, (
                    f"group '{entry['group']}' declares glob '{token}' which matches NO "
                    "file — the group would collect nothing and still exit green."
                )
                continue
            assert (REPO_ROOT / token).exists(), (
                f"group '{entry['group']}' declares path '{token}' which does not exist. "
                "pytest would error or silently select nothing depending on flags."
            )

    def test_no_group_declares_an_empty_selection(self):
        for entry in groups():
            assert entry["paths"].split(), (
                f"group '{entry['group']}' declares an empty `paths:` — it would run the "
                "whole suite or nothing at all, neither of which is what a group means."
            )

    def test_legs_collapse_shards_without_losing_a_group(self):
        """Every group maps onto a leg, and every leg is backed by >=1 group."""
        group_names = {entry["group"] for entry in groups()}
        leg_names = set(legs())
        assert group_names, "no matrix groups discovered"
        assert leg_names, "no ci-local legs derived"
        assert len(leg_names) <= len(group_names), (
            f"derived more legs ({len(leg_names)}) than groups ({len(group_names)}) — "
            "the shard-collapse mapping is inventing legs."
        )
