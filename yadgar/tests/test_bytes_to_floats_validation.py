"""Tests for _bytes_to_floats length validation (Q20).

§10 remaining: storage.py _bytes_to_floats
- Must raise ValueError when len(data) % 4 != 0
- Must raise ValueError when n != expected_dim (when caller passes expected_dim)
"""

import struct

import pytest


class TestBytesToFloatsValidation:
    """_bytes_to_floats must validate input length."""

    @pytest.fixture
    def storage(self, tmp_path):
        from yadgar._shared.storage import StorageEngine

        engine = StorageEngine(str(tmp_path / "test.db"))
        yield engine
        engine.close()

    def test_valid_data_parses(self, storage):
        """4-byte-aligned data parses without error."""
        floats = [1.0, 2.0, 3.0]
        data = struct.pack("<3f", *floats)
        result = storage._bytes_to_floats(data)
        assert result == pytest.approx(floats)

    def test_misaligned_data_raises(self, storage):
        """Data length not divisible by 4 must raise ValueError."""
        bad_data = b"\x00\x01\x02"  # 3 bytes — not %4
        with pytest.raises(ValueError, match="4"):
            storage._bytes_to_floats(bad_data)

    def test_five_bytes_raises(self, storage):
        """5 bytes (not %4) must raise ValueError."""
        bad_data = b"\x00" * 5
        with pytest.raises(ValueError, match="4"):
            storage._bytes_to_floats(bad_data)

    def test_empty_bytes_ok(self, storage):
        """Empty bytes is valid (0 floats)."""
        result = storage._bytes_to_floats(b"")
        assert result == []

    def test_dim_mismatch_raises(self, storage):
        """When expected_dim is passed, mismatch must raise ValueError."""
        data = struct.pack("<3f", 1.0, 2.0, 3.0)  # 3 floats
        with pytest.raises(ValueError, match="dim"):
            storage._bytes_to_floats(data, expected_dim=4)

    def test_dim_match_ok(self, storage):
        """Correct expected_dim passes through."""
        data = struct.pack("<3f", 1.0, 2.0, 3.0)
        result = storage._bytes_to_floats(data, expected_dim=3)
        assert len(result) == 3
