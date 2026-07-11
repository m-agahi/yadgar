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

    Two service-control seams exist in yadgar and BOTH are stubbed here so no
    test can trigger a real systemctl/podman start/stop:

      1. nightly_cycle's host-job seam — the real, module-level wrappers
         ``yadgar.scripts.nightly_cycle._stop_service`` / ``._start_service``
         (added in v5.69 P0; previously this fixture patched names that did not
         exist and the ``hasattr`` guard silently no-opped — the latent defect).
      2. vacuum's service path — ``yadgar.ops.ServiceController`` instance
         methods (``stop`` / ``stop_backend`` / ``start_backend`` /
         ``start_yadgar``).  Vacuum drives the backend lifecycle through a
         ServiceController, NOT through nightly_cycle.  In tests the SurrealDB
         backend is a subprocess started by the harness, never a systemd unit —
         so these MUST be neutralised to no-ops by default.

    Both seams are patched to benign no-ops here (autouse).  Tests that need to
    *drive* the dedicated backend (e.g. BC-E2) supersede the ServiceController
    patch with their own function-scoped controlling fixture (last patch wins).

    Does NOT patch ``subprocess.run`` globally — that would break the real
    surreal subprocess the e2e harness relies on.

    Exposes the recorders as ``service_stub["stop_service"]`` /
    ``["start_service"]`` / ``["svc_stop"]`` / ``["svc_stop_backend"]`` /
    ``["svc_start_backend"]`` / ``["svc_start_yadgar"]`` for assertion.
    """
    stop_mock = MagicMock(return_value=0)
    start_mock = MagicMock(return_value=0)
    svc_stop = MagicMock(return_value=None)
    svc_stop_backend = MagicMock(return_value=None)
    svc_start_backend = MagicMock(return_value=None)
    svc_start_yadgar = MagicMock(return_value=None)

    patchers = []
    try:
        import yadgar.core.scripts.nightly_cycle as _nc

        patchers.append(patch.object(_nc, "_stop_service", stop_mock))
        patchers.append(patch.object(_nc, "_start_service", start_mock))
    except ImportError:
        pass

    try:
        from yadgar.core.ops import ServiceController as _SC

        patchers.append(patch.object(_SC, "stop", svc_stop))
        patchers.append(patch.object(_SC, "stop_backend", svc_stop_backend))
        patchers.append(patch.object(_SC, "start_backend", svc_start_backend))
        patchers.append(patch.object(_SC, "start_yadgar", svc_start_yadgar))
    except ImportError:
        pass

    for p in patchers:
        p.start()
    try:
        yield {
            "stop_service": stop_mock,
            "start_service": start_mock,
            "svc_stop": svc_stop,
            "svc_stop_backend": svc_stop_backend,
            "svc_start_backend": svc_start_backend,
            "svc_start_yadgar": svc_start_yadgar,
        }
    finally:
        for p in patchers:
            p.stop()


# ---------------------------------------------------------------------------
# Core e2e fixture — isolated engines + real SurrealDB
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def e2e_engines(tmp_path_factory):
    """Isolated yadgar engine stack for e2e tests.

    v5.101 (module-scope P1): initialized ONCE per e2e file — init_engines()
    (the SurrealDB schema init floor) runs per module, not per test.  Per-test
    DATA isolation comes from conftest's function-scoped `_wipe_surrealdb_data`
    (rows cleared between tests; the module namespace/schema/engine persist).
    Uses `tmp_path_factory` + `pytest.MonkeyPatch()` because a module-scoped
    fixture cannot request the function-scoped `tmp_path`/`monkeypatch`.

    The env override set here (YADGAR_DATA_DIR → the module tmp dir) is applied
    at module setup, BEFORE the parent conftest's function-scoped
    `isolate_yadgar_paths` runs.  That per-test fixture re-points YADGAR_DATA_DIR
    at a per-test dir for the duration of each test, but the engine stack (and
    its StorageEngine `db_path`) was already wired at module init against the
    module dir; tests use the yielded handles, not fresh env reads, so this is
    consistent for the engine.  Data safety is asserted once here at module
    init against the real production data dir.

    - Starts a fresh real-surreal instance per session (via the session-scoped
      surreal_server + _isolate_surrealdb fixtures inherited from parent conftest).
    - Sets YADGAR_DATA_DIR → module tmp dir (never touches ~/.local/share/yadgar).
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

    from _pytest.monkeypatch import MonkeyPatch

    monkeypatch = MonkeyPatch()
    tmp_path = tmp_path_factory.mktemp("e2e_engines")

    # Set isolated data dir — MUST happen before any import of yadgar.paths
    db_path = str(tmp_path / "e2e_test.db")
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))

    # DATA-SAFETY: assert the override took effect and is NOT under real data dir
    from yadgar._shared import paths as _paths

    resolved_data = _paths._data_dir()
    _assert_not_real_data_dir(resolved_data)

    # Wire up the full engine stack
    from yadgar.core import server

    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")

    from yadgar._shared.runtime.lifecycle import _get_embeddings, _get_storage

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
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# R3 write-path harness adaptation (single-process e2e)
# ---------------------------------------------------------------------------
#
# R3 relocated the write path to the backend: Car 1 removed the QueueDrainer
# construction from core lifecycle (core now owns ONLY the enqueue-side
# FileQueue), and Car 3/R5 turned the write + recall + consolidation tools into
# thin forwarders that POST to the backend HTTP endpoints (/admin, /recall,
# /consolidate) derived from YADGAR_EMBED_URL.
#
# The single-process e2e harness has no backend HTTP server, so:
#   1. Enqueued writes (memorize/wiki_add/anchor/checkpoint) never drain
#      (_st._queue_drainer is None) → reads find nothing.
#   2. Forwarding tools raise "YADGAR_EMBED_URL is not set".
#   3. _st._consolidation is None (built only backend-side in service._get_scheduler).
#
# The correct e2e adaptation is to run the backend logic IN-PROCESS against the
# same _st storage the e2e engine stack already wired — NOT to weaken assertions
# (#52). This mirrors the unit/integration conftest fixtures admin_backend_bypass
# + recall_backend_bypass, extended here to:
#   * autouse (every write-path e2e test gets them uniformly), and
#   * CALL-TIME guarded on YADGAR_EMBED_URL: when a test sets EMBED_URL and wires
#     a real backend (the recall-forwarder / landscape ASGI e2e tests), the
#     bypass delegates to the ORIGINAL forwarder so the real HTTP contract is
#     exercised unchanged. Only when EMBED_URL is unset (the write-path files)
#     does the in-process backend impl run. This keeps the real-backend e2e
#     files (test_recall_backend_contract_e2e / _variants / _landscape / _fusion)
#     green while unblocking the 20 write-path failures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _e2e_backend_drainer(request, _isolate_file_queue):
    """Construct the backend QueueDrainer in-process against the e2e storage.

    Delegates to ``_backend_harness.wire_drainer``.  Only wires when the test
    actually uses ``e2e_engines`` (the write-path files); autouse but gated on
    ``request.fixturenames`` so it does NOT force the module engine stack + a
    live drainer onto tests that deliberately run WITHOUT it (e.g.
    test_vacuum_backup_safety's BC-E3 signal-handler test).

    Depends on ``_isolate_file_queue`` (parent conftest autouse) so it runs
    AFTER the per-test FileQueue reset — ``_get_file_queue()`` then returns the
    LIVE per-test queue.
    """
    if "e2e_engines" not in request.fixturenames:
        yield None
        return

    # Materialise the engine stack (module-scoped) for this test.
    request.getfixturevalue("e2e_engines")

    from yadgar.core import server as _server
    from yadgar.tests._backend_harness import wire_drainer

    with wire_drainer(_server._get_file_queue) as drainer:
        yield drainer


@pytest.fixture(autouse=True)
def _e2e_admin_bypass(monkeypatch):
    """Route ``_forward_admin`` → in-process ``run_admin_op`` when no backend URL.

    Delegates to ``_backend_harness.patch_admin_bypass``.  CALL-TIME guarded on
    YADGAR_EMBED_URL: if a test sets EMBED_URL (real-backend e2e), the original
    HTTP forwarder is used unchanged.
    """
    from yadgar.tests._backend_harness import patch_admin_bypass

    patch_admin_bypass(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _e2e_recall_bypass(monkeypatch):
    """Route recall's ``_forward_to_backend`` → in-process ``_fanout_recall``.

    Delegates to ``_backend_harness.patch_recall_bypass``.  CALL-TIME guarded on
    YADGAR_EMBED_URL: real-backend recall e2e tests exercise the real HTTP path.
    """
    from yadgar.tests._backend_harness import patch_recall_bypass

    patch_recall_bypass(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _e2e_restore_bypass(monkeypatch):
    """Route ``_forward_restore`` → in-process ``backend.restoration.run_restore``.

    T2 Car B: restore is forward-only (POST /restore). Delegates to
    ``_backend_harness.patch_restore_bypass``. CALL-TIME guarded on
    YADGAR_EMBED_URL: real-backend restore e2e tests exercise the real HTTP path.
    """
    from yadgar.tests._backend_harness import patch_restore_bypass

    patch_restore_bypass(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _e2e_viz_bypass(monkeypatch):
    """Route ``_forward_viz`` → in-process ``backend.viz_exec.run_viz_op``.

    T2 Car E3: the /api/graph* handlers are forward-only (POST /viz). Delegates
    to ``_backend_harness.patch_viz_bypass``. CALL-TIME guarded on
    YADGAR_EMBED_URL: real-backend viz e2e tests exercise the real HTTP path.
    """
    from yadgar.tests._backend_harness import patch_viz_bypass

    patch_viz_bypass(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _e2e_consolidate_bypass(monkeypatch):
    """Route the consolidation orchestrator's ``_forward_to_backend`` in-process.

    Delegates to ``_backend_harness.patch_consolidate_bypass``.  CALL-TIME
    guarded on YADGAR_EMBED_URL.  Drops the memoized backend scheduler on
    teardown so the next module rebuilds it against its own live storage.
    """
    from yadgar.tests._backend_harness import patch_consolidate_bypass, teardown_consolidate_bypass

    patch_consolidate_bypass(monkeypatch)
    try:
        yield
    finally:
        teardown_consolidate_bypass()
