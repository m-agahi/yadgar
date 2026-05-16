"""Wiki snapshot utility for §16 Wiki backup automation.

Provides two functions used by entrypoint-backend.sh and tests:

  snapshot_wiki_pages(pages, output_dir) -> str
      Write pages as JSONL to output_dir/wiki_YYYYMMDD_HHMMSS.jsonl.
      Returns the full path of the written file.

  prune_old_snapshots(output_dir, max_age_days=14) -> int
      Delete wiki_*.jsonl files older than max_age_days.
      Returns the number of files deleted.

The entrypoint also calls these via a background loop every 6 hours.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path


def snapshot_wiki_pages(pages: list[dict], output_dir: str) -> str:
    """Write wiki pages as JSONL to output_dir.

    Each page is serialized to one JSON object per line.
    The output filename is wiki_YYYYMMDD_HHMMSS.jsonl.

    Args:
        pages: list of wiki page dicts (from storage.list_wiki_pages or similar).
        output_dir: directory to write the snapshot into.

    Returns:
        Absolute path of the written .jsonl file.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    output_path = Path(output_dir) / f"wiki_{timestamp}.jsonl"

    with open(str(output_path), "w", encoding="utf-8") as fh:
        for page in pages:
            # Strip embedding bytes — they're large and unreadable in backup files
            row = {k: v for k, v in page.items() if k != "embedding" and not isinstance(v, bytes)}
            fh.write(json.dumps(row, default=str) + "\n")

    return str(output_path)


def prune_old_snapshots(output_dir: str, max_age_days: int = 14) -> int:
    """Delete wiki_*.jsonl files older than max_age_days.

    Only wiki_*.jsonl files are touched — other files in the directory
    are left alone.

    Args:
        output_dir: directory to scan.
        max_age_days: files older than this are deleted.

    Returns:
        Number of files deleted.
    """
    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for path in Path(output_dir).glob("wiki_*.jsonl"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            deleted += 1
    return deleted
