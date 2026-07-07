"""TDD tests for v5.9.0: audit_anchors() MCP tool.

Scope:
  - audit_anchors(dry_run=True) returns recommendations; no DB mutations.
  - audit_anchors(dry_run=False) applies safe mutations (forget_expired,
    merge_redundant); logs to action_log.
  - tier="semantic_immortal" rows NEVER auto-mutated regardless of dry_run.
  - is_protected=True legacy rows NEVER auto-mutated.
  - promote action returns DRAFT dict only — never calls wiki_add.
  - Forget-expired only fires for valid_until < now() (negative: future-dated untouched).
  - Merge picks deterministic survivor: higher last_accessed+access_count rank kept.
  - dry_run=False is idempotent: second call returns empty applied list.
  - include_global=True audits directory_context="global" separately.
  - ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN cap respected; extras dropped with _truncated=True.

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("audit_anchors")
    server.init_engines(
        db_path=str(tmp_path / "test_audit_anchors.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar._shared.runtime.lifecycle import _get_storage

    return _get_storage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIR = "/tmp/test_audit_proj"
_GLOBAL_DIR = "global"


def _make_embedding_bytes(n_dims: int, value: float) -> bytes:
    """Create fake embedding: all components=value, then L2-normalised."""
    vec = [value] * n_dims
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return struct.pack(f"{n_dims}f", *vec)


def _insert_anchor(storage, content: str, directory: str = _DIR, **kw) -> int:  # noqa: PLR0912
    """Insert an anchor row directly via storage._q.

    kwargs: embedding, valid_until, migration_grace, tags, tier, is_protected,
            access_count, last_accessed.
    """
    now = storage._now_iso()
    mid = storage._next_id("memory")
    tier = kw.get("tier", "conditional")
    is_protected = kw.get("is_protected", True)
    access_count = kw.get("access_count", 0)
    last_accessed = kw.get("last_accessed") or now
    tags = kw.get("tags") or []
    base_tags = ["_anchor"] + list(tags)
    params: dict = {
        "id": mid,
        "content": content,
        "dir": directory,
        "tags": base_tags,
        "heat": 0.5,
        "is_protected": is_protected,
        "tier": tier,
        "access_count": access_count,
        "last_accessed": last_accessed,
        "created_at": now,
    }
    sql = (
        "CREATE type::record('memory', $id) SET "
        "content = $content, directory_context = $dir, tags = $tags, "
        "heat = $heat, is_protected = $is_protected, tier = $tier, "
        "access_count = $access_count, last_accessed = $last_accessed, "
        "created_at = $created_at"
    )
    emb = kw.get("embedding")
    if emb is not None:
        floats = storage._bytes_to_floats(emb)
        params["emb"] = floats
        sql += ", embedding = $emb"
    valid_until = kw.get("valid_until")
    if valid_until is not None:
        params["valid_until"] = valid_until
        sql += ", valid_until = $valid_until"
    if kw.get("migration_grace"):
        params["grace"] = True
        sql += ", migration_grace = $grace"
    storage._q(sql, params)
    return mid


# Long content for promote detection (>500 words, ≥2 headers)
_PROMOTE_CONTENT = "# Main Rule\n\n" + "word " * 510 + "\n\n## Section Two\n\nMore content here."


# ---------------------------------------------------------------------------
# 1. dry_run=True — returns recommendations, no mutations
# ---------------------------------------------------------------------------


class TestDryRunTrue:
    """audit_anchors(dry_run=True) returns recommendations without mutating DB."""

    def test_returns_expected_keys(self):
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True)
        assert "scanned" in result
        assert "actions" in result
        assert "dry_run" in result
        assert result["dry_run"] is True

    def test_no_applied_field_or_empty_when_dry_run(self):
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True)
        assert result.get("applied", []) == []

    def test_no_db_mutations_on_dry_run(self, storage):
        """Expired anchor not deleted when dry_run=True."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        mid = _insert_anchor(storage, "expired anchor", valid_until=past)

        audit_anchors(directory=_DIR, dry_run=True)

        rows = storage._q(f"SELECT id FROM memory:{mid}")
        assert rows, "Expired anchor was deleted during dry_run=True — must not mutate"

    def test_forget_expired_action_present(self, storage):
        """forget_expired action appears when valid_until < now and migration_grace=False."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        _insert_anchor(storage, "old expired anchor", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        forget_actions = [a for a in actions if a["action"] == "forget_expired"]
        assert forget_actions, "Expected forget_expired action for past valid_until"

    def test_future_dated_not_flagged(self, storage):
        """Anchor with valid_until in the future must NOT appear in forget_expired."""
        from yadgar.core.server.tools.audit import audit_anchors

        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        _insert_anchor(storage, "future anchor", valid_until=future)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        forget_actions = [a for a in actions if a["action"] == "forget_expired"]
        assert not forget_actions, "Future-dated anchor must not appear as forget_expired"

    def test_promote_action_returns_draft_not_wiki_add(self, storage):
        """promote action has draft field; wiki_add never called."""
        from yadgar.core.server.tools.audit import audit_anchors

        _insert_anchor(storage, _PROMOTE_CONTENT, tags=["recipe"])
        result = audit_anchors(directory=_DIR, dry_run=True)
        promote_actions = [a for a in result["actions"] if a["action"] == "promote"]
        assert promote_actions, "Expected promote action for oversized anchor"
        for pa in promote_actions:
            assert "draft" in pa, "promote action must have draft field"
            assert "next_step" in pa, "promote action must have next_step field"

    def test_promote_draft_schema(self, storage):
        """promote draft must have required schema fields."""
        from yadgar.core.server.tools.audit import audit_anchors

        _insert_anchor(storage, _PROMOTE_CONTENT, tags=["workflow"])
        result = audit_anchors(directory=_DIR, dry_run=True)
        promote_actions = [a for a in result["actions"] if a["action"] == "promote"]
        assert promote_actions, "No promote actions found"
        draft = promote_actions[0]["draft"]
        for key in (
            "suggested_slug",
            "suggested_title",
            "suggested_category",
            "suggested_tags",
            "body",
            "rationale",
        ):
            assert key in draft, f"promote draft missing field: {key}"
        assert isinstance(draft["suggested_tags"], list)
        assert draft["body"] == _PROMOTE_CONTENT


# ---------------------------------------------------------------------------
# 2. dry_run=False — applies safe mutations
# ---------------------------------------------------------------------------


class TestDryRunFalse:
    """audit_anchors(dry_run=False) applies safe mutations and logs to action_log."""

    def test_forget_expired_deletes_row(self, storage):
        """dry_run=False deletes expired anchor."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        mid = _insert_anchor(storage, "expired to delete", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=False)

        rows = storage._q(f"SELECT id FROM memory:{mid}")
        assert not rows, "Expired anchor must be deleted when dry_run=False"

        applied = result.get("applied", [])
        assert any(a.get("action") == "forget_expired" for a in applied)

    def test_applied_list_populated(self, storage):
        """applied list has entries when mutations occurred."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "expired row", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=False)
        assert result.get("applied"), "applied list must be non-empty after mutations"

    def test_merge_redundant_keeps_higher_rank(self, storage):
        """Merge: keep anchor with higher last_accessed+access_count; forget lower."""
        from yadgar.core.server.tools.audit import audit_anchors

        emb = _make_embedding_bytes(384, 0.9)

        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        mid_a = _insert_anchor(
            storage,
            "anchor alpha content",
            embedding=emb,
            access_count=10,
            last_accessed=recent,
        )
        old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mid_b = _insert_anchor(
            storage,
            "anchor beta content",
            embedding=emb,
            access_count=2,
            last_accessed=old_ts,
        )

        audit_anchors(directory=_DIR, dry_run=False, cosine_threshold=0.8)

        rows_a = storage._q(f"SELECT id FROM memory:{mid_a}")
        rows_b = storage._q(f"SELECT id FROM memory:{mid_b}")
        assert rows_a, "Higher-rank anchor must be kept after merge"
        assert not rows_b, "Lower-rank anchor must be forgotten after merge"

    def test_semantic_immortal_never_mutated(self, storage):
        """tier=semantic_immortal anchors are never auto-mutated even dry_run=False."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mid = _insert_anchor(
            storage,
            "immortal expired",
            valid_until=past,
            tier="semantic_immortal",
        )

        audit_anchors(directory=_DIR, dry_run=False)

        rows = storage._q(f"SELECT id FROM memory:{mid}")
        assert rows, "semantic_immortal anchor must never be auto-deleted"

    def test_is_protected_legacy_never_mutated(self, storage):
        """Legacy is_protected=True anchors with no tier are never auto-mutated (v5.11 repurpose).

        Pre-v5.8 anchors: is_protected=True, tier=None/absent. These are the legacy rows
        whose repurpose is deferred to v5.11.
        """
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        now = storage._now_iso()
        mid = storage._next_id("memory")
        # Insert WITHOUT tier field — simulates a pre-v5.8 legacy anchor
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $content, directory_context = $dir, tags = $tags, "
            "heat = 0.5, is_protected = true, "
            "valid_until = $vu, created_at = $now, last_accessed = $now, "
            "access_count = 0",
            {
                "id": mid,
                "content": "legacy protected anchor no tier",
                "dir": _DIR,
                "tags": ["_anchor"],
                "vu": past,
                "now": now,
            },
        )

        audit_anchors(directory=_DIR, dry_run=False)

        rows = storage._q(f"SELECT id FROM memory:{mid}")
        assert rows, (
            "Legacy is_protected=True anchor (no tier) must not be auto-deleted (v5.11 repurpose)"
        )

    def test_idempotent_second_call(self, storage):
        """Second call on same state returns empty applied list."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "expire me", valid_until=past)

        audit_anchors(directory=_DIR, dry_run=False)
        result2 = audit_anchors(directory=_DIR, dry_run=False)

        assert result2.get("applied", []) == [], (
            "Second call on same state must return empty applied list"
        )

    def test_promote_not_auto_applied(self, storage):
        """dry_run=False must NOT auto-create wiki pages for promote candidates."""
        from yadgar.core.server.tools.audit import audit_anchors
        from yadgar.core.server.tools.wiki import wiki_list

        _insert_anchor(storage, _PROMOTE_CONTENT, tags=["recipe"])
        audit_anchors(directory=_DIR, dry_run=False)

        pages = wiki_list()
        assert not any("_anchor" in (p.get("tags") or []) for p in pages), (
            "audit_anchors must never auto-create wiki pages"
        )

    def test_action_log_written_on_mutation(self, storage):
        """Mutations are logged to action_log with tool_name=audit_anchors."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "log me", valid_until=past)

        audit_anchors(directory=_DIR, dry_run=False)

        rows = storage._q(
            "SELECT tool_name FROM action_log WHERE tool_name = 'audit_anchors' LIMIT 10"
        )
        assert rows, "action_log must have entry with tool_name='audit_anchors'"


# ---------------------------------------------------------------------------
# 3. include_global=True
# ---------------------------------------------------------------------------


class TestIncludeGlobal:
    """include_global=True audits directory_context="global" separately."""

    def test_global_anchors_scanned_when_flag_set(self, storage):
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "global expired", directory="global", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=True, include_global=True)
        actions = result.get("actions", [])
        forget_actions = [a for a in actions if a.get("action") == "forget_expired"]
        assert forget_actions, "include_global=True must include global expired anchors"

    def test_global_anchors_not_scanned_by_default(self, storage):
        """Without include_global, global anchors are not in scope."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "global expired skip", directory="global", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result.get("actions", [])
        forget_actions = [a for a in actions if a.get("action") == "forget_expired"]
        assert not forget_actions, "Global anchors must not be included unless include_global=True"


# ---------------------------------------------------------------------------
# 4. ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN cap
# ---------------------------------------------------------------------------


class TestMaxActionsCap:
    """ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN cap respected; extras dropped with _truncated=True."""

    def test_cap_respected(self, storage, monkeypatch):
        """With cap=2 and 5 expired anchors, only 2 actions returned."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN", "2")
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()

        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        for i in range(5):
            _insert_anchor(storage, f"expired {i}", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=True)
        assert len(result["actions"]) <= 2, "Actions capped at max"
        assert result.get("_truncated") is True, "_truncated must be True when capped"

        get_settings.cache_clear()

    def test_no_truncation_when_under_cap(self, storage, monkeypatch):
        """No _truncated flag when actions < max."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN", "20")
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()

        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "just one", valid_until=past)

        result = audit_anchors(directory=_DIR, dry_run=True)
        assert not result.get("_truncated"), "_truncated must be absent/False when under cap"

        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 5. Env knob registration (I25)
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    """3 new env knobs present in Settings + config_registry + config_yaml."""

    def test_settings_has_audit_consolidation_enabled(self):
        from yadgar._shared.config import get_settings

        s = get_settings()
        assert hasattr(s, "ANCHOR_AUDIT_CONSOLIDATION_ENABLED")
        assert s.ANCHOR_AUDIT_CONSOLIDATION_ENABLED is True

    def test_settings_has_max_actions_per_run(self):
        from yadgar._shared.config import get_settings

        s = get_settings()
        assert hasattr(s, "ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN")
        assert s.ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN == 20

    def test_settings_has_history_retention_days(self):
        from yadgar._shared.config import get_settings

        s = get_settings()
        assert hasattr(s, "ANCHOR_AUDIT_HISTORY_RETENTION_DAYS")
        assert s.ANCHOR_AUDIT_HISTORY_RETENTION_DAYS == 30

    def test_registry_has_all_three_knobs(self):
        from yadgar._shared.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED" in names
        assert "YADGAR_ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN" in names
        assert "YADGAR_ANCHOR_AUDIT_HISTORY_RETENTION_DAYS" in names

    def test_config_yaml_has_all_three_knobs(self):
        from yadgar._shared.config_yaml import FIELD_META

        assert "anchor_audit_consolidation_enabled" in FIELD_META
        assert "anchor_audit_max_actions_per_run" in FIELD_META
        assert "anchor_audit_history_retention_days" in FIELD_META


# ---------------------------------------------------------------------------
# 6. suggested_call on recommended_actions
# ---------------------------------------------------------------------------


class TestSuggestedCall:
    """audit_anchors action in recommended_actions has suggested_call field."""

    def test_audit_anchors_action_has_suggested_call(self, storage):
        # Force audit_anchors action via an expired anchor (actionability gate — car #20 fix).
        # The old count-only gate (ANCHOR_AUDIT_THRESHOLD=0) is removed; expired anchor is
        # the simplest way to make audit_anchors actionable.
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "trigger audit action", valid_until=past, migration_grace=False)

        result = server.project_brief(_DIR, mode="signals")
        actions = result.get("recommended_actions", [])
        audit_action = next((a for a in actions if a["action"] == "audit_anchors"), None)
        assert audit_action is not None, "audit_anchors action must be emitted"
        assert "suggested_call" in audit_action, "audit_anchors action must have suggested_call"
        sc = audit_action["suggested_call"]
        assert "audit_anchors" in sc
        assert "directory" in sc or _DIR in sc


# ---------------------------------------------------------------------------
# 7. PD-23 migration_grace handler (v5.21.0)
# ---------------------------------------------------------------------------


class TestMigrationGraceHandler:
    """PD-23: verify_grace_expired_anchor recommendation type in audit_anchors().

    When migration_grace=True AND valid_until < now, rows are surfaced as
    verify_grace_expired_anchor candidates requiring user verification.
    They are NEVER auto-applied — skipped=True in all dry_run modes.

    Scope: v5.21.0 deadline driver. First pre-v5.8 anchors expire 2026-08-26.
    """

    def test_verify_grace_action_present_for_expired_grace_row(self, storage):
        """verify_grace_expired_anchor action emitted for migration_grace=True + valid_until<now."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "grace expired anchor", valid_until=past, migration_grace=True)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        grace_actions = [a for a in actions if a["action"] == "verify_grace_expired_anchor"]
        assert grace_actions, "Expected verify_grace_expired_anchor for migration_grace expired row"

    def test_verify_grace_action_shape(self, storage):
        """verify_grace_expired_anchor action dict has required fields."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        mid = _insert_anchor(
            storage, "grace expired content", valid_until=past, migration_grace=True
        )

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        grace_actions = [a for a in actions if a["action"] == "verify_grace_expired_anchor"]
        assert grace_actions, "Expected verify_grace_expired_anchor action"
        ga = grace_actions[0]
        assert "id" in ga
        assert "expired_at" in ga
        assert "rationale" in ga
        assert ga["id"] == mid

    def test_verify_grace_skipped_true_never_auto_applied(self, storage):
        """verify_grace_expired_anchor always has skipped=True — never auto-applied."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "grace do not delete", valid_until=past, migration_grace=True)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        for ga in [a for a in actions if a["action"] == "verify_grace_expired_anchor"]:
            assert ga.get("skipped") is True, "verify_grace_expired_anchor must always be skipped"

    def test_grace_row_not_deleted_dry_run_false(self, storage):
        """migration_grace=True rows are NOT deleted even when dry_run=False."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        mid = _insert_anchor(storage, "grace protected row", valid_until=past, migration_grace=True)

        audit_anchors(directory=_DIR, dry_run=False)

        rows = storage._q(f"SELECT id FROM memory:{mid}")
        assert rows, "migration_grace=True row must NOT be deleted by dry_run=False"

    def test_valid_grace_row_not_flagged(self, storage):
        """migration_grace=True row with valid_until in the future is NOT flagged."""
        from yadgar.core.server.tools.audit import audit_anchors

        future = (datetime.now(UTC) + timedelta(days=60)).isoformat()
        _insert_anchor(storage, "grace still valid", valid_until=future, migration_grace=True)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        grace_actions = [a for a in actions if a["action"] == "verify_grace_expired_anchor"]
        assert not grace_actions, "Future-dated grace row must NOT appear in verify_grace actions"

    def test_non_grace_expired_row_not_flagged_as_grace(self, storage):
        """migration_grace=False expired rows use forget_expired, not verify_grace_expired_anchor."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "normal expired", valid_until=past, migration_grace=False)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        grace_actions = [a for a in actions if a["action"] == "verify_grace_expired_anchor"]
        assert not grace_actions, "Non-grace expired row must use forget_expired, not verify_grace"

    def test_grace_action_rationale_mentions_migration_grace(self, storage):
        """rationale field references migration_grace to aid user understanding."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _insert_anchor(storage, "explain grace", valid_until=past, migration_grace=True)

        result = audit_anchors(directory=_DIR, dry_run=True)
        actions = result["actions"]
        grace_actions = [a for a in actions if a["action"] == "verify_grace_expired_anchor"]
        assert grace_actions
        rationale = grace_actions[0]["rationale"]
        assert "migration_grace" in rationale.lower() or "grace" in rationale.lower(), (
            "rationale must mention grace period for clarity"
        )


# ---------------------------------------------------------------------------
# 8. v5.49 Phase 0 — anchored-by-prose-only detection
# ---------------------------------------------------------------------------


def _insert_prose_only_archive(storage, content: str, **kw) -> int:
    """Insert a memory_archive row that looks like an anchored-by-prose-only memory.

    By default: no _anchor tag, is_protected=false, heat=0.
    kwargs: tags, is_protected, heat.
    """
    now = storage._now_iso()
    aid = storage._next_id("memory_archive")
    tags = kw.get("tags") or []
    is_protected = kw.get("is_protected", False)
    heat = kw.get("heat", 0)
    storage._q(
        "CREATE type::record('memory_archive', $id) SET "
        "content = $content, original_memory_id = $orig, "
        "archived_at = $now, tags = $tags, "
        "is_protected = $is_protected, heat = $heat",
        {
            "id": aid,
            "content": content,
            "orig": 0,
            "now": now,
            "tags": tags,
            "is_protected": is_protected,
            "heat": heat,
        },
    )
    return aid


class TestAnchoredByProseOnly:
    """v5.49 Phase 0 — audit_anchors detects prose-only anchored archives at-risk from retention."""

    def test_audit_anchors_detects_prose_only(self, storage):
        """Archive with no _anchor tag + is_protected=false + heat=0 → count==1, sample contains ID,
        recommended_action present."""
        from yadgar.core.server.tools.audit import audit_anchors

        aid = _insert_prose_only_archive(storage, "prose-only anchor content")

        result = audit_anchors(directory=_DIR, dry_run=True)
        prose = result["anchored_by_prose_only"]
        assert prose["count"] == 1
        assert aid in prose["sample"]
        assert "recommended_action" in prose, "recommended_action must be present when count > 0"

    def test_audit_anchors_skips_tagged_anchor(self, storage):
        """Archive with _anchor tag → not counted as prose-only (count == 0)."""
        from yadgar.core.server.tools.audit import audit_anchors

        _insert_prose_only_archive(storage, "tagged anchor", tags=["_anchor"])

        result = audit_anchors(directory=_DIR, dry_run=True)
        prose = result["anchored_by_prose_only"]
        assert prose["count"] == 0

    def test_audit_anchors_skips_protected(self, storage):
        """Archive with is_protected=true → not counted as prose-only (count == 0)."""
        from yadgar.core.server.tools.audit import audit_anchors

        _insert_prose_only_archive(storage, "protected archive", is_protected=True)

        result = audit_anchors(directory=_DIR, dry_run=True)
        prose = result["anchored_by_prose_only"]
        assert prose["count"] == 0

    def test_audit_anchors_empty_archive(self):
        """No archive rows at all → count == 0 and no recommended_action key."""
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True)
        prose = result["anchored_by_prose_only"]
        assert prose["count"] == 0
        assert "recommended_action" not in prose, (
            "recommended_action must be absent when count == 0"
        )

    def test_audit_anchors_skips_hot_archive(self, storage):
        """Archive with heat > 0 → not counted (recently accessed, not stale)."""
        from yadgar.core.server.tools.audit import audit_anchors

        _insert_prose_only_archive(storage, "hot archive", heat=0.5)

        result = audit_anchors(directory=_DIR, dry_run=True)
        prose = result["anchored_by_prose_only"]
        assert prose["count"] == 0
