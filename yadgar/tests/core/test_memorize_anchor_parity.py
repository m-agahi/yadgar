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

R3 Car 1 (write-half): memorize() no longer has an in-process sync write path —
it validates at the MCP boundary (phase_validate: tier parity, tag injection,
TTL computation, rejections) and then ALWAYS enqueues; the write pipeline runs
only in the backend drainer. The parity contract is therefore observable in the
enqueued payload (tags / tier / valid_until) and in the synchronous rejection
dicts — these tests assert the same contracts at that boundary seam instead of
on mocked storage calls.
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


def _build_memorize_boundary_env(monkeypatch, tmp_path):
    """Set up the MCP-boundary environment for memorize tests (R3 Car 1).

    memorize() always enqueues now — patch a capturing fake file queue and a
    deterministic branch so the boundary path runs to the enqueue seam. Returns
    {"payloads": [...], "settings": mock_settings}; each accepted memorize()
    call appends its enqueued payload dict to "payloads".
    """
    import importlib

    import yadgar._shared.runtime.state as _st

    _mem_mod = importlib.import_module("yadgar.core.server.tools.memorize")

    captured_payloads: list[dict] = []

    def _capture_enqueue(op_type: str, payload: dict) -> str:
        assert op_type == "memorize"
        captured_payloads.append(dict(payload))
        return "queue-id-parity-test"

    fake_queue = MagicMock()
    fake_queue.enqueue.side_effect = _capture_enqueue
    monkeypatch.setattr(_mem_mod, "_get_file_queue", lambda: fake_queue)

    monkeypatch.setattr(_st, "_rules_engine", None)

    mock_settings = _make_mock_settings()
    monkeypatch.setattr(_mem_mod, "settings", mock_settings)

    return {"payloads": captured_payloads, "settings": mock_settings}


# ---------------------------------------------------------------------------
# Auto-set tier="conditional" when is_protected=True and tier=None
# ---------------------------------------------------------------------------


class TestProtectedAutoTier:
    def test_auto_sets_conditional_tier(self, monkeypatch, tmp_path):
        """memorize(is_protected=True) with no tier → tier auto-set to 'conditional'."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Critical architecture decision",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
        )

        # The enqueued payload must carry tier=conditional
        assert env["payloads"], "memorize must have enqueued a payload"
        assert env["payloads"][0].get("tier") == "conditional", (
            f"Expected tier=conditional in enqueued payload, got: {env['payloads'][0]}"
        )

    def test_provided_tier_respected(self, monkeypatch, tmp_path):
        """When tier='ephemeral' is given, it must not be overridden."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Ephemeral working note",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            tier="ephemeral",
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        payload_tier = env["payloads"][0].get("tier")
        # ephemeral must be present, conditional must NOT override it
        assert payload_tier == "ephemeral", (
            f"Expected tier=ephemeral (explicit tier must not be overridden), got: {payload_tier}"
        )


# ---------------------------------------------------------------------------
# Auto-prepend _anchor to tags
# ---------------------------------------------------------------------------


class TestProtectedAutoAnchorTag:
    def test_auto_prepends_anchor_tag(self, monkeypatch, tmp_path):
        """memorize(is_protected=True) must add _anchor to tags."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Important decision",
            context=str(tmp_path),
            tags=["custom-tag"],
            is_protected=True,
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        all_tags = env["payloads"][0].get("tags", [])
        assert "_anchor" in all_tags, (
            f"_anchor must be auto-added to tags. payload tags: {all_tags}"
        )

    def test_anchor_tag_not_duplicated(self, monkeypatch, tmp_path):
        """If _anchor already in tags, it must not be duplicated."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Already anchored",
            context=str(tmp_path),
            tags=["_anchor"],
            is_protected=True,
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        tag_list = env["payloads"][0].get("tags", [])
        assert tag_list.count("_anchor") <= 1, f"_anchor duplicated in {tag_list}"


# ---------------------------------------------------------------------------
# reason kwarg → anchor:{reason} tag
# ---------------------------------------------------------------------------


class TestProtectedReasonTag:
    def test_reason_adds_anchor_colon_tag(self, monkeypatch, tmp_path):
        """memorize(is_protected=True, reason='X') must add 'anchor:X' to tags."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Schema choice",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            reason="schema-decision",
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        all_tags = env["payloads"][0].get("tags", [])
        assert "anchor:schema-decision" in all_tags, f"anchor:reason tag not found. tags={all_tags}"

    def test_empty_reason_no_colon_tag(self, monkeypatch, tmp_path):
        """Empty reason must not add 'anchor:' tag."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Anchor without reason",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            reason="",
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        all_tags = env["payloads"][0].get("tags", [])
        colon_tags = [t for t in all_tags if t.startswith("anchor:")]
        assert len(colon_tags) == 0, f"anchor: tag added for empty reason: {colon_tags}"


# ---------------------------------------------------------------------------
# valid_until computation from tier
# ---------------------------------------------------------------------------


class TestProtectedTTLComputation:
    def test_conditional_tier_gets_90d_ttl(self, monkeypatch, tmp_path):
        """conditional tier → valid_until = now + 90d."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        before = datetime.now(UTC)
        memorize(
            content="Conditional anchor",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            # No tier given — should auto-default to conditional
        )
        after = datetime.now(UTC)

        assert env["payloads"], "memorize must have enqueued a payload"
        valid_until = env["payloads"][0].get("valid_until")
        assert valid_until, f"valid_until missing from payload: {env['payloads'][0]}"
        vu = datetime.fromisoformat(valid_until)
        expected_min = before + timedelta(days=89)
        expected_max = after + timedelta(days=91)
        assert expected_min < vu < expected_max, (
            f"valid_until {vu} not in 90d window [{expected_min}, {expected_max}]"
        )

    def test_ephemeral_tier_gets_14d_ttl(self, monkeypatch, tmp_path):
        """ephemeral tier → valid_until = now + 14d."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        before = datetime.now(UTC)
        memorize(
            content="Ephemeral anchor",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            tier="ephemeral",
        )
        after = datetime.now(UTC)

        assert env["payloads"], "memorize must have enqueued a payload"
        valid_until = env["payloads"][0].get("valid_until")
        assert valid_until, f"valid_until missing from payload: {env['payloads'][0]}"
        vu = datetime.fromisoformat(valid_until)
        expected_min = before + timedelta(days=13)
        expected_max = after + timedelta(days=15)
        assert expected_min < vu < expected_max, f"valid_until {vu} not in 14d window"


# ---------------------------------------------------------------------------
# semantic_immortal requires reason
# ---------------------------------------------------------------------------


class TestSemanticImmortalRequiresReason:
    def test_semantic_immortal_without_reason_rejected(self, monkeypatch, tmp_path):
        """semantic_immortal without reason must be rejected when flag is true."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
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
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
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
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Normal non-protected memory",
            context=str(tmp_path),
            tags=["debug"],
            is_protected=False,
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        all_tags = env["payloads"][0].get("tags", [])
        # _anchor must NOT be auto-added for non-protected memories
        # We passed tags=["debug"] and is_protected=False, so _anchor should not
        # appear unless DECISION_AUTO_PROTECT fired (set False in mock settings)
        assert "_anchor" not in all_tags, (
            f"_anchor must not be auto-added for is_protected=False. tags={all_tags}"
        )

    def test_unprotected_no_tier_default(self, monkeypatch, tmp_path):
        """is_protected=False must not auto-set tier."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Normal memory",
            context=str(tmp_path),
            tags=[],
            is_protected=False,
        )

        # tier should be absent from the payload (memorize only sets the key
        # when a tier was resolved — no auto-set for unprotected memories)
        assert env["payloads"], "memorize must have enqueued a payload"
        assert env["payloads"][0].get("tier") is None, (
            f"tier must not be auto-set for is_protected=False: {env['payloads'][0]}"
        )


# ---------------------------------------------------------------------------
# memorize(is_protected=True) is row-equivalent to anchor()
# ---------------------------------------------------------------------------


class TestRowEquivalence:
    """Post-fix: memorize(is_protected=True) and anchor() should produce same row state."""

    def test_both_produce_anchor_tag(self, monkeypatch, tmp_path):
        """Both paths must result in _anchor in tags."""
        env = _build_memorize_boundary_env(monkeypatch, tmp_path)
        from yadgar.core.server.tools.memorize import memorize

        memorize(
            content="Anchored decision",
            context=str(tmp_path),
            tags=[],
            is_protected=True,
            reason="important-decision",
        )

        assert env["payloads"], "memorize must have enqueued a payload"
        memorize_tags_captured = env["payloads"][0].get("tags", [])
        assert "_anchor" in memorize_tags_captured, (
            f"memorize(is_protected=True) must result in _anchor tag. got: {memorize_tags_captured}"
        )
        assert "anchor:important-decision" in memorize_tags_captured, (
            f"anchor:reason tag missing from memorize result. got: {memorize_tags_captured}"
        )
