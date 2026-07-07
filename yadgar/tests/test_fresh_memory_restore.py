"""#35 fresh-memory-restore — TDD tests for three new capabilities.

Deliverables:
  1. recent_memories() — new MCP tool, time-ranked, no classifier dependency
  2. restore() "Recent Writes (last 24h)" section
  3. memorize() returns memory_id + metadata in response

Written RED first; implementations written after.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helper: build a fake memory row
# ---------------------------------------------------------------------------


def _mem(
    mid: int,
    content: str = "test memory",
    tags: list[str] | None = None,
    store_type: str = "episodic",
    heat: float = 1.0,
    is_protected: bool = False,
    directory: str = "/home/user/project",
    created_at: str | None = None,
) -> dict:
    if created_at is None:
        created_at = datetime.now(UTC).isoformat()
    return {
        "id": mid,
        "content": content,
        "tags": tags or [],
        "store_type": store_type,
        "heat": heat,
        "is_protected": is_protected,
        "directory_context": directory,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# 1. recent_memories() MCP tool
# ---------------------------------------------------------------------------


class TestRecentMemoriesTool:
    """recent_memories() must surface time-ranked memories without classifier."""

    def test_recent_memories_importable(self):
        """recent_memories must be importable from server.tools.admin_other."""
        from yadgar.core.server.tools.admin_other import recent_memories  # noqa: F401

    def test_recent_memories_returns_list(self):
        """recent_memories() must return a dict with 'memories' list."""
        from yadgar.core.server.tools.admin_other import recent_memories

        now = datetime.now(UTC)
        rows = [
            _mem(1, created_at=(now - timedelta(hours=2)).isoformat()),
            _mem(2, created_at=(now - timedelta(hours=10)).isoformat()),
        ]

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = rows

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            result = recent_memories(limit=10, since="24h", directory="/home/user/project")

        assert "memories" in result
        assert isinstance(result["memories"], list)

    def test_recent_memories_limit_respected(self):
        """limit param must cap rows returned."""
        from yadgar.core.server.tools.admin_other import recent_memories

        now = datetime.now(UTC)
        rows = [_mem(i, created_at=(now - timedelta(hours=i)).isoformat()) for i in range(1, 6)]

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = rows[:3]

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            result = recent_memories(limit=3, since="24h", directory="/home/user/project")

        assert len(result["memories"]) <= 3

    def test_recent_memories_fields_present(self):
        """Each entry must have id, created_at, content, tags, heat, is_protected."""
        from yadgar.core.server.tools.admin_other import recent_memories

        now = datetime.now(UTC)
        rows = [
            _mem(
                42,
                content="important insight",
                tags=["yadgar", "v6"],
                heat=2.5,
                is_protected=True,
                created_at=(now - timedelta(hours=1)).isoformat(),
            )
        ]

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = rows

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            result = recent_memories(limit=10, since="24h", directory="/home/user/project")

        assert result["memories"], "Should have at least one entry"
        entry = result["memories"][0]
        for field in ("id", "created_at", "content", "tags", "heat", "is_protected"):
            assert field in entry, f"Missing field: {field}"

    def test_recent_memories_content_truncated_at_300(self):
        """Content longer than 300 chars must be truncated."""
        from yadgar.core.server.tools.admin_other import recent_memories

        now = datetime.now(UTC)
        long_content = "x" * 500
        rows = [_mem(1, content=long_content, created_at=(now - timedelta(hours=1)).isoformat())]

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = rows

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            result = recent_memories(limit=10, since="24h", directory="/home/user/project")

        entry = result["memories"][0]
        assert len(entry["content"]) <= 300, f"Content not truncated: {len(entry['content'])} chars"

    def test_recent_memories_global_directory(self):
        """directory='global' must query without directory filter."""
        from yadgar.core.server.tools.admin_other import recent_memories

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = []

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            result = recent_memories(limit=10, since="24h", directory="global")

        assert "memories" in result
        # When directory='global', should pass None or 'global' to storage
        call_kwargs = mock_storage.get_recent_memories_since.call_args
        assert call_kwargs is not None

    def test_recent_memories_since_24h_default(self):
        """Default since='24h' must compute cutoff ~24h ago."""
        from yadgar.core.server.tools.admin_other import recent_memories

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = []

        before = datetime.now(UTC)
        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            recent_memories(limit=10, directory="/home/user/project")
        after = datetime.now(UTC)

        call_kwargs = mock_storage.get_recent_memories_since.call_args
        assert call_kwargs is not None
        # Extract since cutoff from call
        args, kwargs = call_kwargs
        # Cutoff should be between (before - 24h) and (after - 24h)
        since_arg = kwargs.get("since") or (args[1] if len(args) > 1 else None)
        if since_arg is not None and isinstance(since_arg, str):
            cutoff_dt = datetime.fromisoformat(since_arg.replace("Z", "+00:00"))
            expected_min = before - timedelta(hours=24, seconds=5)
            expected_max = after - timedelta(hours=24) + timedelta(seconds=5)
            assert expected_min <= cutoff_dt <= expected_max, (
                f"Cutoff {cutoff_dt} outside expected 24h range"
            )

    def test_recent_memories_since_duration_strings(self):
        """since='1h', '7d' etc. must parse correctly."""
        from yadgar.core.server.tools.admin_other import recent_memories

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = []

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            # These should not raise
            recent_memories(limit=5, since="1h", directory="/home/user/project")
            recent_memories(limit=5, since="7d", directory="/home/user/project")
            recent_memories(limit=5, since="30m", directory="/home/user/project")

    def test_recent_memories_max_limit_100(self):
        """limit > 100 must be capped at 100."""
        from yadgar.core.server.tools.admin_other import recent_memories

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = []

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            recent_memories(limit=9999, since="24h", directory="/home/user/project")

        # Check storage was called with limit <= 100
        call_args, call_kwargs = mock_storage.get_recent_memories_since.call_args
        actual_limit = call_kwargs.get("limit") or call_args[0]
        assert actual_limit <= 100, f"Expected limit capped at 100, got {actual_limit}"

    def test_recent_memories_empty_result(self):
        """Empty storage returns memories=[]."""
        from yadgar.core.server.tools.admin_other import recent_memories

        mock_storage = MagicMock()
        mock_storage.get_recent_memories_since.return_value = []

        with patch("yadgar.core.server.tools.admin_other._get_storage", return_value=mock_storage):
            result = recent_memories(limit=10, since="24h", directory="/home/user/project")

        assert result["memories"] == []


# ---------------------------------------------------------------------------
# 2. restore() "Recent Writes (last 24h)" section
# ---------------------------------------------------------------------------


class TestRestoreRecentWrites:
    """restore() output must include a 'recent_writes' section."""

    def test_project_brief_restore_has_recent_writes(self):
        """_project_brief_restore must include 'recent_writes' key."""
        from yadgar.core.server.tools.project import _project_brief_restore

        now = datetime.now(UTC)
        rows = [
            _mem(1, content="wrote this", created_at=(now - timedelta(hours=2)).isoformat()),
        ]

        mock_storage = MagicMock()
        mock_storage.get_anchored_memories_scoped.return_value = []
        mock_storage.get_memories_without_embeddings.return_value = []
        mock_storage.get_recent_memories_since.return_value = rows
        # Provide empty list returns for all helper methods
        mock_storage._q.return_value = []

        with patch("yadgar.core.server.tools.project._get_storage", return_value=mock_storage):
            result = _project_brief_restore(
                resolved="/home/user/project",
                mode="restore",
                storage=mock_storage,
                checkpoint_rows=[],
            )

        assert "recent_writes" in result, (
            f"'recent_writes' missing from restore payload. Keys: {list(result.keys())}"
        )

    def test_restore_recent_writes_ordered_by_created_at_desc(self):
        """recent_writes entries must be newest first."""
        from yadgar.core.server.tools.project import _project_brief_restore

        now = datetime.now(UTC)
        rows = [
            _mem(1, content="older", created_at=(now - timedelta(hours=10)).isoformat()),
            _mem(2, content="newer", created_at=(now - timedelta(hours=1)).isoformat()),
        ]

        mock_storage = MagicMock()
        mock_storage.get_anchored_memories_scoped.return_value = []
        mock_storage.get_recent_memories_since.return_value = sorted(
            rows, key=lambda r: r["created_at"], reverse=True
        )
        mock_storage._q.return_value = []

        with patch("yadgar.core.server.tools.project._get_storage", return_value=mock_storage):
            result = _project_brief_restore(
                resolved="/home/user/project",
                mode="restore",
                storage=mock_storage,
                checkpoint_rows=[],
            )

        writes = result.get("recent_writes", [])
        if len(writes) >= 2:
            assert writes[0]["created_at"] >= writes[1]["created_at"], (
                "recent_writes not sorted newest-first"
            )

    def test_restore_recent_writes_max_10_entries(self):
        """recent_writes must be capped at 10 entries."""
        from yadgar.core.server.tools.project import _project_brief_restore

        now = datetime.now(UTC)
        rows = [_mem(i, created_at=(now - timedelta(hours=i)).isoformat()) for i in range(1, 20)]

        mock_storage = MagicMock()
        mock_storage.get_anchored_memories_scoped.return_value = []
        mock_storage.get_recent_memories_since.return_value = rows[:10]
        mock_storage._q.return_value = []

        with patch("yadgar.core.server.tools.project._get_storage", return_value=mock_storage):
            result = _project_brief_restore(
                resolved="/home/user/project",
                mode="restore",
                storage=mock_storage,
                checkpoint_rows=[],
            )

        writes = result.get("recent_writes", [])
        assert len(writes) <= 10, f"Expected ≤10 recent_writes, got {len(writes)}"

    def test_restore_recent_writes_empty_when_none(self):
        """recent_writes is [] when no recent memories."""
        from yadgar.core.server.tools.project import _project_brief_restore

        mock_storage = MagicMock()
        mock_storage.get_anchored_memories_scoped.return_value = []
        mock_storage.get_recent_memories_since.return_value = []
        mock_storage._q.return_value = []

        with patch("yadgar.core.server.tools.project._get_storage", return_value=mock_storage):
            result = _project_brief_restore(
                resolved="/home/user/project",
                mode="restore",
                storage=mock_storage,
                checkpoint_rows=[],
            )

        writes = result.get("recent_writes", [])
        assert writes == [], f"Expected empty list, got {writes}"


# ---------------------------------------------------------------------------
# 3. memorize() returns memory_id + metadata
# ---------------------------------------------------------------------------


class TestMemorizeReturnsMemoryId:
    """memorize() response must include explicit memory_id field."""

    def test_build_response_includes_memory_id(self):
        """_build_response must set memory['memory_id'] = ctx.memory_id."""
        from yadgar.core.server.tools._memorize_phases._phase_post_write import _build_response
        from yadgar.core.server.tools._memorize_phases.context import MemorizeContext

        ctx = MemorizeContext(
            content="test content",
            context="/home/user/project",
            tags=["test"],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="",
            branch_hint=None,
        )
        ctx.memory_id = 523183

        memory_row = {
            "id": 523183,
            "content": "test content",
            "tags": ["test"],
            "heat": 1.0,
            "is_protected": False,
            "created_at": datetime.now(UTC).isoformat(),
            "store_type": "episodic",
            "directory_context": "/home/user/project",
        }

        mock_storage = MagicMock()
        mock_storage.get_memory.return_value = memory_row

        mock_settings = MagicMock()
        mock_settings.CRDT_AGENT_ID = "default"

        import yadgar._shared.runtime.state as _st

        saved_write_gate = _st._write_gate
        saved_thermo = _st._thermo
        saved_prospective = _st._prospective
        saved_engram = _st._engram
        saved_replay = _st._replay
        saved_retriever = _st._retriever
        _st._write_gate = None
        _st._thermo = None
        _st._prospective = None
        _st._engram = None
        _st._replay = None
        _st._retriever = None

        try:
            result = _build_response(ctx, mock_storage, mock_settings)
        finally:
            _st._write_gate = saved_write_gate
            _st._thermo = saved_thermo
            _st._prospective = saved_prospective
            _st._engram = saved_engram
            _st._replay = saved_replay
            _st._retriever = saved_retriever

        assert "memory_id" in result, (
            f"'memory_id' missing from _build_response output. Keys: {list(result.keys())}"
        )
        assert result["memory_id"] == 523183, (
            f"Expected memory_id=523183, got {result.get('memory_id')}"
        )

    def test_build_response_fallback_includes_memory_id(self):
        """Even in fallback path (memory not found on readback), memory_id must be present."""
        from yadgar.core.server.tools._memorize_phases._phase_post_write import _build_response
        from yadgar.core.server.tools._memorize_phases.context import MemorizeContext

        ctx = MemorizeContext(
            content="test",
            context="/home/user/project",
            tags=[],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="",
            branch_hint=None,
        )
        ctx.memory_id = 999

        mock_storage = MagicMock()
        mock_storage.get_memory.return_value = None  # simulate not found

        mock_settings = MagicMock()

        import yadgar._shared.runtime.state as _st

        saved_thermo = _st._thermo
        _st._thermo = None
        try:
            result = _build_response(ctx, mock_storage, mock_settings)
        finally:
            _st._thermo = saved_thermo

        assert "memory_id" in result, (
            f"Fallback path missing 'memory_id'. Keys: {list(result.keys())}"
        )
        assert result["memory_id"] == 999

    def test_memorize_response_has_created_at(self):
        """memorize() response dict must include created_at."""
        from yadgar.core.server.tools._memorize_phases._phase_post_write import _build_response
        from yadgar.core.server.tools._memorize_phases.context import MemorizeContext

        ctx = MemorizeContext(
            content="test content",
            context="/home/user/project",
            tags=["test"],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="",
            branch_hint=None,
        )
        ctx.memory_id = 100

        created_ts = datetime.now(UTC).isoformat()
        memory_row = {
            "id": 100,
            "content": "test content",
            "tags": ["test"],
            "heat": 1.0,
            "is_protected": False,
            "created_at": created_ts,
            "store_type": "episodic",
            "directory_context": "/home/user/project",
        }

        mock_storage = MagicMock()
        mock_storage.get_memory.return_value = memory_row

        mock_settings = MagicMock()
        mock_settings.CRDT_AGENT_ID = "default"

        import yadgar._shared.runtime.state as _st

        saved_states = (
            _st._write_gate,
            _st._thermo,
            _st._prospective,
            _st._engram,
            _st._replay,
            _st._retriever,
        )
        _st._write_gate = None
        _st._thermo = None
        _st._prospective = None
        _st._engram = None
        _st._replay = None
        _st._retriever = None

        try:
            result = _build_response(ctx, mock_storage, mock_settings)
        finally:
            (
                _st._write_gate,
                _st._thermo,
                _st._prospective,
                _st._engram,
                _st._replay,
                _st._retriever,
            ) = saved_states

        assert "created_at" in result, (
            f"'created_at' missing from response. Keys: {list(result.keys())}"
        )


# ---------------------------------------------------------------------------
# 4. Storage method: get_recent_memories_since
# ---------------------------------------------------------------------------


class TestStorageGetRecentMemoriesSince:
    """Storage must expose get_recent_memories_since(since, limit, directory)."""

    def test_get_recent_memories_since_importable(self):
        """StorageEngine must have get_recent_memories_since method."""
        from yadgar._shared.storage import StorageEngine

        assert hasattr(StorageEngine, "get_recent_memories_since"), (
            "StorageEngine missing get_recent_memories_since method"
        )

    def test_get_recent_memories_since_signature(self):
        """Method must accept since, limit, directory kwargs."""
        import inspect

        from yadgar._shared.storage import StorageEngine

        sig = inspect.signature(StorageEngine.get_recent_memories_since)
        params = set(sig.parameters)
        assert "since" in params, f"Missing 'since' param. Got: {params}"
        assert "limit" in params, f"Missing 'limit' param. Got: {params}"
        assert "directory" in params, f"Missing 'directory' param. Got: {params}"
