"""Car C / F2 — heat_updated SSE emit + backend->core event-relay.

Verified bug (2026-07-16, import-trace): heat decay + write-path event pushes run
in the BACKEND process; the ``/api/graph/events`` SSE stream is served by CORE and
reads only core's process-local ``_event_queue``.  Backend-pushed events
(``memory_added``/``wiki_added``/``heat_updated``) therefore never reach a browser.

Fix — relay option (a):
  * backend ``_op_events`` viz op returns ring-buffer entries with ``seq > since``.
  * core polls the backend op each SSE loop iteration and re-pushes new backend
    events into core's own queue (re-stamping seq via ``_push_event``).
  * ``_apply_decay`` emits ONE ``heat_updated`` event with typed ids
    (``mem:N`` + ``entity:N``) built from the reconciled heat intents.

These tests cover the three layers that ARE testable in-harness (op / emit /
merge).  The real browser-SSE path is a user smoke-check (no-browser-harness
convention) — BC-VZ-F2 stays ⏳ until that is driven end-to-end.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import yadgar._shared.runtime.state as _st

# ---------------------------------------------------------------------------
# Helpers (mirror test_heat_single_writer.py)
# ---------------------------------------------------------------------------


def _make_mem(mid: int, heat: float, hours_old: float = 100.0) -> dict:
    last = (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    return {
        "id": mid,
        "heat": heat,
        "last_accessed": last,
        "last_decay_at": None,
        "is_protected": False,
        "access_count_since_decay": 0,
        "tags": [],
        "importance": 0.5,
        "emotional_valence": 0.0,
        "confidence": 0.5,
    }


def _make_entity(eid: int, heat: float, hours_old: float = 100.0) -> dict:
    last = (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    return {"id": eid, "heat": heat, "last_accessed": last, "last_decay_at": None}


def _make_runner(memories: list[dict], entities: list[dict], cold: float = 0.0):
    mock_storage = MagicMock()
    mock_storage.get_all_memories_for_decay_scalar.return_value = memories
    mock_storage.get_all_entities_for_decay.return_value = entities
    mock_storage.get_astrocyte_processes.return_value = []
    mock_storage.batch_writes = MagicMock()

    def compute_decay(mem: dict, hours: float) -> float:
        # strong decay so heat actually changes for a 100h-old memory
        return mem["heat"] * (0.99**hours)

    mock_thermo = MagicMock()
    mock_thermo.compute_decay.side_effect = compute_decay

    mock_settings = MagicMock()
    mock_settings.COLD_THRESHOLD = cold
    mock_settings.ACTION_STREAM_COLD_THRESHOLD = cold
    mock_settings.RECALL_BOOST = 0.0
    mock_settings.DECAY_FACTOR = 0.99
    mock_settings.ASTROCYTE_POOL_ENABLED = False

    from yadgar.backend.consolidation.heat_decay import _HeatDecayMixin

    class _Runner(_HeatDecayMixin):
        pass

    runner = _Runner.__new__(_Runner)
    runner._storage = mock_storage
    runner._thermo = mock_thermo
    runner._settings = mock_settings
    return runner


def _fresh_stats() -> dict:
    return {"memories_archived": 0, "memories_updated": 0}


# ---------------------------------------------------------------------------
# Layer 1 — _apply_decay emits ONE heat_updated event
# ---------------------------------------------------------------------------


class TestApplyDecayEmitsHeatUpdated:
    def test_emits_one_heat_updated_with_typed_ids(self, monkeypatch):
        pushed: list[dict] = []
        monkeypatch.setattr(
            "yadgar.backend.consolidation.heat_decay._push_event",
            lambda ev: pushed.append(ev),
        )
        runner = _make_runner(
            [_make_mem(1, 0.9), _make_mem(2, 0.8)],
            [_make_entity(7, 0.6)],
        )
        runner._apply_decay(_fresh_stats())

        heat_events = [e for e in pushed if e.get("event") == "heat_updated"]
        assert len(heat_events) == 1, f"expected exactly one heat_updated, got {pushed}"
        updates = heat_events[0]["updates"]
        ids = {u["id"] for u in updates}
        assert "mem:1" in ids and "mem:2" in ids, ids
        assert "entity:7" in ids, ids
        # heat values are the reconciled/persisted new_heat, not the old heat
        for u in updates:
            assert isinstance(u["heat"], float)
            assert 0.0 <= u["heat"] < 0.9

    def test_no_event_when_nothing_changed(self, monkeypatch):
        pushed: list[dict] = []
        monkeypatch.setattr(
            "yadgar.backend.consolidation.heat_decay._push_event",
            lambda ev: pushed.append(ev),
        )
        # zero memories + zero entities → no intents → no push
        runner = _make_runner([], [])
        runner._apply_decay(_fresh_stats())
        assert [e for e in pushed if e.get("event") == "heat_updated"] == []

    def test_heat_updated_pushed_after_apply(self, monkeypatch):
        """The push must reference persisted values — it fires after apply_heat_intents."""
        order: list[str] = []
        runner = _make_runner([_make_mem(1, 0.9)], [])
        runner._storage.batch_writes.side_effect = lambda *_a, **_k: order.append("apply")
        monkeypatch.setattr(
            "yadgar.backend.consolidation.heat_decay._push_event",
            lambda ev: order.append("push") if ev.get("event") == "heat_updated" else None,
        )
        runner._apply_decay(_fresh_stats())
        assert order == ["apply", "push"], order


# ---------------------------------------------------------------------------
# Layer 2 — backend _op_events returns entries with seq > since
# ---------------------------------------------------------------------------


class TestBackendEventsOp:
    def test_events_op_registered(self):
        from yadgar.backend.viz_exec import viz_ops

        assert "events" in viz_ops()

    def test_returns_entries_after_since(self, monkeypatch):
        from yadgar.backend.viz_exec import run_viz_op

        # Seed the backend ring buffer deterministically.
        monkeypatch.setattr(_st, "_event_queue", deque(maxlen=500))
        monkeypatch.setattr(_st, "_event_seq", 0)
        import yadgar._shared.server_helpers.server_helpers as _sh

        _sh._push_event({"event": "memory_added", "node": {"id": "mem:1"}})
        _sh._push_event({"event": "heat_updated", "updates": [{"id": "mem:1", "heat": 0.5}]})
        _sh._push_event({"event": "wiki_added", "node": {"id": "wiki:foo"}})

        result = run_viz_op("events", {"since": 1})
        events = result["events"]
        assert [e["seq"] for e in events] == [2, 3]
        assert result["latest_seq"] == 3

    def test_since_at_head_returns_empty(self, monkeypatch):
        from yadgar.backend.viz_exec import run_viz_op

        monkeypatch.setattr(_st, "_event_queue", deque(maxlen=500))
        monkeypatch.setattr(_st, "_event_seq", 0)
        import yadgar._shared.server_helpers.server_helpers as _sh

        _sh._push_event({"event": "memory_added", "node": {}})
        result = run_viz_op("events", {"since": 1})
        assert result["events"] == []
        assert result["latest_seq"] == 1


# ---------------------------------------------------------------------------
# Layer 3 — core relay re-stamps backend seqs onto core's queue
# ---------------------------------------------------------------------------


class TestCoreRelayRestamp:
    def test_relay_restamps_and_dedups(self, monkeypatch):
        # Core's own queue starts empty; one native event lands at core-seq 1.
        monkeypatch.setattr(_st, "_event_queue", deque(maxlen=500))
        monkeypatch.setattr(_st, "_event_seq", 0)
        # reset the process-global backend cursor for a clean test
        monkeypatch.setattr(_st, "_backend_event_cursor", -1, raising=False)

        import yadgar._shared.server_helpers.server_helpers as _sh

        _sh._push_event({"event": "memory_added", "node": {"id": "mem:native"}})

        # Fake backend responses: backend seqs start high (100, 101) — the collision trap.
        from yadgar.core.server import http as _http

        calls = {"n": 0}

        def fake_forward(op, payload, timeout_s=60.0):
            assert op == "events"
            since = payload["since"]
            if calls["n"] == 0:
                calls["n"] += 1
                # first poll: seed to backend max, return nothing (avoid stale flood)
                return {"events": [], "latest_seq": 99}
            if since >= 101:
                return {"events": [], "latest_seq": 101}
            return {
                "events": [
                    {"seq": 100, "event": "memory_added", "node": {"id": "mem:b1"}},
                    {
                        "seq": 101,
                        "event": "heat_updated",
                        "updates": [{"id": "mem:b1", "heat": 0.3}],
                    },
                ],
                "latest_seq": 101,
            }

        monkeypatch.setattr(_http, "_forward_viz", fake_forward)

        # First poll seeds the cursor (no re-push).
        _http._poll_backend_events()
        assert len(list(_st._event_queue)) == 1  # only the native event so far

        # Second poll pulls backend events 100/101 and re-stamps them onto core's queue.
        _http._poll_backend_events()
        seqs = [e["seq"] for e in _st._event_queue]
        # Core-monotonic: native=1, relayed=2,3 — NOT 100/101 (advisor catch #1).
        assert seqs == [1, 2, 3], seqs
        relayed = [e for e in _st._event_queue if e["seq"] in (2, 3)]
        assert relayed[0]["event"] == "memory_added"
        assert relayed[1]["event"] == "heat_updated"
        # backend seq must NOT leak into the re-stamped events
        assert all("seq" in e for e in relayed)
        assert relayed[1]["updates"] == [{"id": "mem:b1", "heat": 0.3}]

        # Third poll: cursor already at 101 → no duplicate re-push.
        _http._poll_backend_events()
        assert [e["seq"] for e in _st._event_queue] == [1, 2, 3]
