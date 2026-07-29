"""Car H2 / defect D4 — CI mirror drift guard + clean-venv wheel-install gate.

Two properties are pinned here.

1. **Mirror parity.** `.github/workflows/*.yml` and `.forgejo/workflows/*.yaml`
   are BOTH canonical (user decision, 2026-07-29) and must stay in sync. A
   byte-identical comparison is impossible — the mirrors carry legitimate,
   deliberate platform differences (see NOT-GUARDED below). So the guard is a
   **structural projection over the `jobs:` mapping only**: job keys, and each
   job's ``needs`` / ``if`` / ``continue-on-error``. That projection is the
   part that decides *what runs and when*, which is exactly the failure mode
   this guard exists to catch: a CI change landing in one mirror only and
   silently doing nothing on the other.

   NOT guarded, deliberately:
     - ``on:``      — genuinely divergent today (see KNOWN_DRIFT_ON_TRIGGERS);
                      also a YAML 1.1/1.2 footgun (PyYAML parses the ``on`` key
                      as boolean ``True``). Out of the projection entirely.
     - ``runs-on``  — ``[self-hosted, linux, x64, yadgar]`` vs ``ubuntu-latest``.
     - ``container``— ``yadgar-ci`` vs ``python:3.14-slim`` (validate only).
     - ``uses:``    — SHA-pinned ``actions/*`` vs ``https://data.forgejo.org/actions/*``.
     - step lists   — GitHub needs a per-job "Trust workspace" step (checkout
                      UID differs from the container user); Forgejo needs
                      explicit runtime-dep installs. Platform, not drift.
     - comments     — the GitHub mirror is far more heavily commented.

   ``ruamel.yaml`` is used rather than PyYAML on purpose: ruamel is a declared
   base dependency (``pyproject.toml`` ``ruamel.yaml>=0.18.0``) whereas PyYAML
   is only a transitive entry in ``uv.lock``, undeclared in ``pyproject.toml``
   — importing it makes a test unrunnable outside the pre-baked CI image.

2. **The clean-venv wheel-install gate exists in both mirrors and blocks.**
   See TestCleanVenvWheelGate for why no other CI job can catch this class.
"""

from __future__ import annotations

import re

import pytest
from ruamel.yaml import YAML

from yadgar.tests._paths import REPO_ROOT

GITHUB_DIR = REPO_ROOT / ".github" / "workflows"
FORGEJO_DIR = REPO_ROOT / ".forgejo" / "workflows"

# The seven workflows that must exist in both mirrors.
WORKFLOWS = [
    "ci-pr",
    "ci-release",
    "eval",
    "mutation-sweep",
    "perf",
    "sdk-js",
    "validate",
]

# ── Known, UNRESOLVED drift ───────────────────────────────────────────────────
# These are real behavioural divergences found when this guard was authored
# (2026-07-29). They are recorded — not silently normalised — so the guard can
# be green today while still failing loudly on any NEW divergence. Each entry
# is pending maintainer adjudication; the correct resolution is to fix the
# mirrors and DELETE the entry, never to add to this list to silence a failure.
#
# `ci-pr.test-gate.needs`: the GitHub mirror's aggregate gate waits on three
# extra jobs (`invariant-checks`, `viz-tests`, `verify-version-bump`) that the
# Forgejo mirror's gate does not — so on Forgejo those three can fail without
# failing the gate.
KNOWN_DRIFT_NEEDS: dict[tuple[str, str], tuple[list[str], list[str]]] = {
    ("ci-pr", "test-gate"): (
        [
            "test-fast",
            "test-shared",
            "test-backend",
            "test-core",
            "test-perf",
            "check-skip-inventory",
            "invariant-checks",
            "viz-tests",
            "verify-version-bump",
        ],
        [
            "test-fast",
            "test-shared",
            "test-backend",
            "test-core",
            "test-perf",
            "check-skip-inventory",
        ],
    ),
}

# `validate.on.pull_request.types`: the GitHub mirror carries a documented
# 2026-07-27 fix (narrowed to `[opened, synchronize]` after `reopened`/`edited`
# caused duplicate runs) that the Forgejo mirror never received. Recorded for
# adjudication only — `on:` is outside the guarded projection.
KNOWN_DRIFT_ON_TRIGGERS = (
    "validate: .github types=[opened, synchronize] vs "
    ".forgejo types=[opened, synchronize, reopened, edited]"
)


def _load(path):
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def _jobs(mirror: str, name: str) -> dict:
    path = (GITHUB_DIR / f"{name}.yml") if mirror == "github" else (FORGEJO_DIR / f"{name}.yaml")
    doc = _load(path)
    assert isinstance(doc, dict) and "jobs" in doc, f"{path} has no jobs: mapping"
    return doc["jobs"]


class TestMirrorParity:
    """Both workflow mirrors are canonical and must not structurally diverge."""

    def test_both_mirrors_hold_the_same_workflow_set(self):
        gh = {p.stem for p in GITHUB_DIR.glob("*.yml")}
        fj = {p.stem for p in FORGEJO_DIR.glob("*.yaml")}
        assert gh == fj == set(WORKFLOWS), (
            "CI mirrors hold different workflow sets.\n"
            f"  .github/workflows/*.yml   : {sorted(gh)}\n"
            f"  .forgejo/workflows/*.yaml : {sorted(fj)}\n"
            f"  expected                  : {sorted(WORKFLOWS)}\n"
            "Both mirrors are canonical — add the workflow to both, and update "
            "WORKFLOWS in this test."
        )

    @pytest.mark.parametrize("name", WORKFLOWS)
    def test_job_keys_match(self, name):
        """A job present in one mirror only is the exact silent-no-op failure mode."""
        gh, fj = set(_jobs("github", name)), set(_jobs("forgejo", name))
        assert gh == fj, (
            f"{name}: job keys diverge between CI mirrors.\n"
            f"  only in .github  : {sorted(gh - fj)}\n"
            f"  only in .forgejo : {sorted(fj - gh)}\n"
            "Both mirrors are canonical — a job added to one only will silently "
            "never run on the other."
        )

    @pytest.mark.parametrize("name", WORKFLOWS)
    def test_job_needs_match(self, name):
        gh, fj = _jobs("github", name), _jobs("forgejo", name)
        for job in sorted(set(gh) & set(fj)):
            g, f = gh[job].get("needs"), fj[job].get("needs")
            g = [g] if isinstance(g, str) else g
            f = [f] if isinstance(f, str) else f
            if (name, job) in KNOWN_DRIFT_NEEDS:
                exp_g, exp_f = KNOWN_DRIFT_NEEDS[(name, job)]
                assert g == exp_g and f == exp_f, (
                    f"{name}.{job}: known-drift entry is stale — the mirrors changed.\n"
                    f"  .github  now: {g}\n  .forgejo now: {f}\n"
                    "If the drift was fixed, DELETE this KNOWN_DRIFT_NEEDS entry. "
                    "Do not edit it to match the new values."
                )
                continue
            assert g == f, (
                f"{name}.{job}: `needs` diverges between CI mirrors.\n"
                f"  .github  : {g}\n  .forgejo : {f}\n"
                "The dependency graph decides what gates what — keep both in sync."
            )

    @pytest.mark.parametrize("name", WORKFLOWS)
    def test_job_conditions_match(self, name):
        """`if:` and `continue-on-error:` decide whether a job runs / blocks."""
        gh, fj = _jobs("github", name), _jobs("forgejo", name)
        for job in sorted(set(gh) & set(fj)):
            for attr in ("if", "continue-on-error"):
                g, f = gh[job].get(attr), fj[job].get(attr)
                assert g == f, (
                    f"{name}.{job}: `{attr}` diverges between CI mirrors.\n"
                    f"  .github  : {g!r}\n  .forgejo : {f!r}\n"
                    "Both mirrors are canonical — apply the change to both."
                )

    def test_known_drift_list_has_not_grown(self):
        """The allowlist is a debt ledger, not an escape hatch."""
        assert set(KNOWN_DRIFT_NEEDS) == {("ci-pr", "test-gate")}, (
            "KNOWN_DRIFT_NEEDS grew. It records drift pending maintainer "
            "adjudication — new divergence must be FIXED in the mirrors, not "
            "added here to silence this guard."
        )


class TestCleanVenvWheelGate:
    """The release pipeline must install the built wheel into an ISOLATED venv.

    Why this cannot be covered by any existing job: every test job installs
    with ``uv pip install --system --no-deps -e .`` against dependencies baked
    into the ``yadgar-ci`` image from ``uv.lock`` (ADR-0089; ``Dockerfile.ci``
    installs the full ``[test,ml]`` extras). So no test job ever resolves
    ``[project.dependencies]`` — a dep present in the image but missing from
    ``pyproject.toml`` is invisible everywhere.

    ``build-sbom``'s own ``pip install <wheel>[sbom]`` does not close this
    either: it runs in that same pre-baked image against *system*
    site-packages, so every declared dep is already satisfied and pip resolves
    nothing. Only an isolated venv (no ``--system-site-packages``) forces a
    real resolve — the ``ModuleNotFoundError: No module named 'surrealdb'``
    class. That install is left alone regardless: ``generate_sbom.sh`` runs
    ``cyclonedx-py environment``, which introspects the *active* environment.
    """

    MIRRORS = [
        ("github", GITHUB_DIR / "ci-release.yml"),
        ("forgejo", FORGEJO_DIR / "ci-release.yaml"),
    ]

    @staticmethod
    def _smoke_run(path) -> str:
        """The smoke step's `run:` script, with comment lines stripped.

        Comments are dropped so the assertions below match on the *executed*
        command rather than on prose that happens to quote a flag name.
        """
        steps = _load(path)["jobs"]["build-sbom"]["steps"]
        matches = [s for s in steps if s.get("name", "").startswith("Clean-venv")]
        assert matches, f"{path.name}: no 'Clean-venv ...' step in build-sbom"
        return "\n".join(
            ln for ln in matches[0]["run"].splitlines() if not ln.lstrip().startswith("#")
        )

    @pytest.mark.parametrize("mirror,path", MIRRORS)
    def test_creates_isolated_venv(self, mirror, path):
        script = self._smoke_run(path)
        venv_lines = [ln for ln in script.splitlines() if "-m venv" in ln]
        assert venv_lines, f"{mirror} ci-release must create a clean venv for the wheel smoke test."
        for ln in venv_lines:
            assert "--system-site-packages" not in ln, (
                f"{mirror} ci-release: the smoke venv must NOT use "
                "--system-site-packages — that re-exposes the yadgar-ci image's "
                f"pre-baked deps and defeats the dependency-resolution check.\n  {ln.strip()}"
            )

    @pytest.mark.parametrize("mirror,path", MIRRORS)
    def test_installs_wheel_into_that_venv(self, mirror, path):
        script = self._smoke_run(path)
        assert re.search(r"/tmp/cleanvenv/bin/pip install .*dist/yadgar-.*\.whl", script), (
            f"{mirror} ci-release must pip-install the built wheel into the clean venv."
        )

    @pytest.mark.parametrize("mirror,path", MIRRORS)
    def test_gate_is_blocking(self, mirror, path):
        """A gate declared continue-on-error is not a gate."""
        jobs = _load(path)["jobs"]
        assert jobs["build-sbom"].get("continue-on-error") is not True, (
            f"{mirror} ci-release: build-sbom carries `continue-on-error: true`, "
            "so the wheel-install gate cannot fail the run."
        )

    @pytest.mark.parametrize("mirror,path", MIRRORS)
    def test_sbom_install_still_targets_the_ambient_env(self, mirror, path):
        """`cyclonedx-py environment` reads the ACTIVE env — don't move it into the venv."""
        text = path.read_text(encoding="utf-8")
        assert re.search(r"^\s+pip install \"dist/yadgar-.*\.whl\[sbom\]\"", text, re.M), (
            f"{mirror} ci-release: the sbom extra must still be installed into the "
            "ambient environment that generate_sbom.sh introspects."
        )


class TestEntryPointSurfaceEnumerated:
    """The smoke step must cover the FULL invocation surface, self-checking.

    Console-scripts-only would not have caught the regression that motivates
    this test: ``yadgar/backend/safe_start`` lost its ``__main__.py`` in the
    ADR-0084 packaging change, breaking ``entrypoint-backend.sh``'s
    ``python3 -m yadgar.backend.safe_start`` — a MODULE target, not a
    ``[project.scripts]`` entry.
    """

    MIRRORS = [GITHUB_DIR / "ci-release.yml", FORGEJO_DIR / "ci-release.yaml"]

    def _console_scripts(self) -> dict[str, str]:
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return data["project"]["scripts"]

    def test_every_console_script_target_is_asserted(self):
        """Each `name = "module:func"` target must appear in both mirrors' smoke step."""
        for path in self.MIRRORS:
            text = TestCleanVenvWheelGate._smoke_run(path)
            for name, target in self._console_scripts().items():
                module, _, func = target.partition(":")
                assert f'"{module}", "{func}"' in text or f"'{module}', '{func}'" in text, (
                    f"{path.name}: console script '{name}' -> '{target}' is not "
                    "probed by the clean-venv smoke step. Add it (import-probe "
                    "the target; do NOT execute yadgar-nightly-cycle or "
                    "yadgar-setup — see the step's comment)."
                )

    def test_module_targets_from_repo_wide_grep_are_asserted(self):
        """Repo-wide grep for `-m yadgar...` module targets — enumeration cannot go stale.

        Scoped to SHIPPED files: docs/, tests and the workflows themselves are
        excluded (they discuss the targets rather than invoke them).
        """
        pattern = re.compile(r"python3?\s+-m\s+(yadgar(?:\.[A-Za-z_][A-Za-z0-9_.]*)*)\b")
        skip_dirs = {".git", "docs", "tests", ".venv", ".claude", "node_modules"}
        found: set[str] = set()
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {".sh", ".service", ".timer", ""}:
                continue
            if skip_dirs & set(path.relative_to(REPO_ROOT).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError, OSError:
                continue
            found.update(pattern.findall(text))

        # Bare `-m yadgar` is covered by the `yadgar --version` console script
        # smoke; only dotted submodule targets need their own spec assertion.
        submodules = {m for m in found if "." in m}
        assert submodules, (
            "repo-wide grep found no `python -m yadgar.<submodule>` targets — the "
            "enumeration regex or the skip list is probably wrong."
        )
        for path in self.MIRRORS:
            text = TestCleanVenvWheelGate._smoke_run(path)
            for mod in sorted(submodules):
                assert f"{mod}.__main__" in text, (
                    f"{path.name}: module target `python -m {mod}` is invoked by a "
                    f"shipped file but '{mod}.__main__' is not probed by the "
                    "clean-venv smoke step. A new entry point was added without "
                    "extending the gate."
                )
