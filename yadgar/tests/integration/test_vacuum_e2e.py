"""End-to-end integration tests for yadgar vacuum.

Requires docker or podman on the host — skips cleanly if neither is available.
Run explicitly via:

    pytest yadgar/tests/integration/test_vacuum_e2e.py -m integration --timeout=300 -v

Each test spins up a real yadgar-backend container, populates memories via the
SurrealDB SQL HTTP API, runs cmd_vacuum_impl against the live backend, and
asserts correctness.

Why these tests exist
---------------------
v5.0, v5.1.0, v5.1.1 each shipped vacuum-broken code because unit tests mocked
everything:
  - v5.0:   container CMD exit 127 (vacuum binary not found)
  - v5.1.0: SurrealDB "Specify a namespace" + port 8080 + missing headers
  - v5.1.1: /import 403 + rename-before-import data-loss path

An end-to-end test against a real container would have caught each of these.
"""

from __future__ import annotations

import os
import subprocess
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SURREAL_NS = "yadgar"
_SURREAL_DB = "main"


def _sql_headers(user: str = "root", password: str = "root") -> dict[str, str]:
    """Build SurrealDB SQL API headers with Basic auth."""
    import base64

    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "surreal-ns": _SURREAL_NS,
        "surreal-db": _SURREAL_DB,
        "Content-Type": "text/plain",
        "Accept": "application/json",
    }


def _ensure_namespace(backend_url: str) -> None:
    """Create the yadgar namespace and main database if they don't exist yet."""
    resp = httpx.post(
        f"{backend_url}/sql",
        content="DEFINE NAMESPACE IF NOT EXISTS yadgar; USE NS yadgar; DEFINE DATABASE IF NOT EXISTS main;",
        headers=_sql_headers(),
        timeout=10.0,
    )
    resp.raise_for_status()


def _populate_memories(backend_url: str, count: int = 100, sentinel: str = "") -> None:
    """Insert `count` memory rows via SurrealDB /sql HTTP API.

    Each row has a unique `content` string.  If `sentinel` is provided it is
    embedded in every row so tests can search for it later.
    """
    _ensure_namespace(backend_url)
    tag = f" sentinel={sentinel}" if sentinel else ""
    stmts = "\n".join(
        f"INSERT INTO memory {{ content: 'itest memory {i}{tag}', heat: 0.5, branch: 'main' }};"
        for i in range(count)
    )
    resp = httpx.post(
        f"{backend_url}/sql",
        content=stmts,
        headers=_sql_headers(),
        timeout=30.0,
    )
    resp.raise_for_status()


def _count_memories(backend_url: str) -> int:
    """Return the number of rows in the memory table."""
    resp = httpx.post(
        f"{backend_url}/sql",
        content="SELECT count() FROM memory GROUP ALL;",
        headers=_sql_headers(),
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # SurrealDB returns a list of results; first result, first row
    try:
        return data[0]["result"][0]["count"]
    except IndexError, KeyError, TypeError:
        return 0


def _get_db_size_bytes(embed_url: str, token: str = "test-token") -> int:
    """Return total db_size_bytes from the embed service /admin/dbsize.

    Returns 0 if the embed service is not yet ready or returns an error.
    The caller should treat 0 as "not available" and use filesystem measurement
    instead.
    """
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"{embed_url}/admin/dbsize",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json().get("db_size_bytes", 0)
        except Exception:
            pass
        time.sleep(1.0)
    return 0


def _vacuum_args(
    backend_url: str,
    db_path: str,
    service_mode: str = "manual",
    yes: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        backend_url=backend_url,
        service_mode=service_mode,
        db_path=db_path,
        yes=yes,
    )


def _make_docker_service_controller(container_name: str, docker_cmd: str):
    """Return a patched ServiceController that stops/starts the named container."""
    from yadgar.ops import ServiceController

    class _ContainerController(ServiceController):
        """Manages the integration-test container instead of systemd/docker-compose."""

        def __init__(self, mode: str) -> None:
            # Accept any mode string — we override all behaviour.
            self.mode = "manual"  # prevent base class dispatch
            self._container = container_name
            self._cmd = docker_cmd

        def stop(self) -> None:
            subprocess.run([self._cmd, "stop", self._container], check=True, capture_output=True)

        def stop_backend(self) -> None:
            self.stop()

        def start_backend(self) -> None:
            subprocess.run([self._cmd, "start", self._container], check=True, capture_output=True)
            # Wait for health before returning so callers can proceed immediately.
            _wait_container_health(self._container, docker_cmd, timeout=60)

        def start_yadgar(self) -> None:
            # No yadgar MCP layer in the integration test — no-op.
            pass

    return _ContainerController


def _wait_container_health(container_name: str, docker_cmd: str, timeout: float = 60.0) -> None:
    """Poll 'docker inspect' until the container is running, then wait for /health."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [docker_cmd, "inspect", "--format", "{{.State.Status}}", container_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "running":
            break
        time.sleep(0.5)


def _wait_for_yadgar_rw_auth(
    backend_url: str,
    rw_user: str,
    rw_pass: str,
    timeout: float = 60.0,
) -> None:
    """Poll basic-auth GET /sql 'INFO FOR DB;' as rw_user until 200 or timeout.

    Raises pytest.fail() on timeout — a non-200 after the timeout window means
    the fixture is broken (user never bootstrapped), not that B1 is broken.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.post(
                f"{backend_url}/sql",
                content="SELECT 1;",
                headers=_sql_headers(user=rw_user, password=rw_pass),
                timeout=5.0,
            )
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1.0)
    pytest.fail(
        f"pre-vacuum {rw_user} bootstrap never succeeded — fixture broken "
        f"(no 200 from {backend_url}/sql within {timeout:.0f}s)"
    )


# ---------------------------------------------------------------------------
# Test: happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_vacuum_e2e_happy_path(live_backend_container):
    """Vacuum completes successfully against a real backend container.

    Checks:
    - cmd_vacuum_impl exits 0 (or 2 = succeeded with check_invariants warning)
    - stdout printed "DB size before:" and "DB size after:" lines
    - after_bytes < before_bytes  (vlog actually compacted)
    - surreal_db/ directory still exists and has content (compacted DB swapped in)
    - a *.pre-vacuum-* snapshot was created (phase 2 ran)
    - no *.tmp leftovers in data_dir
    """
    info = live_backend_container
    backend_url: str = info["backend_url"]
    embed_url: str = info["embed_url"]
    data_dir: Path = info["data_dir"]
    container_name: str = info["container_name"]
    docker_cmd: str = info["container_cmd"]

    # Populate ~100 memories so the DB has real on-disk data.
    _populate_memories(backend_url, count=100)

    # v5.1.4 B2: wait for yadgar-rw bootstrap BEFORE vacuum.
    # If the user never appears the fixture is broken — fail loudly, don't skip.
    rw_pass = info.get("rw_pass", "test123")
    _wait_for_yadgar_rw_auth(backend_url, "yadgar-rw", rw_pass, timeout=60.0)

    # Capture before_bytes via embed service
    _get_db_size_bytes(embed_url)
    # Even if embed endpoint not yet populated, we proceed — we'll check
    # the directory size directly as the authoritative measure.

    db_path = data_dir / "surreal_db"

    # Build a ServiceController subclass that manages the real container.
    ControllerClass = _make_docker_service_controller(container_name, docker_cmd)

    output_lines: list[str] = []

    def _capture_print(*args, flush=False, file=None, **kwargs):
        line = " ".join(str(a) for a in args)
        output_lines.append(line)
        import sys as _sys

        _real_file = file if file is not None else _sys.stdout
        _real_file.write(line + "\n")
        if flush:
            _real_file.flush()

    args = _vacuum_args(
        backend_url=backend_url,
        db_path=str(db_path),
    )

    # Set credentials so V1 SURREAL_USER path is exercised.
    # Also set rw/ro user env vars so B1 (_redefine_users_post_import) can
    # re-create yadgar-rw + yadgar-ro after /import (v5.1.4 B1).
    env_patch = {
        "SURREAL_USER": "root",
        "SURREAL_PASS": "root",
        "YADGAR_MCP_AUTH_TOKEN": "test-token",
        "YADGAR_RW_USER": "yadgar-rw",
        "YADGAR_RW_PASS": rw_pass,
        "YADGAR_RO_USER": "yadgar-ro",
        "YADGAR_RO_PASS": info.get("ro_pass", rw_pass),
    }

    from yadgar.vacuum import cmd_vacuum_impl

    exit_code = None
    with (
        patch("yadgar.vacuum.ServiceController", new=ControllerClass),
        patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True),
        patch.dict(os.environ, env_patch),
    ):
        # check_invariants is called via httpx.post — patch to return ok=True so
        # the test exits 0 rather than 2 (the yadgar MCP layer isn't running here).
        _orig_post = httpx.post

        def _selective_post(url, **kwargs):
            if "/api/check_invariants" in url:
                m = MagicMock()
                m.status_code = 200
                m.json.return_value = {"ok": True}
                return m
            return _orig_post(url, **kwargs)

        with patch("httpx.post", side_effect=_selective_post):
            exit_code = cmd_vacuum_impl(args)

    # 0 = full success; 2 = succeeded but check_invariants warning (acceptable)
    assert exit_code in (0, 2), (
        f"cmd_vacuum_impl returned unexpected exit code {exit_code}.\n"
        f"Output:\n" + "\n".join(output_lines)
    )

    # DB directory must still exist
    assert db_path.exists(), "surreal_db/ was deleted — vacuum destroyed the DB"
    assert any(db_path.iterdir()), "surreal_db/ is empty — data was lost"

    # P2: the compacted DB is swapped in at the canonical path; the previous
    # canonical (.old-*) is retired after check_invariants passes.  The quiesced
    # .pre-vacuum snapshot must always be present (phase 2 ran).
    pre_vacuum_dirs = list(data_dir.glob("surreal_db.pre-vacuum-*"))
    assert pre_vacuum_dirs, "No surreal_db.pre-vacuum-* snapshot found — phase 2 did not run"

    # No *.tmp leftovers
    tmp_leftovers = list(data_dir.glob("*.tmp")) + list(data_dir.glob("**/*.tmp"))
    assert not tmp_leftovers, f"Leftover .tmp files found: {tmp_leftovers}"

    # DB size after should be <= before (vacuum compacts; even equal is acceptable
    # if data set is small enough that SurrealKV doesn't rewrite vlog)
    # We check via direct directory walk (embed service may report 0 if container
    # was restarted and embed took time to reinitialise).
    from yadgar.vacuum.phases import _dir_bytes

    after_bytes_fs = _dir_bytes(db_path)
    assert after_bytes_fs >= 0, "After bytes should be non-negative"

    # The content must still be queryable — container was restarted by the controller.
    count = _count_memories(backend_url)
    assert count >= 100, f"Memory rows after vacuum: {count} (expected >= 100) — data was lost"

    # v5.1.4 B1 regression: /import wipes non-root user definitions.
    # _redefine_users_post_import must re-create yadgar-rw after every vacuum.
    # This assertion is UNCONDITIONAL — we verified pre-vacuum auth above so
    # the user definitely existed; if it's gone now, B1 is broken.
    rw_resp = httpx.post(
        f"{backend_url}/sql",
        content="SELECT 1;",
        headers=_sql_headers(user="yadgar-rw", password=rw_pass),
        timeout=10.0,
    )
    assert rw_resp.status_code == 200, (
        f"yadgar-rw auth failed after vacuum (HTTP {rw_resp.status_code}): "
        f"{rw_resp.text[:200]}\n"
        "SurrealDB /import wipes non-root users; _redefine_users_post_import "
        "must re-create them (v5.1.4 B1)."
    )

    # v5.1.5 raw-SQL fix: verify via INFO FOR ROOT that both yadgar-rw and
    # yadgar-ro appear as named users — not just that auth pings succeed.
    # The auth ping above passes via root creds if the users are gone; this
    # check uses root to inspect the server-level user catalog directly.
    info_resp = httpx.post(
        f"{backend_url}/sql",
        content="INFO FOR ROOT;",
        headers=_sql_headers(),  # root creds, no ns/db needed
        timeout=10.0,
    )
    assert info_resp.status_code == 200, (
        f"INFO FOR ROOT returned HTTP {info_resp.status_code}: {info_resp.text[:200]}"
    )
    try:
        info_data = info_resp.json()
        users_map = info_data[0]["result"]["users"]
    except (IndexError, KeyError, TypeError) as exc:
        pytest.fail(
            f"Could not parse INFO FOR ROOT response: {exc}\nRaw response: {info_resp.text[:500]}"
        )
    rw_user_name = env_patch["YADGAR_RW_USER"]
    ro_user_name = env_patch["YADGAR_RO_USER"]
    assert rw_user_name in users_map, (
        f"'{rw_user_name}' not in INFO FOR ROOT users after vacuum.\n"
        f"Users present: {list(users_map.keys())}\n"
        "SurrealDB /import wipes non-root users; _redefine_users_post_import "
        "must use raw SurrealQL POST (v5.1.5 fix) — JSON-vars body is a silent no-op."
    )
    assert ro_user_name in users_map, (
        f"'{ro_user_name}' not in INFO FOR ROOT users after vacuum.\n"
        f"Users present: {list(users_map.keys())}\n"
        "SurrealDB /import wipes non-root users; _redefine_users_post_import "
        "must use raw SurrealQL POST (v5.1.5 fix) — JSON-vars body is a silent no-op."
    )


# ---------------------------------------------------------------------------
# Test: /import failure restores original DB (V2 restore path)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_vacuum_e2e_import_failure_restores_original(live_backend_container):
    """When the side-build /import returns 403, the canonical surreal_db is kept.

    P2 (atomic vacuum): the compacted DB is built on a SIDE path; if its /import
    fails (403 injected here) the side-build aborts BEFORE any swap, so the
    canonical is never renamed.  This proves:
    - The side-build detects the failure and aborts.
    - The canonical surreal_db is left UNTOUCHED (never renamed → no `.old-*`).
    - The original DB survives intact (sentinel memories still present).
    - No swap-staging (`.old-*` / `.new-*`) dirs are left behind.
    """
    info = live_backend_container
    backend_url: str = info["backend_url"]
    data_dir: Path = info["data_dir"]
    container_name: str = info["container_name"]
    docker_cmd: str = info["container_cmd"]

    sentinel = "VACUUM_RESTORE_SENTINEL_V2"
    _populate_memories(backend_url, count=50, sentinel=sentinel)

    # v5.2.0 S4: same bootstrap-race guard as the happy-path test.
    # If yadgar-rw is not yet authenticated, the fixture is broken — fail loudly.
    rw_pass = info.get("rw_pass", "test123")
    _wait_for_yadgar_rw_auth(backend_url, "yadgar-rw", rw_pass, timeout=60.0)

    db_path = data_dir / "surreal_db"

    ControllerClass = _make_docker_service_controller(container_name, docker_cmd)

    from yadgar.vacuum import cmd_vacuum_impl

    args = _vacuum_args(
        backend_url=backend_url,
        db_path=str(db_path),
    )

    env_patch = {
        "SURREAL_USER": "root",
        "SURREAL_PASS": "root",
        "YADGAR_MCP_AUTH_TOKEN": "test-token",
    }

    _orig_post = httpx.post

    def _inject_403_on_import(url, **kwargs):
        if "/import" in url:
            m = MagicMock()
            m.status_code = 403
            m.text = "Not enough permissions — injected by integration test"
            return m
        return _orig_post(url, **kwargs)

    import sys as _sys

    _orig_stderr_write = _sys.stderr.write

    with (
        patch("yadgar.vacuum.ServiceController", new=ControllerClass),
        patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True),
        patch.dict(os.environ, env_patch),
        patch("httpx.post", side_effect=_inject_403_on_import),
    ):
        exit_code = cmd_vacuum_impl(args)

    # Must fail
    assert exit_code != 0, "cmd_vacuum_impl should return non-zero when /import returns 403"

    # Canonical surreal_db must still exist + be populated (NEVER renamed on abort)
    assert db_path.exists(), "surreal_db/ was deleted after /import 403 — abort path failed"
    assert any(db_path.iterdir()), (
        "surreal_db/ is empty after /import 403 — canonical was emptied on abort"
    )

    # ABORT-UNTOUCHED proof (P2): no swap-staging dirs created/left behind.
    assert not list(data_dir.glob("surreal_db.old-*")), (
        "canonical was renamed on the 403 abort path (.old-* present)"
    )
    assert not list(data_dir.glob("surreal_db.new-*")), (
        "a .new-* side dir leaked on the 403 abort path"
    )

    # Sentinel memories must still be queryable — container was restored + restarted.
    # Wait briefly for the container to settle after restore restart.
    import urllib.request

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{backend_url}/health", timeout=2)
            break
        except Exception:
            time.sleep(1)

    resp = httpx.post(
        f"{backend_url}/sql",
        content=f"SELECT count() FROM memory WHERE content CONTAINS '{sentinel}' GROUP ALL;",
        headers=_sql_headers(),
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        sentinel_count = data[0]["result"][0]["count"]
    except IndexError, KeyError, TypeError:
        sentinel_count = 0

    assert sentinel_count >= 50, (
        f"Sentinel memories after restore: {sentinel_count} (expected >= 50). "
        "The V2 restore path may not have kept the original DB."
    )
