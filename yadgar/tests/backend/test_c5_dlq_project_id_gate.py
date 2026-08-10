"""C5 (0047 PR#40 §5) — the DLQ project_id gate, widened and de-hardcoded.

Three C4b handoffs land here and each has its own failure mode:

1. ``_validate_project_id`` was reachable ONLY from ``_validate_wiki_add``, so
   ``memorize`` / ``anchor`` / ``action_log`` jobs passed the gate unvalidated.
2. The rejection string and the metadata hint hardcoded ``"wiki_add"``, which
   goes stale the moment the gate widens — and the reader of that hint is the
   one who has to fix the call.
3. ``"global"`` was deliberately NOT a sentinel: C4 could not reject it while
   ``resolve_effective_project`` still produced it for every unresolvable tree.
   C5 deletes that tier, so the value becomes unambiguously a defect.
"""

from __future__ import annotations

from yadgar.backend.queue_drainer.dlq import _DLQMixin


class _Gate(_DLQMixin):
    """The mixin under test, free of the drainer's queue/IO surface."""


def _reason(payload: dict, op_type: str = "wiki_add") -> str | None:
    return _Gate()._validate_project_id(payload, op_type)


class TestSentinelSet:
    def test_global_is_now_a_sentinel(self):
        """C4b handoff #3 — must land in the SAME edit as the tier deletion."""
        assert "global" in _Gate._SENTINEL_PROJECT_IDS
        assert _reason({"project_id": "global"}) is not None

    def test_empty_and_unresolved_still_rejected(self):
        for value in ("", "   ", "unresolved"):
            assert _reason({"project_id": value}) is not None, value

    def test_absent_key_rejected(self):
        assert _reason({}) is not None

    def test_a_real_identity_passes(self):
        assert _reason({"project_id": "m-agahi/yadgar"}) is None

    def test_local_prefix_is_not_special_cased_here(self):
        """The gate is not where ``local/`` dies — the MINT is (C2).

        Asserted so nobody "helpfully" adds a ``local/`` prefix check here and
        thereby breaks a legitimately registered ``local/...`` project the
        operator chose. C5 removed the only PRODUCER; the gate stays a
        sentinel check.
        """
        assert _reason({"project_id": "local/aws-work"}) is None


class TestOpTypeIsReported:
    def test_rejection_names_the_actual_op(self):
        """C4b handoff #2 — a memorize rejection must not claim to be wiki_add."""
        reason = _reason({"project_id": ""}, "memorize")
        assert "memorize" in reason
        assert "wiki_add" not in reason

    def test_metadata_hint_names_the_actual_op(self):
        meta = _Gate()._build_missing_project_id_metadata({}, "anchor")
        assert meta["payload_op_type"] == "anchor"
        assert "anchor" in meta["hint"]
        assert "wiki_add" not in meta["hint"]
        # The hint has to say what to PASS, not merely that something is wrong.
        assert 'project="owner/repo"' in meta["hint"]


class TestEnforcementKnobGoneFromTheDrainer:
    def test_missing_directory_rejects_unconditionally(self, monkeypatch):
        """The escape hatch is deleted: setting it OFF changes nothing."""
        monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "false")
        record = {
            "payload": {
                "wiki_schema_version": 2,
                "slug": "s",
                "title": "t",
                "content": "c",
                "category": "reference",
                "project_id": "a/b",
            }
        }
        reason = _Gate()._validate_wiki_add(record)
        assert reason is not None
        assert reason.startswith("missing_directory")


class TestGateReachesEveryOpType:
    """C4b handoff #1 — the gate ran for ``wiki_add`` and nothing else.

    Driven through ``_process_pending_file`` (the real dispatch point) rather
    than by calling ``_validate_project_id`` directly, because the defect was
    never in the validator — it was in what reached it.
    """

    def _drainer(self, tmp_path, recorded):
        from yadgar.backend.queue_drainer import QueueDrainer

        drainer = QueueDrainer.__new__(QueueDrainer)
        drainer._attempts = {}
        drainer._max_permanent = 1

        def _reject(path, fname, attempt, op_type, reject_reason, data, now):
            recorded.append((op_type, reject_reason))

        def _apply(fname, path, data, op_type, now):
            recorded.append((op_type, None))
            return 1

        drainer._reject_permanent_to_dlq = _reject
        drainer._apply_pending = _apply
        return drainer

    def _write(self, tmp_path, op, payload):
        import json

        p = tmp_path / f"{op}.json"
        p.write_text(json.dumps({"op": op, "payload": payload}))
        return p

    def test_memorize_without_project_id_is_rejected(self, tmp_path):
        recorded: list = []
        drainer = self._drainer(tmp_path, recorded)
        path = self._write(tmp_path, "memorize", {"content": "x", "context": "/d"})
        drainer._process_pending_file(path, 0.0)
        assert recorded and recorded[0][0] == "memorize"
        assert recorded[0][1] is not None
        assert recorded[0][1].startswith("missing_project_id")

    def test_anchor_carrying_the_global_sentinel_is_rejected(self, tmp_path):
        recorded: list = []
        drainer = self._drainer(tmp_path, recorded)
        path = self._write(tmp_path, "anchor", {"content": "x", "project_id": "global"})
        drainer._process_pending_file(path, 0.0)
        assert recorded[0][1] is not None
        assert "anchor" in recorded[0][1]

    def test_a_stamped_memorize_still_applies(self, tmp_path):
        recorded: list = []
        drainer = self._drainer(tmp_path, recorded)
        path = self._write(tmp_path, "memorize", {"content": "x", "project_id": "a/b"})
        assert drainer._process_pending_file(path, 0.0) == 1
        assert recorded == [("memorize", None)]
