"""Wiki snapshot utility for §16 Wiki backup automation.

Provides one function used by the backend container entrypoint and tests:

  snapshot_wiki_pages(pages, output_dir) -> str
      Write pages as JSONL to output_dir/wiki_YYYYMMDD_HHMMSS.jsonl.
      Returns the full path of the written file.

Pruning is owned exclusively by the entrypoint-backend.sh loop via:
  ``find /data/backups/wiki -name 'wiki_*.jsonl' -mtime +14 -delete``

The previous ``prune_old_snapshots`` function in this module was dead code
(it was never called from anywhere) and has been removed (ADR-0076 D3).
The container's ``find -mtime`` prune is the single pruning owner; two
owners was never implemented and would have been a no-op collision.

ADR-0076 D3 context:
- Output dir: /data/backups/wiki/ (was /data directly; see D4 layout).
- Cadence: 24 h (was 6 h) — reduces snapshot volume from ~1.9 GB to ~0.6 GB
  for a 14-day retention window.
- The entrypoint loop handles pruning; this module's only job is snapshot_wiki_pages.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def snapshot_wiki_pages(pages: list[dict], output_dir: str) -> str:
    """Write wiki pages as JSONL to output_dir.

    Each page is serialized to one JSON object per line.
    The output filename is wiki_YYYYMMDD_HHMMSS.jsonl.

    Output directory (ADR-0076 D3/D4): should be DATA_DIR/backups/wiki/ —
    the entrypoint-backend.sh loop ensures this dir exists via ``mkdir -p``.

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
