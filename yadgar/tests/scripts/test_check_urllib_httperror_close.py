"""Meta-tests for the urllib HTTPError/response-close lint (Car 0036).

On Python 3.14, a caught ``urllib.error.HTTPError`` that is never closed leaks
a tempfile wrapper and emits a ResourceWarning (fatal under the zero-warning
gate, ADR-0087) — the same class of bug the successful-response path has when
``urlopen()``'s return value is discarded or never closed. This guard is the
anti-recurrence artifact for the Car 0036 sweep: it AST-scans every non-test
.py file for both shapes.

Non-e2e, hermetic.
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):  # type: ignore[return]
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


guard = _load("check_urllib_httperror_close.py")


def _make_repo(tmp_path: Path, source: str, allowlist: dict | None = None) -> Path:
    pkg = tmp_path / "yadgar" / "core"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(textwrap.dedent(source), encoding="utf-8")
    if allowlist is not None:
        (tmp_path / ".urllib-httperror-close-allowlist.json").write_text(
            json.dumps(allowlist), encoding="utf-8"
        )
    return tmp_path


# ---------------------------------------------------------------------------
# Real-codebase passthrough
# ---------------------------------------------------------------------------


def test_real_codebase_is_clean() -> None:
    """The actual repo (with its checked-in allowlist) has zero violations."""
    errors = guard.check(_REPO_ROOT)
    assert errors == [], "Unexpected violations:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Rule (a) — except HTTPError not closed
# ---------------------------------------------------------------------------


class TestRuleA:
    def test_unbound_handler_is_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.error
            import urllib.request

            def caller():
                try:
                    urllib.request.urlopen("http://x")
                except urllib.error.HTTPError:
                    return None
            """,
        )
        violations = guard.collect_violations(repo)
        assert any("mod.py:8" in k for k in violations), violations

    def test_close_call_on_bound_name_is_not_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.error
            import urllib.request

            def caller():
                try:
                    with urllib.request.urlopen("http://x") as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    e.close()
                    return None
            """,
        )
        violations = guard.collect_violations(repo)
        assert not any(":9" in k for k in violations), violations

    def test_helper_call_passed_bound_name_is_not_a_violation(self, tmp_path: Path) -> None:
        """Matches the runtime_config_client.py / session-start-context.py shape:
        ``except HTTPError as e: _close_quietly(e)``."""
        repo = _make_repo(
            tmp_path,
            """
            import urllib.error
            import urllib.request

            def _close_quietly(exc):
                exc.close()

            def caller():
                try:
                    with urllib.request.urlopen("http://x") as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    _close_quietly(e)
                    return None
            """,
        )
        violations = guard.collect_violations(repo)
        assert not any(":12" in k for k in violations), violations

    def test_handler_body_never_closing_bound_name_is_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.error
            import urllib.request

            def caller():
                try:
                    with urllib.request.urlopen("http://x") as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    return None
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":9" in k for k in violations), violations

    def test_non_urllib_httperror_is_not_flagged(self, tmp_path: Path) -> None:
        """httpx.HTTPError is a different exception hierarchy (no py3.14
        tempfile-wrapper leak) — rule (a) must not false-positive on it."""
        repo = _make_repo(
            tmp_path,
            """
            import httpx

            def caller():
                try:
                    httpx.get("http://x")
                except httpx.HTTPError as exc:
                    raise RuntimeError(str(exc)) from exc
            """,
        )
        violations = guard.collect_violations(repo)
        assert violations == {}, violations

    def test_bare_name_from_import_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            from urllib.error import HTTPError
            import urllib.request

            def caller():
                try:
                    urllib.request.urlopen("http://x")
                except HTTPError:
                    return None
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":8" in k for k in violations), violations

    def test_aliased_module_import_is_flagged(self, tmp_path: Path) -> None:
        """``import urllib.error as _err`` then ``_err.HTTPError`` — the
        hook-script shape (post-tool-capture.py, prompt-recall.py, etc)."""
        repo = _make_repo(
            tmp_path,
            """
            def caller():
                import urllib.error as _err
                import urllib.request as _req
                try:
                    _req.urlopen("http://x")
                except _err.HTTPError:
                    return None
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":7" in k for k in violations), violations


# ---------------------------------------------------------------------------
# Rule (b) — urlopen() result not closed
# ---------------------------------------------------------------------------


class TestRuleB:
    def test_bare_expression_call_is_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                urllib.request.urlopen("http://x")
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":5" in k for k in violations), violations

    def test_with_block_is_not_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                with urllib.request.urlopen("http://x") as resp:
                    return resp.read()
            """,
        )
        violations = guard.collect_violations(repo)
        assert violations == {}, violations

    def test_closing_wrapped_with_block_is_not_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import contextlib
            import urllib.request

            def caller():
                with contextlib.closing(urllib.request.urlopen("http://x")) as resp:
                    return resp.read()
            """,
        )
        violations = guard.collect_violations(repo)
        assert violations == {}, violations

    def test_assigned_and_later_closed_is_not_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                resp = urllib.request.urlopen("http://x")
                try:
                    return resp.read()
                finally:
                    resp.close()
            """,
        )
        violations = guard.collect_violations(repo)
        assert violations == {}, violations

    def test_assigned_and_never_closed_is_a_violation(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                resp = urllib.request.urlopen("http://x")
                return resp.read()
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":5" in k for k in violations), violations

    def test_returned_directly_is_a_violation(self, tmp_path: Path) -> None:
        """Pass-through wrapper shape (e.g. daemon/runtime.py::_safe_urlopen) —
        flagged so it must be explicitly allowlisted, not silently trusted."""
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def _safe_urlopen(url, **kwargs):
                return urllib.request.urlopen(url, **kwargs)
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":5" in k for k in violations), violations

    def test_aliased_passthrough_wrapper_name_is_detected(self, tmp_path: Path) -> None:
        """A bare-name call ending in 'urlopen' (e.g. ``_safe_urlopen(...)``) is
        itself treated as a urlopen call site by callers."""
        repo = _make_repo(
            tmp_path,
            """
            def _safe_urlopen(url):
                ...

            def caller():
                _safe_urlopen("http://x")
            """,
        )
        violations = guard.collect_violations(repo)
        assert any(":6" in k for k in violations), violations


# ---------------------------------------------------------------------------
# Allowlist governance
# ---------------------------------------------------------------------------


class TestAllowlistGovernance:
    def test_allowlisted_violation_is_clean(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                urllib.request.urlopen("http://x")
            """,
            allowlist={
                "yadgar/core/mod.py:5": {
                    "rationale": "x" * 40 + " — synthetic test fixture allowlist entry."
                }
            },
        )
        errors = guard.check(repo)
        assert errors == [], errors

    def test_stale_allowlist_entry_is_an_error(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                with urllib.request.urlopen("http://x") as resp:
                    return resp.read()
            """,
            allowlist={
                "yadgar/core/mod.py:5": {
                    "rationale": "x" * 40 + " — no longer a violation, should be flagged stale."
                }
            },
        )
        errors = guard.check(repo)
        assert any("STALE" in e for e in errors), errors

    def test_short_rationale_is_malformed(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            """
            import urllib.request

            def caller():
                urllib.request.urlopen("http://x")
            """,
            allowlist={"yadgar/core/mod.py:5": {"rationale": "too short"}},
        )
        errors = guard.check(repo)
        assert any("MALFORMED" in e for e in errors), errors

    def test_malformed_json_allowlist_is_an_error(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "import urllib.request\n")
        (repo / ".urllib-httperror-close-allowlist.json").write_text("{not json", encoding="utf-8")
        errors = guard.check(repo)
        assert any("MALFORMED allowlist" in e for e in errors), errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_exits_zero_on_clean_repo(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        """
        import urllib.request

        def caller():
            with urllib.request.urlopen("http://x") as resp:
                return resp.read()
        """,
    )
    assert guard.main(["--repo-root", str(repo)]) == 0


def test_main_exits_one_on_violation(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        """
        import urllib.request

        def caller():
            urllib.request.urlopen("http://x")
        """,
    )
    assert guard.main(["--repo-root", str(repo)]) == 1


def test_list_violations_always_exits_zero(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        """
        import urllib.request

        def caller():
            urllib.request.urlopen("http://x")
        """,
    )
    assert guard.main(["--repo-root", str(repo), "--list-violations"]) == 0
