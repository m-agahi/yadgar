"""I33 — check_observe_coverage.py lint test suite (v5.101 P0, warn-mode).

The lint AST-walks in-scope functions and classifies each:
  - dunder / property / trivial  → auto-exempt (no allowlist entry)
  - @_tool / @trace_span / @observe / _rpc_span span source → SATISFIED
  - fq in .observe-allowlist.json (valid entry)              → EXEMPT
  - otherwise                                                → MISSING

Warn-mode: MISSING → exit 0 (report only). Allowlist integrity (stale entry,
missing rationale, bad category) is ALWAYS hard-fail, even in warn-mode.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import check_observe_coverage  # noqa: E402


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


# ── classification ───────────────────────────────────────────────────────────


def test_flags_uninstrumented_nontrivial_fn(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        def compute(items):
            total = 0
            for x in items:
                if x > 0:
                    total += x
            return total
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    missing = [x for x in findings if x.status == "MISSING"]
    assert any(x.qualname == "compute" for x in missing), findings


def test_trivial_fn_auto_exempt(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        def get_name(self):
            return self._name
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("get_name") == "EXEMPT_TRIVIAL", statuses


def test_dunder_auto_exempt(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        class C:
            def __init__(self, x):
                self.x = x
                self.y = x + 1
                self.z = x + 2
                for i in range(x):
                    self.x += i
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    # dunder is exempt regardless of body complexity
    assert statuses.get("C.__init__") == "EXEMPT_DUNDER", statuses


def test_property_auto_exempt(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        class C:
            @property
            def score(self):
                if self._a:
                    return self._b
                return self._c
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("C.score") == "EXEMPT_PROPERTY", statuses


def test_trace_span_satisfies(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        from yadgar._shared.observability.tracing import trace_span

        @trace_span("thing")
        def thing(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("thing") == "SATISFIED", statuses


def test_observe_decorator_satisfies(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="stage", name="thing")
        def thing(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("thing") == "SATISFIED", statuses


def test_allowlist_entry_exempts(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
        def inner_score(cand):
            s = 0
            for f in cand.features:
                s += f.weight * f.value
            return s
        """,
    )
    fq = f"{f.stem}:inner_score"
    allowlist = {
        fq: {
            "category": "hot-loop",
            "rationale": "per-candidate scorer; span/metric per call = 50+/op cardinality bloat",
        }
    }
    findings = check_observe_coverage.scan_file(f, allowlist=allowlist)
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("inner_score") == "EXEMPT_ALLOWLIST", statuses


# ── allowlist integrity (always hard) ────────────────────────────────────────


def test_allowlist_short_rationale_rejected():
    entry = {"category": "hot-loop", "rationale": "too short"}
    errs = check_observe_coverage.validate_allowlist_entry("m:f", entry)
    assert errs, "short rationale must be an error"


def test_allowlist_bad_category_rejected():
    entry = {"category": "nonsense", "rationale": "x" * 45}
    errs = check_observe_coverage.validate_allowlist_entry("m:f", entry)
    assert errs, "invalid category must be an error"


def test_allowlist_valid_entry_ok():
    entry = {
        "category": "hot-loop",
        "rationale": "per-candidate scorer invoked in the ranking inner loop; too hot to instrument",
    }
    errs = check_observe_coverage.validate_allowlist_entry("m:f", entry)
    assert not errs, errs


# ── path-glob exemption (STEP 1 — dir-level exemption by file path) ───────────


def test_glob_exempts_single_file(tmp_path):
    """A single-file glob (yadgar/viz_server.py form) makes its fns EXEMPT_GLOB."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "viz_server.py",
        """
        def render(items):
            out = []
            for x in items:
                if x:
                    out.append(x)
            return out
        """,
    )
    globs = {
        "yadgar/viz_server.py": {
            "category": "generated",
            "rationale": "presentation-layer render helpers; no ops value, exempted per obs-standard",
        }
    }
    findings = check_observe_coverage.scan_file(
        root / "viz_server.py", allowlist={}, exempt_globs=globs, repo_root=tmp_path
    )
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("render") == "EXEMPT_GLOB", statuses


def test_glob_exempts_recursive_dir(tmp_path):
    """A recursive glob (yadgar/seed/**) exempts every fn under the dir tree."""
    root = tmp_path / "yadgar"
    (root / "seed").mkdir(parents=True)
    _write(
        root / "seed",
        "_generate.py",
        """
        def build(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    globs = {
        "yadgar/seed/**": {
            "category": "generated",
            "rationale": "one-shot project bootstrap material generation; not a runtime ops path",
        }
    }
    findings = check_observe_coverage.scan_file(
        root / "seed" / "_generate.py", allowlist={}, exempt_globs=globs, repo_root=tmp_path
    )
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("build") == "EXEMPT_GLOB", statuses


def test_glob_matching_zero_files_is_stale(tmp_path):
    """A glob matching no in-scope function → STALE hard-fail even in warn-mode."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "mod.py",
        """
        def real_fn():
            x = 1
            for i in range(3):
                x += i
            return x
        """,
    )
    (tmp_path / ".observe-allowlist.json").write_text(
        json.dumps(
            {
                "_exempt_globs": {
                    "yadgar/nonexistent/**": {
                        "category": "generated",
                        "rationale": "this glob matches no functions and must fail the stale integrity check",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1  # zero-match glob is a stale error, always hard


def test_glob_bad_category_rejected():
    errs = check_observe_coverage.validate_glob_entry(
        "yadgar/x/**", {"category": "nonsense", "rationale": "x" * 45}
    )
    assert errs, "invalid glob category must be an error"


def test_glob_short_rationale_rejected():
    errs = check_observe_coverage.validate_glob_entry(
        "yadgar/x/**", {"category": "generated", "rationale": "too short"}
    )
    assert errs, "short glob rationale must be an error"


# ── @observe(exempt=...) reason governance (STEP 1 — close P5 hole) ────────────


def test_observe_exempt_empty_reason_not_satisfied(tmp_path):
    """@observe(exempt="") must NOT count as SATISFIED (the P5 governance hole)."""
    f = _write(
        tmp_path,
        "mod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(exempt="")
        def thing(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("thing") != "SATISFIED", statuses


def test_observe_exempt_short_reason_hard_fails(tmp_path):
    """@observe(exempt="<40 chars) → hard-fail integrity error even in warn-mode."""
    _write(
        tmp_path,
        "mod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(exempt="too short")
        def thing(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(tmp_path),
            "--allowlist-file",
            str(tmp_path / "nonexistent.json"),
        ]
    )
    assert rc == 1  # short exempt reason is an integrity error, always hard


def test_observe_exempt_valid_reason_is_exempt(tmp_path):
    """@observe(exempt=">=40 char reason") → EXEMPT_OBSERVE, no integrity error."""
    f = _write(
        tmp_path,
        "mod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(exempt="pure in-memory formatter with no I/O or branching worth a span")
        def thing(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    findings = check_observe_coverage.scan_file(f, allowlist={})
    statuses = {x.qualname: x.status for x in findings}
    assert statuses.get("thing") == "EXEMPT_OBSERVE", statuses


# ── glob-exempt audit report (ADR-0040 option C — non-failing CI safeguard) ───


def test_glob_exempt_report_lists_functions_on_stdout(tmp_path, capsys):
    """The lint prints the count + list of glob-exempted functions to STDOUT so
    glob drift (a whole-dir exemption hiding a modified/new fn) is auditable in
    CI output — without ever affecting the exit code (ADR-0040 option C)."""
    root = tmp_path / "yadgar"
    (root / "seed").mkdir(parents=True)
    _write(
        root / "seed",
        "_generate.py",
        """
        def build(items):
            total = 0
            for x in items:
                total += x
            return total

        def analyze(items):
            out = []
            for x in items:
                if x:
                    out.append(x)
            return out
        """,
    )
    (tmp_path / ".observe-allowlist.json").write_text(
        json.dumps(
            {
                "_exempt_globs": {
                    "yadgar/seed/**": {
                        "category": "generated",
                        "rationale": "one-shot project bootstrap material generation; not a runtime ops path",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc = check_observe_coverage.main(
        [
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0  # report is informational — never fails
    out = capsys.readouterr().out
    # Count line: both glob-exempted functions surfaced.
    assert "2" in out and "glob-exempt" in out.lower(), out
    # The specific glob-exempted functions are enumerated so drift is visible.
    assert "_generate:build" in out, out
    assert "_generate:analyze" in out, out
    # The owning glob is named so a reader knows which entry hides them.
    assert "yadgar/seed/**" in out, out


def test_glob_exempt_report_zero_globs_is_quiet_but_zero_exit(tmp_path, capsys):
    """No _exempt_globs → report emits a 0-count line and still exits 0."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "mod.py",
        """
        from yadgar._shared.observability.tracing import trace_span

        @trace_span("t")
        def thing(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    (tmp_path / ".observe-allowlist.json").write_text("{}", encoding="utf-8")
    rc = check_observe_coverage.main(
        [
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "glob-exempt" in out.lower(), out
    assert "0" in out, out


# ── warn-mode exit code ──────────────────────────────────────────────────────


def test_warn_mode_exit_zero_despite_missing(tmp_path, capsys):
    _write(
        tmp_path,
        "mod.py",
        """
        def compute(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    # empty allowlist file
    (tmp_path / ".observe-allowlist.json").write_text("{}", encoding="utf-8")
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(tmp_path),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0  # warn-mode never blocks on MISSING


def test_live_codebase_lint_runs():
    """Smoke: the real package scan runs and returns an int exit code in warn-mode.

    Ledger task 222: this had one `.parent` too many. check_observe_coverage.py
    lives in `<repo>/scripts/`, so `.parent.parent` IS the repo root and
    `.parent.parent.parent` is the directory ABOVE the checkout — making
    `repo_root / "yadgar"` resolve to the repo root itself rather than the
    package. It looked correct only because this checkout is named `yadgar`, the
    same as the package; a checkout named anything else would not even exist at
    that path. The scan therefore walked the whole repo — `.venv`, `.venv-test`,
    `.claude/worktrees` — and timed out at 300 s once those directories were
    populated, which is what it did in CI and locally.
    """
    repo_root = Path(check_observe_coverage.__file__).resolve().parent.parent
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(repo_root / "yadgar"),
            "--allowlist-file",
            str(repo_root / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0


def test_stale_allowlist_entry_hard_fails(tmp_path):
    """An allowlist key that maps to no existing function → hard fail even in warn-mode."""
    _write(
        tmp_path,
        "mod.py",
        """
        def real_fn():
            x = 1
            for i in range(3):
                x += i
            return x
        """,
    )
    (tmp_path / ".observe-allowlist.json").write_text(
        json.dumps(
            {
                "mod:ghost_fn": {
                    "category": "hot-loop",
                    "rationale": "this function no longer exists in the module and must fail stale check",
                }
            }
        ),
        encoding="utf-8",
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(tmp_path),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1  # stale entry is always hard


# ── ledger task 158: manual run must match what the pre-commit hook sees ─────


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.example"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)


_MISSING_FN_SRC = """
    def do_real_work():
        conn = open("/dev/null", "w")
        conn.write("x")
        conn.close()
        return 1
    """

_FIXED_FN_SRC = """
    from yadgar._shared.observability.observe import observe


    @observe()
    def do_real_work():
        conn = open("/dev/null", "w")
        conn.write("x")
        conn.close()
        return 1
    """


def test_git_index_sees_staged_content_not_unstaged_working_tree(tmp_path):
    """Task 158 repro: a MISSING fn is staged; its @observe fix is written to
    disk but never `git add`ed. pre-commit stashes that unstaged fix before
    running the hook, so the hook still sees the MISSING version. A scanner
    that reads straight off disk would report SATISFIED here — false green —
    because the working tree looks fixed even though nothing was staged.
    `use_git_index=True` must classify this MISSING, matching the hook.
    """
    _init_git_repo(tmp_path)
    f = _write(tmp_path, "mod.py", _MISSING_FN_SRC)
    subprocess.run(["git", "add", "mod.py"], cwd=tmp_path, check=True)
    # Unstaged working-tree edit: "fixes" the file on disk without staging it.
    f.write_text(textwrap.dedent(_FIXED_FN_SRC), encoding="utf-8")

    findings = check_observe_coverage.scan_file(
        f, allowlist={}, repo_root=tmp_path, use_git_index=True
    )
    assert [x.status for x in findings] == ["MISSING"]


def test_disk_read_is_the_false_green_this_fix_closes(tmp_path):
    """Same fixture as above with `use_git_index` left at its old default
    (False) — demonstrates the exact bug: reading the working tree reports
    SATISFIED even though nothing was ever staged.
    """
    _init_git_repo(tmp_path)
    f = _write(tmp_path, "mod.py", _MISSING_FN_SRC)
    subprocess.run(["git", "add", "mod.py"], cwd=tmp_path, check=True)
    f.write_text(textwrap.dedent(_FIXED_FN_SRC), encoding="utf-8")

    findings = check_observe_coverage.scan_file(f, allowlist={}, repo_root=tmp_path)
    assert [x.status for x in findings] == ["SATISFIED"]  # the false green


def test_git_index_falls_back_to_disk_outside_a_git_repo(tmp_path):
    """`use_git_index=True` against a non-git tmp_path (every other test's
    fixture shape) must fall back to a disk read rather than erroring, so
    existing --warn/--list-all callers are unaffected.
    """
    f = _write(tmp_path, "mod.py", _MISSING_FN_SRC)
    findings = check_observe_coverage.scan_file(
        f, allowlist={}, repo_root=tmp_path, use_git_index=True
    )
    assert [x.status for x in findings] == ["MISSING"]


def test_git_index_falls_back_to_disk_for_untracked_file(tmp_path):
    """A brand-new file that was never `git add`ed isn't in the index at all.
    pre-commit's stash never touches untracked files, so both a manual scan
    and the hook see its disk content — the fallback must match that.
    """
    _init_git_repo(tmp_path)
    f = _write(tmp_path, "mod.py", _MISSING_FN_SRC)  # never staged
    findings = check_observe_coverage.scan_file(
        f, allowlist={}, repo_root=tmp_path, use_git_index=True
    )
    assert [x.status for x in findings] == ["MISSING"]


def test_main_no_git_index_flag_opts_back_into_disk_scan(tmp_path):
    """`--no-git-index` is the documented escape hatch back to the pre-fix
    working-tree scan, for deliberately linting mid-edit / unstaged work.

    `root` must be named `yadgar` (matching the real `--root <repo>/yadgar`
    invocation) so `main()`'s repo-root derivation resolves to `tmp_path` —
    the same convention the existing glob tests use.
    """
    _init_git_repo(tmp_path)
    root = tmp_path / "yadgar"
    root.mkdir()
    f = _write(root, "mod.py", _MISSING_FN_SRC)
    subprocess.run(["git", "add", "yadgar/mod.py"], cwd=tmp_path, check=True)
    f.write_text(textwrap.dedent(_FIXED_FN_SRC), encoding="utf-8")  # unstaged fix

    rc_default = check_observe_coverage.main(
        ["--root", str(root), "--allowlist-file", str(tmp_path / ".observe-allowlist.json")]
    )
    rc_no_index = check_observe_coverage.main(
        [
            "--no-git-index",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc_default == 1  # git-index default: sees the staged (unfixed) content
    assert rc_no_index == 0  # opt-out: sees the working tree (fixed on disk)


def test_main_git_index_covers_the_allowlist_file_too(tmp_path):
    """The allowlist itself is a tracked file (`.observe-allowlist.json`); an
    exemption written to disk but not staged must not launder a staged
    MISSING finding, the same way an unstaged code fix must not.
    """
    _init_git_repo(tmp_path)
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(root, "mod.py", _MISSING_FN_SRC)
    alf = tmp_path / ".observe-allowlist.json"
    alf.write_text(json.dumps({}), encoding="utf-8")
    subprocess.run(
        ["git", "add", "yadgar/mod.py", ".observe-allowlist.json"], cwd=tmp_path, check=True
    )
    # Unstaged: exempt the fn in the allowlist, but never `git add` it.
    alf.write_text(
        json.dumps(
            {
                "mod:do_real_work": {
                    "category": "pre-existing",
                    "rationale": "unstaged exemption — must not launder the staged MISSING finding",
                }
            }
        ),
        encoding="utf-8",
    )

    rc = check_observe_coverage.main(["--root", str(root), "--allowlist-file", str(alf)])
    assert rc == 1  # staged allowlist (empty) still applies; MISSING is not exempted


def test_git_index_scans_a_file_deleted_unstaged_from_disk(tmp_path):
    """Deletion axis of task 158: `rm`ing a tracked file without `git rm`
    leaves it staged (in the index) but absent from the working tree.
    `Path.rglob` can't see a file that isn't on disk, so a plain manual scan
    silently drops whatever MISSING functions it carried — but pre-commit's
    stash restores exactly that staged content before running the hook, so
    the hook still scans it. `use_git_index=True` must enumerate + read the
    file from the git index, not the (now-empty) working tree.
    """
    _init_git_repo(tmp_path)
    root = tmp_path / "yadgar"
    root.mkdir()
    f = _write(root, "mod.py", _MISSING_FN_SRC)
    subprocess.run(["git", "add", "yadgar/mod.py"], cwd=tmp_path, check=True)
    f.unlink()  # unstaged working-tree deletion — never `git rm`'d

    rc = check_observe_coverage.main(
        ["--root", str(root), "--allowlist-file", str(tmp_path / ".observe-allowlist.json")]
    )
    assert rc == 1  # do_real_work is still MISSING per the staged content


def test_no_git_index_misses_the_unstaged_deletion(tmp_path):
    """Same fixture with `--no-git-index`: `Path.rglob` can't find the
    deleted file, so the MISSING function it carried goes unreported — the
    false green this fix closes on the deletion axis.
    """
    _init_git_repo(tmp_path)
    root = tmp_path / "yadgar"
    root.mkdir()
    f = _write(root, "mod.py", _MISSING_FN_SRC)
    subprocess.run(["git", "add", "yadgar/mod.py"], cwd=tmp_path, check=True)
    f.unlink()

    rc = check_observe_coverage.main(
        [
            "--no-git-index",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0  # the false green: the deleted file is simply never scanned
