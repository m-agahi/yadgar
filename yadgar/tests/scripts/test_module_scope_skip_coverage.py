"""Task 380 — a module-scope ``importorskip`` must be satisfiable on some leg.

WHY THIS EXISTS
----------------
``yadgar/tests/core/test_v5_172_project_staleness.py`` is 338 lines and 17
tests. It opens with ``pytest.importorskip("sqlalchemy")`` at module scope, so
the WHOLE FILE skips unless ``sqlalchemy`` is installed. Measured locally:
``1 skipped in 1.48s``. ``make test``, ``make test-ci`` and
``scripts/ci-local-legs.sh`` all installed ``--extra test [--extra ml]`` and
never ``--extra sql``, so not one of its 17 tests had ever executed on this
box. Nine sibling modules were in the same position (211 tests in total).

NOTHING REPORTED IT, and that is the interesting part:

  * ``scripts/check_skip_inventory.py`` parses ``pytest -rs`` output for
    ``SKIPPED [N] ...`` lines and passes when every reason matches a sanctioned
    entry. ``yadgar/tests/skip_inventory.json`` sanctions
    ``"sqlalchemy not installed (sql extra)"``. A run that skips all 211 of
    those tests is therefore GREEN — the sanctioned entry is the blindfold.
  * ``test_engine2_integration_coverage.py`` was added after task #77's
    identical "never ran anywhere" miss. It keys on the
    ``xdist_group("engine2_mariadb")`` MARKER and only scans
    ``yadgar/tests/integration/``, so a sqlalchemy-dependent file one directory
    over is invisible to it.
  * ``test_ci_group_coverage.py`` asserts every test DIRECTORY is in some CI
    group. ``yadgar/tests/core/`` is — the directory was covered while the file
    inside it was inert.

Every existing guard asked "is this file selected?" and none asked "can its
imports actually resolve where it is selected?". This one asks the second
question.

WHAT IS ASSERTED
-----------------
For every module-scope ``pytest.importorskip("X")`` in the test tree, at least
one CONFIGURED LEG that would collect the file installs the distribution
providing ``X``. Four surfaces are read, because the legs genuinely differ:

  ci-image      the ``tests`` matrix groups (``scripts/ci_group_manifest.py``)
                running in ``docker.io/openfantasy/yadgar-ci``, whose extras
                are baked by ``Dockerfile.ci``'s ``uv export`` line.
  engine2       ``test-engine2-integration``'s hand-maintained file list and
                its own ``uv run --extra ...`` invocation.
  ci-local      ``scripts/ci-local-legs.sh``'s per-leg ``uv run --extra ...``.
  make-test-ci  the ``test-ci`` Makefile target (whole tree).

SCOPE — MODULE-SCOPE ONLY, deliberately
----------------------------------------
Only ``importorskip`` calls at module scope are checked: those make the ENTIRE
file inert, which is task 380's exact shape and is unambiguous. A
function-scope or fixture-scope ``importorskip`` guards one test and is a
legitimate way to make an optional-feature test degrade gracefully.

That limit is real and is NOT free — ``yadgar/tests/core/test_export_duckdb.py``
guards eleven call sites on ``duckdb``, which no extra in ``pyproject.toml``
provides at all (``yadgar/core/cli/export.py`` advertises
``pip install yadgar[analytics]``, an extra that does not exist). Those tests
run nowhere too. Widening this check to function scope would drag that in, and
fixing it is a different decision than the one this test encodes. It is
recorded here so the next reader finds it rather than rediscovers it.

WHY NOT A COLLECT-ONLY SUBPROCESS
----------------------------------
Same reason ``test_engine2_integration_coverage.py`` gives: running pytest over
the test tree would import modules another train car may be mid-edit on, so a
repo-guard test could go red for someone else's in-progress change. The scan is
``ast`` over source text; the workflow parse uses ``ruamel.yaml`` (a declared
base dependency); no subprocess, no import of the modules under gate.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib

from ruamel.yaml import YAML

from yadgar.tests._paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from ci_group_manifest import groups, legs  # noqa: E402

TESTS_ROOT = REPO_ROOT / "yadgar" / "tests"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCKERFILE_CI = REPO_ROOT / "Dockerfile.ci"
LEGS_SCRIPT = REPO_ROOT / "scripts" / "ci-local-legs.sh"
MAKEFILE = REPO_ROOT / "Makefile"
GITHUB_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-pr.yml"

ENGINE2_JOB = "test-engine2-integration"
ENGINE2_STEP = "Run the engine-#2 integration suite"

# Import name -> distribution name on PyPI. An explicit dict, NOT a clever
# normaliser: a normaliser that silently maps nothing would make every
# assertion below pass vacuously, which is the defect class this file closes.
# A module-scope importorskip on an unlisted module fails
# `test_every_scanned_module_is_mapped` rather than being waved through.
_MODULE_TO_DIST = {
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "asyncmy": "asyncmy",
    "duckdb": "duckdb",
    "surrealdb": "surrealdb",
    "hypothesis": "hypothesis",
    "opentelemetry": "opentelemetry-api",
    "opentelemetry.trace": "opentelemetry-api",
    "opentelemetry.instrumentation.fastapi": "opentelemetry-instrumentation-fastapi",
    "opentelemetry.instrumentation.httpx": "opentelemetry-instrumentation-httpx",
}

_EXTRA_FLAG_RE = re.compile(r"--extra[= ]+([A-Za-z0-9_-]+)")
_INTEGRATION_MARK_RE = re.compile(r"\bmark\.integration\b")
_ENGINE2_FILE_RE = re.compile(r"yadgar/tests/integration/test_[\w]+\.py")


# ---------------------------------------------------------------------------
# The scan: module-scope importorskip
# ---------------------------------------------------------------------------


def _module_scope_importorskips(source: str) -> set[str]:
    """Return the module names ``source`` importorskips at MODULE scope.

    Statements nested inside a ``def``/``async def``/``class`` are skipped, so a
    fixture-scope or per-test guard is not reported. Everything else at the top
    level counts, including a call inside a module-level ``if``/``try``.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if not isinstance(func, ast.Attribute) or func.attr != "importorskip":
                continue
            if sub.args and isinstance(sub.args[0], ast.Constant):
                if isinstance(sub.args[0].value, str):
                    found.add(sub.args[0].value)
    return found


def scan_tree() -> dict[str, set[str]]:
    """Return ``{repo-relative test path: {module names}}`` for the whole tree."""
    found: dict[str, set[str]] = {}
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        try:
            modules = _module_scope_importorskips(path.read_text(encoding="utf-8"))
        except SyntaxError:  # a file mid-edit is not this guard's business
            continue
        if modules:
            found[path.relative_to(REPO_ROOT).as_posix()] = modules
    return found


# ---------------------------------------------------------------------------
# What each surface installs
# ---------------------------------------------------------------------------


def _dists_for_extras(extras: set[str]) -> set[str]:
    """Return the distribution names provided by base deps + *extras*.

    Base dependencies are always present, so they are folded in unconditionally:
    an importorskip on a base dep can never be unsatisfiable.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    requirements = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    pending = list(extras)
    seen_extras: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen_extras or name not in optional:
            continue
        seen_extras.add(name)
        for req in optional[name]:
            requirements.append(req)
            # `dev = ["yadgar[test]", ...]` — follow self-referential extras.
            nested = re.match(r"^\s*yadgar\[([^\]]+)\]", req)
            if nested:
                pending.extend(part.strip() for part in nested.group(1).split(","))
    dists = set()
    for req in requirements:
        name = re.split(r"[<>=!~\[; ]", req.strip(), maxsplit=1)[0]
        if name:
            dists.add(name.lower().replace("_", "-").replace(".", "-"))
    return dists


def _extras_in(text: str) -> set[str]:
    """Extras named by the ``uv run`` / ``uv export`` COMMANDS in *text*.

    Comment lines are dropped and only the uv invocation (plus its backslash
    continuations) is read. Both halves are load-bearing, and the second was
    added after mutation-testing this file caught the first version cheating:
    the ``--extra sql`` fix in ``scripts/ci-local-legs.sh`` carries a comment
    explaining WHY the flag is there, and a whole-file regex counted that
    English sentence as the flag itself. Reverting the real flag then left the
    guard green — a gate reporting success while checking prose, in the file
    written to stop exactly that.
    """
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    captured: list[str] = []
    capturing = False
    for line in lines:
        if not capturing and re.search(r"\buv\s+(?:run|export|sync|pip)\b", line):
            capturing = True
        if capturing:
            captured.append(line)
            # A continuation keeps the command open; anything else ends it.
            if not line.rstrip().endswith("\\"):
                capturing = False
    return set(_EXTRA_FLAG_RE.findall("\n".join(captured)))


def image_extras() -> set[str]:
    """The extras ``Dockerfile.ci``'s ``uv export`` bakes into yadgar-ci."""
    return _extras_in(DOCKERFILE_CI.read_text(encoding="utf-8"))


def local_leg_extras() -> set[str]:
    """The extras ``scripts/ci-local-legs.sh``'s single pytest invocation installs."""
    return _extras_in(LEGS_SCRIPT.read_text(encoding="utf-8"))


def make_test_ci_extras() -> set[str]:
    """The extras the ``test-ci`` Makefile target installs.

    Sliced to the recipe body so a neighbouring target's flags cannot leak in.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("\ntest-ci:")
    rest = text[start + 1 :]
    end = rest.find("\n\n")
    return _extras_in(rest if end == -1 else rest[:end])


def _engine2_run_script() -> str:
    doc = YAML(typ="safe").load(GITHUB_WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"][ENGINE2_JOB]["steps"]
    matches = [s for s in steps if s.get("name") == ENGINE2_STEP]
    assert len(matches) == 1, f"expected one {ENGINE2_STEP!r} step, got {len(matches)}"
    return matches[0]["run"]


def engine2_files_and_extras() -> tuple[set[str], set[str]]:
    script = _engine2_run_script()
    return set(_ENGINE2_FILE_RE.findall(script)), _extras_in(script)


# ---------------------------------------------------------------------------
# Which legs collect which file
# ---------------------------------------------------------------------------


def _covers(paths: str, rel: str) -> bool:
    """True when a whitespace-separated ``paths`` selection would collect *rel*."""
    return any(rel.startswith(p.rstrip("/") + "/") for p in paths.split() if p)


def legs_covering(rel: str) -> dict[str, set[str]]:
    """Return ``{leg label: extras installed}`` for every leg that collects *rel*.

    The matrix groups and the local legs both run
    ``-m 'not integration and not e2e and not perf'``, so a file carrying the
    ``integration`` marker is DESELECTED there however well its path matches —
    marker-deselected is not the same outcome as skipped, and treating it as
    coverage is exactly the hole task #77 fell through.
    """
    covering: dict[str, set[str]] = {}
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    is_integration = bool(_INTEGRATION_MARK_RE.search(source))

    if not is_integration:
        img = image_extras()
        for group in groups():
            if _covers(group["paths"], rel):
                covering[f"ci-image:{group['group']}"] = img
        local = local_leg_extras()
        for leg, paths in legs().items():
            if _covers(paths, rel):
                covering[f"ci-local:{leg}"] = local
        covering["make-test-ci"] = make_test_ci_extras()

    files, extras = engine2_files_and_extras()
    if rel in files:
        covering[f"{ENGINE2_JOB}"] = extras
    return covering


# ---------------------------------------------------------------------------
# Anti-vacuity: every input this file reasons over must be non-empty
# ---------------------------------------------------------------------------


class TestTheScanItselfFindsSomething:
    """An AST/regex drift that stopped matching would make everything below pass
    on empty-minus-empty — the precise defect this module exists to close."""

    def test_the_module_scope_scan_finds_files(self) -> None:
        assert scan_tree(), (
            "no test module matched the module-scope importorskip scan — the "
            "scan is broken, not the fact that nothing guards its imports"
        )

    def test_every_scanned_module_is_mapped_to_a_distribution(self) -> None:
        unmapped = sorted(
            {mod for mods in scan_tree().values() for mod in mods} - set(_MODULE_TO_DIST)
        )
        assert not unmapped, (
            f"{unmapped} are importorskip'd at module scope but absent from "
            "_MODULE_TO_DIST, so this guard cannot tell whether any leg provides "
            "them. Add the import-name -> distribution-name pair."
        )

    def test_every_extras_surface_parses_to_something(self) -> None:
        for label, extras in (
            ("Dockerfile.ci", image_extras()),
            ("scripts/ci-local-legs.sh", local_leg_extras()),
            ("Makefile test-ci", make_test_ci_extras()),
            (f"{ENGINE2_JOB}", engine2_files_and_extras()[1]),
        ):
            assert extras, (
                f"parsed ZERO `--extra` flags out of {label} — the parse drifted; "
                "an empty extras set would make every leg look like it installs "
                "nothing and this guard would fail for the wrong reason (or, if "
                "inverted, pass for it)"
            )

    def test_extras_resolve_to_distributions(self) -> None:
        dists = _dists_for_extras({"sql"})
        assert "sqlalchemy" in dists, dists
        assert "fastapi" in dists, "base dependencies must be folded in"

    def test_engine2_file_list_is_non_empty(self) -> None:
        files, _ = engine2_files_and_extras()
        assert files, f"parsed no test files out of {ENGINE2_JOB}'s run block"

    def test_a_comment_naming_an_extra_is_not_counted_as_installing_it(self) -> None:
        """The regression found by mutation-testing this file against itself.

        ``scripts/ci-local-legs.sh``'s ``--extra sql`` fix carries a comment
        explaining why the flag is there. A whole-file regex counted that
        sentence, so DELETING the real flag left this guard green — a gate
        reporting success while reading prose, inside the file written to stop
        that. The parse must read the command and only the command.
        """
        text = (
            "# the leg needs --extra sql because the image bakes it\n"
            "  # --extra ml is mentioned here too, in prose\n"
            "uv run --extra test python -m pytest yadgar/tests/\n"
        )
        assert _extras_in(text) == {"test"}, _extras_in(text)

    def test_a_multiline_uv_command_is_read_whole(self) -> None:
        """...and narrowing to one line must not lose a continued invocation."""
        text = "uv export --frozen \\\n    --extra test --extra ml --extra sql \\\n    -o /tmp/x\n"
        assert _extras_in(text) == {"test", "ml", "sql"}, _extras_in(text)


# ---------------------------------------------------------------------------
# The assertion
# ---------------------------------------------------------------------------


class TestNoTestFileIsInertOnEveryLeg:
    def test_every_module_scope_importorskip_is_satisfiable_somewhere(self) -> None:
        offenders: list[str] = []
        for rel, modules in sorted(scan_tree().items()):
            covering = legs_covering(rel)
            if not covering:
                offenders.append(
                    f"{rel}: no configured leg collects this file at all "
                    "(a path/marker problem, not a dependency one)"
                )
                continue
            for module in sorted(modules):
                dist = _MODULE_TO_DIST[module]
                satisfied = [
                    label for label, extras in covering.items() if dist in _dists_for_extras(extras)
                ]
                if not satisfied:
                    offenders.append(
                        f"{rel}: importorskip({module!r}) at module scope, but NONE of "
                        f"the legs that collect it install {dist!r} "
                        f"(legs: {sorted(covering)}). Every test in this file is "
                        "inert everywhere."
                    )
        assert not offenders, (
            "these test modules skip at module scope on every configured leg — "
            "they run NOWHERE, and the skip is invisible to "
            "scripts/check_skip_inventory.py because its reason is sanctioned in "
            "yadgar/tests/skip_inventory.json:\n  " + "\n  ".join(offenders)
        )

    def test_the_sql_files_are_the_regression_case(self) -> None:
        """The specific files task 380 was filed for, named so the fix stays.

        Asserting the general rule alone would let a future change satisfy it by
        deleting these files rather than running them.
        """
        scanned = scan_tree()
        for rel in (
            "yadgar/tests/core/test_v5_172_project_staleness.py",
            "yadgar/tests/_shared/test_c6_project_registry_writer.py",
            "yadgar/tests/_shared/test_mariadb_migrations.py",
        ):
            assert rel in scanned, f"{rel} lost its module-scope importorskip or was deleted"
            covering = legs_covering(rel)
            satisfied = [
                label
                for label, extras in covering.items()
                if "sqlalchemy" in _dists_for_extras(extras)
            ]
            assert satisfied, f"{rel} has no leg installing sqlalchemy (legs: {sorted(covering)})"


class TestLocalLegsMirrorTheCiImage:
    """The defect that hid it: the local runner installed a SMALLER set than the
    image whose groups it exists to reproduce.

    ``scripts/ci-local-legs.sh`` ran ``uv run --extra test --extra ml`` against
    the same paths the ``tests`` matrix runs inside ``yadgar-ci``, which bakes
    ``--extra test --extra ml --extra sql``. So a module the image can import
    was skipped locally, and the reason being sanctioned in
    ``skip_inventory.json`` meant no gate could say so. Local/CI parity is the
    entire purpose of that runner; a leg installing fewer dependencies than the
    group it mirrors is not mirroring it.

    ``make test`` is deliberately NOT checked here — it is the everyday target
    and ``asyncmy`` is a compiled driver (see the ``sql`` extra's note in
    ``pyproject.toml``), so it keeps the smaller set on purpose.
    """

    def test_ci_local_legs_install_at_least_what_the_image_bakes(self) -> None:
        missing = sorted(image_extras() - local_leg_extras())
        assert not missing, (
            f"scripts/ci-local-legs.sh installs {sorted(local_leg_extras())} but "
            f"Dockerfile.ci bakes {sorted(image_extras())} — the leg is missing "
            f"{missing}, so it silently skips modules the CI group it mirrors can "
            "actually import."
        )

    def test_make_test_ci_installs_at_least_what_the_image_bakes(self) -> None:
        missing = sorted(image_extras() - make_test_ci_extras())
        assert not missing, (
            f"the `test-ci` Makefile target installs {sorted(make_test_ci_extras())} "
            f"but Dockerfile.ci bakes {sorted(image_extras())} — the target "
            f'documented as "mirrors what CI runs" is missing {missing}.'
        )
