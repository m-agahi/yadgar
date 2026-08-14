"""C15 (0047 §5) — tests for the ADR-0225 ``directory`` residue sweep.

The sweep itself lives in ``scripts/check_directory_residue.py``; read its
module docstring first — in particular the fact that ``directory`` was NOT
removed (C14 measured it surviving on 46 tools in two classes, one of which is
still directory-keyed), so what is ratcheted is a scoping position that can
still *resolve*, not the parameter's existence.

WHY THESE TESTS LOOK THE WAY THEY DO
------------------------------------
This train produced FOUR vacuous passes — a filter that never projected the
column it filtered on; a ``REMOVE FIELD`` no-op that still satisfied an
``INFO FOR TABLE`` assertion; a nightly sweep that archived nothing in
production behind a swallowed ``TypeError`` and green tests; and an invariant
that re-built the object it audited and survived every sabotage. A lint whose
own tests are vacuous is the fifth, so:

* every planted violation is built in ``tmp_path`` and the assertion names the
  **planted subject**, never merely "errors is non-empty" — a test that passes
  on any violation for any reason is C8's failure in a new costume;
* three anti-vacuity floors are asserted against the REAL tree, and each is
  additionally shown to be load-bearing by mutating the thing it guards;
* the token set, the allowlist and the matcher each get a mutation that must
  turn the corresponding check red.

``tmp_path`` is used rather than a committed fixture on purpose: a fixture
``.py`` carrying real residue anywhere under the scan roots would be found by
the repo-wide scan — red on arrival, or an allowlist entry for the lint's own
test data, which makes the allowlist self-referential.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "check_directory_residue.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_directory_residue", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_directory_residue"] = mod
    spec.loader.exec_module(mod)
    return mod


C = _load()


# ---------------------------------------------------------------------------
# A miniature repo, built per-test in tmp_path.
# ---------------------------------------------------------------------------
def _mini_repo(tmp_path: Path, source: str, allowlist: str) -> tuple[Path, Path]:
    """Write a one-file scan root plus an allowlist; return (root, allowlist)."""
    pkg = tmp_path / "yadgar" / "core"
    pkg.mkdir(parents=True)
    (pkg / "planted.py").write_text(source, encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    allow = scripts / "directory_residue_allowlist.txt"
    allow.write_text(allowlist, encoding="utf-8")
    return tmp_path, allow


def _check(root: Path, allow: Path) -> list[str]:
    """Run both directions over the mini repo, floors and siblings disabled.

    The floors and the sibling arm are measured against the REAL tree in their
    own tests below; a two-file tmp repo cannot satisfy them.
    """
    return C.check(root, allow, check_floors=False, check_sibling_lints=False)


_PLANTED_RESIDUE = '''\
"""A module that reintroduces directory scoping."""


def resolve_for_project(directory: str) -> str:
    """The exact regression ADR-0225 forbids."""
    return directory
'''


# ---------------------------------------------------------------------------
# Direction 1 — planted residue must fail, and the message must name it
# ---------------------------------------------------------------------------
# Car I (2026-08-14): the bucket is now ``<path>::<function>`` so a single
# violation in one function flags THAT function and leaves the rest of the
# file alone. The planted source below carries two functions in one file —
# one with `directory` and one without — so the granularity tests can assert
# on both shapes in the same fixture.
_PLANTED_TWO_FUNCTIONS = '''\
"""A module with two functions; only one reintroduces directory scoping."""


def resolve_for_project(directory: str) -> str:
    """The exact regression ADR-0225 forbids."""
    return directory


def innocent_helper(x: int) -> int:
    """No scoping residue here — must NOT be flagged on its own."""
    return x + 1
'''


class TestDirectionOnePlantedResidue:
    def test_planted_residue_with_empty_allowlist_fails(self, tmp_path: Path) -> None:
        root, allow = _mini_repo(tmp_path, _PLANTED_RESIDUE, "# no entries\n")
        errors = _check(root, allow)
        residue = [e for e in errors if e.startswith("RESIDUE:")]
        assert len(residue) == 1, f"expected exactly one RESIDUE error, got {errors}"
        # Car I: the message header is now ``<path>::<function>`` so the
        # violation names the function, not just the file. The two are
        # both load-bearing: the path for the file-level allowlist match,
        # the function name for the planted-subject assertion.
        assert "yadgar/core/planted.py::resolve_for_project" in residue[0]
        assert "yadgar/core/planted.py" in residue[0]
        assert "resolve_for_project(directory)" in residue[0], (
            "the violation must name the planted symbol, not just report that "
            f"something failed: {residue[0]}"
        )

    def test_two_functions_one_file_one_bucket_per_function(self, tmp_path: Path) -> None:
        """Car I — granularity is per FUNCTION, not per file.

        A file with two functions, one carrying residue and one not, must
        produce ONE bucket (``<path>::resolve_for_project``) and leave
        ``<path>::innocent_helper`` silent. Pre-Car-I the bucket was the
        file path and any allowlist entry had to cover the whole file —
        this test pins the ratchet's tighter shape.
        """
        root, allow = _mini_repo(tmp_path, _PLANTED_TWO_FUNCTIONS, "# no entries\n")
        residue, _ = C.find_residue(root, scan_roots=("yadgar/core",))
        planted_buckets = sorted(k for k in residue if k.startswith("yadgar/core/planted.py::"))
        assert planted_buckets == ["yadgar/core/planted.py::resolve_for_project"], (
            "the bucket must be keyed on the FUNCTION, not the file; the "
            f"innocent function must not appear here: {planted_buckets}"
        )

    def test_an_allowlist_entry_clears_the_planted_residue(self, tmp_path: Path) -> None:
        root, allow = _mini_repo(
            tmp_path,
            _PLANTED_RESIDUE,
            "legacy-key  yadgar/core/planted.py  # planted residue for the C15 lint's own tests\n",
        )
        assert _check(root, allow) == []

    def test_deleting_the_only_entry_reopens_direction_one(self, tmp_path: Path) -> None:
        """MUTATION — the allowlist is load-bearing, not decoration."""
        root, allow = _mini_repo(
            tmp_path,
            _PLANTED_RESIDUE,
            "legacy-key  yadgar/core/planted.py  # planted residue for the C15 lint's own tests\n",
        )
        assert _check(root, allow) == []
        allow.write_text("# entry deleted by the mutation test\n", encoding="utf-8")
        errors = _check(root, allow)
        assert any("yadgar/core/planted.py" in e and e.startswith("RESIDUE:") for e in errors)

    def test_removing_the_token_from_the_set_blinds_the_matcher(self, tmp_path: Path) -> None:
        """MUTATION — the token set is load-bearing.

        Drop ``directory`` and the planted violation stops being seen. This is
        the check that would catch a future edit narrowing the token set to make
        a red go away.
        """
        root, allow = _mini_repo(tmp_path, _PLANTED_RESIDUE, "# no entries\n")
        assert any(e.startswith("RESIDUE:") for e in _check(root, allow))
        original = C.RESIDUE_TOKENS
        try:
            C.RESIDUE_TOKENS = frozenset(t for t in original if t != "directory")
            assert not any(e.startswith("RESIDUE:") for e in _check(root, allow)), (
                "removing `directory` from the token set left the planted "
                "violation visible — the matcher is not reading the token set"
            )
        finally:
            C.RESIDUE_TOKENS = original
        assert any(e.startswith("RESIDUE:") for e in _check(root, allow))


# ---------------------------------------------------------------------------
# Direction 2 — stale entries hard-fail, in both of its two shapes
# ---------------------------------------------------------------------------
class TestDirectionTwoStaleAllowlist:
    def test_entry_whose_path_does_not_exist_fails(self, tmp_path: Path) -> None:
        root, allow = _mini_repo(
            tmp_path,
            _PLANTED_RESIDUE,
            "legacy-key  yadgar/core/planted.py  # planted residue for the C15 lint's own tests\n"
            "carve-out-3  yadgar/core/vanished.py  # names a module that was deleted from the tree\n",
        )
        errors = _check(root, allow)
        stale = [e for e in errors if e.startswith("STALE ENTRY (no subject)")]
        assert len(stale) == 1, errors
        assert "yadgar/core/vanished.py" in stale[0]

    def test_entry_whose_file_lost_its_residue_fails(self, tmp_path: Path) -> None:
        """The cross-car shape: a sweep emptied the file, the entry survived."""
        root, allow = _mini_repo(
            tmp_path,
            _PLANTED_RESIDUE,
            "legacy-key  yadgar/core/planted.py  # planted residue for the C15 lint's own tests\n",
        )
        assert _check(root, allow) == []
        # the "sweep": rename the parameter onto project_id
        (root / "yadgar" / "core" / "planted.py").write_text(
            _PLANTED_RESIDUE.replace("directory", "project_id"), encoding="utf-8"
        )
        errors = _check(root, allow)
        stale = [e for e in errors if e.startswith("STALE ENTRY (no residue)")]
        assert len(stale) == 1, errors
        assert "yadgar/core/planted.py" in stale[0]

    def test_stale_is_a_hard_error_not_a_warning(self, tmp_path: Path) -> None:
        """Policy pin — see the allowlist header for the two-clause reason."""
        root, allow = _mini_repo(
            tmp_path,
            "x = 1\n",
            "carve-out-3  yadgar/core/planted.py  # resolves to a real file that carries no residue\n",
        )
        assert _check(root, allow), "a stale entry must FAIL, never merely warn"

    @pytest.mark.parametrize(
        "row",
        [
            "carve-out-3\n",  # one field
            "carve-out-3 a.py b.py  # three fields and a reason\n",
            "not-a-tag  yadgar/core/planted.py  # unknown tag must be rejected\n",
            # 38 chars — under the 40 floor, so the boundary is pinned
            "carve-out-3  yadgar/core/planted.py  # one character short of the forty floor\n",
            "carve-out-3  yadgar/core/planted.py  # a perfectly fine first reason, long enough\n"
            "carve-out-3  yadgar/core/planted.py  # duplicate path, a second and different reason\n",
        ],
    )
    def test_malformed_rows_hard_fail(self, tmp_path: Path, row: str) -> None:
        """An unparsed row would silently grant nothing and hide a residue site."""
        root, allow = _mini_repo(tmp_path, _PLANTED_RESIDUE, row)
        assert any(e.startswith("MALFORMED") for e in _check(root, allow))


class TestUnparseableIsAViolationNotASkip:
    """A file the matcher cannot read must never be scored clean.

    This is not a theoretical hardening. The lint's own first run under
    pre-commit found three files that parse on the venv's Python 3.14 (PEP 758
    permits an unparenthesized ``except A, B:``) and fail on the Python 3.13
    that ``language: system`` hooks run under. ``scan_source`` returned ``[]``
    for them, so two allowlist entries looked STALE — a false Direction-2
    verdict, produced by a swallowed error, visible only at commit time. That
    is C15a's ``_parse_iso`` failure exactly.
    """

    def test_a_file_that_cannot_be_parsed_is_reported(self, tmp_path: Path) -> None:
        root, allow = _mini_repo(tmp_path, "def f(:\n", "# no entries\n")
        errors = _check(root, allow)
        bad = [e for e in errors if e.startswith("UNPARSEABLE")]
        assert len(bad) == 1, errors
        assert "yadgar/core/planted.py" in bad[0]

    def test_an_unparseable_file_does_not_read_as_residue_free(self, tmp_path: Path) -> None:
        """The failure mode in one assertion: silence must not clear an entry."""
        root, allow = _mini_repo(
            tmp_path,
            _PLANTED_RESIDUE,
            "legacy-key  yadgar/core/planted.py  # planted residue for the C15 lint's own tests\n",
        )
        assert _check(root, allow) == []
        (root / "yadgar" / "core" / "planted.py").write_text("def f(:\n", encoding="utf-8")
        errors = _check(root, allow)
        assert any(e.startswith("UNPARSEABLE") for e in errors), (
            "an unparseable file produced no UNPARSEABLE error — it was scored "
            f"clean, which is how a false STALE verdict is manufactured: {errors}"
        )

    def test_the_whole_repo_parses_under_this_interpreter(self) -> None:
        """Green on arrival for the third class too, on whichever Python runs."""
        assert C.find_unparseable(REPO_ROOT) == []

    def test_the_except_tuple_arm_is_interpreter_independent(self, tmp_path: Path) -> None:
        """The `ast.parse` arm alone is blind on 3.14 — this one is not.

        Under Python 3.14, PEP 758 makes `except A, B:` legal, so the parse arm
        is structurally incapable of seeing a regression that breaks the 3.13
        interpreter pre-commit's `language: system` hooks actually run. If a
        ruff-format run strips a `# fmt: skip` paren again, THIS is what fires,
        on either Python.
        """
        root, allow = _mini_repo(
            tmp_path,
            "def f():\n    try:\n        g()\n    except TypeError, ValueError:\n        pass\n",
            "# no entries\n",
        )
        errors = _check(root, allow)
        bad = [e for e in errors if e.startswith("UNPARSEABLE")]
        assert bad, f"the regex arm did not fire on 3.14: {errors}"
        assert "yadgar/core/planted.py:4" in bad[0], bad[0]

    def test_the_parenthesised_form_with_fmt_skip_is_accepted(self, tmp_path: Path) -> None:
        root, allow = _mini_repo(
            tmp_path,
            "def f():\n    try:\n        g()\n"
            "    except (TypeError, ValueError):  # fmt: skip\n        pass\n",
            "# no entries\n",
        )
        assert _check(root, allow) == []

    def test_the_arm_does_not_self_match_on_prose(self) -> None:
        """The lint's own text names the form repeatedly; it must not trip.

        The repo has been bitten once already by a guard that scanned for its
        own marker and fired on the commit message describing it.
        """
        assert C.find_unparseable(REPO_ROOT, scan_roots=("scripts",)) == []
        for prose in (
            '    """...an unparenthesized ``except A, B:`` fails here."""',
            '        "syntax such as PEP 758\'s `except A, B:` fails "',
            "    # bare `except X, Y:` form is a SyntaxError",
        ):
            assert not C._BARE_EXCEPT_TUPLE.match(prose), prose

    def test_the_five_swept_sites_stay_parenthesised(self) -> None:
        """Pin the incidental fix so ruff-format cannot silently undo it."""
        for rel in (
            "benchmarks/run_eval.py",
            "benchmarks/run_longmemeval.py",
            "docs/diagrams/generate.py",
        ):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                assert not C._BARE_EXCEPT_TUPLE.match(line), (
                    f"{rel}:{lineno} lost its parens — re-add "
                    "`except (A, B):  # fmt: skip`; the comment is what stops "
                    "ruff-format stripping them again"
                )


# ---------------------------------------------------------------------------
# The matcher: what it sees, and the survivors it must not touch
# ---------------------------------------------------------------------------
class TestMatcherDiscriminates:
    @pytest.mark.parametrize(
        "src,kind",
        [
            ("def f(directory): pass", "param"),
            ("g(directory='x')", "kwarg"),
            ('d = {"directory": 1}', "key"),
            ('row["caller_dir"]', "key"),
            ("class M:\n    project_directory: str", "field"),
        ],
    )
    def test_scoping_positions_are_matched(self, src: str, kind: str) -> None:
        hits = C.scan_source(src)
        assert hits and hits[0][1] == kind, f"{src!r} → {hits}"

    @pytest.mark.parametrize(
        "src",
        [
            '"""A docstring that mentions the directory of the project."""',
            "# a comment about the directory\nx = 1",
            "directory = compute()",  # a plain local variable
            "y = args.directory",  # an attribute read
            "f(directory)",  # positional: the callee's parameter is the fact
        ],
    )
    def test_non_scoping_positions_are_not_matched(self, src: str) -> None:
        assert C.scan_source(src) == [], (
            f"{src!r} matched. Prose, locals, attribute reads and positional "
            "arguments are excluded by design — see the lint's docstring."
        )

    def test_carve_out_2_is_a_class_strip_not_an_entry(self) -> None:
        """``directory_context`` as the STORED COLUMN never needs an entry."""
        assert C.scan_source('row = {"directory_context": pid}') == []
        assert C.scan_source('x = row["directory_context"]') == []
        # ...but a SIGNATURE of that name is not the column, and does count.
        assert C.scan_source("def f(directory_context): pass")

    def test_exact_identifier_match_spares_the_two_named_survivors(self) -> None:
        """``default_branch`` (git) and ``branch_labels`` (Alembic) must survive.

        ``branch_labels`` is a REQUIRED module-level variable in every
        ``sql/migrations/versions/*.py``; sweeping it breaks the migration
        chain. This pins the exact-identifier rule that spares both — a future
        "improvement" to substring matching fails here first.
        """
        assert C.scan_source('cfg = {"default_branch": "master"}') == []
        assert C.scan_source("def f(default_branch): pass") == []
        assert C.scan_source("branch_labels: str | None = None") == []
        # the bare token is still a ratchet: a reintroduction does fail
        assert C.scan_source("def f(branch): pass")

    def test_branch_is_a_ratchet_not_a_sweep(self) -> None:
        """ADR-0215 already removed branch scoping — record that it is at zero."""
        residue, _ = C.find_residue(REPO_ROOT)
        live = [
            (rel, ln, detail)
            for rel, hits in residue.items()
            for ln, _kind, detail in hits
            if "branch" in detail
        ]
        assert live == [], (
            "the `branch` arm found live scoping sites; it is documented as a "
            f"ratchet at zero, so either sweep these or update the docstring: {live}"
        )


# ---------------------------------------------------------------------------
# Anti-vacuity floors, measured against the REAL tree (ADR-0080)
# ---------------------------------------------------------------------------
class TestFloorsAreLoadBearing:
    def test_the_repo_was_actually_walked(self) -> None:
        _, scanned = C.find_residue(REPO_ROOT)
        assert scanned >= C.MIN_FILES_SCANNED, (
            f"only {scanned} modules walked under {list(C.SCAN_ROOTS)} — the walk "
            "did not reach the tree, so every check would be vacuously green"
        )

    def test_the_allowlist_still_describes_a_real_surface(self) -> None:
        residue, _ = C.find_residue(REPO_ROOT)
        total = sum(len(v) for v in residue.values())
        assert total >= C.MIN_RESIDUE_HITS, (
            f"only {total} residue sites found — below the measured floor of "
            f"{C.MIN_RESIDUE_HITS}. Either the matcher broke or the sweep "
            "finished; if it genuinely finished, LOWER the floor deliberately "
            "rather than letting the guard rot."
        )

    def test_an_empty_matcher_trips_the_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MUTATION — the floor is load-bearing, not a comment.

        Blind the matcher and the floor, not silence, is what fires.
        """
        monkeypatch.setattr(C, "scan_source", lambda _src: [])
        errors = C.check(REPO_ROOT, check_sibling_lints=False)
        assert any(e.startswith("VACUOUS") for e in errors), (
            "a matcher returning nothing produced no VACUOUS error — the "
            f"anti-vacuity floor is not wired: {errors[:3]}"
        )

    def test_an_empty_walk_trips_the_files_floor(self) -> None:
        """MUTATION — scanning nothing must not read as a clean tree."""
        errors = C.check(REPO_ROOT, scan_roots=("does/not/exist",), check_sibling_lints=False)
        assert any("files walked" in e for e in errors)


# ---------------------------------------------------------------------------
# The sibling arm — the cross-car stale-entry class, closed
# ---------------------------------------------------------------------------
class TestSiblingAllowlistsAreClosedIntoThisRun:
    def test_the_sibling_allowlists_parse_and_are_clean(self) -> None:
        errors, parsed = C.check_siblings(REPO_ROOT)
        assert errors == []
        assert parsed >= C.MIN_SIBLING_ENTRIES, (
            f"only {parsed} sibling entries parsed (floor {C.MIN_SIBLING_ENTRIES}) "
            "— a renamed binding, not a finished sweep"
        )

    def test_a_renamed_sibling_binding_is_not_silently_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTATION — the arm that would otherwise rot invisibly.

        If C9a renames ``_ALLOWLIST``, an AST parse finds nothing and this whole
        arm goes green forever. That is vacuous-pass #5, so it is a hard error
        with its own floor.
        """
        monkeypatch.setattr(
            C,
            "_SIBLINGS",
            ((_c9a_rel(), "_RENAMED_AWAY", "yadgar/_shared"),),
        )
        errors, parsed = C.check_siblings(REPO_ROOT)
        assert parsed == 0
        assert any(e.startswith("SIBLING UNPARSEABLE") for e in errors), errors

    def test_a_missing_sibling_module_is_a_hard_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            C,
            "_SIBLINGS",
            (("yadgar/tests/_shared/test_gone.py", "_ALLOWLIST", "yadgar/_shared"),),
        )
        errors, _ = C.check_siblings(REPO_ROOT)
        assert any(e.startswith("SIBLING MISSING") for e in errors), errors

    def test_a_stranded_sibling_entry_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact shape that landed twice: an entry whose subject is gone."""
        stub = REPO_ROOT / "yadgar" / "tests" / "_shared" / "_c15_sibling_probe.py"
        stub_rel = "yadgar/tests/_shared/_c15_sibling_probe.py"
        stub.write_text(
            "_ALLOWLIST = {'storage/wiki.py::function_that_never_existed': 'probe'}\n",
            encoding="utf-8",
        )
        try:
            monkeypatch.setattr(C, "_SIBLINGS", ((stub_rel, "_ALLOWLIST", "yadgar/_shared"),))
            errors, parsed = C.check_siblings(REPO_ROOT)
            assert parsed == 1
            assert len(errors) == 1 and errors[0].startswith("STALE SIBLING ENTRY")
            assert "function_that_never_existed" in errors[0]
        finally:
            stub.unlink()


def _c9a_rel() -> str:
    return "yadgar/tests/_shared/test_c9a_directory_residue_shared.py"


# ---------------------------------------------------------------------------
# Absence-of-project check (Car I, 2026-08-14 train)
# ---------------------------------------------------------------------------
# Each test plants a ``yadgar/core/server/tools/*.py`` file with a different
# shape and asserts on the ``find_absence_of_project`` result. The four cases
# the task spec calls out are all covered; the fifth (a non-tool function
# taking ``directory`` without ``project``) is the same shape as the first
# but with a plain ``def`` — verifying the AST walk keys on ``@_tool``
# decoration, not on the parameter list.
def _mini_tools_root(tmp_path: Path, source: str, name: str = "planted_tool.py") -> Path:
    """Write a single tool module under ``yadgar/core/server/tools/``."""
    tools_dir = tmp_path / "yadgar" / "core" / "server" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / name).write_text(source, encoding="utf-8")
    return tmp_path


_PLANTED_TOOL_WITH_BOTH = '''\
"""Tool with both ``directory`` and ``project`` — the C3 / Car M shape."""

from yadgar.core.server._app import _tool


@_tool()
def resolve_with_both(directory: str | None, project: str | None = None) -> str:
    """The green shape: identity-keyed + project override."""
    return project or directory or ""
'''


_PLANTED_TOOL_WITHOUT_PROJECT = '''\
"""Tool with ``directory`` only — the regression Car I ratchets."""

from yadgar.core.server._app import _tool


@_tool()
def stale_residue(directory: str | None) -> str:
    """The exact failure mode: directory-only, no project override."""
    return directory or ""
'''


_PLANTED_TOOL_NEITHER = '''\
"""Tool with no scoping params — not a residue risk."""

from yadgar.core.server._app import _tool


@_tool()
def unscoped_query(query: str) -> str:
    """Takes neither ``directory`` nor ``project`` — passes."""
    return query
'''


_PLANTED_NON_TOOL_WITH_DIRECTORY = '''\
"""A plain helper that takes ``directory`` — not in scope, no @_tool."""

# Intentionally no ``from yadgar.core.server._app import _tool``.


def helper(directory: str | None) -> str:
    """Out of scope: not @_tool-decorated, so the check ignores it."""
    return directory or ""
'''


_PLANTED_TOOL_BARE_DECORATOR = '''\
"""Bare ``@_tool`` (no parens) — the rare form, must still be caught."""

from yadgar.core.server._app import _tool


@_tool
def bare_form(directory: str | None) -> str:
    """Bare decorator is still a tool registration."""
    return directory or ""
'''


_PLANTED_TOOL_KWARG_ONLY_PROJECT = '''\
"""Tool with keyword-only ``project`` — passes (the set check sees both)."""

from yadgar.core.server._app import _tool


@_tool()
def kwarg_only(directory: str | None, *, project: str | None = None) -> str:
    """The kwarg-only form still satisfies the param-name check."""
    return project or directory or ""
'''


class TestAbsenceOfProjectCheck:
    """Car I (2026-08-14): @_tool functions must take ``project`` when they take ``directory``."""

    def test_tool_with_both_directory_and_project_passes(self, tmp_path: Path) -> None:
        """Green: a tool that has both params is fine."""
        root = _mini_tools_root(tmp_path, _PLANTED_TOOL_WITH_BOTH)
        errors, checked = C.find_absence_of_project(root)
        assert errors == [], (
            f"a tool with both `directory` and `project` must NOT be flagged: {errors}"
        )
        assert checked == 1

    def test_tool_with_only_directory_fails_and_names_the_function(self, tmp_path: Path) -> None:
        """The exact regression Car I ratchets: hard failure naming the function."""
        root = _mini_tools_root(tmp_path, _PLANTED_TOOL_WITHOUT_PROJECT)
        errors, checked = C.find_absence_of_project(root)
        assert len(errors) == 1, f"expected exactly one NO PROJECT error, got {errors}"
        assert errors[0].startswith("NO PROJECT: "), errors[0]
        # The message MUST name the offending function — a test that passes
        # on any violation for any reason is the same vacuous-pass shape C8
        # produced four times on this train.
        assert "planted_tool.py::stale_residue" in errors[0], errors[0]
        assert checked == 1

    def test_tool_with_neither_param_passes(self, tmp_path: Path) -> None:
        """Out of scope: no scoping position means no residue risk."""
        root = _mini_tools_root(tmp_path, _PLANTED_TOOL_NEITHER)
        errors, checked = C.find_absence_of_project(root)
        assert errors == [], (
            f"a tool with neither `directory` nor `project` is not a residue risk: {errors}"
        )
        assert checked == 1

    def test_non_tool_function_with_only_directory_passes(self, tmp_path: Path) -> None:
        """Out of scope: not registered as an MCP tool."""
        root = _mini_tools_root(tmp_path, _PLANTED_NON_TOOL_WITH_DIRECTORY)
        errors, checked = C.find_absence_of_project(root)
        assert errors == [], (
            "a non-tool function is not in scope for this check — only @_tool "
            f"decorated functions are inspected: {errors}"
        )
        assert checked == 0, (
            "the count of inspected tools must be 0 for a file with no @_tool "
            "decorations, otherwise the AST walk is matching on the wrong thing"
        )

    def test_bare_at_tool_decorator_is_still_in_scope(self, tmp_path: Path) -> None:
        """``@_tool`` (no parens) is a valid tool registration and must fire."""
        root = _mini_tools_root(tmp_path, _PLANTED_TOOL_BARE_DECORATOR)
        errors, checked = C.find_absence_of_project(root)
        assert len(errors) == 1, errors
        assert "planted_tool.py::bare_form" in errors[0]
        assert checked == 1

    def test_keyword_only_project_satisfies_the_check(self, tmp_path: Path) -> None:
        """``*, project=None`` is still the parameter set containing ``project``."""
        root = _mini_tools_root(tmp_path, _PLANTED_TOOL_KWARG_ONLY_PROJECT)
        errors, checked = C.find_absence_of_project(root)
        assert errors == [], errors
        assert checked == 1

    def test_the_check_walks_a_real_tools_root(self) -> None:
        """Green on arrival: every @_tool function in the real tree carries both.

        This is the load-bearing assertion. If a future edit drops ``project``
        from any tool's signature, this test catches it. The reverse — adding
        ``project`` to a tool that has ``directory`` — would also be a real
        change worth a test, but that's an additive move and won't regress
        this guard; the load-bearing failure is the subtractive one.
        """
        errors, checked = C.find_absence_of_project(REPO_ROOT)
        assert errors == [], (
            "every @_tool-decorated function in the real tree must take "
            "`project` when it takes `directory`. Violations:\n  " + "\n  ".join(errors)
        )
        assert checked >= C.MIN_TOOLS_CHECKED, (
            f"only {checked} @_tool functions inspected (floor "
            f"{C.MIN_TOOLS_CHECKED}) — the AST walk is not reaching the "
            "real tree, so this arm would be silently green"
        )

    def test_a_renamed_tool_decorator_blinds_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTATION — the floor pins a renamed decorator.

        If ``_tool`` is renamed to ``_register``, the AST walk finds zero
        decorated functions and the arm goes green forever. The
        ``MIN_TOOLS_CHECKED`` floor is what catches this — a renamed
        decorator drops ``checked`` below the floor, ``check()`` reports
        VACUOUS, and the next reader cannot mistake "vacuous" for "clean".
        """
        # Build a mini root with three @_tool functions, then rename the
        # decorator to a string the matcher doesn't recognise.
        src = """\
from yadgar.core.server._app import _tool


@_tool()
def a(directory, project):
    pass


@_tool()
def b(directory, project):
    pass


@_tool()
def c(directory, project):
    pass
"""
        root = _mini_tools_root(tmp_path, src)
        # Pre-condition: 3 tools detected under the real name.
        _, checked = C.find_absence_of_project(root)
        assert checked == 3
        # Stub the allowlist so ``check()`` doesn't error out on the first
        # line — the absence-of-project arm is what we're testing here, not
        # the residue arm.
        scripts = root / "scripts"
        scripts.mkdir()
        allow = scripts / "directory_residue_allowlist.txt"
        allow.write_text("# empty allowlist for the renamed-decorator mutation\n", encoding="utf-8")
        # Now blind the matcher by renaming the constant and re-running
        # through ``check()`` with floors ON.
        monkeypatch.setattr(C, "_is_tool_decorated", lambda _node: False)
        errors = C.check(
            root,
            allow,
            scan_roots=(),
            check_floors=True,
            check_sibling_lints=False,
        )
        # The VACUOUS floor fires; the absence-of-project arm produces no
        # NO PROJECT errors because the matcher is blind.
        assert any(e.startswith("VACUOUS") and "inspected" in e for e in errors), (
            "a blinded `_is_tool_decorated` produced no VACUOUS floor — "
            "the arm would be silently green: " + repr(errors)
        )


# ---------------------------------------------------------------------------
# Green on arrival — the whole premise of landing last
# ---------------------------------------------------------------------------
class TestGreenOnArrival:
    def test_the_real_repo_passes_both_directions(self) -> None:
        errors = C.check(REPO_ROOT)
        assert errors == [], (
            "C15 lands last so the residue lint is GREEN ON ARRIVAL. It is not:\n  "
            + "\n  ".join(errors)
        )

    def test_the_cli_exits_zero(self) -> None:
        assert C.main(["--repo-root", str(REPO_ROOT)]) == 0

    def test_every_allowlist_entry_carries_a_tag_and_a_reason(self) -> None:
        entries, errors = C.parse_allowlist(C.allowlist_path(REPO_ROOT).read_text(encoding="utf-8"))
        assert errors == []
        assert len(entries) >= 60, f"only {len(entries)} entries parsed — did the file move?"
        for tag, pattern, reason in entries:
            assert tag in C.VALID_TAGS, (tag, pattern)
            assert len(reason) >= C.MIN_REASON_CHARS, (pattern, reason)

    def test_the_reason_floor_matches_the_repo_allowlist_family(self) -> None:
        """40, not 20 — the governed-allowlist family's floor, and C9a's."""
        assert C.MIN_REASON_CHARS == 40

    def test_the_lint_does_not_flag_itself(self) -> None:
        """Self-match check — this repo has already been bitten by one.

        A guard that scans for its own marker strings and trips on them is a
        recorded failure here (the pretooluse-router draft that self-triggered
        on the commit message describing it). The lint lives under a scan root,
        so pin that it is clean rather than discovering it as a red.
        """
        assert C.scan_source(_SCRIPT.read_text(encoding="utf-8")) == []
