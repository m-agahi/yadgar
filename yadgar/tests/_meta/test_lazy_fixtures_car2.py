"""Car 2 (test-suite hardening train): lazy surreal/model fixture gating.

The root conftest must NOT pay per-worker infra costs (SurrealDB subprocess
~300MB, embedding model ~700MB RSS) for tests that never need them:

  * ``surreal_server`` reserves a URL at session start but spawns the server
    lazily — on first StorageEngine construction (or explicit fixture request).
  * ``init_engines()``'s eager embedding-model warmup is deferred in tests; the
    model loads on the first actual encode.

Each behavioral probe runs in a DEDICATED child pytest process (probes live in
``lazy_fixture_probes.py``, excluded from directory collection by filename) so
the worker-global lazy state is deterministic regardless of xdist scheduling.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# _meta/ → tests/ → yadgar/ → repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE_FILE = "yadgar/tests/_meta/lazy_fixture_probes.py"

_requires_surreal = pytest.mark.skipif(
    not shutil.which("surreal"), reason="surreal binary not on PATH"
)


def _run_probe(probe_name: str, *, timeout: int = 240) -> subprocess.CompletedProcess:
    """Run one probe test in a fresh child pytest process at the repo root.

    Pops YADGAR_DB_URL so the child reserves its own session URL instead of
    inheriting this worker's (possibly unspawned) reservation.
    """
    env = os.environ.copy()
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("YADGAR_DB_URL", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{_PROBE_FILE}::{probe_name}",
            "-q",
            "--no-header",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "--override-ini=addopts=",
            f"--timeout={timeout - 30}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO_ROOT),
        env=env,
    )


# ---------------------------------------------------------------------------
# Fixture-definition shape (cheap, in-process)
# ---------------------------------------------------------------------------


class TestFixtureShape:
    def test_surreal_server_fixture_is_not_autouse(self):
        """surreal_server must be opt-in (lazy), not autouse-per-worker."""
        from yadgar.tests import conftest as _c

        marker = _c.surreal_server._fixture_function_marker
        assert marker.scope == "session"
        assert not marker.autouse, (
            "surreal_server is autouse — every xdist worker spawns SurrealDB "
            "regardless of need (the Car 2 regression)"
        )

    def test_conftest_exposes_lazy_spawn_state(self):
        """The lazy-spawn seam (handle + ensure function) must exist."""
        from yadgar.tests import conftest as _c

        assert hasattr(_c, "_SURREAL_HANDLE")
        assert callable(_c._ensure_surreal_spawned)


# ---------------------------------------------------------------------------
# Behavioral probes (dedicated child pytest process each)
# ---------------------------------------------------------------------------


class TestLazyBehaviorProbes:
    def test_logic_only_test_pays_no_infra(self):
        """Logic-only test: no surreal spawn, no torch, no sentence-transformers."""
        result = _run_probe("test_probe_logic_only_no_surreal_no_model", timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr

    @_requires_surreal
    def test_storage_engine_construction_spawns_on_demand(self):
        """StorageEngine construction lazily spawns the session server (server mode)."""
        result = _run_probe("test_probe_storage_engine_triggers_spawn", timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr

    @_requires_surreal
    def test_init_engines_defers_model_load(self):
        """init_engines defers model warmup; first encode loads on demand."""
        result = _run_probe("test_probe_init_engines_defers_model_load", timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
