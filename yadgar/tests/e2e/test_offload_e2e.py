"""Real-daemon e2e for Fix A (daemon-offload-A) — the regression guard.

Per the plan (§7) and the audit: the in-process e2e_engines harness BYPASSES
`_instrumented` and the event loop, so it CANNOT exercise the offload or the
loop-block. These tests spawn the ACTUAL core daemon as a subprocess
(`python -m yadgar --transport streamable-http`) against a REAL SurrealDB and
drive it over HTTP (POST /mcp JSON-RPC + GET /health). This is the only path
that hits the real dispatch boundary the fix changes.

Tests:
  - responsiveness A/B (offload OFF vs ON): identical env, only YADGAR_OFFLOAD_TOOLS
    differs. OFF → /health starves (loop blocked by the inline sleep body) — the
    FAIL-ON-TODAY proof. ON → /health responds within a tight latency budget AND
    the N sleep calls run in parallel (wall-clock ≈ sleep, not N×sleep).
  - O2 exhaustion: wedge N ops past the wait_for timeout; assert the /health
    `tool_pool.saturated` field flips False→True (the wiring proof that
    pool_saturated reaches /health) and the handler returns 503 (what P0 keys on).
    Asserting the field flip — not bare 503 — is load-bearing: the fake-embed
    baseline is ALREADY 503, so bare-503 would pass even if the O2 signal never
    fired.

Harness rules: real surreal via the session fixture (skip if absent), isolated
tmp data dir, free port, SIGTERM teardown. No module-scope OTEL poison (the
subprocess inherits OTEL_SDK_DISABLED from `make e2e`).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port: int, timeout: float) -> tuple[int | None, float, dict | None]:
    """GET /health. Returns (status_code_or_None, elapsed_sec, payload_or_None).

    status_code None ⇒ the loop did not answer within `timeout` (starved/000).
    """
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout)
        body = resp.read()
        return resp.status, time.monotonic() - t0, json.loads(body)
    except urllib.error.HTTPError as e:
        # A 503 is STILL a responsive loop — read its body for the pool fields.
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = None
        return e.code, time.monotonic() - t0, payload
    except Exception:
        return None, time.monotonic() - t0, None


def _call_tool(
    port: int, name: str, arguments: dict, *, req_id: int, timeout: float
) -> dict | None:
    """POST a stateless JSON-RPC tools/call. Parse the SSE `data:` line."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
    except Exception:
        return None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:") :].strip())
            except Exception:
                return None
    return None


class _Daemon:
    """A spawned core daemon over HTTP. Context-manager: boots + waits + reaps."""

    def __init__(self, db_url: str, env_extra: dict[str, str], tmp_path) -> None:
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self._db_url = db_url
        self._env_extra = env_extra
        self._data_dir = str(tmp_path / f"daemon_{self.port}")

    def __enter__(self) -> _Daemon:
        env = os.environ.copy()
        env.update(
            {
                "YADGAR_REQUIRE_AUTH": "0",
                "YADGAR_TEST_TOOLS": "1",
                "YADGAR_PROFILE": "minimal",
                "YADGAR_DB_URL": self._db_url,
                "YADGAR_DB_USER": "root",
                "YADGAR_DB_PASS": "root",
                # Fake embed at a refused port → instant connection-refused (not a
                # 2s timeout), so the baseline /health latency stays clean. The
                # embed probe reports false → /health 503 in BOTH arms (a constant,
                # which is why we discriminate on LATENCY, not status).
                "YADGAR_EMBED_URL": "http://127.0.0.1:9/embed",
                "YADGAR_DATA_DIR": self._data_dir,
                "YADGAR_DB_PATH": self._data_dir + "/db",
                # Inherit OTEL_SDK_DISABLED from the parent (make e2e sets it). Do
                # NOT set OTEL here at module scope.
            }
        )
        env.update(self._env_extra)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "yadgar",
                "--transport",
                "streamable-http",
                "--port",
                str(self.port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for the daemon to accept /health (any HTTP response = up).
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("daemon exited during startup")
            code, _, _ = _health(self.port, timeout=1.0)
            if code is not None:
                return self
            time.sleep(0.3)
        raise RuntimeError("daemon did not become reachable")

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@pytest.fixture
def _db_url(surreal_server) -> str:
    if surreal_server is None or not os.environ.get("YADGAR_DB_URL"):
        pytest.skip("real SurrealDB required (surreal binary absent)")
    if not shutil.which("surreal"):
        pytest.skip("surreal binary not on PATH")
    return os.environ["YADGAR_DB_URL"]


# ---------------------------------------------------------------------------
# Test 1 — responsiveness A/B (the red→green guard, MUST starve on OFF)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offload", ["0", "1"])
def test_health_responsiveness_under_load(_db_url, tmp_path, offload):
    """OFF → /health starved by the inline sleep body; ON → /health stays fast.

    Identical env, only YADGAR_OFFLOAD_TOOLS differs — a controlled A/B.
    """
    n = 4
    sleep_sec = 5.0
    with _Daemon(
        _db_url,
        {"YADGAR_OFFLOAD_TOOLS": offload, "YADGAR_TOOL_POOL_WORKERS": "8"},
        tmp_path,
    ) as d:
        # Baseline: /health is responsive before load (single-digit ms).
        code, t_base, _ = _health(d.port, timeout=5.0)
        assert code is not None, "daemon must answer /health before load"

        # Fire N concurrent blocking tool calls.
        fire_t0 = time.monotonic()
        threads = [
            threading.Thread(
                target=_call_tool,
                args=(d.port, "_test_sleep", {"seconds": sleep_sec}),
                kwargs={"req_id": i, "timeout": 30.0},
                daemon=True,
            )
            for i in range(n)
        ]
        for t in threads:
            t.start()
        time.sleep(0.6)  # let the calls land + occupy

        # Poll /health while the calls are in-flight; record max latency / starvation.
        latencies = []
        starved = False
        for _ in range(3):
            code, elapsed, _ = _health(d.port, timeout=8.0)
            latencies.append(elapsed)
            if code is None:  # loop never answered within 8s ⇒ starved
                starved = True
            time.sleep(0.3)

        for t in threads:
            t.join(timeout=30.0)
        wall = time.monotonic() - fire_t0

        if offload == "0":
            # FAIL-ON-TODAY: inline sync sleep occupies the loop thread → /health
            # is starved (times out / >> budget) and the calls run SERIALLY.
            assert starved or max(latencies) > 2.0, (
                f"offload OFF must starve /health under load; latencies={latencies}"
            )
            assert wall > (n * sleep_sec) * 0.6, (
                f"offload OFF must run the calls serially; wall={wall:.1f}s "
                f"(serial≈{n * sleep_sec}s)"
            )
        else:
            # THE FIX: loop free → /health fast the whole time, calls run parallel.
            assert not starved, f"offload ON must keep /health responsive; latencies={latencies}"
            assert max(latencies) < 1.0, (
                f"offload ON /health p-max must stay <1s; latencies={latencies}"
            )
            assert wall < (sleep_sec * 2.0), (
                f"offload ON must run the calls in parallel; wall={wall:.1f}s "
                f"(parallel≈{sleep_sec}s, serial≈{n * sleep_sec}s)"
            )


# ---------------------------------------------------------------------------
# Test 2 — O2 exhaustion: /health tool_pool.saturated flips False→True (→ 503)
# ---------------------------------------------------------------------------


def test_health_degrades_on_pool_saturation(_db_url, tmp_path):
    """Wedge every worker past the wait_for timeout → /health saturated flips True.

    Assert the FIELD FLIP (False→True), not bare 503: the fake-embed baseline is
    ALREADY 503, so bare-503 would pass even with a broken O2 signal. The flip is
    the proof pool_saturated() reaches /health; the accompanying 503 is what P0's
    curl -f keys on.
    """
    workers = 2
    with _Daemon(
        _db_url,
        {
            "YADGAR_OFFLOAD_TOOLS": "1",
            "YADGAR_TOOL_POOL_WORKERS": str(workers),
            "YADGAR_TOOL_TIMEOUT_SEC": "1",
            "YADGAR_TOOL_SATURATION_GRACE_SEC": "1.5",
        },
        tmp_path,
    ) as d:
        # Before saturation: pool not saturated.
        code0, _, p0 = _health(d.port, timeout=5.0)
        assert code0 is not None
        tp0 = (p0 or {}).get("tool_pool", {})
        assert tp0.get("saturated") in (False, None), f"pool must start unsaturated: {tp0}"

        # Wedge `workers` ops, each sleeping FAR past the 1s wait_for timeout.
        wedge_sec = 15.0
        threads = [
            threading.Thread(
                target=_call_tool,
                args=(d.port, "_test_sleep", {"seconds": wedge_sec}),
                kwargs={"req_id": i, "timeout": 30.0},
                daemon=True,
            )
            for i in range(workers)
        ]
        for t in threads:
            t.start()

        # Wait past timeout (1s) + grace (1.5s) + margin for the staleness to trip.
        saturated = False
        sat_code = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            code, _, payload = _health(d.port, timeout=5.0)
            tp = (payload or {}).get("tool_pool", {})
            if tp.get("saturated") is True:
                saturated = True
                sat_code = code
                break
            time.sleep(0.3)

        assert saturated, (
            "/health tool_pool.saturated must flip True while every worker is "
            "wedged past the timeout (worker-side occupancy + completion-staleness)"
        )
        assert sat_code == 503, (
            f"a saturated pool must drive /health → 503 so P0 curl -f kills it; got {sat_code}"
        )

        # The wedged workers self-release at wedge_sec; let the threads drain so
        # teardown is clean (their futures already TimeoutError'd client-side).
        for t in threads:
            t.join(timeout=wedge_sec + 5.0)


# ---------------------------------------------------------------------------
# Test 3 — offload thread-identity assertion (body runs off the loop)
# ---------------------------------------------------------------------------


def test_offload_runs_body_on_worker_thread(_db_url, tmp_path):
    """ON → the tool body runs on a worker thread (name prefix yadgar-tool)."""
    with _Daemon(
        _db_url,
        {"YADGAR_OFFLOAD_TOOLS": "1", "YADGAR_TOOL_POOL_WORKERS": "4"},
        tmp_path,
    ) as d:
        resp = _call_tool(d.port, "_test_thread_id", {}, req_id=1, timeout=10.0)
        assert resp is not None, "tools/call must return a result"
        text = resp["result"]["content"][0]["text"]
        info = json.loads(text)
        assert info["name"].startswith("yadgar-tool"), (
            f"offloaded body must run on a pool worker thread, got {info['name']!r}"
        )
