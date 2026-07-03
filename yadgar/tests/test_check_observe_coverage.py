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
import sys
import textwrap
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent / "scripts")
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
        from yadgar.tracing import trace_span

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
        from yadgar.observability.observe import observe

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
    """Smoke: the real repo scan runs and returns an int exit code in warn-mode."""
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
