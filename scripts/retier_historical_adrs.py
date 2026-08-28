#!/usr/bin/env python3
"""Ledger task 197 (DATA half) — retier historical-status ADR rows.

``_flip_adr_status`` used to flip ``adr.status`` and leave ``adr.tier`` at
whatever the row was CREATED with, so every supersede wrote a self-contradicting
row: ``status='superseded', tier='binding'``. The WRITE-side fix shipped in
v5.184.0 (``ledger_columns.adr_tier_for_flip``, PR #57) and is live in the
deployed daemon — but it only corrects rows flipped from now on. The rows that
were already wrong stay wrong, because the read-side rescue
(``ledger_columns.adr_tier_where``) deliberately classifies only NULL-tier rows
by status and never overrides an explicit stored value.

This script is the one-shot data pass that corrects those stored values.

MEASURED 2026-08-28 on ``m-agahi/yadgar``: 14 superseded + 6 rejected = 20 rows
stored ``tier='binding'``; ``quinyx/flux`` has none. ``adr_list(tier="historical")``
returns 1 row against 21 historical-status rows.

WHY A SCRIPT AND NOT MCP: there is no MCP tool that writes ``adr.tier``.
``adr_add`` creates a new AUTO_INCREMENT row (wrong instrument), ``db_inspect``
reaches SurrealDB only, and the backend op ``ledger.update_adr_tier_subsystem``
is deliberately absent from ``_ADMIN_OPS`` ("that would open a remote adr-mutation
endpoint no caller needs"). The engine method is the only writer, so an operator
script is the only shape this repair can take.

WHY NOT ``seed_adr_tier_subsystem``: its ``_is_already_stamped`` skips any row
with BOTH ``tier`` and ``subsystem`` set — i.e. exactly the rows that need
correcting. It stamps INERT columns; it does not correct wrong ones. It would
also re-derive ``subsystem`` from the body header as a side effect, which is
outside this repair's remit.

RETIER ONLY. The ``subsystem`` column is read from each row and written back
UNCHANGED — ``MariaStorageEngine.update_adr_tier_subsystem`` writes both columns
in one UPDATE, so passing ``None`` would silently NULL D28 data while ``tier``
landed correctly. Verification asserts BOTH columns.

CLASSIFIER: ``ledger_columns.adr_tier_for_flip`` — the SAME function the deployed
write side uses, so the repair and the write side cannot drift. It returns
``None`` for a status D27 does not name (``'archived'``), and those rows are
skipped rather than defaulted.

DRY-RUN BY DEFAULT — ``--apply`` is required to write anything.
IDEMPOTENT — a second run reports 0 candidates. Re-running the DRY RUN after an
apply is therefore the post-write proof.

Connection: engine #2 has no TCP listener (mysqld runs ``--skip-networking``);
the transport is a unix socket. The container's datadir is a host bind-mount
(``~/.local/share/yadgar/mariadb`` -> ``/data/mariadb``), so the socket is
reachable from the host. The option file names the CONTAINER socket path, so
the socket defaults to a sibling of the option file instead (correct on the host
AND inside the container). Override with ``--socket`` if your layout differs.

Usage:
    uv run scripts/retier_historical_adrs.py                       # dry run (default)
    uv run scripts/retier_historical_adrs.py --verbose             # per-row detail
    uv run scripts/retier_historical_adrs.py --apply               # write
    uv run scripts/retier_historical_adrs.py --project-id quinyx/flux
    uv run scripts/retier_historical_adrs.py --include-null-tier   # also stamp NULL-tier rows
    uv run scripts/retier_historical_adrs.py --option-file /path/to/client.cnf

Exit codes:
  0  completed (dry run, or apply succeeded and verified)
  1  one or more rows failed to retier, or failed post-write verification
  2  fatal error (DB unreachable, bad args)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Suppress the OTLP exporter BEFORE any yadgar import (same guard as
# scripts/unescape_adr_bodies.py / scripts/rederive_wiki_links.py).
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("retier_historical_adrs")

DEFAULT_PROJECT_ID = "m-agahi/yadgar"


def _candidates(
    rows: list[dict[str, Any]],
    *,
    include_null_tier: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split *rows* into (needs-retier, null-tier-observed).

    A row needs retiering when its stored ``tier`` CONTRADICTS the D27 tier its
    ``status`` implies. NULL-tier rows are reported separately and left alone by
    default: the read arm (``adr_tier_where``) already classifies them by status,
    so they are not wrong — merely unstamped — and stamping them is a different
    job (``seed_adr_tier_subsystem``). ``--include-null-tier`` folds them in.

    A status D27 does not name (``'archived'``) yields ``None`` from the
    classifier and is skipped entirely, exactly as ``_flip_adr_status`` skips it.
    """
    from yadgar._shared.storage.sql import ledger_columns as lc

    needs: list[dict[str, Any]] = []
    null_tier: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        expected = lc.adr_tier_for_flip(row.get("status"))
        if expected is None:
            continue
        stored = row.get("tier")
        if stored is None:
            if expected == lc.TIER_HISTORICAL:
                null_tier.append(row)
                if include_null_tier:
                    needs.append(row)
            continue
        if str(stored) != expected:
            needs.append(row)
    return needs, null_tier


def _describe(row: dict[str, Any]) -> str:
    """One-line row identity for the operator's pre-mutation review."""
    from yadgar._shared.storage.sql import ledger_columns as lc

    adr_id = int(row["id"])
    return (
        f"ADR-{adr_id:04d} (id={adr_id}) status={row.get('status')!r} "
        f"tier={row.get('tier')!r} -> {lc.adr_tier_for_flip(row.get('status'))!r} "
        f"subsystem={row.get('subsystem')!r} slug={row.get('body_slug')!r}"
    )


async def retier(
    storage: Any,
    *,
    project_id: str,
    apply_changes: bool,
    include_null_tier: bool,
    verbose: bool,
) -> dict[str, int]:
    """Correct contradicting ``adr.tier`` values. Returns a tally.

    Every write is followed by a single-row re-read that asserts BOTH ``tier``
    (landed) and ``subsystem`` (unchanged). A write that reports success and did
    not land counts as ``failed`` — ``ok`` from the driver is not evidence.
    """
    from yadgar._shared.storage.sql import ledger_columns as lc

    rows = await storage.list_adr_rows(
        project_id=project_id, status=None, tier=None, subsystem=None
    )
    if not isinstance(rows, list):
        rows = []

    needs, null_tier = _candidates(rows, include_null_tier=include_null_tier)
    tally = {
        "scanned": len(rows),
        "candidates": len(needs),
        "null_tier_observed": len(null_tier),
        "retiered": 0,
        "failed": 0,
        "verify_failed": 0,
    }

    for row in needs:
        line = _describe(row)
        if verbose or not apply_changes:
            logger.info(
                "%s %s", "[DRY-RUN] would retier" if not apply_changes else "retiering", line
            )
        if not apply_changes:
            continue

        adr_id = int(row["id"])
        target_tier = lc.adr_tier_for_flip(row.get("status"))
        # Pass the row's OWN subsystem straight back: the engine UPDATE writes
        # both columns, so omitting it would NULL D28 data on every repaired row.
        subsystem = row.get("subsystem")
        try:
            await storage.update_adr_tier_subsystem(adr_id, str(target_tier), subsystem)
        except Exception as exc:  # CLI boundary: report, never traceback
            tally["failed"] += 1
            logger.error("failed to retier id=%d: %s", adr_id, exc)
            continue

        # Post-write re-read. Both columns, not just tier.
        after = await storage.get_adr_row(adr_id, project_id=project_id)
        if (
            after is None
            or str(after.get("tier")) != target_tier
            or after.get("subsystem") != subsystem
        ):
            tally["verify_failed"] += 1
            logger.error(
                "VERIFY FAILED id=%d: expected tier=%r subsystem=%r, read back %r",
                adr_id,
                target_tier,
                subsystem,
                None
                if after is None
                else {"tier": after.get("tier"), "subsystem": after.get("subsystem")},
            )
            continue
        tally["retiered"] += 1

    if null_tier and not include_null_tier:
        logger.info(
            "%d historical-status row(s) carry tier=NULL and were LEFT ALONE "
            "(the read arm classifies them by status). Pass --include-null-tier to stamp them.",
            len(null_tier),
        )
    return tally


def _build_config(option_file: Path, socket_override: str | None) -> Any:
    """Read the option file, then point the socket at a sibling of that file.

    The option file's own ``socket`` key names the CONTAINER path
    (``/data/mariadb/mysqld.sock``), which does not exist on the host. The datadir
    is a bind-mount, so ``<option_file.parent>/mysqld.sock`` is correct in BOTH
    places. The explicit ``unix_socket`` wins over the option file's value at
    connect time (the driver's defaults-file parse only fills absent arguments).
    """
    from dataclasses import replace

    from yadgar._shared.storage.sql.config import read_client_option_file

    config = read_client_option_file(option_file)
    socket = socket_override or str(option_file.parent / "mysqld.sock")
    return replace(config, unix_socket=socket)


async def _amain(args: argparse.Namespace) -> int:
    from yadgar._shared.storage.sql.config import default_option_file_path
    from yadgar._shared.storage.sql.mariadb import MariaStorageEngine

    option_file = (
        Path(args.option_file).expanduser() if args.option_file else default_option_file_path()
    )
    try:
        config = _build_config(option_file, args.socket)
    except Exception as exc:  # CLI boundary: report, never traceback
        logger.error("cannot read option file %s: %s", option_file, exc)
        return 2

    if not Path(config.unix_socket).exists():
        logger.error(
            "mysqld socket not found at %s — engine #2 has no TCP listener. "
            "Pass --socket, or run this inside the yadgar-backend container.",
            config.unix_socket,
        )
        return 2

    try:
        storage = MariaStorageEngine(config)
    except Exception as exc:  # CLI boundary: report, never traceback
        logger.error("cannot open engine #2: %s", exc)
        return 2

    try:
        tally = await retier(
            storage,
            project_id=args.project_id,
            apply_changes=args.apply,
            include_null_tier=args.include_null_tier,
            verbose=args.verbose,
        )
    except Exception as exc:  # CLI boundary: report, never traceback
        logger.error("retier pass failed: %s", exc)
        return 2
    finally:
        try:
            await storage.dispose()
        except Exception:  # dispose() must not mask the real exit status
            pass

    logger.info(
        "%s project_id=%s scanned=%d candidates=%d retiered=%d failed=%d "
        "verify_failed=%d null_tier_observed=%d",
        "APPLIED" if args.apply else "DRY-RUN",
        args.project_id,
        tally["scanned"],
        tally["candidates"],
        tally["retiered"],
        tally["failed"],
        tally["verify_failed"],
        tally["null_tier_observed"],
    )
    if not args.apply and tally["candidates"]:
        logger.info(
            "re-run with --apply to write these %d retier(s)",
            tally["candidates"],
        )
    return 1 if (tally["failed"] or tally["verify_failed"]) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the retiers. Without this the script is a read-only dry run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit no-op flag; dry run is already the default.",
    )
    parser.add_argument(
        "--project-id",
        default=DEFAULT_PROJECT_ID,
        help=f"project_id (owner/repo) to repair. Default: {DEFAULT_PROJECT_ID}",
    )
    parser.add_argument(
        "--include-null-tier",
        action="store_true",
        help="Also stamp historical-status rows whose tier is NULL (default: report only).",
    )
    parser.add_argument(
        "--option-file",
        default=None,
        help="MariaDB [client] option file. Default: the resolver's own ladder.",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="mysqld unix socket. Default: mysqld.sock beside the option file.",
    )
    parser.add_argument("--verbose", action="store_true", help="Log every candidate row.")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        logger.error("--apply and --dry-run are mutually exclusive")
        return 2

    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
