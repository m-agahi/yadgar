"""Tests for scripts/check_metric_writers.py (I23 — dead metric lint).

TDD: tests are written before the implementation. They define the required
behaviour of the AST lint script. Run them with:
  uv run pytest yadgar/tests/test_check_metric_writers.py
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "check_metric_writers.py"


def run_script(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the lint script as a subprocess and return the result."""
    import os

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Fixtures helpers (synthetic temp-file pairs)
# ---------------------------------------------------------------------------


def _write_fixture(tmp_path: Path, metrics_src: str, other_src: str | None = None) -> Path:
    """
    Create a minimal metrics module and optional writer file under tmp_path.

    Returns the tmp_path so callers can pass --metrics-dir to the script
    (when that CLI flag is supported) or use it for custom invocation.
    """
    metrics_file = tmp_path / "metrics_fixture.py"
    metrics_file.write_text(textwrap.dedent(metrics_src))
    if other_src is not None:
        writer_file = tmp_path / "writer_fixture.py"
        writer_file.write_text(textwrap.dedent(other_src))
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1 — dead metric triggers exit 1 with the metric in output
# ---------------------------------------------------------------------------


def test_dead_metric_fails(tmp_path):
    """A Gauge with no writer anywhere should produce exit code 1."""
    metrics_src = """\
        from prometheus_client import Gauge
        _test_metric = Gauge("test_metric_total", "A test metric")
    """
    _write_fixture(tmp_path, metrics_src)

    result = run_script(
        "--metrics-files",
        str(tmp_path / "metrics_fixture.py"),
        "--search-dirs",
        str(tmp_path),
        "--exclude-files",
        str(tmp_path / "metrics_fixture.py"),
    )
    assert result.returncode == 1, (
        f"Expected exit 1 but got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "_test_metric" in result.stdout, (
        f"Expected '_test_metric' in output.\nstdout: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 2 — metric with a .set() writer passes (exit 0)
# ---------------------------------------------------------------------------


def test_metric_with_writer_passes(tmp_path):
    """A Gauge that has a .set() call in a sibling file should exit 0."""
    metrics_src = """\
        from prometheus_client import Gauge
        _test_metric = Gauge("test_metric_total", "A test metric")
    """
    writer_src = """\
        from metrics_fixture import _test_metric
        _test_metric.set(0)
    """
    _write_fixture(tmp_path, metrics_src, writer_src)

    result = run_script(
        "--metrics-files",
        str(tmp_path / "metrics_fixture.py"),
        "--search-dirs",
        str(tmp_path),
        "--exclude-files",
        str(tmp_path / "metrics_fixture.py"),
    )
    assert result.returncode == 0, (
        f"Expected exit 0 but got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 3 — --list-all shows metrics + writer counts
# ---------------------------------------------------------------------------


def test_list_all(tmp_path):
    """--list-all should print every declared metric with its writer count."""
    metrics_src = """\
        from prometheus_client import Gauge, Counter
        _my_gauge = Gauge("my_gauge", "gauge")
        _my_counter = Counter("my_counter", "counter")
    """
    writer_src = """\
        from metrics_fixture import _my_gauge
        _my_gauge.set(42)
    """
    _write_fixture(tmp_path, metrics_src, writer_src)

    result = run_script(
        "--list-all",
        "--metrics-files",
        str(tmp_path / "metrics_fixture.py"),
        "--search-dirs",
        str(tmp_path),
        "--exclude-files",
        str(tmp_path / "metrics_fixture.py"),
    )
    assert result.returncode == 0, f"--list-all should not fail. stdout: {result.stdout}"
    assert "_my_gauge" in result.stdout
    assert "_my_counter" in result.stdout
    # _my_gauge has 1 writer; _my_counter has 0. Both must be listed.
    lines = result.stdout.strip().splitlines()
    assert len(lines) >= 2, f"Expected ≥2 lines, got: {result.stdout}"


# ---------------------------------------------------------------------------
# Test 4 — --allowlist exempts the named metric
# ---------------------------------------------------------------------------


def test_allowlist_exempts_metric(tmp_path):
    """--allowlist <name> should suppress the dead-metric error for that var."""
    metrics_src = """\
        from prometheus_client import Gauge
        _dead_metric = Gauge("dead_metric_total", "A dead metric")
    """
    _write_fixture(tmp_path, metrics_src)

    result = run_script(
        "--allowlist",
        "_dead_metric",
        "--metrics-files",
        str(tmp_path / "metrics_fixture.py"),
        "--search-dirs",
        str(tmp_path),
        "--exclude-files",
        str(tmp_path / "metrics_fixture.py"),
    )
    assert result.returncode == 0, f"Allowlisted metric should not fail. stdout: {result.stdout}"


# ---------------------------------------------------------------------------
# Test 5 — .labels(...).set(...) chained pattern counts as a writer
# ---------------------------------------------------------------------------


def test_labels_chained_set(tmp_path):
    """labels().set() chain on a metric variable should be detected as a writer."""
    metrics_src = """\
        from prometheus_client import Gauge
        _labelled_metric = Gauge("labelled_metric", "labelled", ["mode"])
    """
    writer_src = """\
        from metrics_fixture import _labelled_metric
        _labelled_metric.labels(mode="fast").set(1.0)
    """
    _write_fixture(tmp_path, metrics_src, writer_src)

    result = run_script(
        "--metrics-files",
        str(tmp_path / "metrics_fixture.py"),
        "--search-dirs",
        str(tmp_path),
        "--exclude-files",
        str(tmp_path / "metrics_fixture.py"),
    )
    assert result.returncode == 0, (
        f"labels().set() should count. stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 6 — indirect reference (metric passed as function argument) counts
# ---------------------------------------------------------------------------


def test_indirect_function_arg_reference(tmp_path):
    """Passing a metric variable as a function argument counts as a writer reference."""
    metrics_src = """\
        from prometheus_client import Counter
        _passed_metric = Counter("passed_metric_total", "passed as arg")
    """
    writer_src = """\
        from metrics_fixture import _passed_metric

        def record_failure(metric, label):
            metric.inc()

        record_failure(_passed_metric, "err")
    """
    _write_fixture(tmp_path, metrics_src, writer_src)

    result = run_script(
        "--metrics-files",
        str(tmp_path / "metrics_fixture.py"),
        "--search-dirs",
        str(tmp_path),
        "--exclude-files",
        str(tmp_path / "metrics_fixture.py"),
    )
    assert result.returncode == 0, (
        f"Indirect arg reference should count. stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 7 — live codebase (post PR-A..K) must produce exit 0
# ---------------------------------------------------------------------------


def test_live_codebase_all_pass():
    """
    Run the lint against the actual yadgar codebase. This is the live gate.
    All metrics declared in yadgar/metrics.py and yadgar/embed_service_metrics.py
    must have ≥1 writer (or be explicitly allowlisted if they are known
    placeholder set(0) metrics awaiting infrastructure).

    If this test fails, a real dead-metric exists and must be fixed in this PR.
    """
    repo_root = Path(__file__).parent.parent.parent
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, (
        f"Live codebase has dead metrics (I23 violation).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
