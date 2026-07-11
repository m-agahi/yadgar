"""Safe-start guard for the yadgar-backend container (P0 #37, Option D + 5b).

RCA: docs/plans/surrealkv-safe-stop-2026-07-10.md. SurrealKV skips its async
store close on every SIGTERM stop (upstream `impl Drop for Tree` — no fixed
release; corruption class tracked as surrealdb#5001). When SIGTERM lands
mid-compaction the on-disk manifest can reference an sstable that was never
fsynced → `Failed to load manifest` / `Error loading table N: NotFound` →
systemd start-timeout crashloop until a human runs the §6 runbook.

This module automates that runbook, invoked by entrypoint-backend.sh:

  ``preflight`` (before ``surreal start``) — 5b split-brain guard: if a
  leftover ``surreal_db.old-*`` contains writes NEWER than the canonical
  (by INNER-file mtime — dir names and dir mtimes LIE, ``os.rename``
  preserves them; proven misleading in the 07-09/07-10 incident), REFUSE
  to start and point at the runbook. Auto-resolving that state risks
  rolling back good data — a human decides.

  ``recover`` (after surreal dies during the startup health wait) — Option D
  torn-manifest auto-restore: verify the failure signature in the captured
  startup log, move the corrupt canonical aside to ``surreal_db.CORRUPT-<ts>``
  (NEVER deleted — forensics + fallback), COPY in the newest structurally
  complete quiesced candidate (``.old-*`` / ``.pre-vacuum-*``, chosen by
  newest inner-file mtime), and remove the stale ``LOCK``.

Row-count verification of the restore source is NOT possible here: counting
rows requires opening the store, and this code runs precisely because the
store will not open. Structural completeness + inner-mtime recency are the
best pre-open proxies; the post-restore surreal retry plus the next
check_invariants pass are the count-level verification.

Exit codes (consumed by entrypoint-backend.sh):
  0 — ok (preflight passed / restore performed)
  2 — recover: failure signature is NOT a torn manifest (do not touch data)
  3 — recover: torn manifest but no viable restore source (fail loud)
  4 — preflight: split-brain evidence (fresher ``.old``) — refuse startup
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from yadgar._shared.observability.observe import observe

CANONICAL_NAME = "surreal_db"

#: Substrings of the torn-manifest crash signature (RCA §1, journal-verified).
TORN_MANIFEST_PATTERNS: tuple[str, ...] = (
    "Failed to load manifest",
    "Error loading table",
)

#: Backup-candidate glob patterns, in no particular order — selection is by
#: newest INNER-file mtime, never by name.
_CANDIDATE_GLOBS: tuple[str, ...] = (
    f"{CANONICAL_NAME}.old-*",
    f"{CANONICAL_NAME}.pre-vacuum-*",
)

RUNBOOK_POINTER = "docs/plans/surrealkv-safe-stop-2026-07-10.md §6"

EXIT_OK = 0
EXIT_NOT_TORN = 2
EXIT_NO_RESTORE_SOURCE = 3
EXIT_SPLIT_BRAIN = 4


@observe(tier="stage")
def is_torn_manifest_failure(log_text: str) -> bool:
    """True iff *log_text* carries the torn-manifest crash signature."""
    return any(pat in log_text for pat in TORN_MANIFEST_PATTERNS)


@observe(span=False)
def newest_inner_mtime(d: Path) -> float | None:
    """Newest mtime over all FILES inside *d* (recursive); None if no files.

    Inner-file mtime is the ONLY trustworthy freshness signal for surrealkv
    dirs: ``os.rename`` preserves the dir's own mtime, and dir names encode
    the vacuum-swap timestamp, not the content age (RCA §4).
    """
    newest: float | None = None
    for f in d.rglob("*"):
        if f.is_file():
            mtime = f.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    return newest


@observe(span=False)
def is_structurally_complete(d: Path) -> bool:
    """True iff *d* looks like a complete surrealkv store (manifest + data).

    A torn/partial dir missing its manifest must never be picked as a restore
    source. This is a structural proxy — row-count verification requires
    opening the store, which is impossible pre-restore (module docstring).
    """
    if not (d / "manifest").is_file():
        return False
    return any(
        (d / sub).is_dir() and any((d / sub).iterdir()) for sub in ("sstables", "vlog", "wal")
    )


@observe(tier="stage")
def list_restore_candidates(data_dir: Path) -> list[Path]:
    """All ``.old-*`` / ``.pre-vacuum-*`` dirs under *data_dir* (unordered)."""
    out: list[Path] = []
    for pattern in _CANDIDATE_GLOBS:
        out.extend(p for p in data_dir.glob(pattern) if p.is_dir())
    return out


@observe(tier="stage")
def choose_restore_source(candidates: list[Path]) -> Path | None:
    """Pick the restore source: newest INNER-file mtime among complete dirs."""
    best: Path | None = None
    best_mtime = float("-inf")
    for cand in candidates:
        if not is_structurally_complete(cand):
            continue
        mtime = newest_inner_mtime(cand)
        if mtime is not None and mtime > best_mtime:
            best, best_mtime = cand, mtime
    return best


@observe(tier="stage")
def detect_split_brain(data_dir: Path, tolerance_s: float = 60.0) -> Path | None:
    """5b guard: return the ``.old-*`` dir that is FRESHER than the canonical.

    The 07-09 incident state: the live store inode sat at ``surreal_db.old-*``
    (receiving writes for 16 h) while the ``surreal_db`` path held a stale
    decoy. If any ``.old-*``'s newest inner file is newer than the canonical's
    by more than *tolerance_s*, that is split-brain evidence — refuse startup
    and let a human decide (auto-resolving risks rolling back good data).

    Canonical ABSENT → None (fresh install / mid-swap crash; the vacuum's
    ``_recover_interrupted_swap`` owns that state). Canonical present but
    EMPTY while a populated ``.old-*`` exists → split-brain evidence.
    """
    canonical = data_dir / CANONICAL_NAME
    if not canonical.is_dir():
        return None
    canonical_mtime = newest_inner_mtime(canonical)
    threshold = float("-inf") if canonical_mtime is None else canonical_mtime + tolerance_s
    freshest: Path | None = None
    freshest_mtime = threshold
    for old in data_dir.glob(f"{CANONICAL_NAME}.old-*"):
        if not old.is_dir():
            continue
        old_mtime = newest_inner_mtime(old)
        if old_mtime is not None and old_mtime > freshest_mtime:
            freshest, freshest_mtime = old, old_mtime
    return freshest


@observe(tier="stage")
def perform_auto_restore(data_dir: Path) -> tuple[Path, Path | None]:
    """Automate the §6 runbook: preserve the corrupt canonical, copy in the source.

    Returns ``(source, corrupt_aside)`` — *corrupt_aside* is None when the
    canonical was absent. Raises RuntimeError when no viable restore source
    exists (caller fails loud with the runbook pointer).

    Safety properties (tested):
      - the corrupt canonical is MOVED aside (``.CORRUPT-<ts>``), never deleted;
      - the restore is a COPY (``cp -a`` equivalent — mtimes preserved, fresh
        inodes); the source survives as fallback;
      - the stale ``LOCK`` is removed from the restored canonical only.
    """
    canonical = data_dir / CANONICAL_NAME
    source = choose_restore_source(list_restore_candidates(data_dir))
    if source is None:
        raise RuntimeError(
            f"no structurally complete restore source under {data_dir} "
            f"(looked for {', '.join(_CANDIDATE_GLOBS)})"
        )
    corrupt_aside: Path | None = None
    if canonical.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        corrupt_aside = data_dir / f"{CANONICAL_NAME}.CORRUPT-{ts}"
        canonical.rename(corrupt_aside)
    shutil.copytree(source, canonical)  # copy2 semantics: mtimes kept, fresh inodes
    (canonical / "LOCK").unlink(missing_ok=True)
    return source, corrupt_aside


# ---------------------------------------------------------------------------
# CLI — consumed by entrypoint-backend.sh
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _cmd_preflight(data_dir: Path) -> int:
    fresher = detect_split_brain(data_dir)
    if fresher is not None:
        print(
            f"safe_start: REFUSING startup — {fresher.name} contains writes NEWER than "
            f"{CANONICAL_NAME} (inner-file mtime). This is path/inode split-brain evidence "
            f"(the 07-09 incident state); auto-resolving risks rolling back good data.\n"
            f"safe_start: a human must pick the restore source. Runbook: {RUNBOOK_POINTER}",
            file=sys.stderr,
        )
        return EXIT_SPLIT_BRAIN
    print("safe_start: preflight ok", file=sys.stderr)
    return EXIT_OK


@observe(tier="stage")
def _cmd_recover(data_dir: Path, startup_log: Path) -> int:
    log_text = ""
    if startup_log.is_file():
        log_text = startup_log.read_text(errors="replace")
    if not is_torn_manifest_failure(log_text):
        print(
            "safe_start: surreal startup failure is NOT the torn-manifest signature — "
            "refusing to touch the data dir (auto-restore only heals the documented "
            f"corruption class). Runbook: {RUNBOOK_POINTER}",
            file=sys.stderr,
        )
        return EXIT_NOT_TORN
    print(
        "safe_start: TORN MANIFEST detected in surreal startup log — attempting auto-restore "
        "(corrupt canonical will be preserved aside, never deleted)",
        file=sys.stderr,
    )
    try:
        source, corrupt_aside = perform_auto_restore(data_dir)
    except RuntimeError as exc:
        print(
            f"safe_start: auto-restore IMPOSSIBLE: {exc}\n"
            f"safe_start: manual recovery required. Runbook: {RUNBOOK_POINTER}",
            file=sys.stderr,
        )
        return EXIT_NO_RESTORE_SOURCE
    print(
        f"safe_start: AUTO-RESTORE complete — restored from {source.name} "
        f"(newest inner-file mtime); corrupt canonical preserved at "
        f"{corrupt_aside.name if corrupt_aside else '<none — canonical was absent>'}; "
        f"stale LOCK removed. Verify row counts via memory_stats() once the backend is up.",
        file=sys.stderr,
    )
    return EXIT_OK


@observe(tier="stage")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yadgar.backend.safe_start", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="5b split-brain guard (before surreal start)")
    p_pre.add_argument("--data-dir", required=True, type=Path)

    p_rec = sub.add_parser("recover", help="Option D torn-manifest auto-restore")
    p_rec.add_argument("--data-dir", required=True, type=Path)
    p_rec.add_argument("--startup-log", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "preflight":
        return _cmd_preflight(args.data_dir)
    return _cmd_recover(args.data_dir, args.startup_log)


if __name__ == "__main__":  # pragma: no cover — exercised via the CLI tests' main()
    sys.exit(main())
