"""R3 Car 2 — cache-epoch cross-process shared-file store.

The cache-invalidation epoch must be shared across the core and backend
processes: a WRITE lands in the backend process and bumps the epoch; a
CORE read-tool cache keys on the epoch and must observe that bump. Before
Car 2 the epoch was process-local (module dicts) → the two processes had
independent counters → core caches never saw backend writes → stale
project_brief / wiki reads after a write.

These tests pin the shared-file semantics:
  1. bump_epoch(dir) then a FRESH read of _current_epoch(dir) reflects it,
     even after clearing any in-process module state (files, not dicts).
  2. A genuinely SEPARATE process (sys.executable subprocess) that reads the
     same YADGAR_QUEUE_BASE sees a bump made by the parent — proving the
     store is cross-process (a file), not a module global.
  3. bump_epoch(None) (global generation) advances _current_epoch for ALL
     directories, not just one.
  4. _reset_for_test clears the on-disk counters back to 0.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _fresh_module():
    """Import cache_epoch, dropping any cached module so a re-import re-reads
    the base dir env (belt-and-suspenders — the store is file-backed, so a
    live module already reads the shared files, but this proves no in-proc
    memo hides a stale value)."""
    import importlib

    import yadgar._shared.runtime.cache_epoch as ce

    return importlib.reload(ce)


def test_bump_then_fresh_read_reflects_bump(monkeypatch, tmp_path):
    """A bump written to the shared file is visible to a fresh read."""
    monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path))
    ce = _fresh_module()
    ce._reset_for_test()

    d = "/home/u/proj"
    before = ce._current_epoch(d)
    ce.bump_epoch(d)
    # Re-import to prove the value came from the file, not a module dict.
    ce2 = _fresh_module()
    assert ce2._current_epoch(d) == before + 1


def test_cross_process_child_sees_parent_bump(monkeypatch, tmp_path):
    """A separate OS process reading the same YADGAR_QUEUE_BASE observes the
    parent's bump — this is the actual split-brain fix (file, not dict)."""
    monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path))
    ce = _fresh_module()
    ce._reset_for_test()

    d = "/some/project/dir"
    ce.bump_epoch(d)
    ce.bump_epoch(d)  # parent bumped twice → shared file counter == 2

    # Child process: fresh interpreter, same env, reads the file.
    child_src = textwrap.dedent(
        f"""
        from yadgar._shared.runtime.cache_epoch import _current_epoch
        print(_current_epoch({d!r}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", child_src],
        capture_output=True,
        text=True,
        check=True,
    )
    child_epoch = int(result.stdout.strip())
    assert child_epoch == 2, (
        f"child process saw {child_epoch}, expected 2 (parent's bumps must "
        f"be visible cross-process via the shared file). stderr={result.stderr}"
    )


def test_global_bump_advances_all_dirs(monkeypatch, tmp_path):
    """bump_epoch(None) is the global generation — it advances every dir."""
    monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path))
    ce = _fresh_module()
    ce._reset_for_test()

    a, b = "/dir/a", "/dir/b"
    ea, eb = ce._current_epoch(a), ce._current_epoch(b)
    ce.bump_epoch(None)  # cross-directory structural event
    assert ce._current_epoch(a) == ea + 1
    assert ce._current_epoch(b) == eb + 1, "global bump must invalidate ALL dirs"


def test_per_dir_bump_isolated_from_other_dir(monkeypatch, tmp_path):
    """A per-dir bump advances only that dir; the global stays put."""
    monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path))
    ce = _fresh_module()
    ce._reset_for_test()

    a, b = "/dir/a", "/dir/b"
    eb = ce._current_epoch(b)
    ce.bump_epoch(a)
    assert ce._current_epoch(a) == 1
    assert ce._current_epoch(b) == eb, "unrelated dir must not move on a per-dir bump"


def test_reset_for_test_clears_files(monkeypatch, tmp_path):
    """_reset_for_test wipes on-disk counters back to 0."""
    monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path))
    ce = _fresh_module()
    ce._reset_for_test()

    d = "/dir/x"
    ce.bump_epoch(d)
    ce.bump_epoch(None)
    assert ce._current_epoch(d) >= 2
    ce._reset_for_test()
    assert ce._current_epoch(d) == 0, "reset must clear both per-dir and global counters"

    # Fresh re-import after reset also reads 0 (files really gone).
    ce2 = _fresh_module()
    assert ce2._current_epoch(d) == 0


def test_missing_file_reads_zero(monkeypatch, tmp_path):
    """A never-bumped dir reads 0 (missing counter file → 0, no error)."""
    monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path))
    ce = _fresh_module()
    ce._reset_for_test()
    assert ce._current_epoch("/never/touched") == 0
    assert ce._current_epoch(None) == 0
