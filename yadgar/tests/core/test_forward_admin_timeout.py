"""Per-op timeout override in ``_forward_admin`` (backend 5.30.1).

``check_invariants`` walks every memory + wiki row backend-side and takes
33-34s on the production dataset — the flat 30s httpx default guaranteed the
core→backend forward timed out while the backend kept burning CPU. Slow admin
ops get a per-op timeout FLOOR (``max(caller, floor)``); everything else keeps
the fast 30s default.
"""

from __future__ import annotations

import httpx
import pytest

import yadgar.core.forward as fwd


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"result": {"ok": True}}


@pytest.fixture()
def captured_post(monkeypatch):
    """Patch httpx.post to capture the timeout kwarg without any network I/O."""
    monkeypatch.setenv("YADGAR_EMBED_URL", "http://backend.test:8001")
    captured: dict = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", _fake_post)
    return captured


def test_check_invariants_gets_slow_op_timeout(captured_post):
    """check_invariants forwarded with >=120s timeout despite 30s default."""
    fwd._forward_admin("check_invariants", {})
    assert captured_post["timeout"] == 120.0


def test_fast_ops_keep_default_30s(captured_post):
    """Ops without a slow-op entry keep the 30s default."""
    fwd._forward_admin("bookmark_add", {"slug": "x", "label_override": ""})
    assert captured_post["timeout"] == 30.0


def test_explicit_caller_timeout_above_floor_wins(captured_post):
    """An explicit caller timeout larger than the floor is preserved."""
    fwd._forward_admin("check_invariants", {}, timeout_s=600.0)
    assert captured_post["timeout"] == 600.0


def test_explicit_large_timeout_on_fast_op_preserved(captured_post):
    """reembed_all-style explicit long timeouts pass through unchanged."""
    fwd._forward_admin("reembed_all", {}, timeout_s=1800.0)
    assert captured_post["timeout"] == 1800.0
