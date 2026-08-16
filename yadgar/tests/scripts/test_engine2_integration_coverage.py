"""Task #77 — every ``engine2_mariadb`` file must be wired into the CI job that runs it.

WHY THIS EXISTS
----------------
``test-engine2-integration`` (``.github/workflows/ci-pr.yml`` /
``.forgejo/workflows/ci-pr.yaml``) is the ONLY CI job that ever selects
``-m integration`` against a real MariaDB. Its file list is a hand-maintained
``run:`` block — a plain list of paths, edited by hand, with nothing checking
it against the filesystem. That is exactly how two things happened silently:

  * ``test_cross_engine_invariants.py`` shipped carrying
    ``pytest.mark.xdist_group("engine2_mariadb")`` — the same tag as its four
    siblings that ARE in the job's list — and was never added to the list
    itself. Its own docstring says so: "this file is not in the CI job's file
    list, so it carried both defects latently after its three siblings were
    fixed." A file that documents its own invisibility is the strongest
    possible evidence the gate needs to exist.
  * Two brand-new files (``test_task_list_status_filter.py`` — proves the
    4078 ROW-constructor fix; ``test_task_write_clear_columns.py`` — proves
    ``plan_path``/``body_slug`` clear to real SQL NULL) landed carrying the
    ``integration`` marker, which every OTHER CI job's
    ``-m 'not integration and not e2e and not perf'`` addopts excludes. A
    marker-deselected test is never reported SKIPPED (skip and deselect are
    different pytest outcomes), so ``scripts/check_skip_inventory.py`` —
    which parses ``pytest -rs`` output for literal ``SKIPPED [N] ...`` lines
    — cannot see it either. The two files passed locally and never executed
    on a single PR.

The fix for the two immediate files is wiring them into the job (done in the
same commit as this test). This test is the fix for the CLASS: it enumerates
every file under ``yadgar/tests/integration/`` that carries
``pytest.mark.xdist_group("engine2_mariadb")`` — the marker that means "this
file needs the job's live MariaDB fixture, not just any integration marker" —
and asserts the job's file list, in BOTH workflow mirrors, is EXACTLY that
set. Zero allowlist: unlike ``test_ci_group_coverage.py``'s
``DELIBERATELY_UNCOVERED`` (a directory can legitimately be out of the matrix
for a stated reason), there is no legitimate reason for a file carrying this
exact xdist_group tag to be absent from the one job whose fixture group it
was written to share. A new omission fails this test, not silently.

WHY STATIC, NOT A COLLECT-ONLY SUBPROCESS
------------------------------------------
Running ``pytest --collect-only`` over the integration files would import
``yadgar.core.server.tools.task`` (a file another train car may be mid-edit
on) transitively through ``test_task_write_clear_columns.py``'s
``_clear_payload`` helper. A repo-guard test that imports a concurrently-
edited module can go red for someone else's in-progress change, which is
exactly the kind of collision this repo's ``isolation: worktree`` dispatch
convention exists to avoid. The marker scan below is pure text (mirrors
``ci_group_manifest.py``'s own stdlib-only, text-sliced approach) and the
workflow parse uses ``ruamel.yaml`` (a declared base dependency, same as
``test_ci_mirror_parity.py`` uses) — no subprocess, no import of the modules
under gate.

NOT covered by ``test_ci_mirror_parity.py``: that guard's projection is job
keys / ``needs`` / ``if`` / ``continue-on-error`` only (see its own
docstring) — it deliberately does not look at step bodies, so the two
mirrors' ``run:`` file lists could silently diverge from each other and
nothing would catch it. This test does.
"""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

from yadgar.tests._paths import REPO_ROOT

INTEGRATION_DIR = REPO_ROOT / "yadgar" / "tests" / "integration"
GITHUB_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-pr.yml"
FORGEJO_WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "ci-pr.yaml"

JOB_NAME = "test-engine2-integration"
STEP_NAME = "Run the engine-#2 integration suite"

_XDIST_GROUP_RE = re.compile(r"""pytest\.mark\.xdist_group\(\s*['"]engine2_mariadb['"]\s*\)""")
_FILE_TOKEN_RE = re.compile(r"yadgar/tests/integration/test_[\w]+\.py")


def _engine2_marked_files() -> set[str]:
    """Basenames of every yadgar/tests/integration/test_*.py carrying the tag.

    Text search, not AST: the tag is always a literal
    ``pytest.mark.xdist_group("engine2_mariadb")`` string in these files (no
    computed marker names anywhere in this test suite), so a substring/regex
    match is exactly as precise as a parse here and needs no import.
    """
    found: set[str] = set()
    for path in sorted(INTEGRATION_DIR.glob("test_*.py")):
        if _XDIST_GROUP_RE.search(path.read_text(encoding="utf-8")):
            found.add(path.name)
    return found


def _job_run_script(workflow_path: Path, job_name: str, step_name: str) -> str:
    """Return the named step's ``run:`` block text for *job_name* in *workflow_path*."""
    doc = YAML(typ="safe").load(workflow_path.read_text(encoding="utf-8"))
    jobs = doc.get("jobs", {})
    assert job_name in jobs, f"{workflow_path}: no `{job_name}:` job found"
    steps = jobs[job_name].get("steps", [])
    matches = [s for s in steps if s.get("name") == step_name]
    assert matches, f"{workflow_path}: job `{job_name}` has no step named {step_name!r}"
    assert len(matches) == 1, (
        f"{workflow_path}: job `{job_name}` has {len(matches)} steps named "
        f"{step_name!r} — expected exactly one"
    )
    run = matches[0].get("run")
    assert isinstance(run, str) and run.strip(), (
        f"{workflow_path}: step {step_name!r} in `{job_name}` has no `run:` script"
    )
    return run


def _job_file_list(workflow_path: Path) -> set[str]:
    """Basenames of the ``yadgar/tests/integration/test_*.py`` paths the job's run script names."""
    script = _job_run_script(workflow_path, JOB_NAME, STEP_NAME)
    tokens = _FILE_TOKEN_RE.findall(script)
    assert tokens, (
        f"{workflow_path}: step {STEP_NAME!r} names no "
        "yadgar/tests/integration/test_*.py path at all — the regex or the "
        "step body drifted"
    )
    return {Path(t).name for t in tokens}


class TestEngine2FileListCoversTheMarker:
    def test_github_job_runs_every_engine2_marked_file(self):
        marked = _engine2_marked_files()
        listed = _job_file_list(GITHUB_WORKFLOW)
        missing = marked - listed
        assert not missing, (
            f'{sorted(missing)} carry pytest.mark.xdist_group("engine2_mariadb") '
            f"but are NOT in `{JOB_NAME}`'s file list in {GITHUB_WORKFLOW} — "
            "these tests never execute on any PR (task #77: marker-deselected "
            "tests are invisible to check_skip_inventory.py, which only parses "
            "`pytest -rs` SKIPPED lines, never DESELECTED ones). Add the "
            f"file(s) to the `{STEP_NAME}` step's pytest invocation."
        )

    def test_github_job_lists_no_file_that_lost_the_marker(self):
        """The inverse direction: a listed file whose marker was removed/renamed
        would silently run under a stale expectation — catch that too."""
        marked = _engine2_marked_files()
        listed = _job_file_list(GITHUB_WORKFLOW)
        extra = listed - marked
        assert not extra, (
            f"{sorted(extra)} are in `{JOB_NAME}`'s file list in {GITHUB_WORKFLOW} "
            'but no longer carry pytest.mark.xdist_group("engine2_mariadb") — '
            "the tag was removed/renamed without updating the job, or the file "
            "no longer exists."
        )

    def test_forgejo_mirror_matches_the_github_file_list(self):
        """test_ci_mirror_parity.py's projection is job keys/needs/if only — it
        does not look at step bodies, so the two mirrors' file lists could
        silently diverge from each other. This is the check that would catch it."""
        github_listed = _job_file_list(GITHUB_WORKFLOW)
        forgejo_listed = _job_file_list(FORGEJO_WORKFLOW)
        assert github_listed == forgejo_listed, (
            f"`{JOB_NAME}`'s file list diverges between CI mirrors.\n"
            f"  only in .github  : {sorted(github_listed - forgejo_listed)}\n"
            f"  only in .forgejo : {sorted(forgejo_listed - github_listed)}\n"
            "Both mirrors are canonical (ADR per test_ci_mirror_parity.py) — a "
            "file added to one job's list only silently never runs on the other."
        )

    def test_the_marker_scan_itself_finds_something(self):
        """A regex that stopped matching (e.g. the tag string changed shape)
        would make every assertion above pass vacuously — empty minus empty."""
        assert _engine2_marked_files(), (
            "no yadgar/tests/integration/test_*.py file matched the "
            "engine2_mariadb xdist_group regex — the scan is broken, not the "
            "fact that nothing needs a live MariaDB."
        )
