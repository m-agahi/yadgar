"""Tests for surprise-gate SHADOW mode (v5.73.0).

Shadow mode stamps every stored memory with:
  - surprise_score  = the WRITE GATE's surprisal (from should_store())
  - would_reject    = what the gate would decide at WRITE_GATE_SHADOW_THRESHOLD

CRITICAL distinction:
  - ctx.surprise        = thermo.compute_surprise()  → heat boost only
  - ctx.gate_surprisal  = surprisal from _write_gate.should_store()  → this is what we stamp

Assertion checklist
  A. surprise_score on stored memory == gate's surprisal (NOT thermo ctx.surprise)
  B. would_reject matches gate decision at WRITE_GATE_SHADOW_THRESHOLD
  C. would_reject=True memory is still stored (WRITE_GATE_THRESHOLD=0.0, nothing dropped)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(
    content: str = "test content",
    context: str = "/some/dir",
    tags: list[str] | None = None,
    surprise: float = 0.42,  # thermo surprise — intentionally different from gate
    gate_surprisal: float | None = None,
    would_reject: bool | None = None,
):
    """Build a minimal MemorizeContext for phase_store tests."""
    from yadgar._shared.write_exec.context import MemorizeContext

    ctx = MemorizeContext(
        content=content,
        context=context,
        tags=tags or [],
        is_protected=False,
        provenance_agent=None,
        tier=None,
        valid_until=None,
        ttl_days=None,
        reason="test",
    )
    ctx.embedding = [0.1] * 4
    ctx.surprise = surprise  # thermo — used for heat boost, NOT for shadow stamp
    ctx.gate_surprisal = gate_surprisal
    ctx.would_reject = would_reject
    ctx.importance = 0.5
    ctx.valence = 0.0
    ctx.initial_heat = 1.0
    return ctx


# ── A. gate_surprisal on MemorizeContext ─────────────────────────────────────


class TestContextField:
    """MemorizeContext must have gate_surprisal and would_reject fields."""

    def test_gate_surprisal_field_exists(self):
        from yadgar._shared.write_exec.context import MemorizeContext

        ctx = MemorizeContext(
            content="x",
            context="/dir",
            tags=[],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="r",
        )
        # Field must exist with a sane default
        assert hasattr(ctx, "gate_surprisal")

    def test_would_reject_field_exists(self):
        from yadgar._shared.write_exec.context import MemorizeContext

        ctx = MemorizeContext(
            content="x",
            context="/dir",
            tags=[],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="r",
        )
        assert hasattr(ctx, "would_reject")

    def test_gate_surprisal_distinct_from_thermo_surprise(self):
        """gate_surprisal and surprise are separate fields — they must be independently settable."""
        from yadgar._shared.write_exec.context import MemorizeContext

        ctx = MemorizeContext(
            content="x",
            context="/dir",
            tags=[],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="r",
        )
        ctx.surprise = 0.9
        ctx.gate_surprisal = 0.3
        assert ctx.surprise == 0.9
        assert ctx.gate_surprisal == 0.3
        assert ctx.surprise != ctx.gate_surprisal


# ── B. phase_embed captures gate surprisal ───────────────────────────────────


class TestPhaseEmbedCapturesGateSurprisal:
    """phase_embed must copy the GATE's surprisal (not thermo's) into ctx.gate_surprisal."""

    def test_gate_surprisal_captured_from_should_store(self):
        """ctx.gate_surprisal == surprisal returned by _write_gate.should_store()."""
        from yadgar._shared.write_exec.context import MemorizeContext

        fake_surprisal = 0.71  # gate value

        mock_gate = MagicMock()
        mock_gate.should_store.return_value = (True, fake_surprisal, "high_surprisal")

        mock_embeddings = MagicMock()
        mock_embeddings.encode.return_value = [0.1] * 4

        mock_thermo = MagicMock()
        mock_thermo.compute_surprise.return_value = 0.22  # deliberately different
        mock_thermo.compute_importance.return_value = 0.5
        mock_thermo.compute_valence.return_value = 0.0
        mock_thermo.apply_surprise_boost.return_value = 1.0

        mock_settings = MagicMock()
        mock_settings.CONTEXTUAL_PREFIX_ENABLED = False
        mock_settings.WRITE_GATE_SHADOW_THRESHOLD = 0.15

        ctx = MemorizeContext(
            content="architectural decision",
            context="/repo",
            tags=[],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="r",
        )

        with (
            patch("yadgar.backend.write_exec._memorize_phases._phase_embed._st") as mock_st,
            patch("yadgar.backend.write_exec._memorize_phases._phase_embed._lifecycle") as mock_lc,
        ):
            mock_st._write_gate = mock_gate
            mock_st._retriever = None
            mock_st._thermo = mock_thermo
            mock_lc._get_embeddings.return_value = mock_embeddings

            from yadgar.backend.write_exec._memorize_phases._phase_embed import phase_embed

            result = phase_embed(ctx, mock_settings)

        assert result is None, "Gate allowed — should not reject"
        # CRITICAL: gate_surprisal must be the gate's value, not thermo's
        assert ctx.gate_surprisal == fake_surprisal, (
            f"Expected gate_surprisal={fake_surprisal!r} (gate's value), "
            f"got {ctx.gate_surprisal!r}. "
            "thermo ctx.surprise={ctx.surprise!r} must NOT be used."
        )
        # thermo surprise unchanged
        assert ctx.surprise == 0.22

    def test_gate_surprisal_none_when_gate_disabled(self):
        """When _write_gate is None (disabled), gate_surprisal stays at its default."""
        from yadgar._shared.write_exec.context import MemorizeContext

        mock_embeddings = MagicMock()
        mock_embeddings.encode.return_value = [0.1] * 4

        mock_thermo = MagicMock()
        mock_thermo.compute_surprise.return_value = 0.5
        mock_thermo.compute_importance.return_value = 0.5
        mock_thermo.compute_valence.return_value = 0.0
        mock_thermo.apply_surprise_boost.return_value = 1.0

        mock_settings = MagicMock()
        mock_settings.CONTEXTUAL_PREFIX_ENABLED = False
        mock_settings.WRITE_GATE_SHADOW_THRESHOLD = 0.15

        ctx = MemorizeContext(
            content="content",
            context="/dir",
            tags=[],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="r",
        )

        with (
            patch("yadgar.backend.write_exec._memorize_phases._phase_embed._st") as mock_st,
            patch("yadgar.backend.write_exec._memorize_phases._phase_embed._lifecycle") as mock_lc,
        ):
            mock_st._write_gate = None  # gate disabled
            mock_st._retriever = None
            mock_st._thermo = mock_thermo
            mock_lc._get_embeddings.return_value = mock_embeddings

            from yadgar.backend.write_exec._memorize_phases._phase_embed import phase_embed

            result = phase_embed(ctx, mock_settings)

        assert result is None
        # When gate is None, gate_surprisal should be None (default)
        assert ctx.gate_surprisal is None


# ── B. would_reject computed faithfully ──────────────────────────────────────


class TestWouldReject:
    """would_reject uses the gate's own would_reject_at() method (faithful to adaptive logic)."""

    def test_would_reject_true_when_below_shadow_threshold(self):
        """Low surprisal → would_reject=True at shadow threshold."""
        from yadgar.backend.predictive_coding import WriteGate

        # Verify WriteGate has would_reject_at method
        assert hasattr(WriteGate, "would_reject_at"), (
            "WriteGate must have would_reject_at(content, directory, tags, threshold) method"
        )

        mock_storage = MagicMock()
        mock_embeddings = MagicMock()
        mock_retriever = MagicMock()
        mock_settings = MagicMock()
        mock_settings.WRITE_GATE_THRESHOLD = 0.0
        mock_settings.WRITE_GATE_CONTINUITY_WINDOW = 10
        mock_settings.PREDICTIVE_CODING_ENTITY_TTL_SECONDS = 0
        mock_settings.WRITE_GATE_CONTINUITY_DISCOUNT = 0.15

        gate = WriteGate(mock_storage, mock_embeddings, mock_retriever, mock_settings)

        # Patch compute_surprisal to return a known low value
        with patch.object(gate, "compute_surprisal", return_value=0.05):
            # 0.05 < 0.15 shadow threshold → would_reject=True
            result = gate.would_reject_at("low surprise content", "/dir", [], 0.15)
        assert result is True

    def test_would_reject_false_when_above_shadow_threshold(self):
        """High surprisal → would_reject=False at shadow threshold."""
        from yadgar.backend.predictive_coding import WriteGate

        mock_storage = MagicMock()
        mock_embeddings = MagicMock()
        mock_retriever = MagicMock()
        mock_settings = MagicMock()
        mock_settings.WRITE_GATE_THRESHOLD = 0.0
        mock_settings.WRITE_GATE_CONTINUITY_WINDOW = 10
        mock_settings.PREDICTIVE_CODING_ENTITY_TTL_SECONDS = 0
        mock_settings.WRITE_GATE_CONTINUITY_DISCOUNT = 0.15

        gate = WriteGate(mock_storage, mock_embeddings, mock_retriever, mock_settings)

        with patch.object(gate, "compute_surprisal", return_value=0.80):
            # 0.80 >= 0.15 → would_reject=False
            result = gate.would_reject_at("novel architectural decision", "/dir", [], 0.15)
        assert result is False

    def test_would_reject_uses_adaptive_continuity(self):
        """would_reject_at uses the adaptive (continuity-adjusted) threshold, not the raw one.

        Continuity discount means the effective threshold can be lower than the shadow base.
        At very high continuity, effective_threshold = max(0.1, 0.15 - 0.15) = 0.10.
        surprisal=0.12 >= effective_threshold=0.10 → would_reject=False even though 0.12 < 0.15.
        """
        from yadgar.backend.predictive_coding import WriteGate

        mock_storage = MagicMock()
        mock_embeddings = MagicMock()
        mock_retriever = MagicMock()
        mock_settings = MagicMock()
        mock_settings.WRITE_GATE_THRESHOLD = 0.0
        mock_settings.WRITE_GATE_CONTINUITY_WINDOW = 10
        mock_settings.PREDICTIVE_CODING_ENTITY_TTL_SECONDS = 0
        mock_settings.WRITE_GATE_CONTINUITY_DISCOUNT = 0.15

        gate = WriteGate(mock_storage, mock_embeddings, mock_retriever, mock_settings)

        with (
            patch.object(gate, "compute_surprisal", return_value=0.12),
            patch.object(gate, "_compute_task_continuity", return_value=1.0),
        ):
            # discount=1.0*0.15=0.15; effective=max(0.1, 0.15-0.15)=0.10
            # 0.12 >= 0.10 → NOT rejected
            result = gate.would_reject_at("incremental content", "/dir", [], 0.15)
        assert result is False, (
            "Adaptive threshold: high continuity lowers effective threshold below base. "
            "0.12 >= 0.10 (effective) → not rejected despite 0.12 < 0.15 (base)"
        )


# ── C. stored memory carries both fields ─────────────────────────────────────


class TestShadowFieldsStoredOnMemory:
    """After phase_store(), memory row must carry surprise_score (gate) + would_reject."""

    def _make_storage_mock(self, memory_id: int = 42):
        storage = MagicMock()
        storage.update_memory_fields = MagicMock()
        storage.upsert_file_hash = MagicMock()
        storage.get_memory.return_value = None  # astrocyte assign
        return storage

    def test_gate_surprisal_stamped_as_surprise_score(self):
        """After phase_store, update_memory_fields called with surprise_score=gate_surprisal."""

        gate_surprisal = 0.63
        thermo_surprise = 0.11  # different from gate — must NOT be used

        ctx = _make_ctx(surprise=thermo_surprise, gate_surprisal=gate_surprisal, would_reject=False)
        ctx.memory_id = 99
        storage = self._make_storage_mock(99)

        with (
            patch("yadgar.backend.write_exec._memorize_phases._phase_store._st") as mock_st,
            patch("yadgar.backend.write_exec._memorize_phases._phase_store._lifecycle") as mock_lc,
            patch(
                "yadgar.backend.write_exec._memorize_phases._phase_store._file_hash",
                return_value=None,
            ),
        ):
            mock_st._curator = None  # use direct path
            mock_st._consolidation = None
            mock_st._pool = None
            mock_lc._get_storage.return_value = storage
            mock_lc._get_embeddings.return_value = MagicMock()
            mock_lc._get_buffer.return_value = MagicMock()

            # Patch _direct_insert to return a known memory_id
            with patch(
                "yadgar.backend.write_exec._memorize_phases._phase_store._direct_insert",
                return_value=99,
            ):
                from yadgar.backend.write_exec._memorize_phases._phase_store import phase_store

                phase_store(ctx)

        # Collect all update_memory_fields calls
        all_calls = storage.update_memory_fields.call_args_list
        # Find the shadow stamp call
        shadow_calls = [
            c
            for c in all_calls
            if "surprise_score" in c.kwargs or (len(c.args) > 1 and "surprise_score" in c.args[1])
        ]
        assert shadow_calls, (
            "phase_store must call update_memory_fields with surprise_score after write. "
            f"All calls: {all_calls}"
        )
        # Extract the kwargs from the shadow stamp call
        for call in shadow_calls:
            kwargs = call.kwargs
            if "surprise_score" in kwargs:
                assert kwargs["surprise_score"] == gate_surprisal, (
                    f"surprise_score must be gate_surprisal={gate_surprisal}, "
                    f"not thermo ctx.surprise={thermo_surprise}. Got {kwargs['surprise_score']}"
                )

    def test_would_reject_stamped_on_memory(self):
        """would_reject field is stamped on the memory row after write."""
        ctx = _make_ctx(gate_surprisal=0.08, would_reject=True)
        ctx.memory_id = 77
        storage = self._make_storage_mock(77)

        with (
            patch("yadgar.backend.write_exec._memorize_phases._phase_store._st") as mock_st,
            patch("yadgar.backend.write_exec._memorize_phases._phase_store._lifecycle") as mock_lc,
            patch(
                "yadgar.backend.write_exec._memorize_phases._phase_store._file_hash",
                return_value=None,
            ),
        ):
            mock_st._curator = None
            mock_st._consolidation = None
            mock_st._pool = None
            mock_lc._get_storage.return_value = storage
            mock_lc._get_embeddings.return_value = MagicMock()
            mock_lc._get_buffer.return_value = MagicMock()

            with patch(
                "yadgar.backend.write_exec._memorize_phases._phase_store._direct_insert",
                return_value=77,
            ):
                from yadgar.backend.write_exec._memorize_phases._phase_store import phase_store

                phase_store(ctx)

        all_calls = storage.update_memory_fields.call_args_list
        would_reject_calls = [
            c
            for c in all_calls
            if "would_reject" in c.kwargs or (len(c.args) > 1 and "would_reject" in c.args)
        ]
        assert would_reject_calls, (
            f"phase_store must call update_memory_fields with would_reject. All calls: {all_calls}"
        )

    def test_would_reject_true_memory_still_stored(self):
        """WRITE_GATE_THRESHOLD=0.0: a would_reject=True memory MUST be stored.

        This is the core shadow invariant — we stamp but never drop.
        """
        ctx = _make_ctx(gate_surprisal=0.03, would_reject=True)
        ctx.memory_id = 55
        storage = self._make_storage_mock(55)

        with (
            patch("yadgar.backend.write_exec._memorize_phases._phase_store._st") as mock_st,
            patch("yadgar.backend.write_exec._memorize_phases._phase_store._lifecycle") as mock_lc,
            patch(
                "yadgar.backend.write_exec._memorize_phases._phase_store._file_hash",
                return_value=None,
            ),
        ):
            mock_st._curator = None
            mock_st._consolidation = None
            mock_st._pool = None
            mock_lc._get_storage.return_value = storage
            mock_lc._get_embeddings.return_value = MagicMock()
            mock_lc._get_buffer.return_value = MagicMock()

            with patch(
                "yadgar.backend.write_exec._memorize_phases._phase_store._direct_insert",
                return_value=55,
            ) as mock_insert:
                from yadgar.backend.write_exec._memorize_phases._phase_store import phase_store

                phase_store(ctx)
                # Direct insert MUST have been called — memory was stored
                mock_insert.assert_called_once()

        assert ctx.memory_id == 55


# ── D. config knob registered ────────────────────────────────────────────────


class TestShadowGateConfig:
    """WRITE_GATE_SHADOW_THRESHOLD must exist in Settings and be I25-registered."""

    def test_shadow_threshold_in_settings(self):
        from yadgar._shared.config import Settings

        s = Settings()
        assert hasattr(s, "WRITE_GATE_SHADOW_THRESHOLD"), (
            "Settings must have WRITE_GATE_SHADOW_THRESHOLD"
        )
        assert isinstance(s.WRITE_GATE_SHADOW_THRESHOLD, float)
        assert s.WRITE_GATE_SHADOW_THRESHOLD == 0.15

    def test_shadow_threshold_in_registry(self):
        from yadgar._shared.config.config_registry import _REGISTRY

        names = {e.name for e in _REGISTRY}
        assert "YADGAR_WRITE_GATE_SHADOW_THRESHOLD" in names, (
            "YADGAR_WRITE_GATE_SHADOW_THRESHOLD must be in config_registry._REGISTRY"
        )

    def test_shadow_threshold_in_field_meta(self):
        from yadgar._shared.config.config_yaml import FIELD_META

        assert "write_gate_shadow_threshold" in FIELD_META, (
            "write_gate_shadow_threshold must be in FIELD_META"
        )

    def test_would_reject_in_updatable_fields(self):
        from yadgar._shared.storage.client import _MEMORY_UPDATABLE_FIELDS

        assert "would_reject" in _MEMORY_UPDATABLE_FIELDS, (
            "would_reject must be in _MEMORY_UPDATABLE_FIELDS"
        )


# ── E. migration registered ──────────────────────────────────────────────────


class TestShadowGateMigration:
    """Migration 022 must exist and add would_reject to memory."""

    def test_migration_022_in_migrations_list(self):
        from yadgar._shared.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        matching = [v for v in versions if v.startswith("022")]
        assert matching, f"Migration 022_* must be in _MIGRATIONS. Found versions: {versions}"

    def test_migration_022_function_exists(self):
        from yadgar._shared.storage import migrations as m_mod

        # Check the migration function is importable
        fn_names = [m["fn"].__name__ for m in m_mod._MIGRATIONS if m["version"].startswith("022")]
        assert fn_names, "Migration 022 function must be registered in _MIGRATIONS"
