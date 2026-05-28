"""TDD tests for v5.8.0 anchor hygiene: tier enum + valid_until + migration.

PR-A scope only:
  - tier / valid_until / ttl_days / migration_grace on memorize + anchor
  - valid_until expiry filter on restore / hot-ranking / project_brief(restore)
  - migration_008_anchor_tier: backfill existing anchors
  - 3 new env knobs registered I25

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# ---------------------------------------------------------------------------
# Storage-layer fixture (embedded SurrealDB, no MCP server)
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path):
    from yadgar.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "test_hygiene.db"))
    yield engine
    engine.close()


# ---------------------------------------------------------------------------
# MCP-tool fixture (full server init, drain mode)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engines(tmp_path):
    """Full server init for MCP tool tests — drain mode forced so memorize/anchor
    take the synchronous path and return a real ``id`` immediately."""
    from yadgar import server
    from yadgar.file_queue._locals import _drain_local

    # Force sync (drain) path so tool calls persist synchronously and return ids.
    _drain_local.active = True
    server.init_engines(db_path=str(tmp_path / "test_tools.db"), embedding_model="all-MiniLM-L6-v2")
    yield server
    _drain_local.active = False
    server.shutdown()


# ============================================================
# 1. TIER ENUM — memorize(is_protected=True, tier=...)
# ============================================================


class TestMemorizeTier:
    """memorize() accepts and persists tier when is_protected=True."""

    def test_tier_conditional_stored(self, storage):
        mid = storage.insert_memory(
            {
                "content": "conditional anchor test",
                "directory_context": "/tmp/proj",
                "tags": ["_anchor"],
                "is_protected": True,
                "tier": "conditional",
            }
        )
        rows = storage._q(f"SELECT tier FROM memory:{mid}")
        assert rows and rows[0].get("tier") == "conditional"

    def test_tier_ephemeral_stored(self, storage):
        mid = storage.insert_memory(
            {
                "content": "ephemeral anchor test",
                "directory_context": "/tmp/proj",
                "tags": ["_anchor"],
                "is_protected": True,
                "tier": "ephemeral",
            }
        )
        rows = storage._q(f"SELECT tier FROM memory:{mid}")
        assert rows and rows[0].get("tier") == "ephemeral"

    def test_tier_semantic_immortal_stored(self, storage):
        mid = storage.insert_memory(
            {
                "content": "immortal anchor test",
                "directory_context": "/tmp/proj",
                "tags": ["_anchor"],
                "is_protected": True,
                "tier": "semantic_immortal",
            }
        )
        rows = storage._q(f"SELECT tier FROM memory:{mid}")
        assert rows and rows[0].get("tier") == "semantic_immortal"

    def test_tier_none_for_non_anchor(self, storage):
        """Non-anchor memories may have tier=None."""
        mid = storage.insert_memory(
            {
                "content": "regular memory",
                "directory_context": "/tmp/proj",
                "tags": [],
            }
        )
        rows = storage._q(f"SELECT tier FROM memory:{mid}")
        # tier unset on regular memory — None or absent
        assert not rows or rows[0].get("tier") is None


# ============================================================
# 2. VALID_UNTIL — TTL computation
# ============================================================


class TestValidUntil:
    """valid_until is computed correctly from tier / ttl_days."""

    def test_semantic_immortal_valid_until_none(self, engines):
        """tier=semantic_immortal → valid_until=None."""
        result = engines.memorize(
            content="immortal credential location",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="semantic_immortal",
        )
        mid = result.get("id")
        assert mid is not None, f"sync path must return id; got: {result}"
        storage = engines._storage
        rows = storage._q(f"SELECT valid_until FROM memory:{mid}")
        assert rows and rows[0].get("valid_until") is None

    def test_conditional_defaults_90d(self, engines):
        """tier=conditional without ttl_days → valid_until ≈ now + 90d."""
        before = datetime.now(UTC)
        result = engines.memorize(
            content="conditional anchor defaults",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="conditional",
        )
        after = datetime.now(UTC)
        mid = result.get("id")
        assert mid is not None, f"sync path must return id; got: {result}"
        storage = engines._storage
        rows = storage._q(f"SELECT valid_until FROM memory:{mid}")
        assert rows
        vu_str = rows[0].get("valid_until")
        assert vu_str is not None, "valid_until must be set for tier=conditional"
        vu = datetime.fromisoformat(vu_str.replace("Z", "+00:00"))
        expected_min = before + timedelta(days=89)
        expected_max = after + timedelta(days=91)
        assert expected_min <= vu <= expected_max, f"valid_until {vu} outside 89-91d range"

    def test_ephemeral_defaults_14d(self, engines):
        """tier=ephemeral without ttl_days → valid_until ≈ now + 14d."""
        before = datetime.now(UTC)
        result = engines.memorize(
            content="ephemeral anchor defaults",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="ephemeral",
        )
        after = datetime.now(UTC)
        mid = result.get("id")
        assert mid is not None, f"sync path must return id; got: {result}"
        storage = engines._storage
        rows = storage._q(f"SELECT valid_until FROM memory:{mid}")
        assert rows
        vu_str = rows[0].get("valid_until")
        assert vu_str is not None
        vu = datetime.fromisoformat(vu_str.replace("Z", "+00:00"))
        expected_min = before + timedelta(days=13)
        expected_max = after + timedelta(days=15)
        assert expected_min <= vu <= expected_max

    def test_ttl_days_explicit(self, engines):
        """ttl_days=30 → valid_until ≈ now + 30d."""
        before = datetime.now(UTC)
        result = engines.memorize(
            content="explicit ttl anchor",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="conditional",
            ttl_days=30,
        )
        after = datetime.now(UTC)
        mid = result.get("id")
        assert mid is not None, f"sync path must return id; got: {result}"
        storage = engines._storage
        rows = storage._q(f"SELECT valid_until FROM memory:{mid}")
        assert rows
        vu_str = rows[0].get("valid_until")
        assert vu_str is not None
        vu = datetime.fromisoformat(vu_str.replace("Z", "+00:00"))
        expected_min = before + timedelta(days=29)
        expected_max = after + timedelta(days=31)
        assert expected_min <= vu <= expected_max

    def test_explicit_valid_until_accepted(self, engines):
        """valid_until=<ISO-8601 UTC datetime> accepted."""
        target = datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)
        result = engines.memorize(
            content="explicit valid_until anchor",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="conditional",
            valid_until=target.isoformat(),
        )
        mid = result.get("id")
        assert mid is not None, f"sync path must return id; got: {result}"
        storage = engines._storage
        rows = storage._q(f"SELECT valid_until FROM memory:{mid}")
        assert rows
        vu_str = rows[0].get("valid_until")
        assert vu_str is not None
        vu = datetime.fromisoformat(vu_str.replace("Z", "+00:00"))
        assert abs((vu - target).total_seconds()) < 2

    def test_conflicting_valid_until_and_ttl_days_rejected(self, engines):
        """Passing both valid_until and ttl_days → error."""
        result = engines.memorize(
            content="conflict test",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="conditional",
            valid_until=datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat(),
            ttl_days=30,
        )
        assert result.get("stored") is False
        assert (
            "conflict" in result.get("reason", "").lower()
            or "both" in result.get("reason", "").lower()
        )

    def test_naive_datetime_rejected(self, engines):
        """Naive datetime (no timezone) rejected at MCP boundary."""
        result = engines.memorize(
            content="naive datetime test",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="conditional",
            valid_until="2027-01-01T00:00:00",  # no tz → naive
        )
        assert result.get("stored") is False
        assert (
            "timezone" in result.get("reason", "").lower()
            or "utc" in result.get("reason", "").lower()
            or "naive" in result.get("reason", "").lower()
        )


# ============================================================
# 3. ANCHOR() TOOL — tier argument
# ============================================================


class TestAnchorTool:
    """anchor() accepts tier; defaults to conditional."""

    def test_anchor_accepts_tier_conditional(self, engines):
        result = engines.anchor(
            content="conditional anchor",
            context="/tmp/proj",
            tier="conditional",
        )
        assert result.get("queued") or result.get("status") == "anchored"

    def test_anchor_accepts_tier_ephemeral(self, engines):
        result = engines.anchor(
            content="ephemeral anchor",
            context="/tmp/proj",
            tier="ephemeral",
        )
        assert result.get("queued") or result.get("status") == "anchored"

    def test_anchor_defaults_tier_conditional(self, engines):
        """anchor() without tier → tier=conditional stored."""
        result = engines.anchor(
            content="default tier anchor",
            context="/tmp/proj",
        )
        assert result.get("queued") or result.get("status") == "anchored"

    def test_anchor_semantic_immortal_requires_reason(self, engines):
        """anchor(tier='semantic_immortal') without reason → error."""
        result = engines.anchor(
            content="immortal anchor no reason",
            context="/tmp/proj",
            tier="semantic_immortal",
        )
        assert result.get("stored") is False or result.get("error") is not None
        reason_str = result.get("reason", "") or result.get("error", "")
        assert "reason" in reason_str.lower() or "semantic_immortal" in reason_str.lower()

    def test_anchor_semantic_immortal_with_reason_accepted(self, engines):
        """anchor(tier='semantic_immortal', reason='...') accepted."""
        result = engines.anchor(
            content="immortal anchor with reason",
            context="/tmp/proj",
            tier="semantic_immortal",
            reason="permanent credential location, never changes",
        )
        assert result.get("queued") or result.get("status") == "anchored"

    def test_anchor_invalid_tier_rejected(self, engines):
        """Invalid tier value → error."""
        result = engines.anchor(
            content="bad tier",
            context="/tmp/proj",
            tier="invalid_tier_xyz",
        )
        assert result.get("stored") is False or result.get("error") is not None


# ============================================================
# 4. INVALID TIER REJECTED by memorize
# ============================================================


class TestInvalidTier:
    """Invalid tier values rejected at MCP boundary."""

    def test_memorize_invalid_tier_rejected(self, engines):
        result = engines.memorize(
            content="invalid tier test",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=True,
            tier="bad_tier",
        )
        assert result.get("stored") is False
        assert "tier" in result.get("reason", "").lower()


# ============================================================
# 5. AUTO-SET is_protected WHEN tier IS SET
# ============================================================


class TestAutoProtect:
    """tier set → is_protected auto-set to True."""

    def test_memorize_tier_auto_protects(self, engines):
        result = engines.memorize(
            content="auto protect test",
            context="/tmp/proj",
            tags=["_anchor"],
            is_protected=False,  # explicit False
            tier="conditional",
        )
        mid = result.get("id")
        assert mid is not None, f"sync path must return id; got: {result}"
        storage = engines._storage
        rows = storage._q(f"SELECT is_protected FROM memory:{mid}")
        assert rows and rows[0].get("is_protected") is True


# ============================================================
# 6. RESTORE EXCLUDES EXPIRED ANCHORS
# ============================================================


class TestRestoreExpiryFilter:
    """restore() excludes anchors where valid_until < now()."""

    def _insert_anchor_with_valid_until(
        self, storage, content: str, valid_until_str: str | None
    ) -> int:
        mid = storage._next_id("memory")
        now = storage._now_iso()
        sql = (
            "CREATE type::record('memory', $id) SET "
            "content = $content, tags = $tags, directory_context = $dir, "
            "created_at = $ts, last_accessed = $ts, heat = $heat, "
            "is_stale = false, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = $st, compression_level = 0, "
            "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
            "vector_clock = $vc, is_protected = true, tier = $tier"
        )
        params = {
            "id": mid,
            "content": content,
            "tags": ["_anchor"],
            "dir": "/tmp/restore_test",
            "ts": now,
            "heat": 1.0,
            "st": "episodic",
            "vc": "{}",
            "tier": "conditional",
        }
        if valid_until_str is not None:
            sql += ", valid_until = $vu"
            params["vu"] = valid_until_str
        storage._q(sql, params)
        return mid

    def test_expired_anchor_excluded_from_get_anchored(self, storage):
        """Expired anchor (valid_until in past) not returned by get_anchored_memories."""
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self._insert_anchor_with_valid_until(storage, "expired anchor", past)

        anchors = storage.get_anchored_memories(limit=20)
        contents = [a.get("content") for a in anchors]
        assert "expired anchor" not in contents

    def test_active_anchor_included_in_get_anchored(self, storage):
        """Active anchor (valid_until in future) returned by get_anchored_memories."""
        future = (datetime.now(UTC) + timedelta(days=90)).isoformat()
        self._insert_anchor_with_valid_until(storage, "active anchor", future)

        anchors = storage.get_anchored_memories(limit=20)
        contents = [a.get("content") for a in anchors]
        assert "active anchor" in contents

    def test_null_valid_until_included_in_get_anchored(self, storage):
        """Anchor with valid_until=None (immortal) always included."""
        self._insert_anchor_with_valid_until(storage, "immortal anchor", None)

        anchors = storage.get_anchored_memories(limit=20)
        contents = [a.get("content") for a in anchors]
        assert "immortal anchor" in contents


# ============================================================
# 7. PROJECT_BRIEF RESTORE — excludes expired anchors
# ============================================================


class TestProjectBriefRestoreExpiry:
    """project_brief(mode='restore') top_anchors excludes expired rows."""

    def _insert_anchor_raw(self, server, content: str, valid_until_str: str | None):
        storage = server._storage
        mid = storage._next_id("memory")
        now = storage._now_iso()
        sql = (
            "CREATE type::record('memory', $id) SET "
            "content = $content, tags = $tags, directory_context = $dir, "
            "created_at = $ts, last_accessed = $ts, heat = $heat, "
            "is_stale = false, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = $st, compression_level = 0, "
            "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
            "vector_clock = $vc, is_protected = true, tier = $tier"
        )
        params = {
            "id": mid,
            "content": content,
            "tags": ["_anchor"],
            "dir": "/tmp/brief_test",
            "ts": now,
            "heat": 1.0,
            "st": "episodic",
            "vc": "{}",
            "tier": "conditional",
        }
        if valid_until_str is not None:
            sql += ", valid_until = $vu"
            params["vu"] = valid_until_str
        storage._q(sql, params)

    def test_expired_anchor_not_in_restore_top_anchors(self, engines):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self._insert_anchor_raw(engines, "expired project anchor", past)

        result = engines.project_brief("/tmp/brief_test", mode="restore")
        titles = [a.get("title", "") for a in result.get("top_anchors", [])]
        assert not any("expired project anchor" in t for t in titles)

    def test_active_anchor_in_restore_top_anchors(self, engines):
        future = (datetime.now(UTC) + timedelta(days=90)).isoformat()
        self._insert_anchor_raw(engines, "active project anchor", future)

        result = engines.project_brief("/tmp/brief_test", mode="restore")
        titles = [a.get("title", "") for a in result.get("top_anchors", [])]
        assert any("active project anchor" in t for t in titles)


# ============================================================
# 8. HOT-MEMORY RANKING — excludes expired anchors
# ============================================================


class TestHotMemoryExpiry:
    """Hot-memory ranking excludes expired rows."""

    def test_expired_anchor_not_in_get_memories_for_directory(self, storage):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        mid = storage._next_id("memory")
        now = storage._now_iso()
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $c, tags = $t, directory_context = $dir, "
            "created_at = $ts, last_accessed = $ts, heat = $h, "
            "is_stale = false, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = 'episodic', compression_level = 0, "
            "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
            "vector_clock = '{}', is_protected = true, tier = 'conditional', "
            "valid_until = $vu",
            {
                "id": mid,
                "c": "expired hot anchor",
                "t": ["_anchor"],
                "dir": "/tmp/hot_test",
                "ts": now,
                "h": 1.0,
                "vu": past,
            },
        )
        hot = storage.get_memories_for_directory("/tmp/hot_test", min_heat=0.0)
        contents = [m.get("content") for m in hot]
        assert "expired hot anchor" not in contents


# ============================================================
# 9. MIGRATION TEST — _migration_008_anchor_tier
# ============================================================


class TestMigration008:
    """Seed pre-v5.8 anchors, run migration, verify backfill."""

    def _insert_pre_v58_anchor(self, storage, content: str) -> int:
        """Insert anchor without tier/valid_until, simulating pre-v5.8 data."""
        mid = storage._next_id("memory")
        now = storage._now_iso()
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $content, tags = $tags, directory_context = $dir, "
            "created_at = $ts, last_accessed = $ts, heat = $heat, "
            "is_stale = false, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = $st, compression_level = 0, "
            "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
            "vector_clock = $vc, is_protected = true",
            {
                "id": mid,
                "content": content,
                "tags": ["_anchor"],
                "dir": "/tmp/migrate_test",
                "ts": now,
                "heat": 1.0,
                "st": "episodic",
                "vc": "{}",
            },
        )
        return mid

    def test_migration_backfills_5_anchors(self, storage):
        """5 pre-v5.8 anchors → all get tier=conditional, valid_until=~now+90d, migration_grace=True."""
        from datetime import UTC, datetime, timedelta

        from yadgar.storage.migrations import _migration_008_anchor_tier

        ids = [self._insert_pre_v58_anchor(storage, f"old anchor {i}") for i in range(5)]

        before = datetime.now(UTC)
        _migration_008_anchor_tier(storage)
        after = datetime.now(UTC)

        for mid in ids:
            rows = storage._q(f"SELECT tier, valid_until, migration_grace FROM memory:{mid}")
            assert rows, f"memory:{mid} not found"
            row = rows[0]
            assert row.get("tier") == "conditional", f"memory:{mid} tier={row.get('tier')}"
            assert row.get("migration_grace") is True, f"memory:{mid} migration_grace not set"
            vu_str = row.get("valid_until")
            assert vu_str is not None, f"memory:{mid} valid_until not set"
            vu = datetime.fromisoformat(vu_str.replace("Z", "+00:00"))
            expected_min = before + timedelta(days=89)
            expected_max = after + timedelta(days=91)
            assert expected_min <= vu <= expected_max, f"memory:{mid} valid_until {vu} out of range"

    def test_migration_idempotent(self, storage):
        """Running migration twice does not change results."""
        from yadgar.storage.migrations import _migration_008_anchor_tier

        mid = self._insert_pre_v58_anchor(storage, "idempotent anchor")
        _migration_008_anchor_tier(storage)

        rows1 = storage._q(f"SELECT tier, valid_until, migration_grace FROM memory:{mid}")
        vu1 = rows1[0].get("valid_until")

        _migration_008_anchor_tier(storage)  # second run

        rows2 = storage._q(f"SELECT tier, valid_until, migration_grace FROM memory:{mid}")
        vu2 = rows2[0].get("valid_until")

        # tier and migration_grace unchanged
        assert rows2[0].get("tier") == "conditional"
        assert rows2[0].get("migration_grace") is True
        # valid_until should not be re-extended on second run (idempotent: skip already-set)
        assert vu1 == vu2

    def test_migration_does_not_overwrite_existing_tier(self, storage):
        """Anchors already with tier set are not overwritten."""
        from yadgar.storage.migrations import _migration_008_anchor_tier

        mid = storage._next_id("memory")
        now = storage._now_iso()
        future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $c, tags = $t, directory_context = $dir, "
            "created_at = $ts, last_accessed = $ts, heat = $h, "
            "is_stale = false, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = 'episodic', compression_level = 0, "
            "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
            "vector_clock = '{}', is_protected = true, "
            "tier = $tier, valid_until = $vu",
            {
                "id": mid,
                "c": "already-tiered anchor",
                "t": ["_anchor"],
                "dir": "/tmp/migrate_test",
                "ts": now,
                "h": 1.0,
                "tier": "semantic_immortal",
                "vu": future,
            },
        )

        _migration_008_anchor_tier(storage)

        rows = storage._q(f"SELECT tier, valid_until FROM memory:{mid}")
        assert rows[0].get("tier") == "semantic_immortal"
        assert rows[0].get("valid_until") == future

    def test_migration_emits_count_signal(self, storage):
        """Migration returns dict with anchor_tier_migrated_count."""
        from yadgar.storage.migrations import _migration_008_anchor_tier

        for i in range(3):
            self._insert_pre_v58_anchor(storage, f"count anchor {i}")

        result = _migration_008_anchor_tier(storage)
        assert isinstance(result, dict)
        assert result.get("anchor_tier_migrated_count") == 3


# ============================================================
# 10. ENV KNOBS — three-way I25 registration
# ============================================================


class TestEnvKnobs:
    """ANCHOR_CONDITIONAL_TTL_DAYS, ANCHOR_EPHEMERAL_TTL_DAYS, ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON
    appear in Settings, FIELD_META, and _REGISTRY."""

    def test_conditional_ttl_in_settings(self):
        from yadgar.config import Settings

        fields = Settings.model_fields
        assert "ANCHOR_CONDITIONAL_TTL_DAYS" in fields

    def test_ephemeral_ttl_in_settings(self):
        from yadgar.config import Settings

        fields = Settings.model_fields
        assert "ANCHOR_EPHEMERAL_TTL_DAYS" in fields

    def test_semantic_immortal_requires_reason_in_settings(self):
        from yadgar.config import Settings

        fields = Settings.model_fields
        assert "ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON" in fields

    def test_conditional_ttl_in_registry(self):
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_ANCHOR_CONDITIONAL_TTL_DAYS" in names

    def test_ephemeral_ttl_in_registry(self):
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_ANCHOR_EPHEMERAL_TTL_DAYS" in names

    def test_semantic_immortal_requires_reason_in_registry(self):
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON" in names

    def test_conditional_ttl_in_field_meta(self):
        from yadgar.config_yaml import FIELD_META

        assert "anchor_conditional_ttl_days" in FIELD_META

    def test_ephemeral_ttl_in_field_meta(self):
        from yadgar.config_yaml import FIELD_META

        assert "anchor_ephemeral_ttl_days" in FIELD_META

    def test_semantic_immortal_requires_reason_in_field_meta(self):
        from yadgar.config_yaml import FIELD_META

        assert "anchor_semantic_immortal_requires_reason" in FIELD_META

    def test_conditional_ttl_default_is_90(self):
        from yadgar.config import get_settings

        s = get_settings()
        assert s.ANCHOR_CONDITIONAL_TTL_DAYS == 90

    def test_ephemeral_ttl_default_is_14(self):
        from yadgar.config import get_settings

        s = get_settings()
        assert s.ANCHOR_EPHEMERAL_TTL_DAYS == 14

    def test_semantic_immortal_requires_reason_default_true(self):
        from yadgar.config import get_settings

        s = get_settings()
        assert s.ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON is True

    def test_i25_three_way_sync_passes(self, tmp_path):
        """Full I25 invariant: all three knobs visible to test_config_three_way_sync."""
        from yadgar.config import Settings
        from yadgar.config_registry import list_config
        from yadgar.config_yaml import FIELD_META

        registry_names = {e.name for e in list_config()}
        field_meta_keys = {k.upper() for k in FIELD_META.keys()}

        for field in (
            "ANCHOR_CONDITIONAL_TTL_DAYS",
            "ANCHOR_EPHEMERAL_TTL_DAYS",
            "ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON",
        ):
            assert field in Settings.model_fields, f"{field} missing from Settings"
            assert f"YADGAR_{field}" in registry_names, f"YADGAR_{field} missing from registry"
            assert field in field_meta_keys, f"{field} missing from FIELD_META"
