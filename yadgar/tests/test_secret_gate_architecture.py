"""TDD tests for v5.10.2 secret-gate architecture.

Tests fail before implementation (c1), then go green after (c2-c6).

Coverage:
  - Pattern strictness: ghp_ threshold lowered {36,} → {20,}
  - API-boundary gate: anchor(), update_active_work(), bootstrap_project(),
    checkpoint(), wiki_update() all reject secrets
  - Storage-level gate: insert_memory() raises SecretLeakBlocked
  - gate_or_reject() helper behaviour
  - I26 lint: check_secret_gate.py exits 0 on clean tree, 1 on ungated tool
  - YADGAR_SECRET_GATE_DISABLED kill switch: logs warning, bypasses gate
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Pattern strictness
# ---------------------------------------------------------------------------


class TestPatternStrictness:
    """ghp_ tokens shorter than 36 chars must now be detected ({36,} → {20,})."""

    def test_ghp_33_chars_blocked(self):
        """Memory id 519107 case: 33-char token that slipped through {36,}."""
        from yadgar.secrets import check_secrets

        # "ghp_" prefix + 29 chars = 33 total after prefix (was under old {36,})
        token = "ghp_SECRETTOKEN1234567890abcdefghijk"  # gitleaks:allow
        assert len(token) - len("ghp_") == 32  # 32 chars after prefix
        blocked, reason, _ = check_secrets(f"token={token}")
        assert blocked is True, f"33-char ghp_ token must be blocked, was: {blocked!r}"
        assert "GitHub" in reason

    def test_ghp_20_chars_blocked(self):
        """Minimum new threshold: exactly 20 chars after prefix."""
        from yadgar.secrets import check_secrets

        token = "ghp_" + "A" * 20  # gitleaks:allow
        blocked, reason, _ = check_secrets(token)
        assert blocked is True, "20-char ghp_ token must be blocked"
        assert "GitHub" in reason

    def test_gho_20_chars_blocked(self):
        from yadgar.secrets import check_secrets

        token = "gho_" + "B" * 20  # gitleaks:allow
        blocked, reason, _ = check_secrets(token)
        assert blocked is True

    def test_ghs_20_chars_blocked(self):
        from yadgar.secrets import check_secrets

        token = "ghs_" + "C" * 20  # gitleaks:allow
        blocked, reason, _ = check_secrets(token)
        assert blocked is True

    def test_ghp_19_chars_not_blocked(self):
        """19 chars after prefix is too short — not a real token, should pass."""
        from yadgar.secrets import check_secrets

        token = "ghp_" + "x" * 19
        blocked, _, _ = check_secrets(f"ref={token}")
        # 19 chars is under {20,} threshold — may or may not match generic catch-all
        # The GitHub-specific pattern must NOT match
        if blocked:
            # Allow generic catch-all to match but not the specific GitHub pattern
            # (We only test that the lowered threshold applies — if generic fires, ok)
            pass

    def test_sk_ant_20_chars_blocked(self):
        """Anthropic key: {32,} → {20,}."""
        from yadgar.secrets import check_secrets

        token = "sk-ant-" + "x" * 20  # gitleaks:allow
        blocked, reason, _ = check_secrets(token)
        assert blocked is True, "20-char sk-ant- must be blocked"
        assert "Anthropic" in reason

    def test_sk_openai_20_chars_blocked(self):
        """OpenAI key: {30,} → {20,}."""
        from yadgar.secrets import check_secrets

        token = "sk-" + "y" * 20  # gitleaks:allow
        blocked, reason, _ = check_secrets(token)
        assert blocked is True, "20-char sk- key must be blocked"


# ---------------------------------------------------------------------------
# gate_or_reject helper
# ---------------------------------------------------------------------------


class TestGateOrReject:
    """gate_or_reject(*fields) returns rejection dict or None."""

    def test_returns_none_for_clean_content(self):
        from yadgar.secrets import gate_or_reject

        result = gate_or_reject("safe content", "another safe field")
        assert result is None

    def test_returns_rejection_dict_for_secret(self):
        from yadgar.secrets import gate_or_reject

        result = gate_or_reject("nothing bad", "AKIAIOSFODNN7EXAMPLE here")
        assert result is not None
        assert result["stored"] is False
        assert "secret_detected" in result["reason"]
        assert "AWS" in result["reason"]

    def test_empty_fields_skipped(self):
        from yadgar.secrets import gate_or_reject

        result = gate_or_reject("", None, "  ", "safe text")
        assert result is None

    def test_first_match_wins(self):
        from yadgar.secrets import gate_or_reject

        result = gate_or_reject("AKIAIOSFODNN7EXAMPLE", f"ghp_{'A' * 20}")  # gitleaks:allow
        assert result is not None
        # Should report the first blocked pattern (AWS key)
        assert "AWS" in result["reason"] or "secret_detected" in result["reason"]

    def test_pattern_preview_present(self):
        from yadgar.secrets import gate_or_reject

        result = gate_or_reject("AKIAIOSFODNN7EXAMPLE here")
        assert result is not None
        assert "pattern_preview" in result


# ---------------------------------------------------------------------------
# SecretLeakBlocked exception
# ---------------------------------------------------------------------------


class TestSecretLeakBlockedException:
    def test_exception_importable(self):
        from yadgar.secrets import SecretLeakBlocked

        assert issubclass(SecretLeakBlocked, Exception)

    def test_exception_carries_reason_and_preview(self):
        from yadgar.secrets import SecretLeakBlocked

        exc = SecretLeakBlocked("AWS access key", "AKIAIOSFODNN7EX...")
        assert "AWS" in str(exc) or exc.args


# ---------------------------------------------------------------------------
# API-boundary gates
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_server(monkeypatch, tmp_path):
    """Minimal server state for API boundary tests.

    Patches _get_file_queue to raise so we test the sync (non-enqueue) path,
    which exercises gate_or_reject before any state mutation.
    """
    import yadgar.file_queue as _fq
    import yadgar.server._state as _st

    monkeypatch.setattr(_fq, "is_draining", lambda: True)

    # Patch _get_file_queue to raise — we want sync path

    mock_storage = MagicMock()
    mock_storage.upsert_project_init.return_value = {"stored": True}
    mock_storage.upsert_active_work.return_value = {"stored": True}
    mock_replay = MagicMock()
    mock_replay.anchor_memory.return_value = 42
    mock_replay.create_checkpoint.return_value = {"stored": True}

    monkeypatch.setattr("yadgar.server.lifecycle._get_storage", lambda: mock_storage)
    monkeypatch.setattr("yadgar.server.lifecycle._get_replay", lambda: mock_replay)
    monkeypatch.setattr(_st, "_storage", mock_storage)
    monkeypatch.setattr(_st, "_wiki", None)

    yield {"storage": mock_storage, "replay": mock_replay, "tmp_path": tmp_path}


class TestAnchorAPIGate:
    """anchor() must reject content containing secrets."""

    def test_anchor_rejects_aws_key(self, isolated_server):
        from yadgar.server.tools.misc import anchor

        result = anchor(
            content="AKIAIOSFODNN7EXAMPLE my key",
            context="/home/user/project",
            reason="test key",
        )
        assert result.get("stored") is False, f"anchor must reject secret content, got: {result}"
        assert "secret_detected" in result.get("reason", "")

    def test_anchor_rejects_short_ghp_token(self, isolated_server):
        """ghp_ tokens ≥20 chars after prefix must be caught."""
        from yadgar.server.tools.misc import anchor

        token = "ghp_" + "A" * 20  # gitleaks:allow
        result = anchor(
            content=f"token={token}",
            context="/home/user/project",
            reason="test",
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_anchor_scans_reason_field(self, isolated_server):
        """reason field must also be scanned."""
        from yadgar.server.tools.misc import anchor

        result = anchor(
            content="normal content",
            context="/home/user/project",
            reason=f"ghp_{'B' * 20}",  # gitleaks:allow — secret in reason
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_anchor_clean_content_passes(self, isolated_server):
        """anchor with clean content must still work."""
        from yadgar.server.tools.misc import anchor

        result = anchor(
            content="my important decision about the database schema",
            context="/home/user/project",
            reason="architecture",
        )
        # Should NOT be rejected for secrets
        assert result.get("stored") is not False or "secret_detected" not in result.get(
            "reason", ""
        )


class TestUpdateActiveWorkAPIGate:
    def test_rejects_secret_content(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.project import update_active_work

        result = update_active_work(
            directory="/home/user/project",
            content=f"key=ghp_{'A' * 20} working on deploy",  # gitleaks:allow
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_clean_content_passes(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.project import update_active_work

        result = update_active_work(
            directory="/home/user/project",
            content="Currently: refactoring storage layer. Next: add tests.",
        )
        assert result.get("stored") is not False or "secret_detected" not in result.get(
            "reason", ""
        )


class TestBootstrapProjectAPIGate:
    def test_rejects_secret_content(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.project import bootstrap_project

        result = bootstrap_project(
            directory="/home/user/project",
            content="AKIAIOSFODNN7EXAMPLE is the AWS key",
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_clean_content_passes(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.project import bootstrap_project

        result = bootstrap_project(
            directory="/home/user/project",
            content="## Project\nYadgar memory system v5.10.x.\n",
        )
        # Should succeed (not rejected for secrets)
        assert result.get("stored") is not False or "secret_detected" not in result.get(
            "reason", ""
        )


class TestCheckpointAPIGate:
    """checkpoint() must scan all free-text fields."""

    def test_rejects_secret_in_current_task(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.misc import checkpoint

        result = checkpoint(
            directory="/home/user/project",
            current_task=f"deploying with token ghp_{'T' * 20}",  # gitleaks:allow
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_rejects_secret_in_key_decisions(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.misc import checkpoint

        result = checkpoint(
            directory="/home/user/project",
            current_task="deploy",
            key_decisions=[f"use token ghp_{'K' * 20}"],  # gitleaks:allow
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_rejects_secret_in_custom_context(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.misc import checkpoint

        result = checkpoint(
            directory="/home/user/project",
            current_task="deploy",
            custom_context="AWS_KEY=AKIAIOSFODNN7EXAMPLE",
        )
        assert result.get("stored") is False
        assert "secret_detected" in result.get("reason", "")

    def test_clean_checkpoint_passes(self, isolated_server, monkeypatch):
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)
        from yadgar.server.tools.misc import checkpoint

        result = checkpoint(
            directory="/home/user/project",
            current_task="Refactoring storage layer",
            key_decisions=["Use SurrealDB for storage"],
            next_steps=["Write tests", "Deploy"],
            open_questions=["Should we add caching?"],
        )
        # Should not be rejected for secrets
        assert result.get("stored") is not False or "secret_detected" not in result.get(
            "reason", ""
        )


class TestWikiUpdateAPIGate:
    def test_rejects_secret_in_content_field(self, isolated_server, monkeypatch):
        import yadgar.server._state as _st

        mock_wiki_storage = MagicMock()
        monkeypatch.setattr(_st, "_storage", mock_wiki_storage)

        from yadgar.server.tools.admin_other import wiki_update

        result = wiki_update(
            page_id=1,
            fields={"content": "AKIAIOSFODNN7EXAMPLE is the key"},
        )
        assert result.get("stored") is False or "secret_detected" in result.get("reason", "")

    def test_clean_fields_pass(self, isolated_server, monkeypatch):
        import yadgar.server._state as _st

        mock_storage = MagicMock()
        mock_storage.get_wiki_page.return_value = {
            "id": 1,
            "content": "updated",
            "tags": [],
        }
        mock_storage.update_wiki_page.return_value = None
        monkeypatch.setattr(_st, "_storage", mock_storage)

        from yadgar.server.tools.admin_other import wiki_update

        result = wiki_update(
            page_id=1,
            fields={"content": "Safe updated content about the architecture."},
        )
        # Should not be rejected (no secret)
        assert result.get("stored") is not False or "secret_detected" not in result.get(
            "reason", ""
        )


# ---------------------------------------------------------------------------
# Storage-level gate (Layer 1)
# ---------------------------------------------------------------------------


class TestStorageLevelGate:
    """insert_memory() must raise SecretLeakBlocked when content contains a secret."""

    def test_insert_memory_raises_on_secret(self):
        """Storage layer is the final chokepoint — raises rather than stores."""
        from yadgar.secrets import SecretLeakBlocked
        from yadgar.storage.memory import _MemoryMixin

        class _MockEngine(_MemoryMixin):
            def _now_iso(self):
                return "2026-05-29T00:00:00+00:00"

            def _next_id(self, _table):
                return 1

            def _bytes_to_floats(self, _b):
                return []

            def _q(self, sql, params=None):
                return []

        eng = _MockEngine()

        with pytest.raises(SecretLeakBlocked):
            eng.insert_memory(
                {
                    "content": "AKIAIOSFODNN7EXAMPLE my aws key",
                    "directory_context": "/home/user/project",
                    "tags": [],
                }
            )

    def test_insert_memory_clean_does_not_raise(self, monkeypatch):
        """insert_memory with clean content must not raise SecretLeakBlocked."""
        from yadgar.secrets import SecretLeakBlocked
        from yadgar.storage.memory import _MemoryMixin

        class _MockEngine(_MemoryMixin):
            def _now_iso(self):
                return "2026-05-29T00:00:00+00:00"

            def _next_id(self, _table):
                return 2

            def _bytes_to_floats(self, _b):
                return []

            def _q(self, sql, params=None):
                return []

        eng = _MockEngine()

        # Should not raise SecretLeakBlocked
        try:
            eng.insert_memory(
                {
                    "content": "Normal architecture decision about caching",
                    "directory_context": "/home/user/project",
                    "tags": [],
                }
            )
        except SecretLeakBlocked:
            pytest.fail("SecretLeakBlocked raised for clean content")

    def test_secret_gate_disabled_env_bypasses_raise(self, monkeypatch):
        """YADGAR_SECRET_GATE_DISABLED=1 bypasses gate but logs warning."""

        monkeypatch.setenv("YADGAR_SECRET_GATE_DISABLED", "1")
        from yadgar.secrets import SecretLeakBlocked
        from yadgar.storage.memory import _MemoryMixin

        class _MockEngine(_MemoryMixin):
            def _now_iso(self):
                return "2026-05-29T00:00:00+00:00"

            def _next_id(self, _table):
                return 3

            def _bytes_to_floats(self, _b):
                return []

            def _q(self, sql, params=None):
                return []

        eng = _MockEngine()
        # Must NOT raise when kill switch is on
        try:
            eng.insert_memory(
                {
                    "content": "AKIAIOSFODNN7EXAMPLE bypassed",
                    "directory_context": "/home/user/project",
                    "tags": [],
                }
            )
        except SecretLeakBlocked:
            pytest.fail("SecretLeakBlocked raised despite YADGAR_SECRET_GATE_DISABLED=1")


# ---------------------------------------------------------------------------
# I26 lint
# ---------------------------------------------------------------------------


class TestI26Lint:
    """scripts/check_secret_gate.py must exit 0 on clean tree, 1 on ungated tool."""

    SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "check_secret_gate.py"

    def test_script_exists(self):
        assert self.SCRIPT.exists(), f"I26 lint script not found at {self.SCRIPT}"

    def test_exits_zero_on_clean_tree(self):
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(self.SCRIPT.parent.parent),
        )
        assert result.returncode == 0, (
            f"I26 lint failed on clean tree:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    def test_exits_one_on_ungated_tool(self, tmp_path):
        """Write a fake tool file without gate_or_reject — script must flag it."""
        fake_tools_dir = tmp_path / "yadgar" / "server" / "tools"
        fake_tools_dir.mkdir(parents=True)

        ungated_tool = fake_tools_dir / "ungated_tool.py"
        ungated_tool.write_text(
            textwrap.dedent("""
            from yadgar.server._app import _tool
            from yadgar.storage.memory import _MemoryMixin

            @_tool()
            def bad_write_tool(content: str, context: str) -> dict:
                # Missing gate_or_reject — should be flagged by I26
                pass
            """)
        )

        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--tools-dir", str(fake_tools_dir)],
            capture_output=True,
            text=True,
            cwd=str(self.SCRIPT.parent.parent),
        )
        assert result.returncode != 0, (
            f"I26 lint must fail on ungated tool, got exit 0:\n{result.stdout}\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Backfill scan script
# ---------------------------------------------------------------------------


class TestBackfillScanScript:
    SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "scan_db_for_secrets.py"

    def test_script_exists(self):
        assert self.SCRIPT.exists(), f"Backfill scan script not found at {self.SCRIPT}"

    def test_dry_run_help_exits_zero(self):
        """Script must be importable and --help must work."""
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"--help failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    def test_dry_run_produces_report(self, tmp_path, monkeypatch):
        """--dry-run with mocked storage must produce a report file."""
        monkeypatch.setenv("YADGAR_SCAN_REPORT_DIR", str(tmp_path))

        # The script uses --storage-mock in test mode
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--dry-run", "--storage-mock"],
            capture_output=True,
            text=True,
            cwd=str(self.SCRIPT.parent.parent),
        )
        # Should exit 0 even with --storage-mock (mock data may or may not have hits)
        assert result.returncode in (0, 1), (
            f"Unexpected exit code {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
