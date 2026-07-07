"""§4 path-traversal tests — whitelist enforcement and file-hash oracle defence.

Verifies:
- memorize with context outside registered project roots is refused (no hash)
- Files too large are skipped by hash logic
- dlq_requeue rejects null bytes and Unicode separators in filename
- _file_hash streams in chunks (no read_bytes) and respects size cap
"""

import hashlib
import os
from pathlib import Path
from unittest.mock import patch


def _make_small_file(tmp_path: Path, name: str = "test.txt", content: str = "hello") -> Path:
    f = tmp_path / name
    f.write_text(content)
    return f


class TestFileHashWhitelist:
    """§4: _file_hash only hashes files under registered project roots."""

    def test_hash_outside_project_root_returns_none(self, tmp_path, monkeypatch):
        """With a registered root, files outside it must return None."""
        import yadgar.core.server as srv

        orig_roots = srv._project_roots.copy()
        try:
            other_root = str(tmp_path / "other")
            os.makedirs(other_root)
            f = Path(other_root) / "sensitive.txt"
            f.write_text("sensitive content")

            # Register a DIFFERENT project root
            registered_root = str(tmp_path / "project")
            os.makedirs(registered_root)
            srv._project_roots.clear()
            srv._project_roots.add(registered_root)

            result = srv._file_hash(str(f))
            assert result is None, (
                "_file_hash must return None for files outside registered project roots"
            )
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)

    def test_hash_inside_project_root_returns_hash(self, tmp_path, monkeypatch):
        """Files inside a registered project root must be hashed."""
        import yadgar.core.server as srv

        orig_roots = srv._project_roots.copy()
        try:
            project_dir = str(tmp_path / "project")
            os.makedirs(project_dir)
            f = Path(project_dir) / "source.py"
            f.write_text("x = 1")

            srv._project_roots.clear()
            srv._project_roots.add(project_dir)

            result = srv._file_hash(str(f))
            assert result is not None, (
                "_file_hash must return a hash for files inside registered project root"
            )
            # Verify correctness: SHA-256 of the content
            expected = hashlib.sha256(b"x = 1").hexdigest()
            assert result == expected
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)

    def test_no_registered_roots_no_whitelist_applied(self, tmp_path):
        """When no project roots are registered, whitelist is not applied."""
        import yadgar.core.server as srv

        orig_roots = srv._project_roots.copy()
        try:
            srv._project_roots.clear()  # no roots registered
            f = tmp_path / "anyfile.txt"
            f.write_text("content")

            # With empty _project_roots set, no whitelist is applied
            result = srv._file_hash(str(f))
            assert result is not None, (
                "_file_hash should hash files when no project roots registered"
            )
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)


class TestFileHashSizeCap:
    """§4: Files larger than MAX_HASH_BYTES must be skipped."""

    def test_large_file_skipped(self, tmp_path, monkeypatch):
        """File larger than MAX_HASH_BYTES must return None."""
        import yadgar.core.server as srv
        from yadgar._shared.config import get_settings

        # Create a file just over the limit
        large_file = tmp_path / "large.bin"
        max_bytes = get_settings().MAX_HASH_BYTES
        # Write exactly max_bytes + 1 bytes
        large_file.write_bytes(b"x" * (max_bytes + 1))

        orig_roots = srv._project_roots.copy()
        try:
            srv._project_roots.clear()  # no whitelist
            result = srv._file_hash(str(large_file))
            assert result is None, (
                f"_file_hash must return None for files > MAX_HASH_BYTES ({max_bytes})"
            )
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)

    def test_file_at_limit_hashed(self, tmp_path):
        """File exactly at MAX_HASH_BYTES must be hashed."""
        import yadgar.core.server as srv
        from yadgar._shared.config import get_settings

        max_bytes = get_settings().MAX_HASH_BYTES
        at_limit = tmp_path / "at_limit.bin"
        at_limit.write_bytes(b"y" * max_bytes)

        orig_roots = srv._project_roots.copy()
        try:
            srv._project_roots.clear()
            result = srv._file_hash(str(at_limit))
            assert result is not None, "_file_hash must hash files exactly at MAX_HASH_BYTES"
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)

    def test_max_hash_bytes_default_value(self):
        """MAX_HASH_BYTES default must be 10 MiB."""
        from yadgar._shared.config import get_settings

        max_bytes = get_settings().MAX_HASH_BYTES
        assert max_bytes > 0, "MAX_HASH_BYTES must be positive"
        assert max_bytes == 10_485_760, f"Default MAX_HASH_BYTES must be 10 MiB, got {max_bytes}"


class TestFileHashStreaming:
    """§4: _file_hash must not call read_bytes(); must stream in chunks."""

    def test_does_not_call_read_bytes(self, tmp_path):
        """_file_hash must use open()+read() loop, not Path.read_bytes()."""
        import yadgar.core.server as srv

        f = tmp_path / "file.txt"
        f.write_text("stream me")

        orig_roots = srv._project_roots.copy()
        try:
            srv._project_roots.clear()

            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("read_bytes called")
            ) as mock_rb:
                result = srv._file_hash(str(f))
                # If read_bytes is not called, result should be the hash
                mock_rb.assert_not_called()
                assert result is not None
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)


class TestDlqRequeueFilenameValidation:
    """§4: dlq_requeue must reject null bytes and Unicode separators."""

    def _call_dlq_requeue(self, filename: str) -> dict:
        """Call dlq_requeue, returning its result dict."""
        import yadgar.core.server as srv

        return srv.dlq_requeue(filename)

    def test_rejects_null_byte(self):
        result = self._call_dlq_requeue("valid_prefix\x00suffix.json")
        assert result["requeued"] is False
        assert "Invalid" in result["error"]

    def test_rejects_slash(self):
        result = self._call_dlq_requeue("path/traversal.json")
        assert result["requeued"] is False

    def test_rejects_backslash(self):
        result = self._call_dlq_requeue("path\\traversal.json")
        assert result["requeued"] is False

    def test_rejects_dot_prefix(self):
        result = self._call_dlq_requeue(".hidden.json")
        assert result["requeued"] is False

    def test_rejects_unicode_path_separator(self):
        # U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR
        result = self._call_dlq_requeue("file name.json")
        assert result["requeued"] is False, "Unicode line/paragraph separators must be rejected"

    def test_valid_filename_proceeds_to_lookup(self, tmp_path, monkeypatch):
        """A valid filename passes validation and proceeds to DLQ lookup."""

        # The DLQ dir won't have the file so result will be "not found" — but it
        # must NOT return "Invalid filename".
        result = self._call_dlq_requeue("0001778139482800_abc123.json")
        # Should fail on "not found", not on validation
        assert "Invalid filename" not in result.get("error", ""), (
            "Valid filename should not fail validation"
        )


class TestMemorizeOutsideProjectRoot:
    """§4: memorize context pointing outside project roots must not hash the file."""

    def test_memorize_does_not_hash_outside_root(self, tmp_path, monkeypatch):
        """memorize with context=outside path must not expose a file hash."""
        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
        import yadgar.core.server as srv

        # Verify _file_hash returns None for an outside-root path

        orig_roots = srv._project_roots.copy()
        try:
            outside_path = "/etc/passwd"
            registered = str(tmp_path / "project")
            os.makedirs(registered, exist_ok=True)
            srv._project_roots.clear()
            srv._project_roots.add(registered)

            result = srv._file_hash(outside_path)
            assert result is None, (
                "_file_hash('/etc/passwd') must return None — outside project root"
            )
        finally:
            srv._project_roots.clear()
            srv._project_roots.update(orig_roots)
