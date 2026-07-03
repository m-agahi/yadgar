"""TDD tests for v5.9.0: consolidate_now() anchor audit pass.

Scope:
  - consolidate_now() with ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true writes
    _audit_anchors sentinel memory per directory.
  - consolidate_now() with knob false: no _audit_anchors writes.
  - Sentinel content: list of action recommendations (latest-wins single row per dir).
  - Anchor pass skips directories with anchor_count_project < ANCHOR_AUDIT_THRESHOLD.

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import pytest

from yadgar import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("consolidate_anchor_pass")
    server.init_engines(
        db_path=str(tmp_path / "test_consolidate_anchor.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar.server.lifecycle import _get_storage

    return _get_storage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIR = "/tmp/test_consolidate_anchor_proj"


def _insert_anchor(storage, content: str, directory: str = _DIR) -> int:
    now = storage._now_iso()
    mid = storage._next_id("memory")
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, directory_context = $dir, tags = $tags, "
        "heat = 0.5, is_protected = true, tier = 'conditional', "
        "created_at = $now, last_accessed = $now, access_count = 0",
        {
            "id": mid,
            "content": content,
            "dir": directory,
            "tags": ["_anchor"],
            "now": now,
        },
    )
    return mid


def _get_audit_sentinels(storage, directory: str) -> list:
    """Return _audit_anchors sentinel memory rows for a directory."""
    return storage._q(
        "SELECT id, content FROM memory "
        "WHERE directory_context = $dir "
        "AND '_audit_anchors' INSIDE tags LIMIT 10",
        {"dir": directory},
    )


# ---------------------------------------------------------------------------
# 1. ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true writes sentinel
# ---------------------------------------------------------------------------


class TestAnchorPassEnabled:
    """consolidate_now() writes _audit_anchors sentinel when knob is true."""

    def test_sentinel_written_after_consolidate(self, storage, monkeypatch, request):
        """After consolidate_now(), _audit_anchors memory exists for directory with anchors."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_THRESHOLD", "0")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        # Insert enough anchors to pass threshold (threshold=0 means always run)
        _insert_anchor(storage, "anchor content one")
        _insert_anchor(storage, "anchor content two")

        server.consolidate_now(mode="full")

        sentinels = _get_audit_sentinels(storage, _DIR)
        assert sentinels, (
            "_audit_anchors sentinel must be written after consolidate_now() "
            "when ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true"
        )

    def test_sentinel_is_latest_wins(self, storage, monkeypatch, request):
        """Second consolidate_now() overwrites (not appends) sentinel — latest-wins."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_THRESHOLD", "0")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        _insert_anchor(storage, "anchor one for latest wins")

        server.consolidate_now(mode="full")
        server.consolidate_now(mode="full")

        sentinels = _get_audit_sentinels(storage, _DIR)
        assert len(sentinels) == 1, "_audit_anchors must be latest-wins single row (not append)"

    def test_sentinel_content_has_actions(self, storage, monkeypatch, request):
        """Sentinel content is serializable (list of recommendations or empty dict)."""
        import json

        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_THRESHOLD", "0")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        _insert_anchor(storage, "anchor for content check")

        server.consolidate_now(mode="full")

        sentinels = _get_audit_sentinels(storage, _DIR)
        assert sentinels
        content = sentinels[0]["content"]
        # Should be valid JSON or a string that parses
        parsed = json.loads(content)
        assert isinstance(parsed, dict), "Sentinel content must be JSON dict"
        assert "actions" in parsed, "Sentinel content must have 'actions' key"


# ---------------------------------------------------------------------------
# 2. ANCHOR_AUDIT_CONSOLIDATION_ENABLED=false — no sentinel writes
# ---------------------------------------------------------------------------


class TestAnchorPassDisabled:
    """consolidate_now() skips anchor pass when knob is false."""

    def test_no_sentinel_when_disabled(self, storage, monkeypatch, request):
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "false")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        _insert_anchor(storage, "anchor when disabled")

        server.consolidate_now(mode="full")

        sentinels = _get_audit_sentinels(storage, _DIR)
        assert not sentinels, (
            "_audit_anchors must NOT be written when ANCHOR_AUDIT_CONSOLIDATION_ENABLED=false"
        )


# ---------------------------------------------------------------------------
# 3. Threshold gate — skip dirs below threshold
# ---------------------------------------------------------------------------


class TestThresholdGate:
    """Anchor pass skips directories with anchor_count < ANCHOR_AUDIT_THRESHOLD."""

    def test_skips_below_threshold(self, storage, monkeypatch, request):
        """With 1 anchor and threshold=5, sentinel is not written."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_THRESHOLD", "5")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        _insert_anchor(storage, "only one anchor")  # below threshold of 5

        server.consolidate_now(mode="full")

        sentinels = _get_audit_sentinels(storage, _DIR)
        assert not sentinels, "Anchor pass must skip directories below ANCHOR_AUDIT_THRESHOLD"

    def test_runs_at_or_above_threshold(self, storage, monkeypatch, request):
        """With threshold=2 and 2 anchors, sentinel is written."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_THRESHOLD", "2")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        _insert_anchor(storage, "anchor A at threshold")
        _insert_anchor(storage, "anchor B at threshold")

        server.consolidate_now(mode="full")

        sentinels = _get_audit_sentinels(storage, _DIR)
        assert sentinels, "Anchor pass must run when anchor_count >= ANCHOR_AUDIT_THRESHOLD"
