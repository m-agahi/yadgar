"""Task #0027c: bounded wait-for-backend gate at the CORE composition root.

Before this car, ``StorageEngine.__init__`` issued its first migration HTTP call
inline (``_shared/storage/__init__.py`` -> ``migrations._init_schema``), so a core
process started while the backend was down raised ``httpx.ConnectError`` straight
out of ``core_init_engines`` and ``main()``.  The unit's ``Restart=on-failure`` +
``RestartSec=5`` then restarted it into the identical failure — a ~6s crash cycle
that stays under systemd's ``StartLimitBurst``, i.e. an unbounded crashloop.

The fix is a BOUNDED readiness gate in ``core_init_engines``, before it delegates
to the shared ``lifecycle.init_engines``.  Placement is the whole point and these
tests pin it:

* the gate polls the backend's ``/health`` and only then constructs the engines
  (``test_core_startup_waits_for_backend``);
* it gives up on a budget rather than hanging, with an actionable message
  (``test_core_startup_gives_up_after_budget``);
* the BACKEND's own bootstrap calls the SAME ``lifecycle.init_engines``
  (``embed_service._ensure_recall_engines`` -> ``engine_set="slim"``), so a gate
  pushed down into ``init_engines`` would make the backend wait for itself —
  ``test_backend_slim_bootstrap_does_not_wait_for_itself`` is the regression guard
  that stops that "simplification";
* the budget must stay strictly inside the core unit's ``TimeoutStartSec`` or a
  slow-but-fine start becomes a timeout kill
  (``test_retry_budget_is_inside_core_unit_timeout``).

Per ADR-0187's norm the literals stay unpinned — the relation is asserted, not the
number.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from yadgar._shared.config import Settings
from yadgar.core.bootstrap import backend_ready as _br
from yadgar.core.bootstrap import bootstrap as _bs

_HEALTH_BASE = "http://127.0.0.1:18001"


class _Sentinel(Exception):
    """Raised by the delegate spy so the test never builds real engines."""


@pytest.fixture
def fast_settings(monkeypatch):
    """Tiny budget/poll so the gate tests run in milliseconds, not a minute."""

    def _make(wait_sec: float = 1.0, poll_sec: float = 0.001):
        fake = SimpleNamespace(
            BACKEND_READY_WAIT_SEC=wait_sec,
            BACKEND_READY_POLL_SEC=poll_sec,
        )
        monkeypatch.setattr(_br, "get_settings", lambda: fake)
        return fake

    return _make


@pytest.fixture(autouse=True)
def _embed_url(monkeypatch):
    """A remote backend must be configured, else the gate is a no-op by design."""
    monkeypatch.setenv("YADGAR_EMBED_URL", _HEALTH_BASE)


# ---------------------------------------------------------------------------
# 1. the gate waits, and the engines are built AFTER the last probe
# ---------------------------------------------------------------------------


def test_core_startup_waits_for_backend(monkeypatch, fast_settings):
    """core_init_engines retries the probe, then delegates — in that order."""
    fast_settings()
    events: list[str] = []
    results = iter([False, False, True])

    def _probe(url, timeout_s):
        events.append("probe")
        return next(results)

    def _delegate(**kwargs):
        events.append("delegate")
        raise _Sentinel

    monkeypatch.setattr(_br, "_probe_backend_health", _probe)
    monkeypatch.setattr(_bs, "_shared_init_engines", _delegate)

    with pytest.raises(_Sentinel):
        _bs.core_init_engines(db_path=":memory:")

    assert events.count("probe") == 3, f"probe not retried: {events}"
    # Ordering is the whole point — assert it, do not infer it.
    assert events == ["probe", "probe", "probe", "delegate"], events


def test_gate_is_skipped_when_no_remote_backend(monkeypatch, fast_settings):
    """No YADGAR_EMBED_URL => in-process backend => nothing to wait for."""
    fast_settings()
    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        _br, "_probe_backend_health", lambda url, timeout_s: calls.append(url) or True
    )
    monkeypatch.setattr(
        _bs, "_shared_init_engines", lambda **kw: (_ for _ in ()).throw(_Sentinel())
    )

    with pytest.raises(_Sentinel):
        _bs.core_init_engines(db_path=":memory:")
    assert calls == []


def test_zero_budget_disables_the_gate(monkeypatch, fast_settings):
    """BACKEND_READY_WAIT_SEC=0 is the documented escape hatch (plan §7)."""
    fast_settings(wait_sec=0)
    calls: list[str] = []
    monkeypatch.setattr(
        _br, "_probe_backend_health", lambda url, timeout_s: calls.append(url) or False
    )
    monkeypatch.setattr(
        _bs, "_shared_init_engines", lambda **kw: (_ for _ in ()).throw(_Sentinel())
    )

    with pytest.raises(_Sentinel):
        _bs.core_init_engines(db_path=":memory:")
    assert calls == []


# ---------------------------------------------------------------------------
# 2. bounded give-up, with an actionable message
# ---------------------------------------------------------------------------


def test_core_startup_gives_up_after_budget(monkeypatch, fast_settings):
    """Probe never succeeds => typed error inside the budget, naming the URL."""
    budget = 0.3
    fast_settings(wait_sec=budget, poll_sec=0.01)
    monkeypatch.setattr(_br, "_probe_backend_health", lambda url, timeout_s: False)

    started = time.monotonic()
    with pytest.raises(_br.BackendNotReadyError) as excinfo:
        _br.await_backend_ready()
    elapsed = time.monotonic() - started

    assert elapsed < budget + 2.0, f"gate overran its budget: {elapsed}s > {budget}s"
    assert elapsed >= budget * 0.5, f"gate did not actually wait: {elapsed}s"
    assert f"{_HEALTH_BASE}/health" in str(excinfo.value), str(excinfo.value)


def test_gate_error_propagates_out_of_core_init_engines(monkeypatch, fast_settings):
    """The core composition root must NOT swallow the exhaustion error."""
    fast_settings(wait_sec=0.05, poll_sec=0.01)
    monkeypatch.setattr(_br, "_probe_backend_health", lambda url, timeout_s: False)
    monkeypatch.setattr(
        _bs, "_shared_init_engines", lambda **kw: (_ for _ in ()).throw(_Sentinel())
    )

    with pytest.raises(_br.BackendNotReadyError):
        _bs.core_init_engines(db_path=":memory:")


def test_probe_requires_http_200(monkeypatch):
    """A reachable-but-unhealthy backend (503) is NOT ready.

    ``daemon._embed_health_ok`` deliberately returns True on any HTTP response
    (it answers "is the container up"); the startup gate needs the STRONGER
    signal — /health returns 200 only when ``db_ok and engine_loaded``, which is
    exactly the precondition ``_init_schema`` has.  Do not reuse that helper.
    """
    import httpx

    codes = iter([503, 500, 200])

    def _fake_get(url, timeout=None, **kwargs):
        return httpx.Response(next(codes), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    url = f"{_HEALTH_BASE}/health"
    assert _br._probe_backend_health(url, 1.0) is False
    assert _br._probe_backend_health(url, 1.0) is False
    assert _br._probe_backend_health(url, 1.0) is True


def test_main_turns_the_gate_error_into_a_legible_exit(monkeypatch, tmp_path):
    """main() must exit non-zero with the message, not a bare traceback.

    The unit restarts either way, which is correct; what changes is that the
    journal shows one legible line per bounded attempt instead of a ConnectError
    traceback every ~6 seconds forever.
    """
    import yadgar._shared.paths as _paths
    from yadgar.core.server import _startup

    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")
    monkeypatch.setattr(_paths, "PID_PATH", tmp_path / "yadgar.pid")

    msg = "backend http://127.0.0.1:18001/health did not return HTTP 200 within 60s"

    def _raise(**kwargs):
        raise _br.BackendNotReadyError(msg)

    monkeypatch.setattr(_startup, "init_engines", _raise)

    with pytest.raises(SystemExit) as excinfo:
        _startup.main()
    # A string SystemExit code exits non-zero and prints the message to stderr.
    assert str(excinfo.value) == msg
    assert excinfo.value.code != 0


# ---------------------------------------------------------------------------
# 3. the D2 regression guard — the backend must never wait for itself
# ---------------------------------------------------------------------------


def test_backend_slim_bootstrap_does_not_wait_for_itself(monkeypatch, tmp_path):
    """The shared root the BACKEND calls directly must never touch the gate.

    ``embed_service._ensure_recall_engines`` calls
    ``lifecycle.init_engines(local_engines=True, engine_set="slim")``.  If the
    gate ever migrates down into ``init_engines``, the backend would poll its own
    /health during its own startup and deadlock until the budget expires.  Both
    the poll loop and its probe are spied here so either placement trips it.
    """
    from yadgar._shared.runtime import lifecycle
    from yadgar.core import server

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError(
            "backend slim bootstrap invoked the core startup readiness gate — "
            "the gate must stay in core_init_engines (plan #0027c D2)"
        )

    monkeypatch.setattr(_br, "await_backend_ready", _boom)
    monkeypatch.setattr(_br, "_probe_backend_health", _boom)

    db_path = str(tmp_path / "slim_gate.db")
    lifecycle.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2", engine_set="slim")
    server.shutdown()


def test_core_init_engines_slim_stays_identical_to_shared(monkeypatch, fast_settings):
    """core_init_engines(slim) must remain byte-identical to lifecycle(slim).

    bootstrap.py's contract: the slim path short-circuits so the test-facing seam
    builds exactly what the real backend builds.  The gate is therefore FULL-path
    only — otherwise ~90 `server.init_engines(..., engine_set="slim")` callers
    (incl. the slim-parity suite) would start polling a backend.
    """
    fast_settings()
    calls: list[str] = []
    monkeypatch.setattr(
        _br, "_probe_backend_health", lambda url, timeout_s: calls.append(url) or True
    )
    monkeypatch.setattr(_bs, "_shared_init_engines", lambda **kw: "slim-result")

    assert _bs.core_init_engines(db_path=":memory:", engine_set="slim") == "slim-result"
    assert calls == []


# ---------------------------------------------------------------------------
# 4. the relation: budget must fit inside the core unit's start timeout
# ---------------------------------------------------------------------------

_TIMEOUT_RE = re.compile(r"^TimeoutStartSec[^\S\n]*=[^\S\n]*(\d+)", re.MULTILINE)


def test_retry_budget_is_inside_core_unit_timeout(tmp_path):
    """BACKEND_READY_WAIT_SEC < the rendered core unit's TimeoutStartSec.

    A gate that outlives the start timeout converts a slow-but-fine start into a
    ``Restart=on-failure`` crashloop — the exact failure ADR-0185 records for
    ExecStartPost.  The relation is asserted; the literals stay unpinned.
    """
    from yadgar.tests._unit_render import render_systemd

    render_systemd(tmp_path)
    unit = (tmp_path / "units" / "yadgar.service").read_text()
    match = _TIMEOUT_RE.search(unit)
    assert match, f"no TimeoutStartSec in rendered core unit:\n{unit}"
    timeout_start_sec = int(match.group(1))

    budget = Settings.model_fields["BACKEND_READY_WAIT_SEC"].default
    assert budget < timeout_start_sec, (
        f"BACKEND_READY_WAIT_SEC={budget} must be strictly less than the core "
        f"unit's TimeoutStartSec={timeout_start_sec}"
    )
