"""Car B (task #204) — ``recall(mode='landscape')`` must honor ``tags=``.

Today ``_run_landscape_backend`` accepts only ``(query, max_results, project_id,
storage)`` and never threads the caller's ``tags`` down to ``consensus_retrieve``
or to the post-filter — so a landscape query is whole-corpus-within-project, no
matter what the caller asks for. A single char-level pin: when ``tags=[...]`` is
supplied, the post-filter must drop rows whose tag set does not contain every
requested tag. Mirrors the ``opt_in_tags`` subset guard the fanout path already
runs in providers/memory.py:131.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import yadgar._shared.runtime.state as _st
import yadgar.backend.embed_service.embed_service as _es
from yadgar.backend.embed_service import embed_service_routes as routes
from yadgar.backend.embed_service.embed_service_models import RecallRequest

_PROJECT = "m-agahi/yadgar"
_TAG_IN = "wanted-tag"
_TAG_OUT = "unrelated"


@pytest.fixture
def wired(monkeypatch):
    """Patch the route's seams; land a fake pool + fake storage on the route path."""
    monkeypatch.setattr(_es, "_ensure_recall_engines", lambda: None, raising=False)
    monkeypatch.setattr(
        "yadgar._shared.runtime.lifecycle._get_storage", lambda: MagicMock(), raising=False
    )
    monkeypatch.setattr(
        "yadgar._shared.runtime.lifecycle._get_sql_storage", lambda: MagicMock(), raising=False
    )
    from yadgar.backend.retrieval import recall_pipeline as rp

    monkeypatch.setattr(rp, "_compute_db_boost", lambda _m, _s: ([], "now"), raising=False)


def _req(*, tags: list[str] | None, mode: str = "landscape") -> RecallRequest:
    return RecallRequest(query="x", project_id=_PROJECT, tags=tags, mode=mode)


class TestLandscapeTags:
    @pytest.mark.asyncio
    async def test_tags_filter_results_in_landscape_mode(self, monkeypatch, wired):
        """A landscape call with ``tags=[wanted]`` must not return rows lacking it.

        Pre-fix: ``_run_landscape_backend`` ignores ``tags`` entirely; both rows
        reach the caller. The assertion is that the out-of-tag row is dropped.
        """
        fake_pool = MagicMock()
        in_row = {
            "id": 1,
            "project_id": _PROJECT,
            "tags": [_TAG_IN],
            "consensus_score": 0.9,
        }
        out_row = {
            "id": 2,
            "project_id": _PROJECT,
            "tags": [_TAG_OUT],
            "consensus_score": 0.8,
        }
        fake_pool.consensus_retrieve.return_value = [in_row, out_row]
        monkeypatch.setattr(_st, "_pool", fake_pool, raising=False)

        resp = await routes.recall_route(_req(tags=[_TAG_IN]), None)

        ids = [r["id"] for r in resp.results]
        assert 1 in ids, f"tagged-in row dropped by post-filter: {ids}"
        assert 2 not in ids, (
            "row without the requested tag survived the landscape post-filter — "
            "tags= is being ignored end-to-end on the landscape path"
        )

    @pytest.mark.asyncio
    async def test_no_tags_returns_all_project_rows(self, monkeypatch, wired):
        """No ``tags=`` → unfiltered (defensive: the fix must not over-filter)."""
        fake_pool = MagicMock()
        in_row = {"id": 1, "project_id": _PROJECT, "tags": [_TAG_IN]}
        out_row = {"id": 2, "project_id": _PROJECT, "tags": [_TAG_OUT]}
        fake_pool.consensus_retrieve.return_value = [in_row, out_row]
        monkeypatch.setattr(_st, "_pool", fake_pool, raising=False)

        resp = await routes.recall_route(_req(tags=None), None)

        ids = sorted(r["id"] for r in resp.results)
        assert ids == [1, 2], f"empty-tags path dropped rows: {ids}"
