"""Tests for the wiki size-collapse guard (ledger task 271).

The defect this covers: a wiki update recorded ``"+43 -199 lines, 15902 to 3717
bytes"`` **in its own change_summary** and shipped anyway. The collapse was
COMPUTED by ``compute_change_summary`` and then ignored — the same
signals-that-lie shape as the rest of this train, pointed at data.

Enforcement lives at the STORAGE chokepoint
(``_WikiMixin.update_wiki_page``), beside Car J's mutability gate, so it
covers every write path: ``WikiStore.add``'s upsert branch (the path the
incident used), ``WikiStore._apply_text_edit``'s eight anchor-text/positional
ops, ``append_section``, and the ``admin_exec.wiki_update`` op that bypasses
``WikiStore`` entirely.

The over-fire direction is the one that kills a guard, so the boundary cases
below are load-bearing: a metadata-only update on a large page, a shrink that
lands just above the ratio, and a shrink below the byte floor must ALL pass.
"""

from __future__ import annotations

import pytest

from yadgar._shared.refusal import REFUSAL_STATUS, AdminRefusal, refusal_envelope
from yadgar._shared.storage import StorageEngine
from yadgar._shared.storage.truncation_gate import (
    TRUNCATION_MIN_OLD_BYTES,
    TRUNCATION_RATIO_THRESHOLD,
    WikiSizeCollapseError,
    enforce_no_size_collapse,
)

_PROJECT = "m-agahi/yadgar"


def _page(content: str, *, page_id: int = 1, slug: str = "p") -> dict:
    return {"id": page_id, "slug": slug, "content": content}


# ── A. The gate function in isolation ────────────────────────────────────────


class TestGatePredicate:
    def test_growth_passes(self):
        enforce_no_size_collapse(_page("x" * 4000), "x" * 8000, op="t", allowed=False)

    def test_identical_content_passes(self):
        body = "x" * 4000
        enforce_no_size_collapse(_page(body), body, op="t", allowed=False)

    def test_the_real_incident_refuses(self):
        """15902 -> 3717 bytes, ratio 0.234 — the write that started this."""
        with pytest.raises(WikiSizeCollapseError) as exc:
            enforce_no_size_collapse(
                _page("x" * 15902), "y" * 3717, op="update_wiki_page", allowed=False
            )
        assert exc.value.reason == "wiki_size_collapse"

    def test_just_above_ratio_passes(self):
        """A shrink to 51% of a large page is an ordinary prune, not a collapse.

        Guards the over-fire direction: the corpus holds 151 updates in the
        0.9-1.0 band and 44 in 0.6-0.9, so a threshold above 0.5 starts eating
        routine edits.
        """
        old = 10_000
        new = int(old * (TRUNCATION_RATIO_THRESHOLD + 0.01))
        enforce_no_size_collapse(_page("x" * old), "y" * new, op="t", allowed=False)

    def test_below_byte_floor_passes(self):
        """268 -> 119 bytes: a real corpus row, and a meaningless 'collapse'.

        Small pages routinely halve; the floor is what keeps the guard about
        content loss rather than about arithmetic.
        """
        assert 268 < TRUNCATION_MIN_OLD_BYTES
        enforce_no_size_collapse(_page("x" * 268), "y" * 119, op="t", allowed=False)

    def test_exactly_at_floor_with_collapse_refuses(self):
        with pytest.raises(WikiSizeCollapseError):
            enforce_no_size_collapse(
                _page("x" * TRUNCATION_MIN_OLD_BYTES), "y" * 10, op="t", allowed=False
            )

    def test_allowed_flag_lets_the_collapse_through(self):
        enforce_no_size_collapse(_page("x" * 15902), "y" * 3717, op="t", allowed=True)


# ── B. The refusal's shape ───────────────────────────────────────────────────


class TestRefusalShape:
    @pytest.fixture
    def exc(self) -> WikiSizeCollapseError:
        with pytest.raises(WikiSizeCollapseError) as info:
            enforce_no_size_collapse(
                _page("x" * 15902, page_id=7052, slug="agent-prompt-review-pr"),
                "y" * 3717,
                op="update_wiki_page",
                allowed=False,
            )
        return info.value

    def test_is_an_admin_refusal(self, exc):
        """A decision, not a fault — so /admin renders it as 409, not a 500."""
        assert isinstance(exc, AdminRefusal)

    def test_reason_is_distinct_from_the_mutability_reasons(self, exc):
        """Reasons are never collapsed across causes: the operator fix differs."""
        assert exc.reason == "wiki_size_collapse"
        assert exc.reason not in {"wiki_page_locked", "wiki_page_derived"}

    def test_report_carries_the_measured_evidence(self, exc):
        report = exc.refusal_report()
        assert report["page_id"] == 7052
        assert report["slug"] == "agent-prompt-review-pr"
        assert report["old_bytes"] == 15902
        assert report["new_bytes"] == 3717
        assert report["removed_bytes"] == 15902 - 3717
        assert report["ratio"] == pytest.approx(3717 / 15902, abs=1e-4)
        assert report["ratio_threshold"] == TRUNCATION_RATIO_THRESHOLD
        assert report["min_old_bytes"] == TRUNCATION_MIN_OLD_BYTES

    def test_message_names_the_escape_hatch(self, exc):
        """A refusal that doesn't say how to proceed is the same defect in a hat."""
        msg = str(exc)
        assert "allow_truncation=True" in msg
        assert "wiki_add" in msg
        assert "wiki_restore" in msg

    def test_envelope_round_trips_as_a_refusal(self, exc):
        env = refusal_envelope(exc, op="wiki_update")
        assert env["ok"] is False
        assert env["refused"] is True
        assert env["reason"] == "wiki_size_collapse"
        assert env["old_bytes"] == 15902
        assert REFUSAL_STATUS == 409


# ── C. The storage chokepoint ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def storage(module_storage):  # noqa: ARG001 — delegation pattern (test_storage.py)
    return module_storage


def _seed(storage: StorageEngine, slug: str, content: str) -> int:
    return storage.insert_wiki_page(
        {
            "slug": slug,
            "title": slug,
            "content": content,
            "category": "reference",
            "tags": [],
            "confidence": "high",
            "source_memory_ids": [],
            "links": [],
            "directory_context": "/tmp/trunc-test",
            "project_id": _PROJECT,
            "_sanctioned": True,
        }
    )


class TestStorageChokepoint:
    def test_collapsing_update_is_refused_and_does_not_land(self, storage):
        pid = _seed(storage, "trunc-refused", "x" * 15902)
        with pytest.raises(WikiSizeCollapseError) as exc:
            storage.update_wiki_page(pid, {"content": "y" * 3717})
        assert exc.value.reason == "wiki_size_collapse"
        assert exc.value.refusal_report()["page_id"] == pid
        # The row is untouched — refused BEFORE the transaction.
        assert storage.get_wiki_page(pid)["content"] == "x" * 15902

    def test_no_version_row_is_written_for_a_refused_update(self, storage):
        pid = _seed(storage, "trunc-no-version", "x" * 9000)
        before = storage.get_max_version_for_page(pid)
        with pytest.raises(WikiSizeCollapseError):
            storage.update_wiki_page(pid, {"content": "y" * 100})
        assert storage.get_max_version_for_page(pid) == before

    def test_metadata_only_update_on_a_large_page_passes(self, storage):
        """``updates`` with no ``content`` key falls back to the old body.

        Pins the guard to CONTENT changes: page_type retypes (Car G supersede,
        Car K sweep, migration 028) write metadata alone and must never fire.
        """
        pid = _seed(storage, "trunc-metadata-only", "x" * 20000)
        assert storage.update_wiki_page(pid, {"category": "decision"}) is True

    def test_allow_truncation_kwarg_lets_the_write_land(self, storage):
        pid = _seed(storage, "trunc-allowed", "x" * 15902)
        assert (
            storage.update_wiki_page(pid, {"content": "y" * 3717}, _allow_truncation=True) is True
        )
        assert storage.get_wiki_page(pid)["content"] == "y" * 3717

    def test_sanctioned_alone_does_not_bypass_the_size_gate(self, storage):
        """``_sanctioned`` is Car J's lock key, not this one.

        Deliberately NOT coupled: a regenerator that emits a gutted page is the
        case this guard is best placed to catch, and every sanctioned writer
        would be blind to it. Regenerators opt in explicitly instead.
        """
        pid = _seed(storage, "trunc-sanctioned", "x" * 15902)
        with pytest.raises(WikiSizeCollapseError):
            storage.update_wiki_page(pid, {"content": "y" * 3717}, _sanctioned=True)


# ── D. The WikiStore seams that must stay open ───────────────────────────────


class TestWikiStoreSeams:
    def test_restore_version_passes_allow_truncation(self):
        """wiki_restore is the RECOVERY path — it must never be gated.

        Two precedents in-tree: it already bypasses the v5.39 similarity gate
        for the same reason, and ``_reject_if_discipline_weakening`` exempts it
        by name. A restore to an earlier, shorter version is exactly the fix
        for an over-eager growth, and blocking it would leave a truncated page
        with no way back.
        """
        import inspect

        from yadgar._shared.wiki.store import WikiStore

        src = inspect.getsource(WikiStore.restore_version)
        assert "_allow_truncation=True" in src

    def test_wiki_add_options_carries_the_flag(self):
        from yadgar._shared.wiki.contract import WikiAddOptions

        assert WikiAddOptions().allow_truncation is False
        assert WikiAddOptions(allow_truncation=True).allow_truncation is True

    def test_wiki_store_add_forwards_but_never_derives_the_flag(self):
        """``add`` must not decide the exemption — only forward it.

        The canonical-seam exemption lives in ``run_wiki_add_replay``, which is
        the only thing that knows a write is canonical. Keeping the decision out
        of ``add`` is also what keeps its cyclomatic at its allowlisted 16.
        """
        import inspect

        from yadgar._shared.wiki.store import WikiStore

        src = inspect.getsource(WikiStore.add)
        assert "_allow_truncation=bool(o.allow_truncation)" in src
        assert "or sanctioned" not in src

    def test_canonical_seam_is_exempt_at_the_replay(self):
        """Full-body regenerators shrink legitimately — a task-list mirror
        shrinks as tasks complete — so ``_wiki_write_canonical``'s sanctioned
        writes open the gate. At ONE seam, not inside the gate: every other
        sanctioned writer (rollup regeneration, the Car K sweep, reslug) stays
        gated, which is what keeps a generator emitting a gutted page visible.
        """
        import inspect

        from yadgar.backend.write_exec.wiki_add_impl import run_wiki_add_replay

        src = inspect.getsource(run_wiki_add_replay)
        assert 'bool(payload.get("allow_truncation", False)) or _sanctioned' in src


# ── E. The drainer classifies the refusal as permanent ───────────────────────


class TestDrainerClassification:
    def test_size_collapse_is_permanent_not_transient(self):
        """Retrying a refused truncation forever is the wrong failure shape.

        Mirrors the ``slug_exists:`` line beside it: a policy decision cannot
        become true by waiting, so it goes to DLQ on the permanent budget.
        """
        from yadgar.backend.queue_drainer import _classify_error

        assert _classify_error("wiki_size_collapse: page would shrink") == "permanent"

    def test_wait_true_caller_gets_the_message_that_names_the_hatch(self, tmp_path):
        """The refusal has to ARRIVE, not just be raised.

        The gate fires inside the drainer's apply, so it takes the generic
        ``_move_to_dlq`` path and its ``failure_reason`` is the default
        ``permanent_error`` — the two arms that existed here keyed on
        ``failure_reason`` and would have handed a ``wait=True`` caller a bare
        ``None``. Keying on ``last_error`` is what carries the escape hatch's
        name across the queue boundary.
        """
        import json

        from yadgar._shared.file_queue.queue import FileQueue

        fq = FileQueue(str(tmp_path))
        job = fq.dlq_dir / "0001_job.json"
        fq.dlq_dir.mkdir(parents=True, exist_ok=True)
        job.write_text("{}")
        (fq.dlq_dir / "0001_job.json.error.json").write_text(
            json.dumps(
                {
                    "failure_reason": "permanent_error",
                    "last_error": (
                        "wiki_size_collapse: update_wiki_page would drop 77% ... "
                        "wiki_add(..., allow_truncation=True) ... "
                        "wiki_restore(slug, version)."
                    ),
                }
            )
        )

        rejection = fq._read_dlq_rejection(job)
        assert rejection is not None, "a size-collapse DLQ entry must surface, not read as None"
        assert rejection["stored"] is False
        assert rejection["reason"] == "wiki_size_collapse"
        assert "allow_truncation=True" in rejection["hint"]
        assert "wiki_restore" in rejection["hint"]


# ── F. The escape hatch is reachable from the MCP surface ────────────────────


class TestEscapeHatchIsReachable:
    """A refusal naming a flag no caller can pass is the defect in a hat.

    ``wiki_add`` is the whole-page write path and the one the incident used, so
    the assertion "yes, I mean to drop most of this page" has to be sayable
    there. These pin the thread-through end to end: tool signature → queue
    payload → drain replay → ``WikiAddOptions`` → storage kwarg.
    """

    def test_wiki_add_tool_exposes_allow_truncation(self):
        import inspect

        from yadgar.core.server.tools.wiki import wiki_add

        sig = inspect.signature(wiki_add)
        assert "allow_truncation" in sig.parameters
        assert sig.parameters["allow_truncation"].default is False

    def test_wiki_add_docstring_separates_it_from_force(self):
        """``force`` bypasses duplicate detection; it must not open this gate too."""
        from yadgar.core.server.tools.wiki import wiki_add

        doc = wiki_add.__doc__ or ""
        assert "allow_truncation" in doc
        assert "force" in doc

    def test_replay_reads_allow_truncation_from_the_payload(self):
        import inspect

        from yadgar.backend.write_exec.wiki_add_impl import run_wiki_add_replay

        src = inspect.getsource(run_wiki_add_replay)
        assert 'payload.get("allow_truncation"' in src
        # BOTH option bundles — the replace_slug branch is a real write path.
        assert src.count("allow_truncation=_allow_truncation") == 2
