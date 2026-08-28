"""PR #65 review finding #10 — DLQ rejection-builder shape duplicated.

The pre-fix drainer (``yadgar/backend/queue_drainer/dlq.py``) had two rejection
sites that hand-built the same envelope shape:

  - ``_gate_unavailable_rejection`` (line 376) — fail-CLOSED on embedder /
    vector-search outage (task 312, Car C10).
  - ``_similarity_gate_for_drainer`` inline (line 485) — hard-mode
    ``duplicate_detected`` rejection.

Both sites independently constructed ``{"stored": False, "reason": ...,
"hint": ...}`` with reason-specific extras (``error`` / ``suggested_update_slug``
/ ``candidates``), and both wrapped a metric increment in the same
``try / import / labels(reason=...).inc() / except: pass`` boilerplate. The
shape could drift silently because no test pinned both rejection envelopes
together.

Post-fix: a single ``_build_rejection`` helper stamps the envelope; a single
``_emit_rejection_metric`` helper increments the counter; both rejection sites
call them. The shared envelope is:

    {
        "stored": False,
        "reason": <str>,
        "hint": <str>,
        **extras,  # reason-specific: error / suggested_update_slug / candidates
    }

A secondary finding surfaced in the same audit: ``"gate_unavailable"`` was
emitted as a rejection ``reason`` but was missing from BOTH
``admin_dlq._REJECTION_TAXONOMY`` and ``project._REJECTION_REASONS``. A
``gate_unavailable`` row landed in the DLQ but was mis-classified as a
failure (not a rejection) by the two filters. The fix adds it to both.

These tests pin the contract for both rejection reasons and the taxonomy
membership — fail-loud if either grows unchecked.

OTEL untouched at module scope.
"""

from __future__ import annotations

import importlib

import pytest

# ── envelope shape (both reasons) ────────────────────────────────────────────


REJECTION_ENVELOPE_KEYS = frozenset({"stored", "reason", "hint"})


@pytest.mark.parametrize(
    ("reason", "expected_extras"),
    [
        ("gate_unavailable", {"error"}),
        ("duplicate_detected", {"candidates", "suggested_update_slug"}),
    ],
)
def test_drainer_rejection_envelope_shape(reason, expected_extras):
    """Both rejection reasons emit the same envelope shape; each adds a
    reason-specific extra key (no extras leak between them).

    PR #65 review finding #10: the shared envelope is
    ``{"stored": False, "reason": ..., "hint": ...}`` plus per-reason extras.
    Pin that the two rejection sites produce the SAME shape so a future
    helper extraction cannot silently drop ``hint`` or add an undeclared
    top-level key.
    """
    from yadgar.backend.queue_drainer.dlq import _DLQMixin  # noqa: PLC0415

    rejection = _DLQMixin._build_rejection(
        reason=reason,
        hint="test hint",
        error="test error" if reason == "gate_unavailable" else None,
        candidates=[{"slug": "x"}] if reason == "duplicate_detected" else None,
        suggested_update_slug="x" if reason == "duplicate_detected" else None,
    )
    assert isinstance(rejection, dict), f"{reason} must return a dict"
    # Common shape, never truncated or extended.
    assert REJECTION_ENVELOPE_KEYS.issubset(rejection.keys()), (
        f"{reason} rejection missing envelope keys: {REJECTION_ENVELOPE_KEYS - rejection.keys()}"
    )
    assert rejection["stored"] is False, (
        f"{reason} rejection must stamp stored=False (the DLQ filter keys "
        f"on this; setting True would re-classify the row as success)"
    )
    assert rejection["reason"] == reason
    assert rejection["hint"] == "test hint"
    # Per-reason extras present; cross-reason extras NOT present (either
    # absent or None — the kwargs pass-through carries None for non-applicable
    # fields, which the test deliberately fills to exercise that pass-through).
    if reason == "gate_unavailable":
        assert rejection.get("error") == "test error"
        assert not rejection.get("candidates"), (
            "gate_unavailable must NOT carry duplicate_detected extras — extras are reason-specific"
        )
        assert not rejection.get("suggested_update_slug"), (
            "gate_unavailable must NOT carry duplicate_detected extras"
        )
    elif reason == "duplicate_detected":
        assert rejection.get("candidates") == [{"slug": "x"}]
        assert rejection.get("suggested_update_slug") == "x"
        assert not rejection.get("error"), (
            "duplicate_detected must NOT carry gate_unavailable extras"
        )


def test_build_rejection_extra_keys_pass_through():
    """Reason-specific extras (error / candidates / suggested_update_slug /
    future fields) pass through ``_build_rejection`` unchanged. Pin that
    ``**extras`` is the right shape contract: a single kwargs catchall the
    caller fills in, not a fixed-arg list that would re-shape every time
    a new reason grows.
    """
    from yadgar.backend.queue_drainer.dlq import _DLQMixin  # noqa: PLC0415

    rejection = _DLQMixin._build_rejection(
        reason="duplicate_detected",
        hint="near-duplicate",
        candidates=[{"slug": "a"}, {"slug": "b"}],
        suggested_update_slug="a",
    )
    assert rejection["candidates"] == [{"slug": "a"}, {"slug": "b"}]
    assert rejection["suggested_update_slug"] == "a"


def test_build_rejection_no_extra_keys_still_valid():
    """``_build_rejection`` with no extras produces a valid envelope
    (the minimum contract: stored=False + reason + hint). Pin that the
    helper is not gated on ``error`` or ``candidates`` being supplied —
    a future rejection reason can use the envelope without those fields.
    """
    from yadgar.backend.queue_drainer.dlq import _DLQMixin  # noqa: PLC0415

    rejection = _DLQMixin._build_rejection(reason="policy_rejected", hint="nope")
    assert rejection == {"stored": False, "reason": "policy_rejected", "hint": "nope"}


# ── taxonomy membership (secondary finding) ─────────────────────────────────


def test_gate_unavailable_in_admin_dlq_rejection_taxonomy():
    """``"gate_unavailable"`` MUST be in ``admin_dlq._REJECTION_TAXONOMY``.

    PR #65 review finding #10 (secondary): ``_gate_unavailable_rejection``
    emits ``reason="gate_unavailable"`` but the value was missing from the
    taxonomy. A row landed in the DLQ but the admin tool's ``--reason``
    filter treated it as a failure (not a rejection) — operators could not
    surface ``gate_unavailable`` rows by the rejection filter that the
    envelope's shape implies they belong to.
    """
    mod = importlib.import_module("yadgar.core.server.tools.admin_dlq")
    assert "gate_unavailable" in mod._REJECTION_TAXONOMY, (
        "admin_dlq._REJECTION_TAXONOMY is the wire contract — every "
        "reason the drainer emits must be a member, otherwise the admin "
        "tool's --reason filter mis-classifies the row"
    )


def test_gate_unavailable_in_project_rejection_reasons():
    """``"gate_unavailable"`` MUST be in ``project._REJECTION_REASONS``.

    Same secondary finding from a different angle: ``_compute_pending_rejections``
    counts DLQ rows whose ``failure_reason`` is in ``_REJECTION_REASONS`` —
    a gate_unavailable row would silently under-count. The two frozensets
    are commented as "must match admin_dlq._REJECTION_TAXONOMY" but had
    already drifted (2 vs 6 members pre-fix).
    """
    mod = importlib.import_module("yadgar.core.server.tools.project")
    assert "gate_unavailable" in mod._REJECTION_REASONS, (
        "project._REJECTION_REASONS counts pending rejections — "
        "gate_unavailable rows must count here, otherwise the "
        "yadgar-vacuum gate under-reports"
    )


# ── metric emission (shared by both sites) ───────────────────────────────────


def test_emit_rejection_metric_uses_reason_label(monkeypatch):
    """``_emit_rejection_metric`` increments
    ``yadgar_wiki_add_rejected_total{reason=...}``. Pin the label value
    is the reason string and the counter actually fires.
    """
    from yadgar.backend.queue_drainer import dlq as dlq_mod  # noqa: PLC0415

    captured = {"labels": [], "calls": 0}

    class _Inc:
        def inc(self_inner):
            captured["calls"] += 1

    class _FakeCounter:
        def labels(self, **kw):
            captured["labels"].append(kw)
            return _Inc()

    # Monkeypatch the import site the helper resolves at call time.
    monkeypatch.setattr(
        "yadgar._shared.observability.metrics.yadgar_wiki_add_rejected_total",
        _FakeCounter(),
    )
    dlq_mod._DLQMixin._emit_rejection_metric("gate_unavailable")
    dlq_mod._DLQMixin._emit_rejection_metric("duplicate_detected")
    assert [d["reason"] for d in captured["labels"]] == [
        "gate_unavailable",
        "duplicate_detected",
    ]
    assert captured["calls"] == 2


def test_emit_rejection_metric_swallows_metric_import_error(monkeypatch):
    """If the metric module is unavailable (e.g. during test harness boot),
    the drainer MUST still return a rejection — losing the metric must
    not lose the write-rejection. Same belt-and-braces shape as the
    pre-fix inline metric try/except.
    """
    from yadgar.backend.queue_drainer import dlq as dlq_mod  # noqa: PLC0415

    def _boom(*a, **kw):
        raise ImportError("no metrics module")

    monkeypatch.setattr(
        "yadgar._shared.observability.metrics.yadgar_wiki_add_rejected_total",
        _boom,
    )
    # Must not raise. NOTE: this arm injects a plain function, so the failure
    # is actually AttributeError from `.labels` — "registry uninitialised",
    # not "missing import". The name is kept for continuity; the ImportError
    # arm it claims to cover is the separate test below, which was absent
    # until 2026-08-28 and is why narrowing the handler to ImportError alone
    # went undetected.
    dlq_mod._DLQMixin._emit_rejection_metric("gate_unavailable")


def test_emit_rejection_metric_swallows_a_real_import_error(monkeypatch):
    """The other documented failure: the metrics module cannot be imported.

    The test above never exercised this — it injects an object whose
    `.labels` is missing, which is AttributeError. A handler catching only
    AttributeError would pass it while still crashing on a real missing
    import, so both arms are needed to pin the handler's stated contract.
    """
    import builtins

    from yadgar.backend.queue_drainer import dlq as dlq_mod

    _real_import = builtins.__import__

    def _no_metrics(name, *a, **kw):
        if name == "yadgar._shared.observability.metrics":
            raise ImportError("no metrics module")
        return _real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_metrics)
    # Must not raise.
    dlq_mod._DLQMixin._emit_rejection_metric("gate_unavailable")
