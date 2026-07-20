"""#30 gauge car — wire the dead yadgar_subagent_capture_rate gauge + a volume counter.

TDD — written BEFORE the wiring.

Context: yadgar_subagent_capture_rate is a Gauge that is .set(0) at import and NEVER
updated (metrics.py). The live DB showed 0 `from-subagent` memories — capture was
unmeasured. This test drives:

1. A new Counter yadgar_subagent_captures_total that increments by the number of
   findings bullets stored on each /hooks/subagent-stop POST (raw volume — 0-vs-N
   unambiguous).
2. A new Counter yadgar_subagent_stop_posts_total that increments once per POST that
   arrived with a non-empty findings list (distinguishes "arrived with 0 bullets"
   from "never arrived").
3. The previously-dead gauge yadgar_subagent_capture_rate moving OFF 0 after a POST
   with N>0 bullets (last-batch captured count semantic).

Registry is a global singleton — assert deltas, not absolute values (mirrors
test_embed_llm_viz_log_metrics.py delta pattern). Request-mock template from
test_v5_46_9_subagent_stop_findings.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_158_subagent_capture")
    server.init_engines(
        db_path=str(tmp_path / "test_capture.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _make_request(body: bytes):
    """Minimal ASGI-compatible mock Request for hook_subagent_stop."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/hooks/subagent-stop",
        "query_string": b"",
        "headers": Headers({"content-type": "application/json"}).raw,
    }

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, _receive)


def _counter_value(counter) -> float:
    """Read a no-label Counter's current value (prometheus_client internals)."""
    return counter._value.get()


def _gauge_value(gauge) -> float:
    return gauge._value.get()


def _fake_memorize(
    content,
    context,
    tags,
    is_protected=False,
    provenance_agent="default",
    branch_hint=None,
):
    return {"stored": True, "queued": True, "queue_id": "test-q"}


class TestSubagentCaptureMetric:
    """Capture volume must be observable on each /hooks/subagent-stop POST."""

    def test_captures_total_increments_by_bullet_count(self):
        import yadgar.core.server.http as _http
        from yadgar._shared.observability.metrics import (
            yadgar_subagent_capture_rate,
            yadgar_subagent_captures_total,
            yadgar_subagent_stop_posts_total,
        )

        cap_before = _counter_value(yadgar_subagent_captures_total)
        posts_before = _counter_value(yadgar_subagent_stop_posts_total)

        _srv = sys.modules.get("yadgar.core.server")
        with patch.object(_srv, "memorize", _fake_memorize, create=True):
            body = json.dumps(
                {
                    "agent_type": "general-purpose",
                    "cwd": "/tmp/proj",
                    "findings": [
                        "fact: migration 005 adds column",
                        "anchor: some-slug — detail",
                        "gotcha: third bullet",
                    ],
                }
            ).encode()
            resp = asyncio.run(_http.hook_subagent_stop(_make_request(body)))
            data = json.loads(resp.body)

        assert data["stored"] == 3

        cap_after = _counter_value(yadgar_subagent_captures_total)
        posts_after = _counter_value(yadgar_subagent_stop_posts_total)

        assert cap_after - cap_before == 3, (
            f"captures_total should rise by 3, rose by {cap_after - cap_before}"
        )
        assert posts_after - posts_before == 1, (
            f"posts_total should rise by 1, rose by {posts_after - posts_before}"
        )
        # Dead gauge must move OFF 0 — last-batch captured count.
        assert _gauge_value(yadgar_subagent_capture_rate) == 3, (
            "capture_rate gauge must reflect the last batch's captured count (3)"
        )

    def test_empty_findings_does_not_move_volume_metrics(self):
        import yadgar.core.server.http as _http
        from yadgar._shared.observability.metrics import (
            yadgar_subagent_captures_total,
            yadgar_subagent_stop_posts_total,
        )

        cap_before = _counter_value(yadgar_subagent_captures_total)
        posts_before = _counter_value(yadgar_subagent_stop_posts_total)

        _srv = sys.modules.get("yadgar.core.server")
        with patch.object(_srv, "memorize", _fake_memorize, create=True):
            body = json.dumps(
                {"agent_type": "general-purpose", "cwd": "/tmp/proj", "findings": []}
            ).encode()
            resp = asyncio.run(_http.hook_subagent_stop(_make_request(body)))
            data = json.loads(resp.body)

        assert data["stored"] == 0
        # Empty POST short-circuits before the memorize loop → no volume counted.
        assert _counter_value(yadgar_subagent_captures_total) - cap_before == 0
        assert _counter_value(yadgar_subagent_stop_posts_total) - posts_before == 0
