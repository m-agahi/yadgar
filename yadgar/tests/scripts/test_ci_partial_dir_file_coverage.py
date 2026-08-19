"""Task 156 / Car A3 — a partially-wired DELIBERATELY_UNCOVERED dir must be file-complete.

WHY THIS EXISTS
----------------
``test_ci_group_coverage.py`` checks CI coverage at DIRECTORY granularity: a
directory under ``yadgar/tests/`` is either selected by a matrix group, or it
is named in ``DELIBERATELY_UNCOVERED`` with a reason. That check is correct
as far as it goes, but it trusts the reason string — it never verifies the
reason is actually true.

It was not. ``yadgar/tests/integration/`` carries this entry:

    "Run by the dedicated test-engine2-integration job (MariaDB arms, bare
    host + container runtime) and by viz-tests' Layer 2 (Playwright smoke)."

That claim was true for 9 of ``integration/``'s files and its ``viz/``
subdirectory, and false for two: ``test_vacuum_e2e.py`` and
``test_conftest_backend_pin.py`` sat in the same directory as those 9 wired
siblings, carrying no CI wiring at all, in either mirror, since the day they
were written. Nothing caught it because ``DELIBERATELY_UNCOVERED`` is a
directory-level allowlist — once a directory is on it, every file inside is
invisible to ``test_ci_group_coverage.py`` by construction.

THE RULE THIS FILE ENFORCES
-----------------------------
A ``DELIBERATELY_UNCOVERED`` directory is honest under exactly one of two
shapes:

  1. **Zero** of its files are referenced anywhere in ci-pr.yml (either
     mirror) — a genuine blanket exemption, e.g. ``yadgar/tests/e2e/``, which
     `make e2e` runs locally and ci-pr.yml never touches at all.
  2. **Every** ``test_*.py`` file under it is referenced by some job in BOTH
     workflow mirrors — "referenced" meaning its own path, or a directory
     prefix of it (e.g. ``yadgar/tests/integration/viz/``), appears in some
     job's ``run:`` script body or matrix ``paths:`` value.

A directory with SOME files referenced and others not is neither — it is the
shape this car found, and it fails loudly here instead of vanishing back into
an allowlist.

WHY PARSE ``run:`` BODIES VIA ruamel.yaml, NOT RAW FILE TEXT
----------------------------------------------------------------
The comment block above ``test-engine2-integration:`` in ci-pr.yml says so
explicitly: it deliberately avoids writing a bare ``yadgar/tests/<dir>/``
path or a quoted marker in its OWN prose, because
``scripts/check_ci_local_parity.py`` discovers job shape by raw-text
scanning and a stray match in a comment would mis-attribute the job. Parsing
the YAML and reading only ``steps[].run`` (plus matrix ``paths:``, the same
projection ``ci_group_manifest`` uses) sidesteps that: prose comments are
never part of a step's ``run:`` value.

NOT covered here: matrix-group directories (``test_ci_group_coverage.py``
already guarantees whole-directory coverage for those — passing the
directory as a pytest path argument trivially covers every file under it).
This file only tightens the ``DELIBERATELY_UNCOVERED`` side, which is the
side that lied.
"""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

from yadgar.tests._paths import REPO_ROOT
from yadgar.tests.scripts.test_ci_group_coverage import DELIBERATELY_UNCOVERED, TESTS_ROOT

GITHUB_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-pr.yml"
FORGEJO_WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "ci-pr.yaml"


def _all_step_run_text(workflow_path: Path) -> str:
    """Concatenate every job's every step's ``run:`` body plus matrix ``paths:`` tokens.

    Deliberately narrow to these two YAML fields — see the module docstring's
    "WHY PARSE run: BODIES" section for why raw file text is unsafe here.
    """
    doc = YAML(typ="safe").load(workflow_path.read_text(encoding="utf-8"))
    jobs = doc.get("jobs", {}) or {}
    parts: list[str] = []
    for job in jobs.values():
        for step in job.get("steps", []) or []:
            run = step.get("run")
            if isinstance(run, str):
                parts.append(run)
        strategy = job.get("strategy") or {}
        matrix = strategy.get("matrix") or {}
        for entry in matrix.get("include", []) or []:
            paths = entry.get("paths")
            if isinstance(paths, str):
                parts.append(paths)
    return "\n".join(parts)


# A whitespace-delimited path TOKEN, not a raw substring search. Substring
# containment alone is unsound here: "yadgar/tests/integration/" is a
# substring of "yadgar/tests/integration/test_mariadb_migrations.py", so a
# naive substring check on a shallow directory prefix would treat EVERY file
# under a directory as "referenced" the moment ANY ONE file in it is named —
# exactly the false-negative that let test_vacuum_e2e.py and
# test_conftest_backend_pin.py hide next to 9 correctly-wired siblings. Tokens
# are extracted whitespace-bounded (this repo's own convention: every
# `paths:`/`run:` path argument, file or directory, is one whitespace- or
# line-continuation-delimited token — directories always carry a trailing
# `/`, files always end in `.py`) and trailing line-continuation/punctuation
# characters are stripped before comparison.
_TOKEN_RE = re.compile(r"yadgar/tests/\S+")
_TOKEN_TRIM_RE = re.compile(r"[\\.,;:'\"]+$")


def _extract_tokens(run_text: str) -> set[str]:
    tokens: set[str] = set()
    for m in _TOKEN_RE.finditer(run_text):
        tok = _TOKEN_TRIM_RE.sub("", m.group(0))
        if tok.endswith(".py") or tok.endswith("/"):
            tokens.add(tok)
    return tokens


def _is_referenced(rel_posix: str, tokens: set[str]) -> bool:
    """True if *rel_posix* is exactly a token, or under a directory token."""
    for tok in tokens:
        if tok == rel_posix:
            return True
        if tok.endswith("/") and rel_posix.startswith(tok):
            return True
    return False


def _uncovered_dir_files() -> dict[str, list[Path]]:
    """test_*.py files under each DELIBERATELY_UNCOVERED directory, by name."""
    out: dict[str, list[Path]] = {}
    for name in DELIBERATELY_UNCOVERED:
        d = TESTS_ROOT / name
        if d.is_dir():
            out[name] = sorted(d.rglob("test_*.py"))
    return out


class TestPartiallyWiredUncoveredDirsAreFileComplete:
    def test_no_file_silently_missing_from_a_partially_wired_dir(self):
        tokens_gh = _extract_tokens(_all_step_run_text(GITHUB_WORKFLOW))
        tokens_fj = _extract_tokens(_all_step_run_text(FORGEJO_WORKFLOW))

        missing: dict[str, list[str]] = {}
        for name, files in _uncovered_dir_files().items():
            if not files:
                continue
            rels = [f.relative_to(REPO_ROOT).as_posix() for f in files]
            referenced_gh = {r for r in rels if _is_referenced(r, tokens_gh)}
            referenced_fj = {r for r in rels if _is_referenced(r, tokens_fj)}

            if not referenced_gh and not referenced_fj:
                # Honest blanket exemption (e.g. e2e/ — `make e2e` only,
                # ci-pr.yml never touches it). Not this test's concern.
                continue

            unreferenced = (set(rels) - referenced_gh) | (set(rels) - referenced_fj)
            if unreferenced:
                missing[name] = sorted(unreferenced)

        assert not missing, (
            f"{missing}\n"
            "these files sit in a DELIBERATELY_UNCOVERED directory that ALSO "
            "has at least one sibling referenced by a ci-pr.yml job in some "
            "mirror — so the directory is not an honest blanket exemption, "
            "and every file in it must be individually accounted for. A CI "
            "job must reference each listed file, in BOTH workflow mirrors, "
            "or it never runs on any PR (task 156 / Car A3: exactly this "
            "shape, for test_vacuum_e2e.py and test_conftest_backend_pin.py, "
            "went undetected for months)."
        )

    def test_scan_finds_something(self):
        """An empty/broken parse would make the assertion above pass vacuously
        — nothing to compare against is not evidence of coverage."""
        run_text_gh = _all_step_run_text(GITHUB_WORKFLOW)
        run_text_fj = _all_step_run_text(FORGEJO_WORKFLOW)
        assert "yadgar/tests/" in run_text_gh, (
            f"{GITHUB_WORKFLOW}: no yadgar/tests/ path token found in any job's "
            "run: body or matrix paths — the scan is broken, not the fact that "
            "CI references nothing."
        )
        assert "yadgar/tests/" in run_text_fj, (
            f"{FORGEJO_WORKFLOW}: no yadgar/tests/ path token found in any "
            "job's run: body or matrix paths — the scan is broken, not the "
            "fact that CI references nothing."
        )

    def test_deliberately_uncovered_dirs_have_at_least_one_file(self):
        """A DELIBERATELY_UNCOVERED entry naming a dir with zero test_*.py
        files would make the main assertion vacuous for that entry — the same
        vacuity guard test_ci_group_coverage.py runs for the dir's existence,
        extended to "has tests"."""
        empty = [name for name, files in _uncovered_dir_files().items() if not files]
        assert not empty, (
            f"DELIBERATELY_UNCOVERED names {empty} but no test_*.py file exists "
            "under them — stale entry, or the scan pattern drifted."
        )
