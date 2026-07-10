"""P0 #37 Option B — entrypoint-backend.sh safe-stop ordering + torn-stop marker.

RCA: docs/plans/surrealkv-safe-stop-2026-07-10.md. SurrealKV's `impl Drop for
Tree` skips the async store close when the tokio runtime is already torn down
(upstream, unconditional on v3.1.5). The entrypoint cannot fix that, but it
must:

  1. stop the WRITERS first (embed uvicorn + wiki-backup + inode-guard loops)
     so no HTTP write is mid-flight against surreal when it begins shutdown;
  2. THEN SIGTERM surreal and WAIT for its own exit, capturing the status,
     under an internal deadline shorter than podman's --stop-timeout 30;
  3. write a SURREAL_UNCLEAN_STOP marker to $YADGAR_LOG_DIR when surreal
     exits non-zero or overruns the deadline — so a torn stop is DETECTABLE
     (feeds the Option D safe-start auto-restore).

Tests drive the REAL functions extracted from entrypoint-backend.sh (between
the `# --- safe-stop begin` / `# --- safe-stop end` markers) via a bash
harness with fake surreal/embed processes — per the repo's bash-subprocess
test precedent (test_credentials_required.py).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ENTRYPOINT = _REPO_ROOT / "entrypoint-backend.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_block(begin_marker: str, end_marker: str) -> str:
    """Return the entrypoint text between two marker comment lines."""
    text = _ENTRYPOINT.read_text()
    assert begin_marker in text, f"marker {begin_marker!r} missing from entrypoint-backend.sh"
    assert end_marker in text, f"marker {end_marker!r} missing from entrypoint-backend.sh"
    # Cut at the newline AFTER the begin marker so the rest of the marker
    # comment line does not leak into the harness as bash tokens.
    after = text.split(begin_marker, 1)[1].partition("\n")[2]
    block = after.split(end_marker, 1)[0]
    # Drop the trailing partial comment line the end marker sits on.
    return block.rsplit("\n", 1)[0] + "\n"


def _run_harness(tmp_path: Path, harness_body: str, timeout: float = 30.0):
    """Write a bash harness that sources the safe-stop block, run it, return result."""
    safe_stop = _extract_block("# --- safe-stop begin", "# --- safe-stop end")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\n"
        f'YADGAR_LOG_DIR="{tmp_path}"\n'
        f'ORDER_LOG="{tmp_path}/order.log"\n'
        "SURREAL_STOP_DEADLINE=2\n"
        # Reap all fake background processes even when the harness dies early
        # (a leaked while-true fake idles forever — the orphan-surreal lesson).
        "trap 'kill -9 $(jobs -p) 2>/dev/null' EXIT\n"
        f"{safe_stop}\n"
        f"{harness_body}\n"
    )
    harness.chmod(0o755)
    return subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=timeout)


_FAKE_EMBED = (
    'bash -c \'trap "echo embed-term >> "$ORDER_LOG"; exit 0" TERM; '
    "while true; do sleep 0.05; done' &\nEMBED_PID=$!\n"
)

_FAKE_SURREAL_CLEAN = (
    'bash -c \'trap "echo surreal-term >> "$ORDER_LOG"; exit 0" TERM; '
    "while true; do sleep 0.05; done' &\nSURREAL_PID=$!\n"
)

_FAKE_SURREAL_NONZERO = (
    'bash -c \'trap "echo surreal-term >> "$ORDER_LOG"; exit 7" TERM; '
    "while true; do sleep 0.05; done' &\nSURREAL_PID=$!\n"
)

# Ignores SIGTERM entirely — forces the deadline/SIGKILL path.
_FAKE_SURREAL_STUBBORN = (
    "bash -c 'trap \"\" TERM; while true; do sleep 0.05; done' &\nSURREAL_PID=$!\n"
)


# ---------------------------------------------------------------------------
# Behavioral tests (bash harness against the REAL extracted functions)
# ---------------------------------------------------------------------------


class TestSafeStopOrdering:
    def test_writers_stopped_before_surreal(self, tmp_path):
        """cleanup() must TERM+reap the embed writer BEFORE signalling surreal."""
        body = (
            "export ORDER_LOG\n"
            + _FAKE_EMBED
            + _FAKE_SURREAL_CLEAN
            + "sleep 0.3\n"  # let both fakes install their traps
            + "cleanup\n"
        )
        result = _run_harness(tmp_path, body)
        order = (tmp_path / "order.log").read_text().splitlines()
        assert order == ["embed-term", "surreal-term"], (
            f"writers must stop BEFORE surreal; got order={order}, stderr={result.stderr!r}"
        )

    def test_clean_surreal_exit_no_marker_and_exit_zero(self, tmp_path):
        """Surreal exits 0 on TERM → no torn-stop marker, cleanup exits 0."""
        body = "export ORDER_LOG\n" + _FAKE_EMBED + _FAKE_SURREAL_CLEAN + "sleep 0.3\ncleanup\n"
        result = _run_harness(tmp_path, body)
        assert result.returncode == 0, f"clean stop must exit 0; stderr={result.stderr!r}"
        assert not (tmp_path / "SURREAL_UNCLEAN_STOP").exists(), (
            "no torn-stop marker on a clean surreal exit"
        )

    def test_nonzero_surreal_exit_writes_marker(self, tmp_path):
        """Surreal exits 7 on TERM → SURREAL_UNCLEAN_STOP marker with the status."""
        body = "export ORDER_LOG\n" + _FAKE_EMBED + _FAKE_SURREAL_NONZERO + "sleep 0.3\ncleanup\n"
        result = _run_harness(tmp_path, body)
        marker = tmp_path / "SURREAL_UNCLEAN_STOP"
        assert marker.exists(), f"marker must exist on non-zero exit; stderr={result.stderr!r}"
        content = marker.read_text()
        assert "reason=nonzero-exit" in content
        assert "surreal_exit_status=7" in content
        assert result.returncode != 0, "cleanup must exit non-zero on a torn stop"

    def test_deadline_overrun_writes_timeout_marker_and_kills(self, tmp_path):
        """Surreal ignoring TERM beyond the deadline → timeout marker + SIGKILL."""
        start = time.monotonic()
        body = _FAKE_EMBED + _FAKE_SURREAL_STUBBORN + "sleep 0.3\ncleanup\n"
        result = _run_harness(tmp_path, body, timeout=25.0)
        elapsed = time.monotonic() - start
        marker = tmp_path / "SURREAL_UNCLEAN_STOP"
        assert marker.exists(), f"marker must exist on deadline overrun; stderr={result.stderr!r}"
        assert "reason=timeout" in marker.read_text()
        assert result.returncode != 0
        # deadline=2s; the whole harness must finish well under podman's 30s
        assert elapsed < 15.0, f"stop must respect the internal deadline; took {elapsed:.1f}s"

    def test_stop_surreal_waits_for_exit_before_returning(self, tmp_path):
        """The container must not exit while surreal is still shutting down.

        Fake surreal takes ~1s to exit after TERM; cleanup must still be
        running when it finishes (i.e. cleanup returns AFTER surreal's exit).
        """
        slow_surreal = (
            'bash -c \'trap "sleep 1; echo surreal-done >> "$ORDER_LOG"; exit 0" TERM; '
            "while true; do sleep 0.05; done' &\nSURREAL_PID=$!\n"
        )
        body = (
            "export ORDER_LOG\n"
            + slow_surreal
            + "sleep 0.3\n_stop_surreal_and_wait\n"
            + f'echo stop-returned >> "{tmp_path}/order.log"\n'
        )
        _run_harness(tmp_path, body)
        order = (tmp_path / "order.log").read_text().splitlines()
        assert "surreal-done" in order and "stop-returned" in order
        assert order.index("surreal-done") < order.index("stop-returned"), (
            f"_stop_surreal_and_wait must WAIT for surreal's exit before returning; got {order}"
        )


class TestInodeGuard:
    """5a (in-container): surreal fds resolving outside /data/surreal_db = split brain."""

    def _run_guard(self, tmp_path: Path, fd_dir_name: str):
        guard = _extract_block("# --- inode-guard begin", "# --- inode-guard end")
        (tmp_path / fd_dir_name).mkdir()
        harness = tmp_path / "guard_harness.sh"
        harness.write_text(
            "#!/bin/bash\n"
            f'YADGAR_LOG_DIR="{tmp_path}"\n'
            f'SPLIT_BRAIN_MARKER="{tmp_path}/SURREAL_SPLIT_BRAIN"\n'
            f'SURREAL_DATA_ROOT="{tmp_path}"\n'
            # Fake surreal holding an fd open inside fd_dir_name
            f'bash -c \'exec 9> "{tmp_path}/{fd_dir_name}/held.file"; '
            "while true; do sleep 0.05; done' &\nSURREAL_PID=$!\n"
            # EXIT trap: reap the fake even if the sourced guard fails to parse
            # (a leaked fake idles forever — the orphan-pytest-surreal lesson).
            "trap 'kill -9 \"$SURREAL_PID\" 2>/dev/null' EXIT\n"
            "sleep 0.2\n"
            f"{guard}\n"
            "_check_store_inode_coherence\nrc=$?\n"
            'kill -9 "$SURREAL_PID" 2>/dev/null\n'
            "exit $rc\n"
        )
        harness.chmod(0o755)
        return subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=15)

    def test_fd_in_old_dir_flags_split_brain(self, tmp_path):
        result = self._run_guard(tmp_path, "surreal_db.old-20260709_191332")
        assert result.returncode != 0, (
            f"fd open inside surreal_db.old-* must flag split brain; stderr={result.stderr!r}"
        )
        assert "SPLIT_BRAIN" in result.stderr
        assert (tmp_path / "SURREAL_SPLIT_BRAIN").exists()

    def test_fd_in_canonical_is_coherent(self, tmp_path):
        result = self._run_guard(tmp_path, "surreal_db")
        assert result.returncode == 0, (
            f"fd inside the canonical dir is coherent; stderr={result.stderr!r}"
        )
        assert not (tmp_path / "SURREAL_SPLIT_BRAIN").exists()


# ---------------------------------------------------------------------------
# Textual invariants on the shipped entrypoint (cheap regression guards)
# ---------------------------------------------------------------------------


class TestEntrypointText:
    @pytest.fixture()
    def text(self) -> str:
        return _ENTRYPOINT.read_text()

    def test_cleanup_no_longer_kills_surreal_and_embed_together(self, text):
        """The old cleanup killed all PIDs in one `kill` — writers-first now."""
        assert 'kill "$SURREAL_PID" "$EMBED_PID"' not in text, (
            "cleanup must not signal surreal and the embed writer in one kill — "
            "writers stop FIRST (P0 #37 Option B)"
        )

    def test_internal_deadline_below_podman_stop_timeout(self, text):
        assert "SURREAL_STOP_DEADLINE" in text
        assert ":-25}" in text.split("SURREAL_STOP_DEADLINE=")[1].split("\n")[0], (
            "default internal deadline must be 25s (< podman --stop-timeout 30)"
        )

    def test_marker_written_under_log_dir(self, text):
        assert 'TORN_STOP_MARKER="${YADGAR_LOG_DIR}/SURREAL_UNCLEAN_STOP"' in text

    def test_startup_logs_previous_unclean_stop(self, text):
        assert 'if [ -f "${TORN_STOP_MARKER}" ]' in text, (
            "startup must surface a previous torn stop (observability)"
        )
