import pytest

from yadgar._shared.config import Settings
from yadgar.core.staleness import StalenessDetector
from yadgar.tests.core.conftest import TEST_PROJECT_ID


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture(autouse=True)
def _wire_backend_storage(storage, monkeypatch):
    """T2 Car E1: the detector forwards flag WRITES to backend admin ops, which
    resolve storage via _st._storage — point it at the module engine so the op
    writes land where the assertions read."""
    import yadgar._shared.runtime.state as _st

    monkeypatch.setattr(_st, "_storage", storage)


@pytest.fixture
def settings(tmp_path):
    return Settings(DB_PATH=str(tmp_path / "test.db"))


@pytest.fixture
def detector(storage, settings):
    return StalenessDetector(storage, settings)


def _make_memory(content="test memory", directory="/tmp/project", **kwargs):
    base = {
        "content": content,
        "directory_context": directory,
        "project_id": TEST_PROJECT_ID,
        "tags": ["test"],
    }
    base.update(kwargs)
    return base


class TestComputeFileHash:
    def test_compute_file_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")

        hash1 = StalenessDetector._compute_file_hash(str(f))
        hash2 = StalenessDetector._compute_file_hash(str(f))

        assert isinstance(hash1, str)
        assert len(hash1) == 64  # SHA-256 hex digest
        assert hash1 == hash2

    def test_hash_changes_on_modification(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("original content")
        hash1 = StalenessDetector._compute_file_hash(str(f))

        f.write_text("modified content")
        hash2 = StalenessDetector._compute_file_hash(str(f))

        assert hash1 != hash2

    def test_hash_returns_empty_for_missing_file(self, tmp_path):
        result = StalenessDetector._compute_file_hash(str(tmp_path / "nope.txt"))
        assert result == ""


class TestValidateMemory:
    def test_validate_memory_fresh(self, storage, detector, tmp_path):
        f = tmp_path / "source.py"
        f.write_text("def main(): pass")

        file_hash = StalenessDetector._compute_file_hash(str(f))
        storage.upsert_file_hash(str(f), file_hash)

        mem_id = storage.insert_memory(
            _make_memory(
                content="main function",
                directory=str(tmp_path),
                file_hash=file_hash,
            )
        )

        result = detector.validate_memory(mem_id)
        assert result["valid"] is True
        assert result["reason"] == "file unchanged"

    def test_validate_memory_stale(self, storage, detector, tmp_path):
        f = tmp_path / "source.py"
        f.write_text("def main(): pass")

        file_hash = StalenessDetector._compute_file_hash(str(f))
        storage.upsert_file_hash(str(f), file_hash)

        mem_id = storage.insert_memory(
            _make_memory(
                content="main function",
                directory=str(tmp_path),
                file_hash=file_hash,
            )
        )

        f.write_text("def main(): return 42")

        result = detector.validate_memory(mem_id)
        assert result["valid"] is False
        assert result["reason"] == "file changed"

        memory = storage.get_memory(mem_id)
        assert memory["is_stale"] is True

    def test_validate_memory_no_file_hash(self, storage, detector):
        mem_id = storage.insert_memory(_make_memory(content="no file ref"))

        result = detector.validate_memory(mem_id)
        assert result["valid"] is True
        assert result["reason"] == "no file reference"

    def test_validate_memory_not_found(self, detector):
        result = detector.validate_memory(9999)
        assert result["valid"] is False
        assert result["reason"] == "memory not found"


class TestStaleMemoryHeat:
    def test_stale_memory_heat_halved(self, storage, detector, tmp_path):
        f = tmp_path / "module.py"
        f.write_text("original code")

        file_hash = StalenessDetector._compute_file_hash(str(f))
        storage.upsert_file_hash(str(f), file_hash)

        mem_id = storage.insert_memory(
            _make_memory(
                content="module docs",
                directory=str(tmp_path),
                file_hash=file_hash,
                heat=0.8,
            )
        )

        f.write_text("changed code")

        detector.validate_memory(mem_id)

        memory = storage.get_memory(mem_id)
        assert memory["heat"] == pytest.approx(0.4)
        assert memory["is_stale"] is True


class TestFileDeletion:
    def test_file_deletion_detection(self, storage, detector, tmp_path):
        f = tmp_path / "ephemeral.py"
        f.write_text("temporary code")

        file_hash = StalenessDetector._compute_file_hash(str(f))
        storage.upsert_file_hash(str(f), file_hash)

        mem_id = storage.insert_memory(
            _make_memory(
                content="about ephemeral",
                directory=str(tmp_path),
                file_hash=file_hash,
            )
        )

        f.unlink()

        result = detector.validate_memory(mem_id)
        assert result["valid"] is False

        memory = storage.get_memory(mem_id)
        assert memory["is_stale"] is True
