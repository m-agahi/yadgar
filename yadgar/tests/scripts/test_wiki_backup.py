"""§16 Wiki backup automation tests.

Tests:
- wiki_snapshot() produces a valid .jsonl file
- wiki_snapshot() output contains one JSON object per line

ADR-0076 D3: prune_old_snapshots() was removed from wiki_snapshot.py (dead code —
it was never called from anywhere).  Pruning is now exclusively owned by the
entrypoint-backend.sh loop via ``find /data/backups/wiki -name 'wiki_*.jsonl' -mtime +14 -delete``.
The TestWikiRetentionPrune class has been removed accordingly.
"""

import json
from pathlib import Path

import pytest

from yadgar.core.scripts.wiki_snapshot import snapshot_wiki_pages


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
