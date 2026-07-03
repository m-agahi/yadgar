"""Unit tests for scripts/check_backend_bump.py.

Tests the pure ``check()`` function — no git fixture required.
Three scenarios specified in v5.1.6 task:
  (a) backend file changed without version bump → fail
  (b) backend file changed with version bump → pass
  (c) only non-backend files changed → pass
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the hook from scripts/ — not a package, use direct path injection.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_backend_bump import _is_backend_build_input, check, collect_ci_inputs  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SERVER_JSON_501 = json.dumps({"version": "5.1.5", "backend_version": "5.0.1"})
_SERVER_JSON_502 = json.dumps({"version": "5.1.6", "backend_version": "5.0.2"})


# ---------------------------------------------------------------------------
# _is_backend_build_input
# ---------------------------------------------------------------------------


class TestIsBackendBuildInput:
    def test_entrypoint_backend_sh(self) -> None:
        assert _is_backend_build_input("entrypoint-backend.sh") is True

    def test_dockerfile_backend(self) -> None:
        assert _is_backend_build_input("Dockerfile.backend") is True

    def test_backend_dir_file(self) -> None:
        assert _is_backend_build_input("backend/run.sh") is True

    def test_yadgar_backend_subpackage_file(self) -> None:
        # v5.60: the 4 backend modules moved under yadgar/backend/.
        assert _is_backend_build_input("yadgar/backend/embed_service.py") is True

    def test_pyproject_toml_not_backend(self) -> None:
        assert _is_backend_build_input("pyproject.toml") is False

    def test_entrypoint_sh_not_backend(self) -> None:
        # entrypoint.sh (core) is NOT a backend build input
        assert _is_backend_build_input("entrypoint.sh") is False

    def test_yadgar_source_not_backend(self) -> None:
        assert _is_backend_build_input("yadgar/vacuum/__init__.py") is False


# ---------------------------------------------------------------------------
# Scenario (a): backend file changed, no version bump → FAIL
# ---------------------------------------------------------------------------


class TestBackendChangedNoVersionBump:
    def test_entrypoint_changed_same_version(self) -> None:
        """entrypoint-backend.sh staged, but backend_version unchanged → fail."""
        ok, msg = check(
            staged_files=["entrypoint-backend.sh"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=_SERVER_JSON_501,
        )
        assert ok is False
        assert "backend_version" in msg
        assert "unchanged" in msg

    def test_dockerfile_changed_same_version(self) -> None:
        """Dockerfile.backend staged, backend_version unchanged → fail."""
        ok, msg = check(
            staged_files=["Dockerfile.backend"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=_SERVER_JSON_501,
        )
        assert ok is False
        assert "unchanged" in msg

    def test_server_json_not_staged(self) -> None:
        """Backend file staged but server.json not in index → fail with helpful message."""
        ok, msg = check(
            staged_files=["entrypoint-backend.sh"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=None,
        )
        assert ok is False
        assert "server.json" in msg
        assert "not staged" in msg

    def test_backend_dir_file_changed_no_bump(self) -> None:
        """File under backend/ staged, backend_version unchanged → fail."""
        ok, msg = check(
            staged_files=["backend/init.sh"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=_SERVER_JSON_501,
        )
        assert ok is False

    def test_multiple_backend_files_no_bump(self) -> None:
        """Multiple backend files staged, no version bump → fail listing all files."""
        ok, msg = check(
            staged_files=["entrypoint-backend.sh", "Dockerfile.backend"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=_SERVER_JSON_501,
        )
        assert ok is False
        assert "entrypoint-backend.sh" in msg
        assert "Dockerfile.backend" in msg


# ---------------------------------------------------------------------------
# Scenario (b): backend file changed WITH version bump → PASS
# ---------------------------------------------------------------------------


class TestBackendChangedWithVersionBump:
    def test_entrypoint_changed_with_bump(self) -> None:
        """entrypoint-backend.sh staged + backend_version bumped → pass."""
        ok, msg = check(
            staged_files=["entrypoint-backend.sh", "server.json"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=_SERVER_JSON_502,
        )
        assert ok is True
        assert "5.0.1" in msg
        assert "5.0.2" in msg

    def test_dockerfile_changed_with_bump(self) -> None:
        """Dockerfile.backend staged + backend_version bumped → pass."""
        ok, msg = check(
            staged_files=["Dockerfile.backend"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=_SERVER_JSON_502,
        )
        assert ok is True

    def test_head_missing_backend_version(self) -> None:
        """If HEAD server.json lacks backend_version entirely, any value is a bump."""
        head_no_ver = json.dumps({"version": "5.1.5"})  # no backend_version key
        ok, _msg = check(
            staged_files=["entrypoint-backend.sh"],
            server_json_head=head_no_ver,
            server_json_staged=_SERVER_JSON_502,
        )
        assert ok is True

    def test_initial_commit_no_head(self) -> None:
        """HEAD does not exist (initial commit) → any staged version is accepted."""
        ok, _msg = check(
            staged_files=["entrypoint-backend.sh"],
            server_json_head=None,
            server_json_staged=_SERVER_JSON_502,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# Scenario (c): only non-backend files changed → PASS
# ---------------------------------------------------------------------------


class TestNonBackendChanges:
    def test_only_python_source_changed(self) -> None:
        """Pure Python changes → hook is a no-op (passes)."""
        ok, _msg = check(
            staged_files=["yadgar/vacuum/__init__.py", "yadgar/tests/test_vacuum.py"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=None,  # server.json not even staged
        )
        assert ok is True

    def test_only_pyproject_changed(self) -> None:
        """pyproject.toml bump only → no backend check needed."""
        ok, _msg = check(
            staged_files=["pyproject.toml", "server.json"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=json.dumps({"version": "5.1.6", "backend_version": "5.0.1"}),
        )
        assert ok is True

    def test_empty_staged_files(self) -> None:
        """No files staged → pass."""
        ok, _msg = check(
            staged_files=[],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=None,
        )
        assert ok is True

    def test_entrypoint_sh_core_not_triggering(self) -> None:
        """entrypoint.sh (core, not backend) must not trigger the check."""
        ok, _msg = check(
            staged_files=["entrypoint.sh"],
            server_json_head=_SERVER_JSON_501,
            server_json_staged=None,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# collect_ci_inputs — CI mode (injected git runner, no real git required)
# ---------------------------------------------------------------------------


def _make_fake_git(
    merge_base: str,
    changed_files: list[str],
    base_server_json: str | None,
    head_server_json: str | None,
) -> object:
    """Return a callable that stubs git responses for collect_ci_inputs tests."""

    def run_git(args: list[str]) -> str:
        cmd = args[0] if args else ""
        if cmd == "merge-base":
            return merge_base + "\n"
        if cmd == "diff":
            return "\n".join(changed_files) + ("\n" if changed_files else "")
        if cmd == "show":
            ref_and_path = args[1] if len(args) > 1 else ""
            if ref_and_path.endswith(":server.json"):
                if ref_and_path.startswith(merge_base):
                    return base_server_json or ""
                # HEAD:server.json
                return head_server_json or ""
        return ""

    return run_git


class TestCollectCiInputs:
    """collect_ci_inputs uses merge-base, not the moving tip of base_ref."""

    def test_returns_changed_files_and_versions(self) -> None:
        """Happy path: changed file list and both server.json versions returned."""
        fake_git = _make_fake_git(
            merge_base="abc123",
            changed_files=["yadgar/backend/embed_service.py"],
            base_server_json=_SERVER_JSON_501,
            head_server_json=_SERVER_JSON_502,
        )
        changed, base_srv, head_srv = collect_ci_inputs("origin/master", fake_git)
        assert changed == ["yadgar/backend/embed_service.py"]
        assert base_srv == _SERVER_JSON_501
        assert head_srv == _SERVER_JSON_502

    def test_no_backend_files_changed(self) -> None:
        """Pure Python change — no backend files in diff."""
        fake_git = _make_fake_git(
            merge_base="abc123",
            changed_files=["yadgar/vacuum/__init__.py"],
            base_server_json=_SERVER_JSON_501,
            head_server_json=_SERVER_JSON_501,
        )
        changed, base_srv, head_srv = collect_ci_inputs("origin/master", fake_git)
        assert changed == ["yadgar/vacuum/__init__.py"]
        # check() would return ok=True for these inputs
        ok, _msg = check(changed, base_srv, head_srv)
        assert ok is True

    def test_missing_merge_base_falls_back_to_base_ref(self) -> None:
        """If merge-base returns empty, base_ref itself is used as the base."""

        calls: list[list[str]] = []

        def run_git(args: list[str]) -> str:
            calls.append(args)
            if args[0] == "merge-base":
                return ""  # simulate failure (shallow clone, no common ancestor)
            if args[0] == "diff":
                return "yadgar/backend/embed_service.py\n"
            if args[0] == "show":
                return _SERVER_JSON_501
            return ""

        changed, base_srv, _head_srv = collect_ci_inputs("origin/master", run_git)
        # Fallback: diff is called with base_ref, not an empty string.
        diff_call = next(c for c in calls if c[0] == "diff")
        assert "origin/master" in diff_call
        assert changed == ["yadgar/backend/embed_service.py"]

    def test_server_json_absent_at_base(self) -> None:
        """server.json absent at merge-base → base_server returned as None."""
        fake_git = _make_fake_git(
            merge_base="abc123",
            changed_files=["yadgar/backend/embed_service.py"],
            base_server_json=None,
            head_server_json=_SERVER_JSON_502,
        )
        changed, base_srv, head_srv = collect_ci_inputs("origin/master", fake_git)
        assert base_srv is None
        assert head_srv == _SERVER_JSON_502
        # check() treats None head as initial commit → passes with any staged version
        ok, _msg = check(changed, base_srv, head_srv)
        assert ok is True

    def test_full_ci_flow_backend_changed_no_bump_fails(self) -> None:
        """End-to-end CI gate: backend changed + version unchanged → fail."""
        fake_git = _make_fake_git(
            merge_base="abc123",
            changed_files=["yadgar/backend/embed_service.py", "server.json"],
            base_server_json=_SERVER_JSON_501,
            head_server_json=_SERVER_JSON_501,  # same version!
        )
        changed, base_srv, head_srv = collect_ci_inputs("origin/master", fake_git)
        ok, msg = check(changed, base_srv, head_srv)
        assert ok is False
        assert "backend_version" in msg
        assert "unchanged" in msg

    def test_full_ci_flow_backend_changed_with_bump_passes(self) -> None:
        """End-to-end CI gate: backend changed + version bumped → pass."""
        fake_git = _make_fake_git(
            merge_base="abc123",
            changed_files=["yadgar/backend/embed_service.py", "server.json"],
            base_server_json=_SERVER_JSON_501,
            head_server_json=_SERVER_JSON_502,
        )
        changed, base_srv, head_srv = collect_ci_inputs("origin/master", fake_git)
        ok, msg = check(changed, base_srv, head_srv)
        assert ok is True
        assert "5.0.2" in msg
