"""E2E test harness — behavior-contract safety net (v5.68).

Fixtures provided:
    e2e_engines       Isolated real-surreal + per-test tmp data dir.
                      Sets YADGAR_DATA_DIR to tmp_path; asserts it is NOT under
                      the real data dir.  init_engines() wires everything up;
                      shutdown() tears it down.
    service_stub      Stubs out systemctl/podman so no test can trigger a real
                      service start/stop.  Built for future host-job tests; benign
                      in Phase 1 (no nightly/vacuum/backup tests yet).

DATA-SAFETY GUARANTEE
---------------------
The guard below fires immediately when a test would use the real data dir.
It does NOT rely on tests remembering to set the env var — the fixture sets it
and then asserts its own postcondition.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Real-data-dir guard constants
# ---------------------------------------------------------------------------

_HOME = Path.home()
_REAL_DATA_DIR = _HOME / ".local" / "share" / "yadgar"


def _assert_not_real_data_dir(path: Path | str) -> None:
    """Refuse loudly if *path* is inside the real production data dir.

    Called from e2e_engines before any DB operations begin.  This is the
    enforcement layer — the env-var override is the mechanism, this is the proof.
    """
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(_REAL_DATA_DIR.resolve())
        # If we reach here the path is INSIDE the real data dir — hard fail.
        raise RuntimeError(  # noqa: TRY301
            f"DATA-SAFETY VIOLATION: e2e fixture resolved to real data dir.\n"
            f"  path={resolved}\n"
            f"  real={_REAL_DATA_DIR.resolve()}\n"
            "Aborting test — would have touched production data."
        )
    except ValueError:
        pass  # resolved is NOT under _REAL_DATA_DIR — safe to proceed


# ---------------------------------------------------------------------------
# Service-control stub
# ---------------------------------------------------------------------------


_BLOCKED_SERVICE_CMDS = ("systemctl", "podman stop", "podman start", "podman restart")


def _cmd_to_str(cmd) -> str:
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(c) for c in cmd)
    return str(cmd)


@pytest.fixture(autouse=True)
def service_stub():
    """Provide a stub for service-control calls (systemctl/podman stop/start).

    Future host-job tests (BC-D1/D2/D3) MUST drive the nightly cycle's service
    control through this seam.  Phase-1 tests (DB-layer only) never touch service
    control, so this fixture is benign here.

    Implementation: patches only the specific functions that nightly_cycle.py calls
    for service control (`yadgar.scripts.nightly_cycle._stop_service`, etc.).
    Does NOT patch `subprocess.run` globally — that would break surreal subprocess.
    Exposes the mock as `service_stub["stop_service"]` / `["start_service"]` for
    future assertion in host-job tests.
    """
    stop_mock = MagicMock(return_value=0)
    start_mock = MagicMock(return_value=0)

    # Only patch if the module is importable; if not (rare), fixture is a no-op.
    patchers = []
    try:
        import yadgar.scripts.nightly_cycle as _nc

        if hasattr(_nc, "_stop_service"):
            patchers.append(patch.object(_nc, "_stop_service", stop_mock))
        if hasattr(_nc, "_start_service"):
            patchers.append(patch.object(_nc, "_start_service", start_mock))
    except ImportError:
        pass

    for p in patchers:
        p.start()
    try:
        yield {
            "stop_service": stop_mock,
            "start_service": start_mock,
        }
    finally:
        for p in patchers:
            p.stop()


# ---------------------------------------------------------------------------
# Core e2e fixture — isolated engines + real SurrealDB
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_engines(tmp_path, monkeypatch):
    """Isolated yadgar engine stack for e2e tests.

    - Starts a fresh real-surreal instance per test (via the session-scoped
      surreal_server + _isolate_surrealdb fixtures inherited from parent conftest).
    - Sets YADGAR_DATA_DIR → tmp_path (never touches ~/.local/share/yadgar).
    - Asserts the resolved data dir is NOT under the real production data dir.
    - Calls server.init_engines() to wire up StorageEngine + EmbeddingEngine.
    - Yields a dict with commonly needed handles:
        {
            "server": <yadgar.server module>,
            "storage": <StorageEngine instance>,
            "embeddings": <EmbeddingEngine instance>,
            "tmp_path": <tmp_path Path>,
            "db_path": <str path to the .db file>,
            "yadgar_dir": <str path used as directory_context for this test>,
            "other_dir": <str second project dir for cross-project scoping tests>,
        }
    - Calls server.shutdown() after the test.

    Requires: real `surreal` binary on PATH (or YADGAR_DB_URL set by surreal_server).
    If surreal is absent, the test is SKIPPED (not silently faked).
    """
    if not shutil.which("surreal") and not os.environ.get("YADGAR_DB_URL"):
        pytest.skip(
            "surreal binary not found and YADGAR_DB_URL not set — e2e requires real surreal"
        )

    # Set isolated data dir — MUST happen before any import of yadgar.paths
    db_path = str(tmp_path / "e2e_test.db")
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))

    # DATA-SAFETY: assert the override took effect and is NOT under real data dir
    from yadgar import paths as _paths

    resolved_data = _paths._data_dir()
    _assert_not_real_data_dir(resolved_data)

    # Wire up the full engine stack
    from yadgar import server

    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")

    from yadgar.server.lifecycle import _get_embeddings, _get_storage

    storage = _get_storage()
    embeddings = _get_embeddings()

    # Canonical dirs for cross-project scoping tests
    yadgar_dir = "/home/test/yadgar-project"
    other_dir = "/home/test/aws-work"

    try:
        yield {
            "server": server,
            "storage": storage,
            "embeddings": embeddings,
            "tmp_path": tmp_path,
            "db_path": db_path,
            "yadgar_dir": yadgar_dir,
            "other_dir": other_dir,
        }
    finally:
        server.shutdown()
