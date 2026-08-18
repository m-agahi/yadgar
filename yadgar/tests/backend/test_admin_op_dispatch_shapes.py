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


# ── task 193: every op the CORE forwards must be REGISTERED ──────────────────
#
# The complement of test 1 above. That test asks "is every registered op
# callable?"; this one asks "is every op the core calls actually registered?".
# Both halves are needed, and only this half catches a tool that is fully
# implemented, exported and documented but absent from ``_ADMIN_OPS``:
# ``run_admin_op`` raises ``KeyError`` and the route maps it to a bare HTTP 400
# with no logged reason, so the tool looks merely broken rather than unwired.
#
# That is exactly how ``wiki_set_mutability`` shipped dead. It is the sole
# documented escape hatch for the Car J mutability gate, which guards
# insert/update/delete on ``page_type='adr'`` pages — so with the op missing,
# the entire ADR corpus was uneditable and undeletable, and the gate's own
# error text pointed at a tool that returned 400. Verified live 2026-08-18.
#
# Derived EMPIRICALLY from the source, so a tool added later with no
# registration fails here rather than being discovered in production.


def _forwarded_op_names() -> dict[str, set[str]]:
    """Resolve every ``_forward_admin`` op name reachable under ``yadgar/core``.

    Handles the two real call shapes: a string literal, and a module-level
    ``_NAME_OP = "op"`` constant passed by name (``tools/task.py``,
    ``backup/quiesce.py``). ``staleness.py`` forwards a *parameter*, which no
    static scan can resolve; its ops are covered by their own call sites.
    """
    import pathlib  # noqa: PLC0415
    import re  # noqa: PLC0415

    literal = re.compile(r'_forward_admin\(\s*"([a-z_0-9]+)"')
    by_const = re.compile(r"_forward_admin\(\s*([A-Z_][A-Z_0-9]*)\s*,")
    const_def = re.compile(r'^([A-Z_][A-Z_0-9]*)\s*=\s*"([a-z_0-9]+)"', re.M)

    core = pathlib.Path(__file__).resolve().parents[2] / "core"
    found: dict[str, set[str]] = {}
    for path in core.rglob("*.py"):
        src = path.read_text()
        consts = dict(const_def.findall(src))
        names = list(literal.findall(src))
        names += [consts[c] for c in by_const.findall(src) if c in consts]
        for op in names:
            found.setdefault(op, set()).add(path.name)
    return found


def test_every_forwarded_op_is_registered() -> None:
    """No ``_forward_admin`` call may name an op absent from ``_ADMIN_OPS``."""
    forwarded = _forwarded_op_names()
    # Guard the guard: if the scan resolves nothing the assertion below is
    # vacuous, which is the failure mode that lets this rot silently.
    assert len(forwarded) > 50, f"scan resolved only {len(forwarded)} ops — regex rotted"

    missing = {
        op: sorted(files) for op, files in forwarded.items() if op not in admin_exec._ADMIN_OPS
    }
    assert not missing, (
        "core forwards admin ops that are not registered — each raises "
        f"KeyError -> HTTP 400 at runtime: {missing}"
    )


def test_wiki_set_mutability_is_registered_and_callable() -> None:
    """The Car J mutability escape hatch is reachable through the dispatch.

    Pinned by name as well as by the sweep above: this op is what stands
    between an operator and a permanently immutable ADR corpus, so a
    regression here should name itself rather than appear as a dict diff.
    """
    impl = admin_exec._ADMIN_OPS.get("wiki_set_mutability")
    assert impl is not None, "wiki_set_mutability missing from _ADMIN_OPS"

    # Reachable through the real sync dispatcher, reaching its own body rather
    # than dying in the adapter. A WikiStore stub stands in because the impl
    # asserts one is initialised before it validates anything.
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    seen: dict = {}

    class _StubWiki:
        def set_mutability_by_slug(self, slug, value, *, reason):
            seen.update(slug=slug, value=value, reason=reason)
            return {"ok": True, "slug": slug, "rows_updated": 1, "page_ids": [7]}

    with (
        patch.object(admin_exec, "_ensure_engines", lambda: None),
        patch.object(_st, "_wiki", _StubWiki()),
    ):
        # Empty reason is the documented rejection (D26 audit-log requirement),
        # and it must be refused BEFORE the storage write.
        rejected = admin_exec.run_admin_op(
            "wiki_set_mutability", {"slug": "zz-probe", "value": "free", "reason": ""}
        )
        assert rejected["ok"] is False
        assert "reason is required" in rejected["error"]
        assert seen == {}, "rejected call must not reach the storage writer"

        # The real path: payload reaches the sole writer with all three fields.
        accepted = admin_exec.run_admin_op(
            "wiki_set_mutability",
            {"slug": "zz-probe", "value": "free", "reason": "task 193 pin"},
        )

    assert accepted["ok"] is True
    assert seen == {"slug": "zz-probe", "value": "free", "reason": "task 193 pin"}
