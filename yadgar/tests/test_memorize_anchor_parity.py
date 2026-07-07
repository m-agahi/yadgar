"""TDD tests for memorize/anchor parity (v5.10.x).

Tests fail before implementation (c7), then go green after (c8).

Coverage per plan §4:
  - memorize(is_protected=True) auto-sets tier="conditional" when tier=None
  - memorize(is_protected=True) auto-prepends _anchor to tags
  - memorize(is_protected=True, reason="X") adds anchor:X to tags
  - memorize(is_protected=True, tier="ephemeral") uses ANCHOR_EPHEMERAL_TTL_DAYS
  - memorize(is_protected=True, tier="semantic_immortal") without reason → raises
  - memorize(is_protected=False) → defaults unchanged, no _anchor injection
  - audit_anchors finds both anchor()- and memorize(is_protected=True)-created rows
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_settings(**kwargs):
    defaults = {
        "ANCHOR_CONDITIONAL_TTL_DAYS": 90,
        "ANCHOR_EPHEMERAL_TTL_DAYS": 14,
        "ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON": True,
        "DECISION_AUTO_PROTECT": False,
        "CONTEXTUAL_PREFIX_ENABLED": False,
        "REINJECT_ON_WRITE": False,
        "REINJECTION_ENABLED": False,
        "REINJECTION_MAX_RESULTS": 3,
        "MICRO_CHECKPOINT_ENABLED": False,
        "CRDT_AGENT_ID": "test-agent",
        "HOT_THRESHOLD": 0.5,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _build_memorize_sync_env(monkeypatch, tmp_path):
    """Set up sync (is_draining=True) environment for memorize tests."""
    import importlib

    import yadgar._shared.runtime.state as _st
    import yadgar.core.file_queue as _fq

    monkeypatch.setattr(_fq, "is_draining", lambda: True)
    # Patch the direct reference inside memorize module (module-level import)
    _mem_mod = importlib.import_module("yadgar.core.server.tools.memorize")
    monkeypatch.setattr(_mem_mod, "is_draining", lambda: True)

    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = 100
    mock_storage.get_memory.return_value = {
        "id": 100,
        "content": "test",
        "tags": [],
        "heat": 1.0,
        "is_protected": True,
        "tier": None,
        "valid_until": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    mock_storage.update_memory_fields.return_value = None
    mock_storage.update_memory_scores.return_value = None
    mock_storage.upsert_file_hash.return_value = None
    mock_storage._q.return_value = []

    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = None
    mock_embeddings.get_model_name.return_value = "test-model"

    mock_buffer = MagicMock()
    mock_buffer.capture.return_value = None
    mock_buffer.capture_action.return_value = None
    mock_buffer.get_action_summary.return_value = None

    monkeypatch.setattr(_st, "_storage", mock_storage)
    monkeypatch.setattr(_st, "_embeddings", mock_embeddings)
    monkeypatch.setattr(_st, "_buffer", mock_buffer)
    monkeypatch.setattr(_st, "_curator", None)
    monkeypatch.setattr(_st, "_thermo", None)
    monkeypatch.setattr(_st, "_write_gate", None)
    monkeypatch.setattr(_st, "_retriever", None)
    monkeypatch.setattr(_st, "_consolidation", None)
    monkeypatch.setattr(_st, "_pool", None)
    monkeypatch.setattr(_st, "_prospective", None)
    monkeypatch.setattr(_st, "_engram", None)
    monkeypatch.setattr(_st, "_replay", None)
    monkeypatch.setattr(_st, "_rules_engine", None)

    monkeypatch.setattr("yadgar._shared.runtime.lifecycle._get_storage", lambda: mock_storage)
    monkeypatch.setattr("yadgar._shared.runtime.lifecycle._get_embeddings", lambda: mock_embeddings)
    monkeypatch.setattr("yadgar._shared.runtime.lifecycle._get_buffer", lambda: mock_buffer)

    mock_settings = _make_mock_settings()
    monkeypatch.setattr(_mem_mod, "settings", mock_settings)

    return {"storage": mock_storage, "embeddings": mock_embeddings, "settings": mock_settings}


# ---------------------------------------------------------------------------
# Auto-set tier="conditional" when is_protected=True and tier=None
# ---------------------------------------------------------------------------


class TestProtectedAutoTier:
    def test_auto_sets_conditional_tier(self, monkeypatch, tmp_path):
        """memorize(is_protected=True) with no tier → tier auto-set to 'conditional'."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        # Capture the insert call to check tier
        insert_calls = []

        def _capture_insert(memory, **kwargs):
            insert_calls.append(dict(memory))
            return 100

        env["storage"].insert_memory.side_effect = _capture_insert

        memorize(
            content="Critical architecture decision",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
        )

        # After insert, update_memory_fields should carry tier=conditional
        # OR insert_memory should have tier=conditional in the payload
        update_calls = env["storage"].update_memory_fields.call_args_list
        all_tier_args = [
            call.kwargs.get("tier") or (call.args[1] if len(call.args) > 1 else None)
            for call in update_calls
        ]
        insert_tier = insert_calls[0].get("tier") if insert_calls else None

        # At least one path must have tier="conditional"
        assert insert_tier == "conditional" or "conditional" in all_tier_args, (
            f"Expected tier=conditional somewhere. insert_tier={insert_tier}, "
            f"update tiers={all_tier_args}"
        )

    def test_provided_tier_respected(self, monkeypatch, tmp_path):
        """When tier='ephemeral' is given, it must not be overridden."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        insert_calls = []

        def _capture_insert(memory, **kwargs):
            insert_calls.append(dict(memory))
            return 100

        env["storage"].insert_memory.side_effect = _capture_insert

        memorize(
            content="Ephemeral working note",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            tier="ephemeral",
        )

        insert_tier = insert_calls[0].get("tier") if insert_calls else None
        update_calls = env["storage"].update_memory_fields.call_args_list
        update_tiers = [c.kwargs.get("tier") for c in update_calls if c.kwargs.get("tier")]

        # ephemeral must be present, conditional must NOT override it
        assert insert_tier == "ephemeral" or "ephemeral" in update_tiers, (
            f"Expected tier=ephemeral. insert_tier={insert_tier}, updates={update_tiers}"
        )
        assert "conditional" not in update_tiers or insert_tier != "conditional", (
            f"conditional must not override explicit ephemeral. tiers={update_tiers}"
        )


# ---------------------------------------------------------------------------
# Auto-prepend _anchor to tags
# ---------------------------------------------------------------------------


class TestProtectedAutoAnchorTag:
    def test_auto_prepends_anchor_tag(self, monkeypatch, tmp_path):
        """memorize(is_protected=True) must add _anchor to tags."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Important decision",
            context=str(tmp_path),
            tags=["custom-tag"],
            is_protected=True,
        )

        # Check tags via update_memory_fields calls
        update_calls = env["storage"].update_memory_fields.call_args_list
        all_tags = []
        for call in update_calls:
            tags = call.kwargs.get("tags")
            if tags is not None:
                all_tags.extend(tags)

        assert "_anchor" in all_tags, (
            f"_anchor must be auto-added to tags. update calls: {update_calls}"
        )

    def test_anchor_tag_not_duplicated(self, monkeypatch, tmp_path):
        """If _anchor already in tags, it must not be duplicated."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Already anchored",
            context=str(tmp_path),
            tags=["_anchor"],
            is_protected=True,
        )

        update_calls = env["storage"].update_memory_fields.call_args_list
        all_tag_lists = [
            call.kwargs.get("tags") for call in update_calls if call.kwargs.get("tags")
        ]
        for tag_list in all_tag_lists:
            assert tag_list.count("_anchor") <= 1, f"_anchor duplicated in {tag_list}"


# ---------------------------------------------------------------------------
# reason kwarg → anchor:{reason} tag
# ---------------------------------------------------------------------------


class TestProtectedReasonTag:
    def test_reason_adds_anchor_colon_tag(self, monkeypatch, tmp_path):
        """memorize(is_protected=True, reason='X') must add 'anchor:X' to tags."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Schema choice",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            reason="schema-decision",
        )

        update_calls = env["storage"].update_memory_fields.call_args_list
        all_tags = []
        for call in update_calls:
            tags = call.kwargs.get("tags")
            if tags is not None:
                all_tags.extend(tags)

        assert "anchor:schema-decision" in all_tags, f"anchor:reason tag not found. tags={all_tags}"

    def test_empty_reason_no_colon_tag(self, monkeypatch, tmp_path):
        """Empty reason must not add 'anchor:' tag."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Anchor without reason",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            reason="",
        )

        update_calls = env["storage"].update_memory_fields.call_args_list
        all_tags = []
        for call in update_calls:
            tags = call.kwargs.get("tags")
            if tags is not None:
                all_tags.extend(tags)

        colon_tags = [t for t in all_tags if t.startswith("anchor:")]
        assert len(colon_tags) == 0, f"anchor: tag added for empty reason: {colon_tags}"


# ---------------------------------------------------------------------------
# valid_until computation from tier
# ---------------------------------------------------------------------------


class TestProtectedTTLComputation:
    def test_conditional_tier_gets_90d_ttl(self, monkeypatch, tmp_path):
        """conditional tier → valid_until = now + 90d."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        insert_calls = []

        def _capture(memory, **kwargs):
            insert_calls.append(dict(memory))
            return 100

        env["storage"].insert_memory.side_effect = _capture

        before = datetime.now(UTC)
        memorize(
            content="Conditional anchor",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            # No tier given — should auto-default to conditional
        )
        after = datetime.now(UTC)

        # Check valid_until in insert payload
        if insert_calls and insert_calls[0].get("valid_until"):
            vu = datetime.fromisoformat(insert_calls[0]["valid_until"])
            expected_min = before + timedelta(days=89)
            expected_max = after + timedelta(days=91)
            assert expected_min < vu < expected_max, (
                f"valid_until {vu} not in 90d window [{expected_min}, {expected_max}]"
            )

    def test_ephemeral_tier_gets_14d_ttl(self, monkeypatch, tmp_path):
        """ephemeral tier → valid_until = now + 14d."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        insert_calls = []

        def _capture(memory, **kwargs):
            insert_calls.append(dict(memory))
            return 100

        env["storage"].insert_memory.side_effect = _capture

        before = datetime.now(UTC)
        memorize(
            content="Ephemeral anchor",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            tier="ephemeral",
        )
        after = datetime.now(UTC)

        if insert_calls and insert_calls[0].get("valid_until"):
            vu = datetime.fromisoformat(insert_calls[0]["valid_until"])
            expected_min = before + timedelta(days=13)
            expected_max = after + timedelta(days=15)
            assert expected_min < vu < expected_max, f"valid_until {vu} not in 14d window"


# ---------------------------------------------------------------------------
# semantic_immortal requires reason
# ---------------------------------------------------------------------------


class TestSemanticImmortalRequiresReason:
    def test_semantic_immortal_without_reason_rejected(self, monkeypatch, tmp_path):
        """semantic_immortal without reason must be rejected when flag is true."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        env["settings"].ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON = True
        from yadgar.core.server.tools.memorize import memorize

        result = memorize(
            content="Forever anchor",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            tier="semantic_immortal",
            # No reason kwarg
        )
        assert result.get("stored") is False, (
            f"semantic_immortal without reason must be rejected: {result}"
        )

    def test_semantic_immortal_with_reason_accepted(self, monkeypatch, tmp_path):
        """semantic_immortal with reason must be accepted."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        env["settings"].ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON = True
        from yadgar.core.server.tools.memorize import memorize

        result = memorize(
            content="Forever anchor with reason",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            tier="semantic_immortal",
            reason="This is a permanent architectural constraint",
        )
        # Should NOT be rejected for missing reason
        assert "requires" not in result.get("reason", "").lower(), f"Unexpected rejection: {result}"


# ---------------------------------------------------------------------------
# Negative test: is_protected=False must not inject defaults
# ---------------------------------------------------------------------------


class TestUnprotectedNoDefaults:
    def test_unprotected_no_anchor_tag(self, monkeypatch, tmp_path):
        """is_protected=False must not add _anchor to tags."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Normal non-protected memory",
            context=str(tmp_path),
            tags=["debug"],
            is_protected=False,
        )

        update_calls = env["storage"].update_memory_fields.call_args_list
        all_tags = []
        for call in update_calls:
            tags = call.kwargs.get("tags")
            if tags is not None:
                all_tags.extend(tags)

        # _anchor must NOT be auto-added for non-protected memories
        # (it may appear if caller explicitly passed it, but it should not be auto-injected)
        # We passed tags=["debug"] and is_protected=False, so _anchor should not appear
        # unless DECISION_AUTO_PROTECT fired (but we set it False in mock settings)
        assert "_anchor" not in all_tags, (
            f"_anchor must not be auto-added for is_protected=False. tags={all_tags}"
        )

    def test_unprotected_no_tier_default(self, monkeypatch, tmp_path):
        """is_protected=False must not auto-set tier."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        insert_calls = []

        def _capture(memory, **kwargs):
            insert_calls.append(dict(memory))
            return 100

        env["storage"].insert_memory.side_effect = _capture

        memorize(
            content="Normal memory",
            context=str(tmp_path),
            tags=[],
            is_protected=False,
        )

        # tier should be None in insert payload (no auto-set)
        if insert_calls:
            assert insert_calls[0].get("tier") is None, (
                f"tier must not be auto-set for is_protected=False: {insert_calls[0]}"
            )


# ---------------------------------------------------------------------------
# memorize(is_protected=True) is row-equivalent to anchor()
# ---------------------------------------------------------------------------


class TestRowEquivalence:
    """Post-fix: memorize(is_protected=True) and anchor() should produce same row state."""

    def test_both_produce_anchor_tag(self, monkeypatch, tmp_path):
        """Both paths must result in _anchor in tags."""
        env = _build_memorize_sync_env(monkeypatch, tmp_path)
        import yadgar.core.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)

        from yadgar.core.server.tools.memorize import memorize

        memorize_tags_captured = []

        call_count = [0]

        def _capture_for_memorize(memory_id, **kwargs):
            if call_count[0] < 10 and kwargs.get("tags"):
                memorize_tags_captured.extend(kwargs["tags"])
            call_count[0] += 1

        env["storage"].update_memory_fields.side_effect = _capture_for_memorize

        memorize(
            content="Anchored decision",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            reason="important-decision",
        )

        assert "_anchor" in memorize_tags_captured, (
            f"memorize(is_protected=True) must result in _anchor tag. got: {memorize_tags_captured}"
        )
        assert "anchor:important-decision" in memorize_tags_captured, (
            f"anchor:reason tag missing from memorize result. got: {memorize_tags_captured}"
        )
