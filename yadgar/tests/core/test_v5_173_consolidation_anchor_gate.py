"""Car C7c (task #339) — consolidation emits ``_from_consolidation`` + anchor gate.

Word-salad auto-abstracted schemas ("Recurring pattern across 7 observations: …")
were being emitted by ``_promote_pattern`` and then auto-promoted to anchors
(``is_protected=True``, ``_anchor`` tag) by the memorize write pipeline. Once
anchored, they became zombie noise: protected from decay, immune to curation,
and ranking high on recall because of importance=1.0 — exactly the
phantom-namespace shape §1.4 forbids.

Three layered defenses:

  1. **Stamp at emit.** ``_promote_pattern`` writes ``_from_consolidation=True``
     on the row payload and adds ``_from_consolidation`` to the tag list. The
     tag is the load-bearing signal the other two layers read.
  2. **Suppress at promote.** ``abstract_to_schema`` returns ``None`` when the
     schema carries no identifier / ADR / file signal — the schema-level word
     salad is dropped before it can be inserted at all.
  3. **Carve out at anchor stamping.** ``_should_promote_to_anchor`` returns
     False when ``_from_consolidation`` is in tags, so even a schema that
     *does* survive layer 2 does not become an anchor.

The legacy audit pass (``audit_anchors``) gains a new
``consolidation_anchor_review`` field: any row that was anchored BEFORE this
car but happens to also carry ``_from_consolidation`` (i.e. new emissions that
retroactively get tagged) is surfaced for human review. Legacy rows without
the tag are untouched — the retro migration is a separate task.

Knob ``YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED`` (default true) gates the
audit surface; pass-through ``resolve_knob`` pattern shared with the rest of
the config-integrity surface (DRY with v5.95.0).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ──────────────────────────────────────────────────────────────────────────
# Step 1 + Step 2: _is_word_salad
# ──────────────────────────────────────────────────────────────────────────


class TestIsWordSalad:
    """``_is_word_salad`` classifies a schema string as a real abstraction or noise."""

    def test_plain_recurring_pattern_is_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        # The canonical word salad from the task body — no identifiers, no ADR,
        # no file refs, no tag suffix.
        assert (
            _is_word_salad("Recurring pattern across 5 observations: token token token token", [])
            is True
        )

    def test_salad_with_common_tags_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        # Same body, but common_tags carries a real tag signal.
        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: token token token token",
                ["file", "python"],
            )
            is False
        )

    def test_salad_with_backtick_identifier_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: wrapper around `foo.py`",
                [],
            )
            is False
        )

    def test_salad_with_adr_ref_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: rule from ADR-0420",
                [],
            )
            is False
        )

    def test_salad_with_issue_ref_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: applies to fix #1234",
                [],
            )
            is False
        )

    def test_salad_with_mr_ref_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: gate added in MR-42",
                [],
            )
            is False
        )

    def test_salad_with_md_file_ref_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: see README.md for details",
                [],
            )
            is False
        )

    def test_salad_with_snake_case_identifier_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: uses token_auth_handler",
                [],
            )
            is False
        )

    def test_salad_with_camelcase_identifier_is_not_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        assert (
            _is_word_salad(
                "Recurring pattern across 5 observations: applies to TokenAuthHandler",
                [],
            )
            is False
        )

    def test_short_salad_is_salad(self):
        from yadgar.backend.cls_store.patterns import _is_word_salad

        # The degenerate body fallback that abstract_to_schema emits when no
        # common words survive.
        assert _is_word_salad("Recurring pattern: the and for", []) is True


# ──────────────────────────────────────────────────────────────────────────
# Step 1: abstract_to_schema gates word salad to None
# ──────────────────────────────────────────────────────────────────────────


class TestAbstractToSchemaGating:
    """``abstract_to_schema`` returns None for word-salad inputs."""

    def test_word_salad_input_returns_none(self):
        from yadgar.backend.cls_store.patterns import _PatternsMixin

        mixin = _PatternsMixin.__new__(_PatternsMixin)
        # Cluster with no shared meaningful words → fallback short body, no tags.
        cluster = [
            {"id": 1, "content": "the and for", "tags": []},
            {"id": 2, "content": "the and for with that", "tags": []},
        ]
        assert mixin.abstract_to_schema(cluster) is None

    def test_signal_bearing_input_returns_string(self):
        from yadgar.backend.cls_store.patterns import _PatternsMixin

        mixin = _PatternsMixin.__new__(_PatternsMixin)
        # The cluster carries a backtick file ref → real signal.
        cluster = [
            {"id": 1, "content": "Edit `foo.py` for the auth flow", "tags": []},
            {"id": 2, "content": "Change `foo.py` to add retry", "tags": []},
            {"id": 3, "content": "Update `foo.py` with new error handler", "tags": []},
        ]
        schema = mixin.abstract_to_schema(cluster)
        assert isinstance(schema, str)
        assert schema.startswith("Recurring pattern")

    def test_tag_signal_returns_string(self):
        from yadgar.backend.cls_store.patterns import _PatternsMixin

        mixin = _PatternsMixin.__new__(_PatternsMixin)
        # Body has only stop-words → fallback path; tags carry the signal.
        # The gate must accept the schema when common_tags is non-empty.
        cluster = [
            {"id": 1, "content": "the and for bar baz", "tags": ["auth", "python"]},
            {"id": 2, "content": "the and for bar baz with qux", "tags": ["auth", "python"]},
        ]
        # The common words are bar, baz, qux (in 2/2 memories). They are
        # real, non-stop-word tokens, so meaningful is non-empty and the
        # full schema path is taken.
        schema = mixin.abstract_to_schema(cluster)
        assert isinstance(schema, str)
        assert "auth" in schema.lower() or "python" in schema.lower()


# ──────────────────────────────────────────────────────────────────────────
# Step 1: _promote_pattern stamps _from_consolidation
# ──────────────────────────────────────────────────────────────────────────


class TestPromotePatternStamping:
    """``_promote_pattern`` stamps ``_from_consolidation`` on the row payload."""

    def test_emitted_row_carries_flag_and_tag(self, monkeypatch):
        from yadgar.backend.cls_store import promotion
        from yadgar.backend.cls_store.patterns import _PatternsMixin

        captured: dict = {}

        class _FakeStorage:
            def insert_memory(self, row: dict) -> int:
                captured.update(row)
                return 999

            def update_memory_fields(self, memory_id, **fields):
                return None

            def get_entity_by_name(self, _name):
                return None

            def insert_entity(self, ent: dict) -> int:
                return ent.get("id", 1)

            def get_relationships_among_entities(self, _eids):
                return []

            def insert_relationship(self, _rel):
                return 1

            def reinforce_relationship(self, _rid):
                return None

            def search_vectors(self, *args, **kwargs):
                return []

        class _FakeEmbed:
            def encode(self, _text):
                return [0.0, 0.1, 0.2]

            def similarity(self, _a, _b):
                return 0.0

            def get_model_name(self):
                return "fake-model"

        class _FakeMixin(_PatternsMixin, promotion._PromotionMixin):
            _storage = _FakeStorage()
            _embeddings = _FakeEmbed()
            _settings = type(
                "S",
                (),
                {"CURATION_SIMILARITY_THRESHOLD": 0.9},
            )()

            def abstract_to_schema(self, _cluster):
                return "Recurring pattern across 3 observations: jwt auth `foo.py`"

            def _near_duplicate_semantic_exists(self, _emb):
                return False

        # Stub the cluster's project_id resolution + dominant_directory so
        # the promotion reaches the insert_memory call. Patch via the names
        # bound into promotion.py — patching the source function's __call__
        # is a no-op because it is a plain function, not an object with a
        # mutable __call__ attribute.
        monkeypatch.setattr(
            "yadgar.backend.cls_store.promotion.resolve_project_id_from_rows",
            lambda rows: "owner/repo",
        )
        monkeypatch.setattr(
            "yadgar.backend.cls_store.promotion.dominant_directory",
            lambda dirs: "/tmp/x",
        )

        m = _FakeMixin()
        result = m._promote_pattern({"memories": [{"id": 1}]})

        assert result is True
        assert captured.get("_from_consolidation") is True
        assert "_from_consolidation" in captured["tags"]
        assert "semantic" in captured["tags"]
        assert "auto-abstracted" in captured["tags"]


# ──────────────────────────────────────────────────────────────────────────
# Step 2: _should_promote_to_anchor carve-out
# ──────────────────────────────────────────────────────────────────────────


class TestShouldPromoteToAnchor:
    """``_should_promote_to_anchor`` returns False for ``_from_consolidation`` rows."""

    def test_consolidation_tag_blocks_anchor(self):
        from yadgar._shared.write_exec.context import MemorizeContext
        from yadgar._shared.write_exec.validate import _should_promote_to_anchor

        ctx = MemorizeContext(
            content="x" * 50,
            context=None,
            tags=["semantic", "auto-abstracted", "_from_consolidation"],
            is_protected=True,
            provenance_agent="default",
            tier="conditional",
            valid_until=None,
            ttl_days=None,
            reason="r",
        )
        # Force the auto-promote state: a memorize() that arrived with tier set
        # has is_protected=True and would normally be anchored. The carve-out
        # says: NOT for consolidation rows.
        assert _should_promote_to_anchor(ctx) is False

    def test_plain_auto_row_keeps_anchor(self):
        from yadgar._shared.write_exec.context import MemorizeContext
        from yadgar._shared.write_exec.validate import _should_promote_to_anchor

        ctx = MemorizeContext(
            content="x" * 50,
            context=None,
            tags=["semantic"],
            is_protected=True,
            provenance_agent="default",
            tier="conditional",
            valid_until=None,
            ttl_days=None,
            reason="r",
        )
        assert _should_promote_to_anchor(ctx) is True

    def test_unprotected_row_is_not_anchor(self):
        from yadgar._shared.write_exec.context import MemorizeContext
        from yadgar._shared.write_exec.validate import _should_promote_to_anchor

        ctx = MemorizeContext(
            content="x" * 50,
            context=None,
            tags=[],
            is_protected=False,
            provenance_agent="default",
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="",
        )
        # No protection signal → nothing to promote.
        assert _should_promote_to_anchor(ctx) is False


# ──────────────────────────────────────────────────────────────────────────
# Step 4: settings knob
# ──────────────────────────────────────────────────────────────────────────


class TestConsolidationAnchorAuditKnob:
    """The new knob follows the resolve_knob pattern."""

    def test_knob_default_true(self, monkeypatch):
        monkeypatch.delenv("YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED", raising=False)
        from yadgar._shared.config.config import _consolidation_anchor_audit_enabled

        assert _consolidation_anchor_audit_enabled() is True

    def test_knob_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED", "false")
        from yadgar._shared.config.config import _consolidation_anchor_audit_enabled

        assert _consolidation_anchor_audit_enabled() is False

    def test_knob_disabled_via_zero(self, monkeypatch):
        monkeypatch.setenv("YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED", "0")
        from yadgar._shared.config.config import _consolidation_anchor_audit_enabled

        assert _consolidation_anchor_audit_enabled() is False


# ──────────────────────────────────────────────────────────────────────────
# Step 5: audit_anchors surfaces consolidation-anchored rows for review
# ──────────────────────────────────────────────────────────────────────────


class TestAuditAnchorsConsolidationReview:
    """``audit_anchors`` returns ``consolidation_anchor_review`` when knob is on.

    Patches ``_scan_anchor_rows`` so the test exercises the carve-out and
    gating logic without standing up the full CLS pipeline.
    """

    @pytest.fixture
    def _engines(self, tmp_path):
        from yadgar.core import server

        server.init_engines(
            db_path=str(tmp_path / "consolidation_anchor_review.db"),
            embedding_model="all-MiniLM-L6-v2",
        )
        yield
        server.shutdown()

    @pytest.fixture
    def _rows(self):
        return [
            {
                "id": 11,
                "content": "Recurring pattern across 3 observations: jwt auth `foo.py`",
                "tags": ["_anchor", "_from_consolidation", "auto-abstracted"],
                "is_protected": True,
                "heat": 0.8,
                "last_seen_at": "2026-08-01T00:00:00Z",
            },
            {
                "id": 22,
                "content": "Legitimate anchor without consolidation tag",
                "tags": ["_anchor"],
                "is_protected": True,
                "heat": 1.0,
                "last_seen_at": "2026-08-02T00:00:00Z",
            },
        ]

    def test_knob_on_returns_review_list(self, monkeypatch, _engines, _rows):
        monkeypatch.setenv("YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED", "true")
        from yadgar.core.server.tools import audit as audit_mod

        with patch.object(audit_mod, "_scan_anchor_rows", return_value=list(_rows)):
            result = audit_mod.audit_anchors(directory="/tmp/x", dry_run=True)

        assert "consolidation_anchor_review" in result
        review = result["consolidation_anchor_review"]
        assert len(review) == 1
        assert review[0]["memory_id"] == 11
        assert "_from_consolidation" in review[0]["tags"]
        assert review[0]["is_protected"] is True
        # Content preview capped at 200 chars
        assert len(review[0]["content_preview"]) <= 200

    def test_knob_off_returns_empty_review(self, monkeypatch, _engines, _rows):
        monkeypatch.setenv("YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED", "false")
        from yadgar.core.server.tools import audit as audit_mod

        with patch.object(audit_mod, "_scan_anchor_rows", return_value=list(_rows)):
            result = audit_mod.audit_anchors(directory="/tmp/x", dry_run=True)

        assert result.get("consolidation_anchor_review") == []

    def test_legacy_anchor_untouched(self, monkeypatch, _engines, _rows):
        """A row with _anchor but no _from_consolidation is NOT flagged."""
        monkeypatch.setenv("YADGAR_CONSOLIDATION_ANCHOR_AUDIT_ENABLED", "true")
        from yadgar.core.server.tools import audit as audit_mod

        with patch.object(audit_mod, "_scan_anchor_rows", return_value=list(_rows)):
            result = audit_mod.audit_anchors(directory="/tmp/x", dry_run=True)

        review_ids = {r["memory_id"] for r in result["consolidation_anchor_review"]}
        assert 22 not in review_ids, "legacy anchors must not appear in review list"
