"""Engine-#2 car B — the admin dispatcher accepts SYNC *and* ASYNC op bodies.

Why the car exists: every backend admin op body is a plain sync function that the
/admin route dispatches via ``asyncio.to_thread``. ``asyncmy`` is an async-only
driver, so a MariaDB op cannot be awaited from inside a worker thread without a
per-call private event loop. ``run_admin_op_async`` adds the second shape WITHOUT
converting a single existing op.

These tests pin the four properties the change must not get wrong:

1. a SYNC op still runs OFF the event loop (asserted by thread identity, not by
   spying on ``asyncio.to_thread`` — spying tests the call, not the property);
2. an ASYNC op is awaited, ON the loop thread, and returns its value;
3. an ASYNC op's exception propagates with the same type/args as a SYNC op's, so
   the two paths cannot drift in error shape;
4. coroutine detection survives the decorator stack real ops carry (``@observe``).

No live DB and no engine construction: ``_ensure_engines`` is stubbed, and the
dispatch table is patched per-test.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import contextmanager
from typing import Any

import pytest

from yadgar._shared.observability.observe import observe
from yadgar.backend import admin_exec
from yadgar.backend.admin_exec import run_admin_op, run_admin_op_async


@contextmanager
def _registered(op_name: str, impl: Any):
    """Register *impl* under *op_name* with engine composition stubbed out."""
    from unittest.mock import patch

    with (
        patch.dict(admin_exec._ADMIN_OPS, {op_name: impl}),
        patch.object(admin_exec, "_ensure_engines", lambda: None),
    ):
        yield


# ---------------------------------------------------------------------------
# 1. The sync path must not regress: still dispatched, still OFF the loop.
# ---------------------------------------------------------------------------
async def test_sync_op_still_runs_in_a_worker_thread() -> None:
    """A SYNC op body still executes on a thread that is NOT the loop thread."""
    seen: dict[str, int] = {}

    def _sync_op(payload: dict) -> dict:
        seen["thread"] = threading.get_ident()
        return {"echo": payload["v"]}

    with _registered("car_b_sync", _sync_op):
        result = await run_admin_op_async("car_b_sync", {"v": 7})

    assert result == {"echo": 7}
    assert "thread" in seen, "the sync op body never ran"
    assert seen["thread"] != threading.get_ident(), (
        "a sync op body ran ON the event-loop thread — the asyncio.to_thread "
        "property that keeps storage IO off the loop has regressed"
    )


async def test_sync_op_dispatch_matches_direct_run_admin_op() -> None:
    """The async entry point returns exactly what the sync entry point returns."""

    def _sync_op(payload: dict) -> dict:
        return {"n": payload["n"] * 2}

    with _registered("car_b_sync", _sync_op):
        direct = run_admin_op("car_b_sync", {"n": 3})
        via_async = await run_admin_op_async("car_b_sync", {"n": 3})

    assert direct == via_async == {"n": 6}


# ---------------------------------------------------------------------------
# 2. The async path: awaited on the loop, value returned.
# ---------------------------------------------------------------------------
async def test_async_op_is_awaited_and_returns_its_value() -> None:
    """An ASYNC op body is awaited directly and its return value is passed through."""
    seen: dict[str, int] = {}

    async def _async_op(payload: dict) -> dict:
        await asyncio.sleep(0)
        seen["thread"] = threading.get_ident()
        return {"echo": payload["v"]}

    with _registered("car_b_async", _async_op):
        result = await run_admin_op_async("car_b_async", {"v": "x"})

    assert result == {"echo": "x"}
    assert seen["thread"] == threading.get_ident(), (
        "an async op body must be awaited ON the event loop — running it in a "
        "worker thread is precisely what asyncmy cannot tolerate"
    )


async def test_async_op_result_is_not_a_coroutine() -> None:
    """The dispatcher must not leak an un-awaited coroutine to the caller."""

    async def _async_op(payload: dict) -> dict:
        return {"ok": True}

    with _registered("car_b_async", _async_op):
        result = await run_admin_op_async("car_b_async", {})

    assert not inspect.isawaitable(result)
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# 3. Error shape must not diverge between the two paths.
# ---------------------------------------------------------------------------
async def test_async_op_error_propagates_like_a_sync_op_error() -> None:
    """A raise from an async body surfaces with the same type + args as a sync one."""

    def _sync_boom(payload: dict) -> dict:
        raise ValueError("boom", 42)

    async def _async_boom(payload: dict) -> dict:
        raise ValueError("boom", 42)

    with _registered("car_b_sync", _sync_boom):
        with pytest.raises(ValueError) as sync_exc:
            await run_admin_op_async("car_b_sync", {})

    with _registered("car_b_async", _async_boom):
        with pytest.raises(ValueError) as async_exc:
            await run_admin_op_async("car_b_async", {})

    assert type(async_exc.value) is type(sync_exc.value)
    assert async_exc.value.args == sync_exc.value.args == ("boom", 42)


async def test_unknown_op_still_raises_keyerror() -> None:
    """Unknown ops keep raising KeyError — the /admin route maps it to 400."""
    with pytest.raises(KeyError):
        await run_admin_op_async("car_b_does_not_exist", {})


def test_sync_entry_point_rejects_an_async_op() -> None:
    """run_admin_op refuses a coroutine op loudly instead of returning a coroutine.

    The in-process test bypasses (conftest ``_forward_admin`` → ``run_admin_op``)
    call the sync entry point directly; silently handing them an un-awaited
    coroutine would be a far worse failure than a TypeError.
    """

    async def _async_op(payload: dict) -> dict:
        return {}

    with _registered("car_b_async", _async_op):
        with pytest.raises(TypeError, match="async"):
            run_admin_op("car_b_async", {})


# ---------------------------------------------------------------------------
# 4. Detection through the decorator stack real ops carry.
# ---------------------------------------------------------------------------
def test_iscoroutinefunction_survives_the_observe_decorator() -> None:
    """@observe on an ``async def`` must still answer True to iscoroutinefunction.

    ``observe._build_wrapper`` branches on the ORIGINAL function and returns a
    genuine ``async def`` wrapper; ``functools.wraps`` copies metadata without
    touching ``__code__``. If this ever regresses, the dispatcher would silently
    push an async op into a worker thread.
    """

    @observe(tier="boundary", metric="tests.car_b.observed_async_op")
    async def _observed_async_op(payload: dict) -> dict:
        return {"observed": True}

    @observe(tier="boundary", metric="tests.car_b.observed_sync_op")
    def _observed_sync_op(payload: dict) -> dict:
        return {"observed": False}

    assert inspect.iscoroutinefunction(_observed_async_op) is True
    assert inspect.iscoroutinefunction(_observed_sync_op) is False
    assert admin_exec._is_async_op(_observed_async_op) is True
    assert admin_exec._is_async_op(_observed_sync_op) is False


def test_iscoroutinefunction_survives_every_observe_tier() -> None:
    """Every tier real ops use keeps the async-ness visible."""

    @observe(tier="stage", metric="tests.car_b.tier_stage")
    async def _stage(payload: dict) -> dict:
        return {}

    @observe(tier="hot", metric="tests.car_b.tier_hot")
    async def _hot(payload: dict) -> dict:
        return {}

    @observe(tier="hot", metric="tests.car_b.tier_hot_nospan", span=False)
    async def _hot_nospan(payload: dict) -> dict:
        return {}

    assert admin_exec._is_async_op(_stage) is True
    assert admin_exec._is_async_op(_hot) is True
    assert admin_exec._is_async_op(_hot_nospan) is True


def test_iscoroutinefunction_survives_the_exempt_passthrough() -> None:
    """@observe(exempt=...) is a no-op passthrough and must not hide the coroutine."""

    @observe(
        exempt=(
            "test-only shim proving the categorized exempt passthrough keeps a "
            "coroutine function detectable by the dispatcher"
        )
    )
    async def _op(payload: dict) -> dict:
        return {}

    assert admin_exec._is_async_op(_op) is True


async def test_decorated_async_op_dispatches_through_the_async_path() -> None:
    """End-to-end: a REGISTERED, @observe-decorated async op is awaited on the loop."""
    seen: dict[str, int] = {}

    @observe(tier="boundary", metric="tests.car_b.registered_async_op")
    async def _async_op(payload: dict) -> dict:
        seen["thread"] = threading.get_ident()
        return {"v": payload["v"]}

    with _registered("car_b_async", _async_op):
        result = await run_admin_op_async("car_b_async", {"v": 1})

    assert result == {"v": 1}
    assert seen["thread"] == threading.get_ident()


# ---------------------------------------------------------------------------
# 5. The route is the deliverable's caller (plan §5 acceptance rule).
# ---------------------------------------------------------------------------
async def test_admin_route_awaits_an_async_op() -> None:
    """POST /admin's handler dispatches an async op body and wraps its result."""
    from unittest.mock import patch

    import yadgar.backend.embed_service.embed_service as _es  # noqa: PLC0415
    from yadgar.backend.embed_service.embed_service_routes import admin_route

    async def _async_op(payload: dict) -> dict:
        return {"handled": payload["v"]}

    req = _es.AdminRequest(op="car_b_async", payload={"v": "route"})

    with (
        _registered("car_b_async", _async_op),
        patch.object(_es, "_ensure_recall_engines", lambda: None),
    ):
        resp = await admin_route(req)

    assert resp.result == {"handled": "route"}


async def test_admin_route_unknown_op_still_400() -> None:
    """An unregistered op keeps mapping to HTTP 400 through the new dispatcher."""
    from unittest.mock import patch

    from fastapi import HTTPException

    import yadgar.backend.embed_service.embed_service as _es  # noqa: PLC0415
    from yadgar.backend.embed_service.embed_service_routes import admin_route

    req = _es.AdminRequest(op="car_b_does_not_exist", payload={})

    with patch.object(_es, "_ensure_recall_engines", lambda: None):
        with pytest.raises(HTTPException) as exc:
            await admin_route(req)

    assert exc.value.status_code == 400
