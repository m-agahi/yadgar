"""Targeted tests for task 233 / C3 — backend cold-path start budget.

The bug: TimeoutStartSec=180 on yadgar-backend.service was sized for the warm
path (image cached, model loaded). The cold path — a fresh image pull after a
version bump — observed 3.68 GB at typical ~30 MB/s = ~120s, plus a 20-40s
backend model load, plus a readiness probe. systemd killed the pull mid-copy,
Restart=on-failure restarted in 5s, the runtime re-pulled from byte zero, and
the loop never converged.

The fix: bump the backend's cold-path budget to 300s AND cap the restart loop
with StartLimitBurst=3 / StartLimitIntervalSec=600 so a TRULY stuck pull
surfaces a `failed` state instead of looping forever in `activating (start)`.
The core unit keeps its 120s budget — its image is small and its warm-up has no
pull phase, so the silent-restart-loop class is not the same.

These tests pin the four behaviours the car depends on:
  1. readiness_for accepts and propagates cold_budget through to the Readiness.
  2. cold_budget defaults to budget when not supplied (no behaviour change for
     callers that don't override it — the core unit is one such caller).
  3. build_backend_unit emits TimeoutStartSec=cold_budget AND StartLimitBurst=3
     + StartLimitIntervalSec=600.
  4. build_core_unit keeps TimeoutStartSec=budget (its 120s) and DOES NOT emit
     StartLimit* (the warm core is not the same class of failure).
"""

from __future__ import annotations

from yadgar.core.daemon.unit_model import render_unit
from yadgar.core.daemon.units import (
    Readiness,
    build_backend_unit,
    build_core_unit,
    readiness_for,
)
from yadgar.tests.core.test_unit_model import minimal_spec


def _rendered(unit) -> str:
    return render_unit(unit)


def _spec(backend: bool):
    """Backend vs core spec — same shape, distinct ports so the rendered units
    branch on the same fields the renderer uses for real."""
    if backend:
        return minimal_spec(
            backend_container="yadgar-backend",
            backend_embed_port=8001,
            backend_surreal_port=8000,
        )
    return minimal_spec(
        backend_container="yadgar-core",
        backend_embed_port=8765,
        backend_surreal_port=8766,
    )


# ── readiness_for / Readiness ────────────────────────────────────────────────


def test_readiness_for_propagates_cold_budget_to_readiness():
    ready = readiness_for("docker", url="http://x/health", retries=10, budget=180, cold_budget=300)
    assert isinstance(ready, Readiness)
    assert ready.budget == 180
    assert ready.cold_budget == 300


def test_readiness_for_defaults_cold_budget_to_budget():
    """Callers that don't override cold_budget get the prior warm-path behaviour."""
    ready = readiness_for("docker", url="http://x/health", retries=10, budget=180)
    assert ready.cold_budget == 180


def test_readiness_cold_budget_default_falls_back_on_construction():
    """Direct Readiness(cold_budget=0) — the dataclass sentinel — must
    default to budget via __post_init__ so legacy callers do not regress."""
    ready = Readiness(
        type_directives=(),
        budget=120,
    )
    assert ready.cold_budget == 120


# ── build_backend_unit: cold-path budget + StartLimit ────────────────────────


def test_backend_unit_emits_cold_budget_as_timeout_start_sec():
    """The fix: 300s ceiling on the cold path so a fresh 3.68 GB pull finishes
    inside the start window and the unit does not get killed mid-copy."""
    spec = _spec(backend=True)
    text = _rendered(build_backend_unit(spec))
    assert "\nTimeoutStartSec=300\n" in text, (
        f"backend unit must emit TimeoutStartSec=300 on the cold path (task 233 / C3).\n"
        f"Got:\n{text}"
    )


def test_backend_unit_emits_start_limit_to_cap_restart_loop():
    """3 failed starts in 10 minutes is a stuck pull (registry / network / disk),
    not a transient flake. Without StartLimit*, systemd loops forever in
    `activating (start)` with no signal that the unit is stuck."""
    spec = _spec(backend=True)
    text = _rendered(build_backend_unit(spec))
    assert "\nStartLimitBurst=3\n" in text, "missing StartLimitBurst=3 on backend"
    assert "\nStartLimitIntervalSec=600\n" in text, "missing StartLimitIntervalSec=600 on backend"


def test_backend_unit_section_order_has_start_limit_under_unit():
    """StartLimit* are [Unit]-section directives and must come BEFORE the
    [Service] block. This test pins the section ordering so a future move
    under [Service] (where they would be silently ignored) fails."""
    spec = _spec(backend=True)
    text = _rendered(build_backend_unit(spec))
    unit_end = text.index("\n[Service]")
    sl_pos = text.index("\nStartLimitBurst=3\n")
    assert sl_pos < unit_end, (
        "StartLimitBurst=3 must live in the [Unit] section (before [Service]). "
        "systemd silently ignores [Service]-section StartLimit*."
    )


# ── build_core_unit: warm-path budget, no StartLimit ────────────────────────


def test_core_unit_keeps_warm_budget_unchanged():
    """The core's 120s budget is sized for warm starts (image cached, no model
    load). task 233 must NOT widen it — the silent-restart-loop class is not
    the same there."""
    spec = _spec(backend=False)
    text = _rendered(build_core_unit(spec))
    assert "\nTimeoutStartSec=120\n" in text, (
        "core unit must keep TimeoutStartSec=120 (warm path, no pull phase)."
    )
    assert "\nTimeoutStartSec=300\n" not in text, (
        "core unit MUST NOT take the 300s cold budget — its image is small "
        "and its warm-up has no pull phase (task 233 / C3)."
    )


def test_core_unit_does_not_emit_start_limit():
    """The cap is added on the BACKEND unit only. A future regression that
    emits StartLimit* on the core (where the warm-up is fast and the
    silent-restart-loop class is not the same) is caught here."""
    spec = _spec(backend=False)
    text = _rendered(build_core_unit(spec))
    assert "StartLimitBurst" not in text, (
        "core unit must not carry StartLimitBurst — the cap is a backend-only "
        "fix for the forever-restart-loop on a stuck cold pull (task 233 / C3)."
    )
    assert "StartLimitIntervalSec" not in text, (
        "core unit must not carry StartLimitIntervalSec either."
    )
