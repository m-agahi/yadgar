"""C10 — task #317: FileQueue._read_dlq_rejection surfaces the reason for ALL rejection modes.

The wait=True caller receives ``{"status": "rejected", "result": <dict>|None}``.
Pre-C10, ``_read_dlq_rejection`` returned a structured dict only for
``duplicate_detected``, ``slug_exists``, and the ``wiki_size_collapse:`` last_error
substring — every other rejection mode produced ``result=None`` and a caller had
no way to tell WHICH gate had refused the request. C10 unifies the surface so
every non-OK reason reaches the caller with at minimum ``{"reason": ...}``.

Motivating incident: a drainer file ends up in the DLQ for a class the reader
does not know about (e.g. ``malformed_payload``, ``engine_2_unavailable``,
``project_not_registered``) and the client sees ``{status: rejected, result: None}``
— byte-identical to "drainer landed in DLQ but never wrote a sidecar". This test
guards that gap.
"""

from __future__ import annotations

import json

from yadgar._shared.file_queue.queue import FileQueue


def _seed_dlq_with_sidecar(
    tmp_path,
    *,
    failure_reason: str,
    last_error: str | None = None,
    failure_metadata: dict | None = None,
) -> tuple[str, FileQueue]:
    """Drop a fake DLQ file + sidecar matching the drainer contract.

    Returns the job_id the caller would use to wait_for().
    """
    fq = FileQueue(tmp_path)
    job_id = "deadbeef"
    payload_path = fq.dlq_dir / f"0001__{job_id}.json"
    payload_path.write_text(json.dumps({"op_type": "memorize", "job_id": job_id}))
    sidecar = payload_path.parent / (payload_path.name + ".error.json")
    meta: dict = {
        "op_type": "memorize",
        "failure_reason": failure_reason,
        "classification": "permanent",
        "attempts": 1,
    }
    if last_error is not None:
        meta["last_error"] = last_error
    if failure_metadata is not None:
        meta["failure_metadata"] = failure_metadata
    sidecar.write_text(json.dumps(meta))
    return job_id, fq


class TestReadDlqRejectionSurfacesAllModes:
    """Every rejection mode the drainer can produce must reach the caller
    as a non-None ``result`` with at minimum a ``reason`` key.

    Pre-C10: only ``duplicate_detected``, ``slug_exists``, and the wiki size-collapse
    substring had bespoke return paths; every other reason fell through to ``return None``.
    """

    def test_duplicate_detected_surfaces_candidates(self, tmp_path):
        job_id, fq = _seed_dlq_with_sidecar(
            tmp_path,
            failure_reason="duplicate_detected",
            failure_metadata={"candidates": [{"id": 7, "score": 0.92}]},
        )
        result = fq.wait_for_job(job_id, timeout=0.05)
        assert result["status"] == "rejected"
        assert result["result"] is not None
        assert result["result"]["reason"] == "duplicate_detected"
        assert result["result"]["candidates"] == [{"id": 7, "score": 0.92}]

    def test_slug_exists_surfaces_slug(self, tmp_path):
        job_id, fq = _seed_dlq_with_sidecar(
            tmp_path,
            failure_reason="slug_exists",
            failure_metadata={"slug": "agent-prompt-foo", "hint": "use upsert=True"},
        )
        result = fq.wait_for_job(job_id, timeout=0.05)
        assert result["status"] == "rejected"
        assert result["result"] is not None
        assert result["result"]["reason"] == "slug_exists"
        assert result["result"]["slug"] == "agent-prompt-foo"
        assert result["result"]["hint"] == "use upsert=True"

    def test_malformed_payload_surfaces_reason(self, tmp_path):
        """A drainer-class failure the reader does not recognise must still
        surface a reason — never ``result=None``.

        Pre-C10: ``malformed_payload`` fell through to ``return None`` because
        the reader had no branch for it. The caller would see ``result=None``
        and could not distinguish "sidecar absent" from "sidecar present but
        unknown reason".
        """
        job_id, fq = _seed_dlq_with_sidecar(
            tmp_path,
            failure_reason="malformed_payload",
            last_error="missing required key: project_id",
        )
        result = fq.wait_for_job(job_id, timeout=0.05)
        assert result["status"] == "rejected", result
        assert result["result"] is not None, (
            "DLQ rejection must surface a reason for ALL failure_reason values, "
            "not just the three pre-C10 special-cased (duplicate_detected, "
            "slug_exists, wiki_size_collapse). Got result=None for "
            "reason='malformed_payload'."
        )
        assert result["result"]["reason"] == "malformed_payload"
        # Last error is preserved verbatim so the operator can read the original
        # rejection message without re-opening the sidecar.
        assert result["result"]["hint"] == "missing required key: project_id"

    def test_wiki_size_collapse_surfaces_hint(self, tmp_path):
        """Regression guard for the pre-existing wiki_size_collapse branch."""
        job_id, fq = _seed_dlq_with_sidecar(
            tmp_path,
            failure_reason="permanent_error",
            last_error=(
                "wiki_size_collapse: page shrank from 12.3 KB to 1.1 KB; "
                "set allow_truncation=True or call wiki_restore(slug, version)"
            ),
        )
        result = fq.wait_for_job(job_id, timeout=0.05)
        assert result["status"] == "rejected"
        assert result["result"] is not None
        assert result["result"]["reason"] == "wiki_size_collapse"
        assert "allow_truncation=True" in result["result"]["hint"]

    def test_unknown_reason_no_longer_returns_none(self, tmp_path):
        """Catch-all: a drainer-class failure_reason the reader has NEVER been
        taught must still surface a reason to the caller.

        Pre-C10: ``return None`` for anything not in the special-case set,
        so a real unknown rejection looked identical to "no sidecar written".
        """
        job_id, fq = _seed_dlq_with_sidecar(
            tmp_path,
            failure_reason="engine_2_unavailable",
            last_error="MariaStorageEngine is None at apply()",
        )
        result = fq.wait_for_job(job_id, timeout=0.05)
        assert result["status"] == "rejected"
        assert result["result"] is not None
        # The reason MUST be the original failure_reason, not a fallback string,
        # so an operator can grep the drainer code for the rejection class.
        assert result["result"]["reason"] == "engine_2_unavailable"
        assert "MariaStorageEngine is None" in result["result"]["hint"]

    def test_no_sidecar_still_returns_none(self, tmp_path):
        """Sanity: a DLQ file with NO sidecar must still return ``result=None``.

        The bug fix in C10 is about a sidecar WITH an unknown reason, not about
        a missing sidecar. The two failure modes must stay distinguishable.
        """
        fq = FileQueue(tmp_path)
        job_id = "no-sidecar"
        (fq.dlq_dir / f"0001__{job_id}.json").write_text(json.dumps({"op_type": "memorize"}))
        result = fq.wait_for_job(job_id, timeout=0.05)
        assert result["status"] == "rejected"
        assert result["result"] is None
