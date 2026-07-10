"""P0 #37 Option D + 5b — yadgar.backend.safe_start (torn-manifest auto-restore).

RCA: docs/plans/surrealkv-safe-stop-2026-07-10.md §4–6. Key invariants under test:

  - restore-source selection is by NEWEST INNER-FILE mtime, never dir name or
    dir mtime (os.rename preserves dir mtime — proven misleading on 07-10);
  - the corrupt canonical is preserved aside as surreal_db.CORRUPT-<ts>,
    NEVER deleted;
  - the restore is a COPY (source survives as fallback), and the stale LOCK
    is removed from the restored canonical;
  - split-brain preflight (5b): a leftover .old-* with inner files NEWER than
    the canonical's refuses startup (exit 4) — a human decides (runbook);
  - recovery only fires on the documented torn-manifest failure signature.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from yadgar.backend import safe_start

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TORN_LOG = (
    "2026-07-10T11:19:23.000Z ERROR surrealkv::levels: Error loading table 11: "
    'Io(NotFound) "No such file or directory"\n'
    "2026-07-10T11:19:23.100Z ERROR surrealdb_server::cli: There was a problem with "
    "the datastore: Failed to load manifest: IO error: No such file or directory (os error 2)\n"
)


def _make_store(d: Path, *, stamp: float | None = None, with_lock: bool = False) -> Path:
    """Create a minimal structurally-complete surrealkv dir layout."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest").write_bytes(b"\x00manifest")
    for sub in ("sstables", "vlog", "wal"):
        (d / sub).mkdir(exist_ok=True)
    (d / "vlog" / "00000000000000000001.vlog").write_bytes(b"v" * 64)
    (d / "wal" / "00000000000000000001.wal").write_bytes(b"w" * 64)
    (d / "sstables" / "00000000000000000010.sst").write_bytes(b"s" * 64)
    if with_lock:
        (d / "LOCK").write_bytes(b"")
    if stamp is not None:
        for f in d.rglob("*"):
            os.utime(f, (stamp, stamp))
        os.utime(d, (stamp, stamp))
    return d


# ---------------------------------------------------------------------------
# Torn-manifest signature detection
# ---------------------------------------------------------------------------


class TestTornManifestSignature:
    def test_rca_failure_signature_detected(self):
        assert safe_start.is_torn_manifest_failure(_TORN_LOG) is True

    def test_manifest_line_alone_detected(self):
        assert safe_start.is_torn_manifest_failure("Failed to load manifest: IO error") is True

    def test_table_notfound_alone_detected(self):
        assert safe_start.is_torn_manifest_failure("Error loading table 3: Io(NotFound)") is True

    def test_unrelated_error_not_detected(self):
        assert safe_start.is_torn_manifest_failure("address already in use") is False

    def test_empty_log_not_detected(self):
        assert safe_start.is_torn_manifest_failure("") is False


# ---------------------------------------------------------------------------
# Restore-source selection: INNER-file mtime, never dir name / dir mtime
# ---------------------------------------------------------------------------


class TestRestoreSourceSelection:
    def test_newest_inner_mtime_ignores_dir_mtime(self, tmp_path):
        d = _make_store(tmp_path / "store")
        now = time.time()
        inner = d / "wal" / "00000000000000000001.wal"
        os.utime(inner, (now, now))
        os.utime(d, (now - 86400 * 30, now - 86400 * 30))  # dir mtime LIES (30d old)
        got = safe_start.newest_inner_mtime(d)
        assert got == pytest.approx(now, abs=2.0)

    def test_newest_inner_mtime_empty_dir_is_none(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert safe_start.newest_inner_mtime(d) is None

    def test_choose_source_picks_freshest_inner_content_not_name(self, tmp_path):
        now = time.time()
        # OLDER name, but FRESHER inner content (the 07-10 incident shape:
        # .old-<yesterday> held live writes through the crash).
        old = _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now - 60)
        # NEWER name, stale content.
        pre = _make_store(tmp_path / "surreal_db.pre-vacuum-20260710_120000", stamp=now - 7200)
        chosen = safe_start.choose_restore_source([pre, old])
        assert chosen == old, "must pick by newest INNER-file mtime, not dir name"

    def test_choose_source_skips_structurally_incomplete(self, tmp_path):
        now = time.time()
        complete = _make_store(tmp_path / "surreal_db.pre-vacuum-20260701_000000", stamp=now - 7200)
        torn = tmp_path / "surreal_db.old-20260710_000000"
        torn.mkdir()
        (torn / "vlog").mkdir()
        f = torn / "vlog" / "1.vlog"
        f.write_bytes(b"v")  # fresh but NO manifest → incomplete
        os.utime(f, (now, now))
        chosen = safe_start.choose_restore_source([complete, torn])
        assert chosen == complete, "structurally incomplete candidates must be skipped"

    def test_choose_source_none_when_no_viable_candidate(self, tmp_path):
        empty = tmp_path / "surreal_db.old-20260710_000000"
        empty.mkdir()
        assert safe_start.choose_restore_source([empty]) is None

    def test_is_structurally_complete_requires_manifest(self, tmp_path):
        d = _make_store(tmp_path / "s")
        assert safe_start.is_structurally_complete(d) is True
        (d / "manifest").unlink()
        assert safe_start.is_structurally_complete(d) is False


# ---------------------------------------------------------------------------
# Auto-restore choreography (runbook §6, automated)
# ---------------------------------------------------------------------------


class TestPerformAutoRestore:
    def _incident_layout(self, tmp_path):
        now = time.time()
        canonical = _make_store(tmp_path / "surreal_db", stamp=now - 30, with_lock=True)
        old = _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now - 10)
        return canonical, old

    def test_corrupt_canonical_preserved_aside_never_deleted(self, tmp_path):
        canonical, _old = self._incident_layout(tmp_path)
        marker = canonical / "manifest"
        marker.write_bytes(b"TORN")
        safe_start.perform_auto_restore(tmp_path)
        corrupt = list(tmp_path.glob("surreal_db.CORRUPT-*"))
        assert len(corrupt) == 1, "corrupt canonical must be moved aside, not deleted"
        assert (corrupt[0] / "manifest").read_bytes() == b"TORN"

    def test_restore_is_copy_source_survives(self, tmp_path):
        _canonical, old = self._incident_layout(tmp_path)
        source, _aside = safe_start.perform_auto_restore(tmp_path)
        assert source == old
        assert old.exists(), "restore must COPY — the source stays as fallback"
        assert (tmp_path / "surreal_db" / "manifest").exists()

    def test_stale_lock_removed_from_restored_canonical(self, tmp_path):
        _canonical, old = self._incident_layout(tmp_path)
        (old / "LOCK").write_bytes(b"stale")
        safe_start.perform_auto_restore(tmp_path)
        assert not (tmp_path / "surreal_db" / "LOCK").exists(), (
            "stale LOCK must be removed so the fresh backend can acquire it"
        )

    def test_raises_when_no_restore_source(self, tmp_path):
        _make_store(tmp_path / "surreal_db")
        with pytest.raises(RuntimeError):
            safe_start.perform_auto_restore(tmp_path)

    def test_restore_works_when_canonical_absent(self, tmp_path):
        _make_store(tmp_path / "surreal_db.old-20260709_191332")
        source, aside = safe_start.perform_auto_restore(tmp_path)
        assert (tmp_path / "surreal_db" / "manifest").exists()
        assert aside is None
        assert source.exists()


# ---------------------------------------------------------------------------
# 5b split-brain preflight
# ---------------------------------------------------------------------------


class TestSplitBrainPreflight:
    def test_fresher_old_refuses(self, tmp_path):
        now = time.time()
        _make_store(tmp_path / "surreal_db", stamp=now - 3600)
        old = _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now)
        assert safe_start.detect_split_brain(tmp_path) == old

    def test_stale_old_passes(self, tmp_path):
        now = time.time()
        _make_store(tmp_path / "surreal_db", stamp=now)
        _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now - 3600)
        assert safe_start.detect_split_brain(tmp_path) is None

    def test_no_old_passes(self, tmp_path):
        _make_store(tmp_path / "surreal_db")
        assert safe_start.detect_split_brain(tmp_path) is None

    def test_absent_canonical_passes(self, tmp_path):
        # Fresh install / mid-swap crash — vacuum recovery owns that state.
        _make_store(tmp_path / "surreal_db.old-20260709_191332")
        assert safe_start.detect_split_brain(tmp_path) is None

    def test_empty_canonical_with_populated_old_refuses(self, tmp_path):
        (tmp_path / "surreal_db").mkdir()
        old = _make_store(tmp_path / "surreal_db.old-20260709_191332")
        assert safe_start.detect_split_brain(tmp_path) == old

    def test_within_tolerance_passes(self, tmp_path):
        now = time.time()
        _make_store(tmp_path / "surreal_db", stamp=now - 30)
        _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now - 10)
        # 20s fresher < 60s tolerance → not split-brain evidence
        assert safe_start.detect_split_brain(tmp_path, tolerance_s=60.0) is None


# ---------------------------------------------------------------------------
# CLI (consumed by entrypoint-backend.sh) — exit-code contract
# ---------------------------------------------------------------------------


class TestCli:
    def test_preflight_ok_exit_0(self, tmp_path):
        _make_store(tmp_path / "surreal_db")
        assert safe_start.main(["preflight", "--data-dir", str(tmp_path)]) == 0

    def test_preflight_split_brain_exit_4_mentions_runbook(self, tmp_path, capsys):
        now = time.time()
        _make_store(tmp_path / "surreal_db", stamp=now - 3600)
        _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now)
        rc = safe_start.main(["preflight", "--data-dir", str(tmp_path)])
        assert rc == 4
        err = capsys.readouterr().err
        assert "surrealkv-safe-stop-2026-07-10.md" in err, "must point at the runbook"

    def test_recover_not_torn_exit_2(self, tmp_path):
        _make_store(tmp_path / "surreal_db")
        log = tmp_path / "startup.log"
        log.write_text("address already in use")
        rc = safe_start.main(["recover", "--data-dir", str(tmp_path), "--startup-log", str(log)])
        assert rc == 2, "recovery must NOT fire on a non-torn-manifest failure"
        assert not list(tmp_path.glob("surreal_db.CORRUPT-*"))

    def test_recover_torn_no_source_exit_3(self, tmp_path, capsys):
        _make_store(tmp_path / "surreal_db")
        log = tmp_path / "startup.log"
        log.write_text(_TORN_LOG)
        rc = safe_start.main(["recover", "--data-dir", str(tmp_path), "--startup-log", str(log)])
        assert rc == 3
        assert "surrealkv-safe-stop-2026-07-10.md" in capsys.readouterr().err

    def test_recover_torn_with_source_exit_0_and_restores(self, tmp_path):
        now = time.time()
        _make_store(tmp_path / "surreal_db", stamp=now - 30, with_lock=True)
        old = _make_store(tmp_path / "surreal_db.old-20260709_191332", stamp=now - 10)
        log = tmp_path / "startup.log"
        log.write_text(_TORN_LOG)
        rc = safe_start.main(["recover", "--data-dir", str(tmp_path), "--startup-log", str(log)])
        assert rc == 0
        assert list(tmp_path.glob("surreal_db.CORRUPT-*")), "corrupt dir preserved"
        assert old.exists(), "source preserved"
        assert (tmp_path / "surreal_db" / "manifest").exists()
        assert not (tmp_path / "surreal_db" / "LOCK").exists()

    def test_recover_missing_startup_log_exit_2(self, tmp_path):
        _make_store(tmp_path / "surreal_db")
        rc = safe_start.main(
            ["recover", "--data-dir", str(tmp_path), "--startup-log", str(tmp_path / "nope.log")]
        )
        assert rc == 2
