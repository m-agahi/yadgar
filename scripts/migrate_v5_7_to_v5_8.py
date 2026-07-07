#!/usr/bin/env python3
"""One-time migration: backfill tier/valid_until/migration_grace on existing anchors (v5.8.0).

Equivalent to _migration_008_anchor_tier in yadgar/storage/migrations.py, which runs
automatically on first startup. This standalone script is provided for:
  - Offline/one-shot execution against a running SurrealDB
  - Dry-run preview of affected rows
  - Post-migration audit

Usage:
    uv run scripts/migrate_v5_7_to_v5_8.py                 # live run
    uv run scripts/migrate_v5_7_to_v5_8.py --dry-run       # preview only
    uv run scripts/migrate_v5_7_to_v5_8.py --db-url http://localhost:8000

The script is idempotent — re-running safely skips already-migrated rows.

Sentinel: the startup migration (008_anchor_tier) writes a schema_version row.
This script does NOT check/write that sentinel; it acts on memory rows directly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the yadgar package is importable from the repo root
# ---------------------------------------------------------------------------
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("migrate_v5_7_to_v5_8")


def _migrate(db_url: str | None, dry_run: bool) -> int:
    """Run the migration. Returns count of migrated rows."""
    os.environ.setdefault("YADGAR_ALLOW_ROOT", "1")
    os.environ.setdefault("YADGAR_DB_PASS", "root")
    os.environ.setdefault("YADGAR_DB_USER", "root")
    if db_url:
        os.environ["YADGAR_DB_URL"] = db_url

    from yadgar._shared.config import get_settings
    from yadgar._shared.storage import StorageEngine

    s = get_settings()
    ttl_days = int(s.ANCHOR_CONDITIONAL_TTL_DAYS)
    valid_until_str = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()

    db_path = os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")
    storage = StorageEngine(db_path)

    rows = storage._q("SELECT id, content FROM memory WHERE '_anchor' INSIDE tags AND tier IS NONE")

    if not rows:
        logger.info("No pre-v5.8 anchors found — nothing to migrate")
        storage.close()
        return 0

    logger.info("Found %d anchor(s) to migrate", len(rows))

    for row in rows:
        mid = storage._extract_id(row.get("id"))
        content_preview = (row.get("content") or "")[:60].replace("\n", " ")
        if dry_run:
            logger.info(
                "[DRY-RUN] would migrate memory:%d | tier=conditional | valid_until=%s | content=%r",
                mid,
                valid_until_str,
                content_preview,
            )
        else:
            storage._q(
                f"UPDATE memory:{int(mid)} SET "
                "tier = $tier, valid_until = $vu, migration_grace = $grace",
                {"tier": "conditional", "vu": valid_until_str, "grace": True},
            )
            logger.info(
                "migrated memory:%d | tier=conditional | valid_until=%s | content=%r",
                mid,
                valid_until_str,
                content_preview,
            )

    storage.close()
    logger.info(
        "%s complete: %d row(s) %s",
        "DRY-RUN" if dry_run else "migration",
        len(rows),
        "would be migrated" if dry_run else "migrated",
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--db-url", default=None, help="SurrealDB URL (overrides YADGAR_DB_URL)")
    args = parser.parse_args()

    _migrate(db_url=args.db_url, dry_run=args.dry_run)
    sys.exit(0)


if __name__ == "__main__":
    main()
