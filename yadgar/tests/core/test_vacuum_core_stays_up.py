"""task:0111 / ADR-0188 — a vacuum stops the BACKEND only; the core stays up.

Phase 2 used to call ``svc.stop()``, which is ``("yadgar", "yadgar-backend")``
(``yadgar/core/ops/ops.py``), so every vacuum took the whole memory engine down —
measured at ~68 s of a 136 s run, dropping every connected MCP session.  That
scope was inherited from the 2026-05-12 manual DB-rebuild ritual
(``docs/PLAN_V4_8.md``), not derived: every surviving mechanical rationale is
BACKEND-scoped (torn-segment copy, ``_assert_backend_quiesced`` which polls the
SurrealDB port, ADR-0090's corrupt-on-reopen, and the ``/proc`` inode-coherence
scan which only matches ``surreal … start`` argv).  The core holds no fd into
the store — it reaches the DB over HTTP (ADR-0078).

What this file pins:

1. Phase 2 stops the backend and ONLY the backend.
2. A full happy-path run never calls ``stop`` and never calls ``start_yadgar``
   — the core was never stopped, so there is nothing to start.
3. The finalize HARD GATE survived the deletion of that start.  ``start_yadgar``
   and ``_wait_for_yadgar_health`` sat on adjacent lines; deleting the start and
   taking the wait with it would silently retire a rollback trigger.  The gate
   is not vacuous: core ``/health`` is READINESS and probes ``YADGAR_DB_URL``'s
   own ``/health`` (``yadgar/core/server/http.py::_build_health_payload`` →
   ``_probe_dependency``), so it still proves "the backend came back on the
   compacted DB and the core can reach it".
4. That gate runs BEFORE the advisory ``check_invariants`` call, so a later
   refactor cannot reorder an advisory ahead of a hard gate.

The abort-path belts (``_restart_services_after_abort``) are deliberately NOT
touched — see ``TestAbortPathsRestartCore`` in
``test_vacuum_finalize_verification.py``: a generator change does not rewrite
units already installed, so a pre-flip host still cascades and still needs them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yadgar.tests.core.test_vacuum_finalize_verification import _ci_post_factory, _run_vacuum

# ---------------------------------------------------------------------------
# 1. Phase 2 stops the backend and only the backend
# ---------------------------------------------------------------------------


def _recording_svc() -> tuple[MagicMock, list[str]]:
    """A ServiceController double that records the lifecycle calls made on it.

    Names are recorded EXACTLY. Assertions must use element membership
    (``"stop" not in calls``) and never a substring scan — ``"stop" in
    "stop_backend"`` is true, which would make the whole file a false green.
    """
    calls: list[str] = []
    svc = MagicMock()
    for name in ("stop", "stop_backend", "start_backend", "start_yadgar"):
        getattr(svc, name).side_effect = lambda n=name: calls.append(n)
    return svc, calls


def test_snapshot_phase_stops_only_the_backend(tmp_path: Path) -> None:
    """``_vacuum_snapshot_and_drop`` must call ``stop_backend()``, never ``stop()``."""
    from yadgar.core.vacuum.phases import _vacuum_snapshot_and_drop

    db_path = tmp_path / "surreal_db"
    (db_path / "vlog").mkdir(parents=True)
    (db_path / "vlog" / "00001.vlog").write_bytes(b"x" * 1024)

    svc, calls = _recording_svc()
    snapshot = _vacuum_snapshot_and_drop(db_path, tmp_path, svc, before_bytes=1024)

    assert calls == ["stop_backend"], (
        f"phase 2 must quiesce the BACKEND only — recorded {calls}. `stop()` is "
        "('yadgar', 'yadgar-backend') and takes the core down with it (ADR-0188)."
    )
    assert snapshot.exists(), "phase 2 no longer produced the pre-vacuum snapshot"


def test_snapshot_phase_banner_does_not_claim_it_stops_daemons(tmp_path, capsys) -> None:
    """The phase-2 banner is what an operator reads in the vacuum log."""
    from yadgar.core.vacuum.phases import _vacuum_snapshot_and_drop

    db_path = tmp_path / "surreal_db"
    db_path.mkdir(parents=True)
    svc, _calls = _recording_svc()
    _vacuum_snapshot_and_drop(db_path, tmp_path, svc, before_bytes=0)

    banner = capsys.readouterr().out
    assert "stopping daemons" not in banner, (
        f"phase-2 banner still says it stops the daemons (plural): {banner!r}"
    )
    assert "backend" in banner, f"phase-2 banner does not name the backend: {banner!r}"


# ---------------------------------------------------------------------------
# 2. A full happy-path run never stops — nor restarts — the core
# ---------------------------------------------------------------------------


def test_full_vacuum_never_stops_core(monkeypatch) -> None:
    run = _run_vacuum(monkeypatch)
    assert run.exit_code == 0, "happy-path vacuum must succeed"

    names = [c[0] for c in run.svc.method_calls]
    assert "stop" not in names, (
        f"vacuum called svc.stop() — that stops BOTH units and drops every "
        f"connected MCP session (ADR-0188). Recorded: {names}"
    )
    assert "stop_backend" in names, f"vacuum stopped nothing at all: {names}"


def test_full_vacuum_does_not_start_core_on_the_success_path(monkeypatch) -> None:
    """The core was never stopped, so finalize has nothing to start.

    Keeping the start would be harmless-but-dishonest on a flipped host and
    would hide a regression of the phase-2 scope: a vacuum that started
    stopping the core again would still look green.
    """
    run = _run_vacuum(monkeypatch)
    assert run.exit_code == 0
    names = [c[0] for c in run.svc.method_calls]
    assert "start_yadgar" not in names, (
        f"finalize still starts the core on the success path: {names}"
    )


# ---------------------------------------------------------------------------
# 3-4. The finalize HARD GATE survived, and still precedes check_invariants
# ---------------------------------------------------------------------------


def test_finalize_still_gates_on_core_health(monkeypatch) -> None:
    """Deleting the start must not take the wait with it.

    ``_wait_for_yadgar_health`` returning False must still roll the swap back
    (canonical restored from ``.old``, exit 2) — that is the gate proving the
    backend came up on the COMPACTED DB and the core can reach it.
    """
    run = _run_vacuum(
        monkeypatch,
        extra_patches=[patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=False)],
    )
    assert run.exit_code == 2, "an unhealthy core must fail the run, not warn"
    assert run.canonical_is_original, "the swap was NOT rolled back on the health-gate failure"
    assert not run.compacted_retained, "the unverified compacted DB was retained"


def test_finalize_gate_polls_readiness_not_liveness() -> None:
    """The gate must probe ``/health``, never ``/health/live``.

    This is the assertion that keeps the gate from going VACUOUS.  The test
    above mocks ``_wait_for_yadgar_health`` wholesale, so it proves the rollback
    WIRING survived — not that the probe can still observe a down backend.  With
    the core staying up across the whole vacuum, a "simplification" to
    ``/health/live`` would compile, pass every other test in this file, and
    silently retire a data-safety gate: liveness is process-local (ADR-0019,
    ``core/server/http.py``'s ``/health/live`` makes no outbound dependency
    probe), so a core that never went down would answer 200 with the backend
    still dead.  Only READINESS round-trips the backend.

    Asserted on the source rather than by driving it, because the property is
    "which URL is built", and any HTTP-level double would have to encode that
    same knowledge to check it.
    """
    import inspect

    from yadgar.core.vacuum import _wait_for_yadgar_health

    src = inspect.getsource(_wait_for_yadgar_health)
    assert "/health/live" not in src, (
        "the finalize gate probes LIVENESS — process-local, so it is satisfied "
        "by a core that never went down even with the backend dead (ADR-0019). "
        "The gate must probe readiness (`{url}/health`), which round-trips the "
        "backend and is the only thing proving the compacted DB serves."
    )
    assert 'f"{url}/health"' in src, (
        f"the finalize gate no longer builds a bare `{{url}}/health` readiness "
        f"URL — check what it probes now:\n{src}"
    )


def test_finalize_health_gate_precedes_check_invariants(monkeypatch) -> None:
    """Ordering pin: the HARD gate must run before the ADVISORY call.

    ``check_invariants`` is advisory in finalize (task:0045 D2). If a refactor
    reordered it ahead of the health gate, a rollback-worthy run would first
    spend a request on an advisory probe against a core whose backend is not
    yet verified — and, worse, the ordering would read as if the advisory were
    the gate.
    """
    seen: list[str] = []
    inner = _ci_post_factory(200, True)

    def recording_post(url: str, **kwargs):
        seen.append(url)
        return inner(url, **kwargs)

    run = _run_vacuum(
        monkeypatch,
        post=recording_post,
        extra_patches=[patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=False)],
    )
    assert run.exit_code == 2
    assert not [u for u in seen if "/api/check_invariants" in u], (
        f"check_invariants was called even though the hard health gate failed: {seen}"
    )


# ---------------------------------------------------------------------------
# 5. The abort-path belts are load-bearing on pre-flip hosts — do not simplify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["start_backend", "start_yadgar"])
def test_abort_belt_keeps_both_starts(method) -> None:
    """``_restart_services_after_abort`` must keep BOTH starts, backend first.

    A generator change does not rewrite units already installed: a host whose
    ``yadgar.service`` still carries ``Requires=yadgar-backend.service``
    cascades on the backend stop exactly as before, and this belt is the only
    thing that brings its core back. A reader of "the core is never stopped
    now" would be tempted to delete the ``start_yadgar()`` here.
    """
    from yadgar.core.vacuum import _restart_services_after_abort

    svc, calls = _recording_svc()
    _restart_services_after_abort(svc)
    assert method in calls, f"abort belt dropped {method}(): {calls}"
    assert calls.index("start_backend") < calls.index("start_yadgar"), (
        f"abort belt must start the backend before the core: {calls}"
    )
