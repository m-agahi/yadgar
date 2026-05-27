"""Tests for scripts/check_trace_spans.py (I24 — @trace_span lint).

TDD: tests are written before the implementation. They define the required
behaviour of the AST lint script. Run them with:
  uv run pytest yadgar/tests/test_check_trace_spans.py
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "check_trace_spans.py"


def run_script(*args: str) -> subprocess.CompletedProcess:
    """Run the lint script as a subprocess and return the result."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Fixtures: synthetic yadgar/server/http.py in a temp root
# ---------------------------------------------------------------------------


def _make_fake_repo(tmp_path: Path, http_src: str) -> Path:
    """Create a minimal fake repo layout under tmp_path with a custom http.py."""
    server_dir = tmp_path / "yadgar" / "server"
    server_dir.mkdir(parents=True)
    (server_dir / "http.py").write_text(textwrap.dedent(http_src))
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1 — missing @trace_span → exit 1 with function name in output
# ---------------------------------------------------------------------------


def test_missing_trace_span_fails(tmp_path):
    """A public top-level async def without @trace_span should trigger exit 1."""
    http_src = """\
        async def health_check(request):
            return {}
    """
    repo = _make_fake_repo(tmp_path, http_src)

    # Invoke scan() directly via import to avoid needing the full script CLI
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_trace_spans", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    all_fns, missing = mod.scan(repo_root=repo)
    assert len(all_fns) == 1
    assert len(missing) == 1
    assert missing[0].func_name == "health_check"


# ---------------------------------------------------------------------------
# Test 2 — @trace_span present → exit 0
# ---------------------------------------------------------------------------


def test_trace_span_present_passes(tmp_path):
    """A public function with @trace_span should be reported as OK (no missing)."""
    http_src = """\
        @trace_span("hook.health")
        async def health_check(request):
            return {}
    """
    repo = _make_fake_repo(tmp_path, http_src)

    import importlib.util

    spec = importlib.util.spec_from_file_location("check_trace_spans", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    all_fns, missing = mod.scan(repo_root=repo)
    assert len(all_fns) == 1
    assert len(missing) == 0


# ---------------------------------------------------------------------------
# Test 3 — private functions (starting with _) are skipped
# ---------------------------------------------------------------------------


def test_private_functions_skipped(tmp_path):
    """Functions starting with _ should not be checked."""
    http_src = """\
        async def _helper(request):
            pass

        async def _make_event_stream(request):
            pass
    """
    repo = _make_fake_repo(tmp_path, http_src)

    import importlib.util

    spec = importlib.util.spec_from_file_location("check_trace_spans", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    all_fns, missing = mod.scan(repo_root=repo)
    assert len(all_fns) == 0  # private functions not in scope
    assert len(missing) == 0


# ---------------------------------------------------------------------------
# Test 4 — --allowlist exempts named functions
# ---------------------------------------------------------------------------


def test_allowlist_exempts_function(tmp_path):
    """--allowlist <name> should suppress the missing-span error for that function."""
    http_src = """\
        async def unspanned_fn(request):
            return {}
    """
    repo = _make_fake_repo(tmp_path, http_src)

    import importlib.util

    spec = importlib.util.spec_from_file_location("check_trace_spans", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    all_fns, missing = mod.scan(repo_root=repo, allowlist={"unspanned_fn"})
    assert len(missing) == 0, f"Allowlisted function should not appear in missing. Got: {missing}"


# ---------------------------------------------------------------------------
# Test 5 — @trace_span() with no args also counts
# ---------------------------------------------------------------------------


def test_trace_span_no_args_counts(tmp_path):
    """@trace_span() (call with no arguments) should still satisfy the requirement."""
    http_src = """\
        @trace_span()
        async def hook_compact(request):
            return {}
    """
    repo = _make_fake_repo(tmp_path, http_src)

    import importlib.util

    spec = importlib.util.spec_from_file_location("check_trace_spans", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    all_fns, missing = mod.scan(repo_root=repo)
    assert len(missing) == 0


# ---------------------------------------------------------------------------
# Test 6 — mix of spanned + unspanned (only unspanned reported)
# ---------------------------------------------------------------------------


def test_mixed_functions(tmp_path):
    """Only un-spanned public functions should appear in missing list."""
    http_src = """\
        @trace_span("hook.health")
        async def health_check(request):
            return {}

        async def api_stats(request):
            return {}

        async def _private_helper():
            pass
    """
    repo = _make_fake_repo(tmp_path, http_src)

    import importlib.util

    spec = importlib.util.spec_from_file_location("check_trace_spans", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    all_fns, missing = mod.scan(repo_root=repo)
    assert len(all_fns) == 2  # health_check + api_stats (not _private_helper)
    assert len(missing) == 1
    assert missing[0].func_name == "api_stats"


# ---------------------------------------------------------------------------
# Test 7 — live codebase must produce exit 0
# ---------------------------------------------------------------------------


def test_live_codebase_all_pass():
    """
    Run the lint against the actual yadgar codebase. This is the live gate.
    All public handler functions in yadgar/server/http.py must have @trace_span.

    If this test fails, a real missing-span exists and must be fixed in this PR.
    """
    repo_root = Path(__file__).parent.parent.parent
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, (
        f"Live codebase has un-spanned public handlers (I24 violation).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
