"""v5.169.1 fix: PyYAML imported by shipped modules but never declared in pyproject.

`pyproject.toml` declares `ruamel.yaml>=0.18.0` as the only YAML dependency.
PyYAML (`yaml`) shows up in uv.lock ONLY as a transitive dependency of the
optional `ml` extra (huggingface-hub / transformers) — a base install (no
`[ml]` extra) genuinely has no PyYAML installed. Three loader functions
nonetheless did `import yaml` first and only fell back to ruamel.yaml on
ImportError, meaning their primary code path silently depended on whichever
packages happened to be installed in a given environment — the same
undeclared-dependency shape that shipped the "No module named surrealdb"
class of bug before.

Fix: those loaders now use ruamel.yaml (the always-present, declared hard
dependency) exclusively; the incidental PyYAML preference is removed.

TWO ARMS (the second added by the gate-honesty train, car I)
------------------------------------------------------------
1. The ORIGINAL arm below scans a hardcoded three-module list under `yadgar/`
   and forbids `import yaml` there in ANY form, guarded or not. Unchanged.
2. `test_no_module_in_the_repo_imports_pyyaml_unguarded` scans EVERY `.py` in
   the repo — `yadgar/tests/`, `scripts/`, `benchmarks/`, `docs/` included —
   and forbids an UNGUARDED PyYAML import. Arm 1 could not see outside its
   three files, which is why a bare `import yaml` lived in
   `yadgar/tests/scripts/test_v5_46_12_backend_version_canonical.py` for
   months. See the long comment above that test for the full rationale.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

_YADGAR_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _YADGAR_ROOT.parent

# The three loader modules that had the "prefer PyYAML, except ImportError:
# fall back to ruamel.yaml" shape (found by repo-wide grep for `import yaml`
# outside yadgar/tests/). NOTE: yadgar/core/server/tools/project.py's
# `_scan_stale_wiki_slugs` also does an optional `import yaml` but is a
# structurally different, already-safe pattern (sets `_yaml = None` on
# ImportError and degrades further inside `_parse_frontmatter`, which has its
# own independent ruamel fallback) — out of scope for this fix.
_TARGET_MODULES = [
    "core/server/tools/agent_prompts.py",
    "core/cli/seed.py",
    "_shared/wiki/wiki_meta.py",
]

_BARE_PYYAML_IMPORT_RE = re.compile(r"^\s*import yaml\b", re.MULTILINE)


def test_yaml_loaders_do_not_import_undeclared_pyyaml():
    """No shipped loader may `import yaml` (PyYAML) — it is not a declared
    dependency (only ruamel.yaml is, per pyproject.toml). These loaders must
    use ruamel.yaml directly, not prefer an incidental transitive package."""
    offenders = []
    for rel in _TARGET_MODULES:
        src = (_YADGAR_ROOT / rel).read_text()
        if _BARE_PYYAML_IMPORT_RE.search(src):
            offenders.append(rel)
    assert not offenders, (
        f"modules still `import yaml` (undeclared PyYAML dependency): {offenders}. "
        "Use ruamel.yaml (yadgar's declared hard YAML dependency) instead."
    )


# ---------------------------------------------------------------------------
# Repo-wide arm (gate-honesty car I)
# ---------------------------------------------------------------------------
#
# The three-module scan above is a gate blind to where the bug actually lives.
# `_TARGET_MODULES` is a hardcoded list of three files under `yadgar/`, and
# `yadgar/tests/` is not scanned at all -- which is exactly how a bare
# `import yaml` inside `test_sync_version_hook_fires_on_server_json`
# (yadgar/tests/scripts/test_v5_46_12_backend_version_canonical.py) survived for
# months, deterministically breaking plain `make test` while `make test-ci`
# (`--extra ml`, which drags PyYAML in transitively via huggingface-hub) went
# green. Car G fixed that instance; this arm is the gate that should have caught
# it. Same class as ledger task 394: correct trigger, correct wiring, scope
# structurally incapable of covering the files that actually change.
#
# WHAT IT FORBIDS: an UNGUARDED import of PyYAML anywhere in the repo -- one
# that raises ModuleNotFoundError and breaks the module when the optional `ml`
# extra is absent. That is precisely the shape of the bug above.
#
# WHAT IT ALLOWS, and why this is not a hole: an import inside a
# `try` / `except ImportError` (or `ModuleNotFoundError`, or bare `except`),
# in EITHER the try body or the handler. Such an import cannot break anything --
# the module already carries a fallback for its absence. Two live sites rely on
# this and both pass on their merits rather than by exemption, so this arm needs
# no allowlist at all:
#   * docs/diagrams/generate.py -- `try: from ruamel.yaml ... except ImportError:
#     import yaml`. PyYAML is the LAST-resort fallback so the tool also runs
#     standalone outside the project venv. The sanctioned ordering.
#   * yadgar/core/server/tools/project.py::_scan_stale_wiki_slugs --
#     `try: import yaml as _yaml except ImportError: _yaml = None`, degrading
#     through `_parse_frontmatter`'s own independent ruamel fallback. The module
#     docstring above already records this as out of scope for the v5.169.1 fix.
#
# `except Exception` deliberately does NOT count as a guard: it would catch
# ImportError, but it is ambiguous about intent and no site in the repo needs it.
#
# The three-module arm is left EXACTLY as it was. It forbids `import yaml` in
# those loaders in ANY form, guarded or not, which is stricter than this arm;
# replacing it would relax the rule for the files v5.169.1 was filed about.

#: Directories that are not repo source. Matched against a path RELATIVE to the
#: repo root -- never the absolute path. An absolute-parts match silently scans
#: ZERO files when the checkout is an agent worktree under
#: `<repo>/.claude/worktrees/<name>/`, because every path then contains
#: `.claude`. Measured while writing this test: the first draft reported
#: "scanned 0 unparseable 0" and would have passed vacuously forever.
_NON_SOURCE_DIRS = frozenset(
    {
        ".venv",
        ".git",
        ".claude",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
    }
)

#: A virtualenv marks its own root with this file. Pruning on the MARKER rather
#: than on a name list is what keeps the scan from depending on what somebody
#: called their env directory: `.venv` is the repo convention, but `venv/`,
#: `env/`, `.venv-ml/` and `UV_PROJECT_ENVIRONMENT=<anything>` are all one
#: command away, and third-party code is wall-to-wall `import yaml`. Measured:
#: with a name list alone, a repo-local `venv/` leaked a top-level module
#: (`venv/bin_probe.py`) into the scan and turned this gate red on code the repo
#: does not own. `site-packages` used to be in the list above and hid only the
#: nested case.
_VENV_MARKER = "pyvenv.cfg"

#: Floor for the anti-vacuity assertion. The repo held 1450 scannable modules on
#: 2026-08-27; a scan that suddenly sees a handful is broken, not clean.
_MIN_SCANNED_FILES = 500

_IMPORT_ERROR_NAMES = frozenset({"ImportError", "ModuleNotFoundError"})


def _iter_repo_python_files(root: Path = _REPO_ROOT) -> list[Path]:
    """Every ``.py`` file under *root* that is actually repo source.

    Prunes whole subtrees rather than filtering afterwards, so a virtualenv is
    never descended into at all.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _NON_SOURCE_DIRS and not (here / d / _VENV_MARKER).is_file()
        ]
        found.extend(here / f for f in filenames if f.endswith(".py"))
    return found


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """True for ``except ImportError`` / ``ModuleNotFoundError`` / bare ``except``."""
    if handler.type is None:
        return True
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, ast.Name) and n.id in _IMPORT_ERROR_NAMES for n in nodes)


def _guarded_lines(tree: ast.AST) -> set[int]:
    """Line numbers covered by a ``try``/``except ImportError`` guard."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_handler_catches_import_error(h) for h in node.handlers):
            continue
        for stmt in list(node.body) + [s for h in node.handlers for s in h.body]:
            guarded.update(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
    return guarded


def _is_pyyaml(name: str | None) -> bool:
    """True for PyYAML's ``yaml`` package; False for ``ruamel.yaml``."""
    if not name:
        return False
    return name == "yaml" or name.startswith("yaml.")


def unguarded_pyyaml_imports(source: str) -> list[int]:
    """Line numbers of PyYAML imports not covered by an ImportError guard.

    Pure function so the detector is itself testable -- a gate nobody watched
    fail is not a verified gate (see ``TestUnguardedPyYAMLDetector``).
    """
    tree = ast.parse(source)
    guarded = _guarded_lines(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        names: list[str | None]
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a RELATIVE import (`from .yaml import ...`) — a repo
            # module, never PyYAML.
            names = [node.module] if node.level == 0 else []
        else:
            continue
        if any(_is_pyyaml(n) for n in names) and node.lineno not in guarded:
            hits.append(node.lineno)
    return sorted(hits)


class TestUnguardedPyYAMLDetector:
    """The detector's own liveness -- it must FIRE, not merely not-crash."""

    def test_bare_module_level_import_is_flagged(self):
        assert unguarded_pyyaml_imports("import yaml\n") == [1]

    def test_bare_import_inside_a_function_is_flagged(self):
        """The real bug's shape: indented, inside a test body, with a noqa."""
        src = "def test_thing():\n    import yaml  # noqa: PLC0415\n    assert yaml\n"
        assert unguarded_pyyaml_imports(src) == [2]

    def test_from_import_is_flagged(self):
        assert unguarded_pyyaml_imports("from yaml import safe_load\n") == [1]

    def test_import_guarded_in_the_try_body_passes(self):
        src = "try:\n    import yaml\nexcept ImportError:\n    yaml = None\n"
        assert unguarded_pyyaml_imports(src) == []

    def test_import_guarded_in_the_handler_passes(self):
        """``docs/diagrams/generate.py``'s shape: ruamel first, PyYAML fallback."""
        src = "try:\n    from ruamel.yaml import YAML\nexcept ImportError:\n    import yaml\n"
        assert unguarded_pyyaml_imports(src) == []

    def test_ruamel_is_never_flagged(self):
        """ruamel.yaml is the DECLARED hard dependency -- flagging it is wrong."""
        src = "from ruamel.yaml import YAML\nimport ruamel.yaml\n"
        assert unguarded_pyyaml_imports(src) == []

    def test_except_other_than_import_error_is_not_a_guard(self):
        src = "try:\n    import yaml\nexcept ValueError:\n    yaml = None\n"
        assert unguarded_pyyaml_imports(src) == [2]


class TestRepoWalkScope:
    """The walk's own correctness — both failure modes were MEASURED, not feared."""

    def test_a_virtualenv_is_pruned_whatever_it_is_called(self, tmp_path):
        """A repo-local env named anything but `.venv` must not enter the scan.

        Measured before the `pyvenv.cfg` prune existed: a name list alone hid
        `<env>/lib/python3.14/site-packages/**` (via a `site-packages` entry) but
        NOT a module sitting at the env root, so a repo-local `venv/` turned this
        gate red on code the repo does not own.
        """
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("x = 1\n")
        for name in ("venv", "env", ".venv-ml"):
            env = tmp_path / name
            (env / "lib" / "python3.14" / "site-packages" / "pkg").mkdir(parents=True)
            (env / _VENV_MARKER).write_text("home = /usr\n")
            (env / "top_level.py").write_text("import yaml\n")
            (env / "lib" / "python3.14" / "site-packages" / "pkg" / "m.py").write_text(
                "import yaml\n"
            )

        found = {p.relative_to(tmp_path).as_posix() for p in _iter_repo_python_files(tmp_path)}
        assert found == {"src/real.py"}, f"virtualenv content leaked into the scan: {found}"

    def test_a_dot_claude_worktree_path_does_not_empty_the_scan(self, tmp_path):
        """Absolute-parts matching would return ZERO here — the vacuity trap."""
        root = tmp_path / ".claude" / "worktrees" / "agent-abc"
        (root / "yadgar").mkdir(parents=True)
        (root / "yadgar" / "mod.py").write_text("x = 1\n")

        found = _iter_repo_python_files(root)
        assert [p.name for p in found] == ["mod.py"], (
            f"a checkout under .claude/worktrees/ scanned {len(found)} files — an "
            "absolute-parts skip match empties the walk and passes vacuously"
        )

    def test_a_sibling_agent_worktree_is_not_scanned(self, tmp_path):
        """The other half of `.claude`: prune it BELOW the root, keep it AT the root.

        `.claude/worktrees/<agent>/` holds live agent checkouts of this same
        repo. Scanning them makes this gate's verdict depend on whether a
        sibling agent happens to be running — measured on 2026-08-27, when it
        went red naming `agent-a5f5ed373d2462eba/...:252`, a pre-fix copy of a
        bug already fixed on the train. That is the same "reports a state that
        is not the repo's" failure this gate exists to catch, so pruning is by
        directory NAME during descent: a `.claude` child of the walk root is
        skipped, while a root that IS itself inside `.claude/worktrees/` still
        scans its own files (the test above).
        """
        (tmp_path / "yadgar").mkdir()
        (tmp_path / "yadgar" / "real.py").write_text("x = 1\n")
        sibling = tmp_path / ".claude" / "worktrees" / "agent-xyz" / "yadgar"
        sibling.mkdir(parents=True)
        (sibling / "leaked.py").write_text("import yaml\n")

        found = {p.name for p in _iter_repo_python_files(tmp_path)}
        assert found == {"real.py"}, (
            f"scan reached a sibling agent worktree: {sorted(found)} — this gate's "
            "result must not depend on which agents are running"
        )


def test_no_module_in_the_repo_imports_pyyaml_unguarded():
    """Repo-wide: nothing may hard-depend on PyYAML, which is not declared.

    Scans EVERY repo ``.py`` -- including ``yadgar/tests/``, ``scripts/``,
    ``benchmarks/`` and ``docs/`` -- not the three hardcoded modules the older
    arm above names. Unparseable files hard-fail rather than being skipped: a
    file this gate cannot read is a file it cannot vouch for, and swallowing the
    error is how a scan reports success over ground it never covered.
    """
    files = _iter_repo_python_files()
    assert len(files) >= _MIN_SCANNED_FILES, (
        f"scan covered only {len(files)} files (floor {_MIN_SCANNED_FILES}) — the walk "
        "is broken, so a clean result here would be vacuous"
    )

    offenders: list[str] = []
    unparseable: list[str] = []
    for path in files:
        rel = path.relative_to(_REPO_ROOT)
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            unparseable.append(f"{rel}: {exc}")
            continue
        if "yaml" not in source:
            continue  # cheap pre-filter; ast.parse only where it can matter
        try:
            hits = unguarded_pyyaml_imports(source)
        except SyntaxError as exc:
            unparseable.append(f"{rel}: {exc}")
            continue
        offenders.extend(f"{rel}:{line}" for line in hits)

    assert not unparseable, f"files this gate could not read, so could not vouch for: {unparseable}"
    assert not offenders, (
        f"unguarded PyYAML import(s): {offenders}. PyYAML is NOT a declared dependency "
        "(pyproject.toml declares ruamel.yaml only, and "
        "test_pyproject_declares_no_pyyaml_dependency forbids declaring it) — it reaches an "
        "environment only transitively via the optional `ml` extra, so this breaks plain "
        "`make test` while `make test-ci` stays green. Use ruamel.yaml, or guard the import "
        "with try/except ImportError and a working fallback."
    )


def test_pyproject_declares_no_pyyaml_dependency():
    """Guard the premise: pyproject.toml must not declare PyYAML as a base
    dependency. If this ever flips, the loaders above may legitimately prefer
    it again — but today ruamel.yaml is the only declared YAML dependency."""
    pyproject_src = (_YADGAR_ROOT.parent / "pyproject.toml").read_text()
    assert "ruamel.yaml" in pyproject_src
    assert re.search(r'"pyyaml', pyproject_src, re.IGNORECASE) is None


@pytest.fixture
def pyyaml_blocked(monkeypatch):
    """Force `import yaml` to raise ImportError regardless of whether PyYAML
    is actually installed in the environment running this test (e.g. a
    dev venv with the `ml` extra installed would otherwise mask the gap)."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    yield


def test_genesis_yaml_loads_with_pyyaml_absent(pyyaml_blocked):
    """_load_genesis_yaml must parse materials/agent_prompts.yaml correctly
    via ruamel.yaml alone when PyYAML is absent."""
    from yadgar.core.server.tools.agent_prompts import _load_genesis_yaml

    data = _load_genesis_yaml()
    assert isinstance(data, dict)
    assert "prompts" in data and len(data["prompts"]) >= 1
    assert "contract" in data
    assert "disciplines" in data
    for entry in data["prompts"]:
        assert {"pattern", "purpose", "content"} <= entry.keys()


def test_anchors_yaml_loads_with_pyyaml_absent(pyyaml_blocked):
    """_load_anchors_yaml must parse materials/anchors.yaml correctly via
    ruamel.yaml alone when PyYAML is absent."""
    from importlib.resources import files

    from yadgar.core.cli.seed import _load_anchors_yaml

    anchors_path = str(files("yadgar.core.seed").joinpath("materials").joinpath("anchors.yaml"))
    entries = _load_anchors_yaml(anchors_path)
    assert isinstance(entries, list)
    assert len(entries) >= 1
    for e in entries:
        assert "content" in e
        assert "tags" in e


def test_page_type_schemas_loads_with_pyyaml_absent(pyyaml_blocked):
    """_load_page_type_schemas must parse schemas/wiki_page_types.yaml
    correctly via ruamel.yaml alone when PyYAML is absent."""
    from yadgar._shared.wiki.wiki_meta import _load_page_type_schemas

    data = _load_page_type_schemas()
    assert isinstance(data, dict)
    assert "schema_version" in data
    assert "page_types" in data
    assert isinstance(data["page_types"], dict)
    assert len(data["page_types"]) >= 1
