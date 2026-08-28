"""Task 408 — the nightly audit sentinel must carry the coverage block.

Task 391 gave ``audit_anchors`` a ``coverage`` block: the scan selector is
``'_anchor' INSIDE tags AND directory_context = $dir``, which is NARROWER than
"this project's protected rows", and the tool used to report only the narrow
number (``scanned: 95`` against 102 protected rows on the live corpus).

That fix reached the INTERACTIVE path only. ``_run_anchor_audit_pass`` — the
unattended consolidation pass — forwards the whole ``audit_anchors`` result to
the backend ``write_audit_sentinel`` op, and that op re-serialised only
``{actions, scanned, _truncated, audited_at}``. The coverage block was computed,
shipped across the boundary, and dropped on the floor. The nightly sentinel
therefore recorded the exact bare ``scanned`` number task 391 exists to qualify,
on the path nobody watches.

TDD: RED against the four-key serialiser, GREEN once ``coverage`` is carried
through.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

_COVERAGE = {
    "scanned": 95,
    "scanned_protected": 95,
    "protected_total": 102,
    "unscanned": 7,
    "unscanned_reasons": {"no_anchor_tag": 6, "directory_context_mismatch": 1},
    "unscanned_sample": {"no_anchor_tag": [1, 2, 3]},
    "scope_keys": {"directory_context": ["/repo"], "project_id": "m-agahi/yadgar"},
}


def _storage_mock() -> MagicMock:
    storage = MagicMock()
    storage._now_iso.return_value = "2026-08-28T00:00:00Z"
    storage._next_id.return_value = 42
    return storage


def _written_content(storage: MagicMock) -> dict:
    """Parse the sentinel JSON out of the CREATE call's bind params."""
    for call in storage._q.call_args_list:
        params = call.args[1] if len(call.args) > 1 else None
        if isinstance(params, dict) and "content" in params:
            return json.loads(params["content"])
    raise AssertionError("no CREATE call with a `content` bind param was made")


class TestSentinelCarriesCoverage:
    def test_coverage_block_is_recorded(self, monkeypatch):
        """The coverage block audit_anchors computed reaches the sentinel intact."""
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.audit import write_audit_sentinel

        storage = _storage_mock()
        monkeypatch.setattr(_st, "_storage", storage)

        result = write_audit_sentinel(
            {
                "directory": "/repo",
                "audit_result": {
                    "actions": [],
                    "scanned": 95,
                    "coverage": _COVERAGE,
                },
            }
        )

        assert result == {"written": True}
        content = _written_content(storage)
        assert content["coverage"] == _COVERAGE, (
            "the nightly sentinel must record WHY the scan missed rows, not "
            "just the bare `scanned` count task 391 qualified"
        )

    def test_existing_keys_still_present(self, monkeypatch):
        """Additive only — the pre-391 sentinel shape is a subset of the new one."""
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.audit import write_audit_sentinel

        storage = _storage_mock()
        monkeypatch.setattr(_st, "_storage", storage)

        write_audit_sentinel(
            {
                "directory": "/repo",
                "audit_result": {
                    "actions": [{"action": "forget_expired", "id": 1}],
                    "scanned": 95,
                    "_truncated": True,
                    "coverage": _COVERAGE,
                },
            }
        )

        content = _written_content(storage)
        assert content["actions"] == [{"action": "forget_expired", "id": 1}]
        assert content["scanned"] == 95
        assert content["_truncated"] is True
        assert content["audited_at"] == "2026-08-28T00:00:00Z"

    def test_absent_coverage_says_so_rather_than_going_quiet(self, monkeypatch):
        """A payload with no coverage records that fact — it does not omit the key.

        Silently dropping the key here would reproduce the defect one level up:
        a reader of the sentinel could not tell "coverage was not computed" from
        "this build predates task 391". One marker key, not a taxonomy.
        """
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.audit import write_audit_sentinel

        storage = _storage_mock()
        monkeypatch.setattr(_st, "_storage", storage)

        write_audit_sentinel({"directory": "/repo", "audit_result": {"actions": [], "scanned": 3}})

        content = _written_content(storage)
        assert "coverage" in content
        assert content["coverage"] == {"error": "coverage absent from audit result"}
