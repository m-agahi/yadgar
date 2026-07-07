"""§16 Wiki backup automation tests.

Tests:
- wiki_snapshot() produces a valid .jsonl file
- wiki_snapshot() output contains one JSON object per line
- wiki_retention_prune() deletes files older than 14 days
- wiki_retention_prune() preserves files newer than 14 days
"""

import json
import os
import time
from pathlib import Path

import pytest

from yadgar.core.scripts.wiki_snapshot import prune_old_snapshots, snapshot_wiki_pages


@pytest.fixture()
def snapshot_dir(tmp_path):
    """Temporary directory for wiki snapshots."""
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


class TestWikiSnapshot:
    def test_snapshot_creates_jsonl_file(self, snapshot_dir):
        """snapshot_wiki_pages() creates a .jsonl file in the output directory."""
        pages = [
            {"id": 1, "slug": "test-page", "title": "Test Page", "content": "hello"},
            {"id": 2, "slug": "other-page", "title": "Other Page", "content": "world"},
        ]
        output_path = snapshot_wiki_pages(pages, str(snapshot_dir))
        assert output_path is not None
        assert Path(output_path).exists()
        assert output_path.endswith(".jsonl")

    def test_snapshot_filename_pattern(self, snapshot_dir):
        """snapshot filename matches wiki_YYYYMMDD_HHMMSS.jsonl pattern."""
        import re

        pages = [{"id": 1, "slug": "page", "title": "T", "content": "c"}]
        output_path = snapshot_wiki_pages(pages, str(snapshot_dir))
        fname = Path(output_path).name
        pattern = r"^wiki_\d{8}_\d{6}\.jsonl$"
        assert re.match(pattern, fname), (
            f"Filename {fname!r} does not match wiki_YYYYMMDD_HHMMSS.jsonl"
        )

    def test_snapshot_valid_jsonl_content(self, snapshot_dir):
        """Each line in the snapshot file is valid JSON."""
        pages = [
            {"id": 1, "slug": "page-a", "title": "A", "content": "content a"},
            {"id": 2, "slug": "page-b", "title": "B", "content": "content b"},
        ]
        output_path = snapshot_wiki_pages(pages, str(snapshot_dir))
        lines = Path(output_path).read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert isinstance(record, dict)

    def test_snapshot_preserves_page_fields(self, snapshot_dir):
        """Snapshot JSON contains the expected page fields."""
        pages = [{"id": 42, "slug": "my-page", "title": "My Page", "content": "body"}]
        output_path = snapshot_wiki_pages(pages, str(snapshot_dir))
        line = Path(output_path).read_text().strip()
        record = json.loads(line)
        assert record["slug"] == "my-page"
        assert record["content"] == "body"

    def test_snapshot_empty_pages(self, snapshot_dir):
        """snapshot_wiki_pages() with no pages creates an empty .jsonl file."""
        output_path = snapshot_wiki_pages([], str(snapshot_dir))
        assert Path(output_path).exists()
        content = Path(output_path).read_text().strip()
        assert content == "" or content == "[]"


class TestWikiRetentionPrune:
    def _make_file(self, snapshot_dir: Path, name: str, age_days: float) -> Path:
        """Create a dummy snapshot file with an appropriate mtime."""
        f = snapshot_dir / name
        f.write_text('{"slug": "test"}\n')
        mtime = time.time() - age_days * 86400
        os.utime(str(f), (mtime, mtime))
        return f

    def test_prune_deletes_old_files(self, snapshot_dir):
        """Files older than 14 days are deleted."""
        old_file = self._make_file(snapshot_dir, "wiki_20200101_000000.jsonl", age_days=15)
        prune_old_snapshots(str(snapshot_dir), max_age_days=14)
        assert not old_file.exists(), f"Old file {old_file} should have been pruned"

    def test_prune_preserves_recent_files(self, snapshot_dir):
        """Files newer than 14 days are NOT deleted."""
        recent_file = self._make_file(snapshot_dir, "wiki_20990101_000000.jsonl", age_days=1)
        prune_old_snapshots(str(snapshot_dir), max_age_days=14)
        assert recent_file.exists(), f"Recent file {recent_file} should be preserved"

    def test_prune_only_targets_wiki_jsonl(self, snapshot_dir):
        """Prune only targets wiki_*.jsonl files, not other files."""
        other_file = self._make_file(snapshot_dir, "backup_20200101.surql", age_days=20)
        prune_old_snapshots(str(snapshot_dir), max_age_days=14)
        assert other_file.exists(), f"Non-wiki file {other_file} should NOT be pruned"

    def test_prune_exactly_at_boundary(self, snapshot_dir):
        """File at exactly max_age_days old: check boundary behaviour."""
        # 14 days + 1 second = should be pruned
        boundary_old = self._make_file(snapshot_dir, "wiki_old.jsonl", age_days=14 + (1 / 86400))
        # 13 days = should be preserved
        boundary_new = self._make_file(snapshot_dir, "wiki_new.jsonl", age_days=13)
        prune_old_snapshots(str(snapshot_dir), max_age_days=14)
        assert not boundary_old.exists(), "File >14d should be pruned"
        assert boundary_new.exists(), "File <14d should be preserved"
