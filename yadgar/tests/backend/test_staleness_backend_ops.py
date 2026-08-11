"""T2 Car E1 — staleness heat-decay compute relocated to backend /admin ops.

The StalenessDetector keeps only the host-FS half in core (watchdog events,
file hashing, directory walks). The stateless-over-DB flag compute — "given a
changed file, find affected memories, halve heat, mark stale, upsert the file
hash" — runs backend-side (census verdict #8, ADR-0078):

- ``staleness_file_changed``: one watchdog event (filepath + new hash).
- ``staleness_scan``: batch of walked files (path + hash each).
- ``staleness_flag_memory``: single-memory flag (validate_memory write half).

TDD: RED before the ops existed, GREEN with them.
"""

from __future__ import annotations

import pytest

from yadgar._shared.config import Settings
from yadgar.core.staleness import StalenessDetector

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: dict without this key is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"


@pytest.fixture(scope="module")
def storage(module_storage):
    return module_storage


@pytest.fixture(autouse=True)
def _wire_backend_storage(storage, monkeypatch):
    """Backend admin ops resolve storage via _st._storage — point it at the
    module engine so op writes land where the assertions read."""
    import yadgar._shared.runtime.state as _st

    monkeypatch.setattr(_st, "_storage", storage)


@pytest.fixture
def settings(tmp_path):
    return Settings(DB_PATH=str(tmp_path / "test.db"))


@pytest.fixture
def detector(storage, settings):
    return StalenessDetector(storage, settings)


def _mem(storage, directory, file_hash, heat=0.8, content="m"):
    return storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": content,
            "directory_context": directory,
            "tags": ["test"],
            "file_hash": file_hash,
            "heat": heat,
        }
    )


# ---------------------------------------------------------------------------
# Op registration
# ---------------------------------------------------------------------------


class TestOpsRegistered:
    def test_all_three_registered(self):
        from yadgar.backend.admin_exec import admin_ops

        ops = admin_ops()
        assert "staleness_file_changed" in ops
        assert "staleness_scan" in ops
        assert "staleness_flag_memory" in ops


# ---------------------------------------------------------------------------
# staleness_file_changed
# ---------------------------------------------------------------------------


class TestFileChangedOp:
    def test_changed_file_flags_memories_and_upserts_hash(self, storage, tmp_path):
        from yadgar.backend.admin_exec.staleness import staleness_file_changed

        f = tmp_path / "mod.py"
        old_hash, new_hash = "aaa111", "bbb222"
        storage.upsert_file_hash(str(f), old_hash)
        mem_id = _mem(storage, str(tmp_path), old_hash, heat=0.8)

        result = staleness_file_changed({"filepath": str(f), "new_hash": new_hash})

        assert result["changed"] is True
        assert result["memories_flagged"] >= 1
        memory = storage.get_memory(mem_id)
        assert memory["is_stale"] is True
        assert memory["heat"] == pytest.approx(0.4)
        assert storage.get_file_hash(str(f)) == new_hash

    def test_unchanged_file_flags_nothing(self, storage, tmp_path):
        from yadgar.backend.admin_exec.staleness import staleness_file_changed

        f = tmp_path / "same.py"
        storage.upsert_file_hash(str(f), "samehash")
        mem_id = _mem(storage, str(tmp_path), "samehash", heat=0.8)

        result = staleness_file_changed({"filepath": str(f), "new_hash": "samehash"})

        assert result["changed"] is False
        assert result["memories_flagged"] == 0
        assert storage.get_memory(mem_id)["heat"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# staleness_scan (batch)
# ---------------------------------------------------------------------------


class TestScanOp:
    def test_batch_flags_with_global_dedup(self, storage, tmp_path):
        from yadgar.backend.admin_exec.staleness import staleness_scan

        a, b = tmp_path / "a.py", tmp_path / "b.py"
        storage.upsert_file_hash(str(a), "hash_a1")
        storage.upsert_file_hash(str(b), "hash_b1")
        # One memory tied to a's hash — also in both files' parent dir, so the
        # directory arm would re-find it for b: global dedup must count it once.
        mem_id = _mem(storage, str(tmp_path), "hash_a1", heat=1.0)

        result = staleness_scan(
            {
                "files": [
                    {"path": str(a), "hash": "hash_a2"},
                    {"path": str(b), "hash": "hash_b2"},
                ]
            }
        )

        assert result["files_changed"] == 2
        assert result["memories_flagged"] == 1
        memory = storage.get_memory(mem_id)
        assert memory["is_stale"] is True
        # Halved exactly once despite two changed files matching it.
        assert memory["heat"] == pytest.approx(0.5)
        assert storage.get_file_hash(str(a)) == "hash_a2"
        assert storage.get_file_hash(str(b)) == "hash_b2"


# ---------------------------------------------------------------------------
# staleness_flag_memory (validate_memory write half)
# ---------------------------------------------------------------------------


class TestFlagMemoryOp:
    def test_halves_heat_and_marks_stale(self, storage, tmp_path):
        from yadgar.backend.admin_exec.staleness import staleness_flag_memory

        mem_id = _mem(storage, str(tmp_path), "deadbeef", heat=0.6)

        result = staleness_flag_memory({"memory_id": mem_id})

        assert result["flagged"] is True
        memory = storage.get_memory(mem_id)
        assert memory["is_stale"] is True
        assert memory["heat"] == pytest.approx(0.3)

    def test_missing_memory_is_noop(self, storage):
        from yadgar.backend.admin_exec.staleness import staleness_flag_memory

        result = staleness_flag_memory({"memory_id": 999999})
        assert result["flagged"] is False


# ---------------------------------------------------------------------------
# Core detector no longer writes directly
# ---------------------------------------------------------------------------


class TestDetectorForwards:
    def test_no_direct_storage_writes_in_core_module(self):
        import inspect

        import yadgar.core.staleness.staleness as staleness_mod

        src = inspect.getsource(staleness_mod)
        for banned in (
            "update_memory_heat",
            "update_memory_staleness",
            "upsert_file_hash",
            "get_memories_by_file_hash",
            "get_memories_for_directory",
        ):
            assert banned not in src, (
                f"core StalenessDetector must forward the flag compute "
                f"(found direct storage call {banned!r})"
            )

    def test_detector_validate_memory_flags_via_backend(self, storage, detector, tmp_path):
        """End-to-end through the detector: host hash mismatch → backend flag."""
        f = tmp_path / "source.py"
        f.write_text("def main(): pass")
        file_hash = StalenessDetector._compute_file_hash(str(f))
        storage.upsert_file_hash(str(f), file_hash)
        mem_id = _mem(storage, str(tmp_path), file_hash, heat=0.8, content="main fn")

        f.write_text("def main(): return 42")

        result = detector.validate_memory(mem_id)
        assert result["valid"] is False
        assert storage.get_memory(mem_id)["is_stale"] is True
