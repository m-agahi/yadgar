"""recall() output-size cap — denylist projection + content cap + byte backstop.

Car `fix/recall-output-size-cap` (task:0085). `recall()` was a pure forwarder
with zero size bound (`tools/recall.py:273` returned the raw backend rows), so a
single unlucky call could emit ~78 KB and blow the harness tool-output cap —
pushing agents off the memory system and back to grep.

Three mechanisms under test:
  1. denylist field projection (memory rows are metadata-heavy: 38.8% of a real
     row was scoring/thermodynamic internals no caller reads);
  2. per-row content cap with a VISIBLE `_truncated` marker carrying an exact-ID
     recovery path (wiki rows are content-heavy — only a content cap touches them);
  3. a total-byte backstop that drops low-ranked rows behind one `_dropped`
     marker, mirroring the Cowan overflow shape at `cognitive_load.py:142`.

The load-bearing invariant is the deferred side-effect closure
(`recall.py:269-272`): `_apply_recall_session_side_effects` MUST keep receiving
the UNTRIMMED rows (SR transitions and the action buffer read fields the
denylist removes). Note that `test_recall_pipeline_unit.py:325` cannot catch a
violation of this — it compares by `==`, so both a shaped-but-equal list and an
in-place-mutated list pass. The tests here are deliberately non-vacuous: the
fixture row carries BOTH a denylisted field AND over-cap content.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest


def _compact(obj) -> int:
    """Serialised byte size, matching the shaper's own accounting."""
    return len(json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8"))


# ═══════════════════════════════════════════════════════════════════════════
# 1. Denylist projection
# ═══════════════════════════════════════════════════════════════════════════


class TestDenylistProjection:
    def test_denylisted_fields_removed_survivors_kept(self):
        """Projection drops exactly the listed internals; payload fields survive."""
        from yadgar.core.server.tools.recall import (
            _RECALL_PROJECTION_DENYLIST,
            _shape_recall_results,
        )

        row = {
            "id": 529839,
            "content": "short content",
            "tags": ["semantic"],
            "directory_context": "/home/max/git/yadgar",
            "heat": 1.0,
            "_source": "memory",
            # denylisted internals
            "contextual_prefix": "[Project: yadgar] [Tags: semantic] ",
            "sr_x": 0.0,
            "sr_y": 0.0,
            "plasticity": 1.0,
            "vector_clock": "{}",
            "original_content": "the full pre-compression text " * 20,
            "_rerank_score": 0.075,
            "_chunk_id": 0,
        }

        out = _shape_recall_results([row], max_chars=1200, max_bytes=65536)
        assert len(out) == 1
        shaped = out[0]

        # Every denylisted key present on input is gone.
        for key in row:
            if key in _RECALL_PROJECTION_DENYLIST:
                assert key not in shaped, f"denylisted field {key!r} survived projection"

        # Payload fields survive untouched.
        for key in ("id", "content", "tags", "directory_context", "heat", "_source"):
            assert shaped[key] == row[key], f"{key!r} must survive projection unchanged"

    def test_projection_is_denylist_not_allowlist_unknown_fields_survive(self):
        """A field the retrieval pipeline adds later must default to VISIBLE."""
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {"id": 1, "content": "c", "_source": "memory", "some_future_field": "keep me"}
        shaped = _shape_recall_results([row], max_chars=1200, max_bytes=65536)[0]
        assert shaped["some_future_field"] == "keep me"

    def test_landscape_mode_consensus_fields_retained(self):
        """mode="landscape" stamps consensus_score/voting_domains — the allowlist trap.

        `astrocyte_pool.py:330-331` writes these per row and they are a documented
        part of the return contract (`recall.py:166-168,177-178`). An allowlist
        projection would have silently deleted them.
        """
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {
            "id": 7,
            "content": "landscape row",
            "_source": "memory",
            "consensus_score": 0.83,
            "voting_domains": ["infra", "retrieval"],
        }
        shaped = _shape_recall_results([row], max_chars=1200, max_bytes=65536)[0]
        assert shaped["consensus_score"] == 0.83
        assert shaped["voting_domains"] == ["infra", "retrieval"]

    def test_input_rows_are_not_mutated(self):
        """Shaping must build new dicts — the closure reads the originals."""
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {
            "id": 1,
            "content": "x" * 5000,
            "contextual_prefix": "[Project: yadgar] ",
            "_source": "memory",
        }
        before = json.dumps(row, sort_keys=True, default=str)
        _shape_recall_results([row], max_chars=100, max_bytes=65536)
        assert json.dumps(row, sort_keys=True, default=str) == before, (
            "_shape_recall_results mutated its input rows"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Per-row content cap + visible marker
# ═══════════════════════════════════════════════════════════════════════════


class TestContentCap:
    def test_row_under_cap_has_no_truncated_marker(self):
        """Small results stay fully transparent — byte-identical modulo denylist."""
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {"id": 1, "content": "well under the cap", "_source": "memory"}
        shaped = _shape_recall_results([row], max_chars=1200, max_bytes=65536)[0]
        assert "_truncated" not in shaped
        assert shaped == row

    def test_row_over_cap_truncated_with_counts_and_memory_fetch_hint(self):
        from yadgar.core.server.tools.recall import _shape_recall_results

        content = "y" * 2515
        row = {"id": 529839, "content": content, "_source": "memory"}
        shaped = _shape_recall_results([row], max_chars=1200, max_bytes=65536)[0]

        assert shaped["content"] == content[:1200]
        assert shaped["_truncated"] == {
            "kept": 1200,
            "total": 2515,
            "fetch": "memory_get(529839)",
        }

    def test_wiki_row_fetch_hint_uses_slug_not_id(self):
        """Wiki rows carry BOTH slug and id — branch on _source, not key presence."""
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {
            "id": 6899,
            "slug": "yadgar-adr-0035",
            "content": "z" * 4000,
            "_source": "wiki",
        }
        shaped = _shape_recall_results([row], max_chars=1200, max_bytes=65536)[0]
        assert shaped["_truncated"]["fetch"] == 'wiki_read("yadgar-adr-0035")'

    def test_missing_source_does_not_raise(self):
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {"id": 5, "content": "q" * 3000}
        shaped = _shape_recall_results([row], max_chars=1200, max_bytes=65536)[0]
        assert shaped["_truncated"]["fetch"] == "memory_get(5)"

    def test_non_string_content_is_left_alone(self):
        from yadgar.core.server.tools.recall import _shape_recall_results

        row = {"id": 5, "content": None, "_source": "memory"}
        shaped = _shape_recall_results([row], max_chars=10, max_bytes=65536)[0]
        assert shaped["content"] is None
        assert "_truncated" not in shaped


# ═══════════════════════════════════════════════════════════════════════════
# 3. Total-byte backstop
# ═══════════════════════════════════════════════════════════════════════════


class TestByteBackstop:
    def test_drops_lowest_ranked_rows_and_appends_one_marker(self):
        """Rank order is preserved; the tail is dropped behind a single marker."""
        from yadgar.core.server.tools.recall import _shape_recall_results

        rows = [{"id": i, "content": "a" * 500, "_source": "memory"} for i in range(10)]
        out = _shape_recall_results(rows, max_chars=1200, max_bytes=2000)

        markers = [r for r in out if "_dropped" in r]
        assert len(markers) == 1, "exactly one _dropped marker expected"
        assert out[-1] is markers[0], "marker must be the trailing element"

        kept = [r for r in out if "_dropped" not in r]
        assert [r["id"] for r in kept] == list(range(len(kept))), "top-ranked rows kept in order"
        assert markers[0]["_dropped"] == {
            "rows": 10 - len(kept),
            "reason": "total_byte_budget",
            "budget": 2000,
        }

    def test_no_marker_when_budget_is_sufficient(self):
        from yadgar.core.server.tools.recall import _shape_recall_results

        rows = [{"id": i, "content": "a" * 10, "_source": "memory"} for i in range(3)]
        out = _shape_recall_results(rows, max_chars=1200, max_bytes=65536)
        assert len(out) == 3
        assert all("_dropped" not in r for r in out)

    def test_first_row_always_survives_even_if_over_budget(self):
        """A tiny budget must still return the top hit, not an empty list."""
        from yadgar.core.server.tools.recall import _shape_recall_results

        rows = [{"id": i, "content": "a" * 400, "_source": "memory"} for i in range(4)]
        out = _shape_recall_results(rows, max_chars=1200, max_bytes=10)
        assert out[0]["id"] == 0
        assert out[-1]["_dropped"]["rows"] == 3

    def test_shaped_output_respects_the_byte_budget(self):
        from yadgar.core.server.tools.recall import _shape_recall_results

        rows = [{"id": i, "content": "a" * 900, "_source": "memory"} for i in range(50)]
        out = _shape_recall_results(rows, max_chars=1200, max_bytes=8192)
        payload = [r for r in out if "_dropped" not in r]
        assert _compact(payload) <= 8192

    def test_empty_input_returns_empty(self):
        from yadgar.core.server.tools.recall import _shape_recall_results

        assert _shape_recall_results([], max_chars=1200, max_bytes=65536) == []


# ═══════════════════════════════════════════════════════════════════════════
# 4. The load-bearing seam invariant: closure gets UNTRIMMED rows
# ═══════════════════════════════════════════════════════════════════════════


class TestSideEffectClosureGetsUntrimmedRows:
    def test_closure_untrimmed_caller_shaped(self):
        """Non-vacuous: the fixture row has a denylisted field AND over-cap content.

        A shaped-but-equal list or an in-place mutation would both slip past
        `test_recall_pipeline_unit.py:325`; asserting on the closure's actual
        argument object is what makes this real.
        """
        import yadgar.core.server.tools  # noqa: F401
        from yadgar._shared.runtime.recall_side_effects_fork import drain_session_side_effects

        mod = sys.modules["yadgar.core.server.tools.recall"]

        raw = [
            {
                "id": 42,
                "content": "L" * 4000,
                "contextual_prefix": "[Project: yadgar] ",
                "sr_x": 0.5,
                "_source": "memory",
            }
        ]

        with (
            patch.object(mod, "_forward_to_backend", return_value=raw),
            patch.object(mod, "_apply_recall_session_side_effects") as mock_session,
            patch.object(mod, "_st") as mock_st,
        ):
            mock_st._consolidation = None
            with (
                patch("yadgar.core.server.tools.project._detect_branch", return_value="master"),
                patch(
                    "yadgar.core.server.tools.project._get_default_branch", return_value="master"
                ),
            ):
                result = mod.recall(query="seam test", directory="/tmp", max_results=5)

            drain_session_side_effects(timeout=10.0)

            passed = mock_session.call_args[0][0]

        # The closure saw the untrimmed rows.
        assert passed[0]["contextual_prefix"] == "[Project: yadgar] "
        assert passed[0]["sr_x"] == 0.5
        assert len(passed[0]["content"]) == 4000
        assert "_truncated" not in passed[0]

        # The caller got the shaped rows.
        assert "contextual_prefix" not in result[0]
        assert "sr_x" not in result[0]
        assert len(result[0]["content"]) == 1200
        assert result[0]["_truncated"]["total"] == 4000


# ═══════════════════════════════════════════════════════════════════════════
# 5. max_chars per-call override
# ═══════════════════════════════════════════════════════════════════════════


class TestMaxCharsParam:
    def test_signature_exposes_max_chars_defaulting_to_none(self):
        import inspect

        from yadgar.core.server.tools.recall import recall

        params = inspect.signature(recall).parameters
        assert "max_chars" in params, "recall missing max_chars parameter"
        assert params["max_chars"].default is None

    @pytest.mark.parametrize("bad", [0, -1])
    def test_zero_or_negative_max_chars_raises(self, bad):
        from yadgar.core.server.tools.recall import recall

        with pytest.raises(ValueError, match="max_chars"):
            recall(query="q", directory="/tmp", max_chars=bad)

    def test_max_chars_validated_before_backend_call(self):
        """Guard runs alongside the existing type/mode/profile checks — no I/O first."""
        import yadgar.core.server.tools  # noqa: F401

        mod = sys.modules["yadgar.core.server.tools.recall"]
        with patch.object(mod, "_forward_to_backend") as fwd:
            with pytest.raises(ValueError, match="max_chars"):
                mod.recall(query="q", directory="/tmp", max_chars=0)
            fwd.assert_not_called()

    def test_per_call_max_chars_overrides_the_configured_default(self):
        import yadgar.core.server.tools  # noqa: F401
        from yadgar._shared.runtime.recall_side_effects_fork import drain_session_side_effects

        mod = sys.modules["yadgar.core.server.tools.recall"]
        raw = [{"id": 1, "content": "M" * 900, "_source": "memory"}]

        with (
            patch.object(mod, "_forward_to_backend", return_value=raw),
            patch.object(mod, "_apply_recall_session_side_effects"),
            patch.object(mod, "_st") as mock_st,
        ):
            mock_st._consolidation = None
            with (
                patch("yadgar.core.server.tools.project._detect_branch", return_value="master"),
                patch(
                    "yadgar.core.server.tools.project._get_default_branch", return_value="master"
                ),
            ):
                result = mod.recall(query="q", directory="/tmp", max_results=1, max_chars=100)
            drain_session_side_effects(timeout=10.0)

        assert len(result[0]["content"]) == 100
        assert result[0]["_truncated"]["kept"] == 100


# ═══════════════════════════════════════════════════════════════════════════
# 6. Config: Settings + registry + FIELD_META (I25 three-way)
# ═══════════════════════════════════════════════════════════════════════════


class TestConfigWiring:
    def test_settings_defaults(self):
        from yadgar._shared.config import get_settings

        s = get_settings()
        assert s.RECALL_MAX_CONTENT_CHARS == 1200
        assert s.RECALL_MAX_TOTAL_BYTES == 65536

    def test_registry_entries_present(self):
        from yadgar._shared.config.config_registry import _REGISTRY

        names = {e.name for e in _REGISTRY}
        assert "YADGAR_RECALL_MAX_CONTENT_CHARS" in names
        assert "YADGAR_RECALL_MAX_TOTAL_BYTES" in names

    def test_field_meta_entries_present(self):
        from yadgar._shared.config.config_yaml import FIELD_META

        assert "recall_max_content_chars" in FIELD_META
        assert "recall_max_total_bytes" in FIELD_META

    def test_env_override_wins(self, monkeypatch):
        from yadgar._shared.config import get_settings
        from yadgar._shared.config.config_registry import clear_config_caches

        monkeypatch.setenv("YADGAR_RECALL_MAX_CONTENT_CHARS", "333")
        monkeypatch.setenv("YADGAR_RECALL_MAX_TOTAL_BYTES", "4444")
        clear_config_caches()
        try:
            s = get_settings()
            assert s.RECALL_MAX_CONTENT_CHARS == 333
            assert s.RECALL_MAX_TOTAL_BYTES == 4444
        finally:
            monkeypatch.undo()
            clear_config_caches()

    def test_yaml_respected_when_env_unset(self, monkeypatch, tmp_path):
        from yadgar._shared.config import get_settings
        from yadgar._shared.config.config_registry import clear_config_caches

        cfg = tmp_path / "yadgar-recall-cap.yaml"
        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
        monkeypatch.delenv("YADGAR_RECALL_MAX_CONTENT_CHARS", raising=False)
        monkeypatch.delenv("YADGAR_RECALL_MAX_TOTAL_BYTES", raising=False)
        cfg.write_text("recall_max_content_chars: 777\nrecall_max_total_bytes: 8888\n")
        clear_config_caches()
        try:
            s = get_settings()
            assert s.RECALL_MAX_CONTENT_CHARS == 777
            assert s.RECALL_MAX_TOTAL_BYTES == 8888
        finally:
            monkeypatch.undo()
            clear_config_caches()


# ═══════════════════════════════════════════════════════════════════════════
# 7. adr_list pagination — row COUNT, not row width (57 KB observed)
# ═══════════════════════════════════════════════════════════════════════════


class TestAdrListPagination:
    def test_signature_exposes_limit_and_offset(self):
        import inspect

        from yadgar.core.server.tools.adr import adr_list

        params = inspect.signature(adr_list).parameters
        assert "limit" in params
        assert "offset" in params
        assert params["offset"].default == 0

    def test_slices_and_reports_total_when_truncated(self):
        from yadgar.core.server.tools import adr as adr_mod

        rows = [
            {"adr_id": f"ADR-{i:04d}", "status": "accepted", "title": f"t{i}"} for i in range(120)
        ]
        with (
            patch.object(adr_mod, "_resolve_project_root", return_value="/proj"),
            patch.object(adr_mod, "wiki_read", return_value={"content": "x"}),
            patch.object(adr_mod, "parse_index_rows", return_value=rows),
        ):
            out = adr_mod.adr_list(directory="/proj", limit=10)

        assert [r["adr_id"] for r in out["adrs"]] == [f"ADR-{i:04d}" for i in range(10)]
        assert out["count"] == 10
        assert out["total"] == 120
        assert out["truncated"] is True
        assert out["next_offset"] == 10

    def test_offset_pages_forward(self):
        from yadgar.core.server.tools import adr as adr_mod

        rows = [{"adr_id": f"ADR-{i:04d}", "status": "open"} for i in range(30)]
        with (
            patch.object(adr_mod, "_resolve_project_root", return_value="/proj"),
            patch.object(adr_mod, "wiki_read", return_value={"content": "x"}),
            patch.object(adr_mod, "parse_index_rows", return_value=rows),
        ):
            out = adr_mod.adr_list(directory="/proj", limit=10, offset=25)

        assert [r["adr_id"] for r in out["adrs"]] == [f"ADR-{i:04d}" for i in range(25, 30)]
        assert out["count"] == 5
        assert out["total"] == 30
        assert "next_offset" not in out

    def test_shape_unchanged_when_nothing_is_truncated(self):
        """Existing callers must not see new keys — test_adr.py asserts exact dicts."""
        from yadgar.core.server.tools import adr as adr_mod

        rows = [{"adr_id": "ADR-0001", "status": "open"}]
        with (
            patch.object(adr_mod, "_resolve_project_root", return_value="/proj"),
            patch.object(adr_mod, "wiki_read", return_value={"content": "x"}),
            patch.object(adr_mod, "parse_index_rows", return_value=rows),
        ):
            out = adr_mod.adr_list(directory="/proj")

        assert set(out) == {"adrs", "count"}
        assert out["count"] == 1
