"""C10 (0047 §5(d)) — EVERY registered admin op is actually callable by the dispatch.

Both dispatchers call ``impl(payload)`` — a single positional dict. Four
registered bodies were declared keyword-only (``def f(*, a, b)``) or required an
injected ``storage``, so that call raised ``TypeError`` **every time**:

    reslug, retype_page_type, seed_adr_rows, seed_task_from_pages

(``seed_task_from_pages`` was later deleted outright — not idempotent, see
``docs/CHANGELOG.md`` — so only the three surviving ops are pinned below.)

They had never executed through ``/admin``. ``retype_page_type`` is D23's "sole
sanctioned writer" for the ADR supersede lifecycle transition, so that
transition had never run through this route either.

The bug is a DISPATCH-SHAPE mismatch, which means only an invocation test
catches it — a registration test that merely asserts the key exists passes
happily while the op is unreachable. Test 1 below is therefore derived
EMPIRICALLY from the live table rather than from a hand-maintained list, so a
keyword-only op added later fails here instead of silently becoming unreachable.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from yadgar.backend import admin_exec

#: The ops C10 adapted that still exist. Named so a regression that drops an
#: adapter is reported against the specific op rather than as an opaque count.
_ADAPTED_OPS = ("reslug", "retype_page_type", "seed_adr_rows")


def test_every_registered_op_binds_to_the_dispatch_call_shape() -> None:
    """No registered op may reject ``impl(payload)``.

    This is the property the four broken ops violated. Derived from the live
    ``_ADMIN_OPS`` table, so it also covers ops added after C10.
    """
    unbindable: list[str] = []
    for name, impl in sorted(admin_exec._ADMIN_OPS.items()):
        try:
            inspect.signature(impl).bind({})
        except TypeError as exc:
            unbindable.append(f"{name}: {exc}")

    assert not unbindable, (
        "these admin ops cannot be called as impl(payload) and are therefore "
        "unreachable through /admin — wrap them with _kwargs_op or "
        "_payload_storage_op in admin_exec/__init__.py:\n  " + "\n  ".join(unbindable)
    )


@pytest.mark.parametrize("op_name", _ADAPTED_OPS)
def test_adapted_op_is_registered_and_bindable(op_name: str) -> None:
    """Each op C10 adapted is still present and still accepts a payload dict."""
    assert op_name in admin_exec._ADMIN_OPS, f"{op_name} vanished from the registry"
    inspect.signature(admin_exec._ADMIN_OPS[op_name]).bind({})


def test_kwargs_adapter_explodes_payload_into_keywords() -> None:
    """``_kwargs_op`` maps payload keys onto a keyword-only body."""
    seen: dict = {}

    def body(*, alpha: str, beta: int) -> dict:
        seen.update(alpha=alpha, beta=beta)
        return {"ok": True}

    adapted = admin_exec._kwargs_op(body)
    assert adapted({"alpha": "a", "beta": 2}) == {"ok": True}
    assert seen == {"alpha": "a", "beta": 2}


def test_kwargs_adapter_preserves_coroutine_detection() -> None:
    """An async body stays async through the adapter.

    Load-bearing: ``_is_async_op`` inspects the wrapper's own code flags. A sync
    wrapper around a coroutine body would report False, and ``run_admin_op``
    would return an un-awaited coroutine object as if it were the result dict.
    """

    async def abody(*, alpha: str) -> dict:
        return {"alpha": alpha}

    adapted = admin_exec._kwargs_op(abody)
    assert admin_exec._is_async_op(adapted) is True
    assert asyncio.run(adapted({"alpha": "x"})) == {"alpha": "x"}


def test_kwargs_adapter_does_not_mask_the_wrapped_signature() -> None:
    """The adapter must NOT use functools.wraps.

    ``functools.wraps`` copies ``__wrapped__``, which makes ``inspect.signature``
    report the wrapped keyword-only signature — hiding the exact mismatch test 1
    exists to detect and turning that test vacuous.
    """

    def body(*, alpha: str) -> dict:
        return {}

    adapted = admin_exec._kwargs_op(body)
    inspect.signature(adapted).bind({})  # would raise if the signature leaked through


def test_payload_storage_adapter_injects_runtime_storage() -> None:
    """``_payload_storage_op`` passes the payload positionally and injects storage."""
    sentinel = object()
    seen: dict = {}

    def body(payload: dict, *, storage) -> dict:
        seen.update(payload=payload, storage=storage)
        return {"ok": True}

    adapted = admin_exec._payload_storage_op(body)
    with patch.object(admin_exec, "_resolve_storage", lambda: sentinel):
        assert adapted({"dry_run": True}) == {"ok": True}
    assert seen == {"payload": {"dry_run": True}, "storage": sentinel}


def test_seed_adr_rows_dispatches_async_not_sync() -> None:
    """``seed_adr_rows`` is an async body; the sync dispatcher must refuse it.

    Pins that the adapter did not accidentally flatten it to sync, which would
    hand a coroutine object back to the caller as a result dict.
    """
    assert admin_exec._is_async_op(admin_exec._ADMIN_OPS["seed_adr_rows"]) is True
    with patch.object(admin_exec, "_ensure_engines", lambda: None):
        with pytest.raises(TypeError, match="dispatch it via run_admin_op_async"):
            admin_exec.run_admin_op("seed_adr_rows", {})
