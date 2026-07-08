"""RED tests for v5.42.6 — enforcement-off knobs (YADGAR_DIRECTORY_ENFORCEMENT,
YADGAR_BRANCH_ENFORCEMENT).

TDD: written BEFORE implementation. Tests start RED and go GREEN once the two
knobs are wired into _validate_wiki_add, _validate_branch_context, AND the
MCP boundary rejection sites in wiki.py, misc.py, memorize.py, project.py.

Design:
- YADGAR_DIRECTORY_ENFORCEMENT (default true) — when false, _validate_wiki_add
  skips the directory_context check (step 5) and logs WARN instead of rejecting.
  MCP boundary in wiki_add also consults this knob.
- YADGAR_BRANCH_ENFORCEMENT (default true) — when false, _validate_wiki_add,
  _validate_branch_context, and MCP boundary checks skip the branch check and
  log WARN instead.
- Both defaults true: existing 122+ tests are unaffected (default-ON = current
  behavior).
- Metric: yadgar_writes_with_enforcement_relaxed{enforcement="directory"|"branch"}
  increments each time a write passes because enforcement is off.

Coverage:
K1. directory enforcement OFF → _validate_wiki_add passes missing-directory record.
K2. directory enforcement ON (default) → _validate_wiki_add rejects missing-directory.
K3. branch enforcement OFF → _validate_wiki_add passes missing-branch record.
K4. branch enforcement ON (default) → _validate_wiki_add rejects missing-branch.
K5. branch enforcement OFF → _validate_branch_context passes missing-branch record.
K6. branch enforcement ON (default) → _validate_branch_context rejects missing-branch.
K7. WARN log fires when directory enforcement is off and record lacks directory.
K8. WARN log fires when branch enforcement is off and record lacks branch.
K9. Metric yadgar_writes_with_enforcement_relaxed{enforcement="directory"} increments
    when directory enforcement is off.
K10. Metric yadgar_writes_with_enforcement_relaxed{enforcement="branch"} increments
     when branch enforcement is off.
K11. Env parsing: YADGAR_DIRECTORY_ENFORCEMENT=false/0/FALSE/False → OFF.
K12. Env parsing: YADGAR_BRANCH_ENFORCEMENT=false/0/FALSE/False → OFF.
K13. Env parsing: unset (no env var) → defaults ON for both knobs.
K14. Env parsing: garbage value "banana" → fails-safe to ON.
K15. MCP wiki_add: YADGAR_BRANCH_ENFORCEMENT=false → does NOT return missing_branch error.
K16. MCP wiki_add: YADGAR_DIRECTORY_ENFORCEMENT=false → does NOT return missing_directory error.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_drainer(tmp_path):
    """Build a minimal QueueDrainer with a real FileQueue for unit tests."""
    import yadgar._shared.runtime.state as _st
    from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

    q = FileQueue(tmp_path)
    return QueueDrainer(queue=q, storage_factory=lambda: _st._storage, drain_interval=9999)


def _missing_directory_record() -> dict:
    """wiki_add record with all required fields EXCEPT directory_context."""
    return {
        "op": "wiki_add",
        "payload": {
            "wiki_schema_version": 2,
            "slug": "enforcement-test",
            "title": "Enforcement Test",
            "content": "Non-degenerate content for enforcement knob test.",
            "category": "reference",
            "tags": ["test"],
            "branch": "master",
            # directory_context deliberately absent
        },
    }


def _missing_branch_record() -> dict:
    """wiki_add record with all required fields EXCEPT branch."""
    return {
        "op": "wiki_add",
        "payload": {
            "wiki_schema_version": 2,
            "slug": "branch-enforcement-test",
            "title": "Branch Enforcement Test",
            "content": "Non-degenerate content for branch enforcement knob test.",
            "category": "reference",
            "tags": ["test"],
            "directory_context": "/home/max/git/yadgar",
            # branch deliberately absent
        },
    }


def _missing_branch_memory_record() -> dict:
    """memorize record without branch context."""
    return {
        "op": "memorize",
        "payload": {
            "content": "test memory content",
            "directory_context": "/home/max/git/yadgar",
            # branch deliberately absent
        },
    }


# ── K1-K2: directory enforcement knob on _validate_wiki_add ──────────────────


class TestDirectoryEnforcementKnob:
    """K1-K2: YADGAR_DIRECTORY_ENFORCEMENT controls directory check in _validate_wiki_add."""

    def test_enforcement_off_passes_missing_directory(self, tmp_path):
        """K1: YADGAR_DIRECTORY_ENFORCEMENT=false → _validate_wiki_add returns None."""
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            result = drainer._validate_wiki_add(record)

        assert result is None, (
            f"directory enforcement OFF should pass missing-directory record; got: {result!r}"
        )

    def test_enforcement_on_rejects_missing_directory(self, tmp_path):
        """K2: YADGAR_DIRECTORY_ENFORCEMENT=true (default) → rejects missing directory."""
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "true"}):
            result = drainer._validate_wiki_add(record)

        assert result is not None, "directory enforcement ON should reject missing-directory record"
        assert "directory" in result.lower()


# ── K3-K4: branch enforcement knob on _validate_wiki_add ─────────────────────


class TestBranchEnforcementKnobWikiAdd:
    """K3-K4: YADGAR_BRANCH_ENFORCEMENT controls branch check in _validate_wiki_add."""

    def test_enforcement_off_passes_missing_branch(self, tmp_path):
        """K3: YADGAR_BRANCH_ENFORCEMENT=false → _validate_wiki_add returns None."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_record()

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "false"}):
            result = drainer._validate_wiki_add(record)

        assert result is None, (
            f"branch enforcement OFF should pass missing-branch record; got: {result!r}"
        )

    def test_enforcement_on_rejects_missing_branch(self, tmp_path):
        """K4: YADGAR_BRANCH_ENFORCEMENT=true (default) → rejects missing branch."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_record()

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "true"}):
            result = drainer._validate_wiki_add(record)

        assert result is not None, "branch enforcement ON should reject missing-branch record"
        assert "branch" in result.lower()


# ── K5-K6: branch enforcement knob on _validate_branch_context ───────────────


class TestBranchEnforcementKnobMemoryOp:
    """K5-K6: YADGAR_BRANCH_ENFORCEMENT controls branch check in _validate_branch_context."""

    def test_enforcement_off_passes_missing_branch(self, tmp_path):
        """K5: YADGAR_BRANCH_ENFORCEMENT=false → _validate_branch_context returns None."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_memory_record()

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "false"}):
            result = drainer._validate_branch_context(record)

        assert result is None, (
            f"branch enforcement OFF should pass missing-branch memory record; got: {result!r}"
        )

    def test_enforcement_on_rejects_missing_branch(self, tmp_path):
        """K6: YADGAR_BRANCH_ENFORCEMENT=true (default) → rejects missing branch memory op."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_memory_record()

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "true"}):
            result = drainer._validate_branch_context(record)

        assert result is not None, "branch enforcement ON should reject missing-branch memory op"
        assert "branch" in result.lower()


# ── K7-K8: WARN log fires when enforcement is relaxed ────────────────────────


class TestEnforcementRelaxedWarnLog:
    """K7-K8: WARN log emitted when enforcement is off and check would have rejected."""

    def test_warn_log_fires_when_directory_enforcement_off(self, tmp_path, caplog):
        """K7: WARN logged when directory enforcement is off + record missing directory."""
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            with caplog.at_level(logging.WARNING):
                drainer._validate_wiki_add(record)

        assert any(
            "directory" in m.lower() and "enforcement" in m.lower() for m in caplog.messages
        ), (
            "Expected WARN log containing 'directory' and 'enforcement' when enforcement is off; "
            f"got messages: {caplog.messages}"
        )

    def test_warn_log_fires_when_branch_enforcement_off_wiki_add(self, tmp_path, caplog):
        """K8: WARN logged when branch enforcement is off + wiki_add record missing branch."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_record()

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "false"}):
            with caplog.at_level(logging.WARNING):
                drainer._validate_wiki_add(record)

        assert any("branch" in m.lower() and "enforcement" in m.lower() for m in caplog.messages), (
            "Expected WARN log containing 'branch' and 'enforcement' when enforcement is off; "
            f"got messages: {caplog.messages}"
        )


# ── K9-K10: metric counter increments when enforcement relaxed ────────────────


class TestEnforcementRelaxedMetric:
    """K9-K10: yadgar_writes_with_enforcement_relaxed counter increments on relaxation."""

    def test_directory_relaxed_metric_increments(self, tmp_path):
        """K9: yadgar_writes_with_enforcement_relaxed{enforcement='directory'} increments."""
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        before = _get_counter_value(
            "yadgar_writes_with_enforcement_relaxed_total", {"enforcement": "directory"}
        )

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            drainer._validate_wiki_add(record)

        after = _get_counter_value(
            "yadgar_writes_with_enforcement_relaxed_total", {"enforcement": "directory"}
        )
        assert after > before, f"Counter should have incremented; before={before}, after={after}"

    def test_branch_relaxed_metric_increments(self, tmp_path):
        """K10: yadgar_writes_with_enforcement_relaxed{enforcement='branch'} increments."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_record()

        before = _get_counter_value(
            "yadgar_writes_with_enforcement_relaxed_total", {"enforcement": "branch"}
        )

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "false"}):
            drainer._validate_wiki_add(record)

        after = _get_counter_value(
            "yadgar_writes_with_enforcement_relaxed_total", {"enforcement": "branch"}
        )
        assert after > before, f"Counter should have incremented; before={before}, after={after}"


def _get_counter_value(metric_name: str, labels: dict) -> float:
    """Read a prometheus counter value from yadgar's private registry.

    metric_name should include the _total suffix (e.g. 'foo_total').
    """
    from yadgar._shared.metrics import _registry  # noqa: PLC0415

    for metric in _registry.collect():
        for sample in metric.samples:
            if sample.name == metric_name and sample.labels == labels:
                return sample.value
    return 0.0


# ── K11-K14: env parsing edges ────────────────────────────────────────────────


class TestEnforcementEnvParsing:
    """K11-K14: env var parsing for enforcement knobs."""

    @pytest.mark.parametrize("val", ["false", "0", "FALSE", "False"])
    def test_directory_enforcement_false_values(self, tmp_path, val):
        """K11: YADGAR_DIRECTORY_ENFORCEMENT falsy values → enforcement OFF."""
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": val}):
            result = drainer._validate_wiki_add(record)

        assert result is None, (
            f"YADGAR_DIRECTORY_ENFORCEMENT={val!r} should turn off enforcement; got: {result!r}"
        )

    @pytest.mark.parametrize("val", ["false", "0", "FALSE", "False"])
    def test_branch_enforcement_false_values(self, tmp_path, val):
        """K12: YADGAR_BRANCH_ENFORCEMENT falsy values → enforcement OFF."""
        drainer = _make_drainer(tmp_path)
        record = _missing_branch_record()

        with patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": val}):
            result = drainer._validate_wiki_add(record)

        assert result is None, (
            f"YADGAR_BRANCH_ENFORCEMENT={val!r} should turn off enforcement; got: {result!r}"
        )

    def test_unset_env_defaults_on(self, tmp_path):
        """K13: unset YADGAR_*_ENFORCEMENT → defaults ON (both knobs)."""
        drainer = _make_drainer(tmp_path)

        # Remove vars if set in environment
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("YADGAR_DIRECTORY_ENFORCEMENT", "YADGAR_BRANCH_ENFORCEMENT")
        }

        with patch.dict(os.environ, clean_env, clear=True):
            dir_result = drainer._validate_wiki_add(_missing_directory_record())
            branch_result = drainer._validate_wiki_add(_missing_branch_record())

        assert dir_result is not None, "unset YADGAR_DIRECTORY_ENFORCEMENT should default ON"
        assert branch_result is not None, "unset YADGAR_BRANCH_ENFORCEMENT should default ON"

    @pytest.mark.parametrize("val", ["true", "1", "TRUE", "True", "banana", "yes"])
    def test_garbage_or_truthy_values_default_on(self, tmp_path, val):
        """K14: garbage or truthy values → fail-safe ON for both knobs."""
        drainer = _make_drainer(tmp_path)

        with patch.dict(
            os.environ,
            {
                "YADGAR_DIRECTORY_ENFORCEMENT": val,
                "YADGAR_BRANCH_ENFORCEMENT": val,
            },
        ):
            dir_result = drainer._validate_wiki_add(_missing_directory_record())
            branch_result = drainer._validate_wiki_add(_missing_branch_record())

        assert dir_result is not None, (
            f"YADGAR_DIRECTORY_ENFORCEMENT={val!r} should be ON (fail-safe)"
        )
        assert branch_result is not None, (
            f"YADGAR_BRANCH_ENFORCEMENT={val!r} should be ON (fail-safe)"
        )


# ── K15-K16: MCP boundary enforcement knobs ───────────────────────────────────


class TestMCPBoundaryEnforcementKnobs:
    """K15-K16: enforcement knobs gate MCP boundary rejection sites in wiki.py.

    Tests call wiki_add() directly (not via drainer). is_draining() is patched
    to False so the MCP boundary code runs. The file-queue enqueue is patched
    out so no real queue write occurs.
    """

    def test_branch_enforcement_off_skips_mcp_missing_branch_rejection(self):
        """K15: YADGAR_BRANCH_ENFORCEMENT=false → wiki_add does not return missing_branch."""
        from unittest.mock import MagicMock, patch

        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.wiki import wiki_add

        _fake_queue = MagicMock()
        _fake_queue.enqueue.return_value = None
        _fake_wiki = MagicMock()

        with (
            patch.dict(os.environ, {"YADGAR_BRANCH_ENFORCEMENT": "false"}),
            # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
            # enqueues); the symbol lives in the backend queue_drainer.
            patch("yadgar.backend.queue_drainer.is_draining", return_value=False),
            patch("yadgar.core.server.tools.wiki._get_file_queue", return_value=_fake_queue),
            patch.object(_st, "_wiki", _fake_wiki),
        ):
            result = wiki_add(
                title="Test Page",
                content="Test content for MCP boundary knob test.",
                category="reference",
                tags=["test"],
                # branch omitted — would normally hard-reject
                branch=None,
                branch_hint=None,
                directory="global",
            )

        assert result.get("error") != "missing_branch", (
            f"YADGAR_BRANCH_ENFORCEMENT=false should skip MCP missing_branch rejection; got: {result!r}"
        )

    def test_directory_enforcement_off_skips_mcp_missing_directory_rejection(self):
        """K16: YADGAR_DIRECTORY_ENFORCEMENT=false → wiki_add does not return missing_directory."""
        from unittest.mock import MagicMock, patch

        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.wiki import wiki_add

        _fake_queue = MagicMock()
        _fake_queue.enqueue.return_value = None
        _fake_wiki = MagicMock()

        with (
            patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}),
            # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
            # enqueues); the symbol lives in the backend queue_drainer.
            patch("yadgar.backend.queue_drainer.is_draining", return_value=False),
            patch("yadgar.core.server.tools.wiki._get_file_queue", return_value=_fake_queue),
            patch.object(_st, "_wiki", _fake_wiki),
        ):
            result = wiki_add(
                title="Test Page",
                content="Test content for MCP boundary knob test.",
                category="reference",
                tags=["test"],
                branch="master",
                branch_hint=None,
                # directory omitted — would normally hard-reject
                directory=None,
            )

        assert result.get("error") != "missing_directory", (
            f"YADGAR_DIRECTORY_ENFORCEMENT=false should skip MCP missing_directory rejection; got: {result!r}"
        )
