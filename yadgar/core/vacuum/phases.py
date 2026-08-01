"""Vacuum phase helpers: snapshot/drop, export, cleanup.

Functions here are not patched by tests — only _wait_for_health,
_wait_for_yadgar_health, _log_consolidation_row, and ServiceController
are patched (those live in __init__.py so patches intercept calls).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from yadgar._shared.observability.observe import observe
from yadgar.core.ops import ServiceController
from yadgar.core.vacuum.strip import strip_export_for_vacuum as strip_action_log


@observe(tier="stage")
def _dir_bytes(path: Path) -> int:
    """Return total size of all files in path (recursive). Returns 0 if missing."""
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@observe(tier="stage")
def _run_cleanup_script(yadgar_home: Path, pattern: str, keep_n: int) -> None:
    """Prune old pre-vacuum snapshots, keeping only the `keep_n` most recent.

    Inline Python implementation (C-3): replaces the broken subprocess call to
    cleanup-backups.sh, which only accepted --dry-run and rejected positional args.

    Files matching `pattern` inside `yadgar_home` are sorted by mtime (newest
    first); any beyond the first `keep_n` are deleted.
    """
    import glob

    glob_pattern = str(yadgar_home / pattern)
    candidates = sorted(glob.glob(glob_pattern), key=os.path.getmtime, reverse=True)
    to_delete = candidates[keep_n:]
    for path in to_delete:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"[vacuum] pruned snapshot: {path}", file=sys.stderr)
        except OSError as exc:
            print(f"[vacuum] failed to prune {path}: {exc}", file=sys.stderr)


#: The UTC ``%Y%m%d_%H%M%S`` stamp both vacuum writers embed in the artefact NAME
#: (``vacuum_export_<TS>.surql`` here, ``surreal_db.pre-vacuum-<TS>`` below).
#: Retention keys off the name rather than ``st_mtime`` so a ``touch``, an rsync,
#: or a restore-from-backup cannot reshuffle the window — mtime is a property of
#: the filesystem, the stamp is a property of the vacuum RUN.
_ARTEFACT_STAMP_RE = re.compile(r"(\d{8}_\d{6})")


def _artefact_stamp(name: str) -> str | None:
    """The run stamp embedded in *name*, or None when it carries none."""
    match = _ARTEFACT_STAMP_RE.search(name)
    return match.group(1) if match else None


@observe(tier="stage")
def _reap_export_pairs(yadgar_home: Path, keep_runs: int) -> None:
    """Keep the newest *keep_runs* export RUNS; delete every file of older runs.

    task:0046 (C).  ``_run_cleanup_script`` counts FILES, and one run writes TWO
    (``vacuum_export_<TS>.surql`` + ``.filtered.surql``), so "keep 2 runs" was
    spelled ``2 * 2 = 4`` files.  A run that died between the two writes leaves
    an odd file and the window silently degrades to one whole run plus a
    half-pair — an unusable artefact still costing ~100 MB.  Grouping by the
    stamp makes the ceiling mean what it says.

    A file carrying no parseable stamp is its own OLDEST group and always goes.
    That direction is deliberate and is the OPPOSITE of
    :func:`_reap_snapshots_by_age`: export scratch is diagnostic, so an
    unparseable name means partial-write debris; a snapshot is a rollback anchor,
    so an unparseable name is kept.
    """
    groups: dict[str, list[Path]] = {}
    doomed: list[Path] = []
    for path in yadgar_home.glob("vacuum_export_*"):
        stamp = _artefact_stamp(path.name)
        if stamp is None:
            doomed.append(path)
        else:
            groups.setdefault(stamp, []).append(path)
    for stamp in sorted(groups, reverse=True)[max(0, keep_runs) :]:
        doomed.extend(groups[stamp])
    for path in doomed:
        try:
            path.unlink()
            print(f"[vacuum] pruned export scratch: {path}", file=sys.stderr)
        except OSError as exc:
            print(f"[vacuum] failed to prune {path}: {exc}", file=sys.stderr)


@observe(tier="stage")
def _reap_snapshots_by_age(yadgar_home: Path, max_age_days: int) -> None:
    """Age backstop for ``surreal_db.pre-vacuum-*``; the NEWEST is always exempt.

    task:0046 (B), mirroring ADR-0076 D1's ``.old`` backstop: a host that vacuums
    rarely should not carry a six-month-old copy of a DB the live one no longer
    resembles.  Two deliberate asymmetries against :func:`_reap_export_pairs`,
    both because a snapshot is the last-resort ROLLBACK anchor (ADR-0090) rather
    than a diagnostic:

    1. The newest is exempt UNCONDITIONALLY — not "unless older than X".  A host
       that has not vacuumed in a year still ends the run with an anchor.
    2. A dir whose name carries no parseable stamp is KEPT.  An unreadable name
       is not evidence that a recovery artefact is stale, and deleting one on
       that basis is the worst failure this backstop could have.
    """
    if max_age_days <= 0:
        return
    dated = sorted(
        (stamp, path)
        for path in yadgar_home.glob("surreal_db.pre-vacuum-*")
        if (stamp := _artefact_stamp(path.name)) is not None
    )
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).strftime("%Y%m%d_%H%M%S")
    for stamp, path in dated[:-1]:  # [:-1] IS the floor — the newest never enters
        if stamp >= cutoff:
            continue
        print(
            f"[vacuum] age-backstop: reaping snapshot {path.name} (>{max_age_days}d old)",
            flush=True,
        )
        shutil.rmtree(str(path), ignore_errors=True)


@observe(tier="stage")
def _surreal_headers() -> dict[str, str]:
    """SurrealDB v2+ /export rejects with HTTP 400 'Specify a namespace' without
    these headers. Vacuum is an admin operation — use root credentials.

    Credential precedence:
      1. SURREAL_USER / SURREAL_PASS  (preferred — root IAM, used by entrypoint)
      2. YADGAR_RW_USER / YADGAR_RW_PASS  (canonical post-rename; new installs only write RW)
      3. YADGAR_DB_USER / YADGAR_DB_PASS  (legacy alias — backward compat for old installs)
      4. root / root  (built-in SurrealDB default)
    """
    import base64

    if os.environ.get("SURREAL_USER"):
        user = os.environ["SURREAL_USER"]
        password = os.environ.get("SURREAL_PASS", "root")
    elif os.environ.get("YADGAR_RW_USER"):
        user = os.environ["YADGAR_RW_USER"]
        password = os.environ.get("YADGAR_RW_PASS", "root")
    elif os.environ.get("YADGAR_DB_USER"):
        user = os.environ["YADGAR_DB_USER"]
        password = os.environ.get("YADGAR_DB_PASS", "root")
    else:
        user = "root"
        password = "root"
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "surreal-ns": "yadgar",
        "surreal-db": "main",
        "Accept": "application/json",
    }


@observe(tier="stage")
def _vacuum_export(backend_url: str, yadgar_home: Path) -> tuple[Path, Path]:
    """Phase 1: GET /export, strip action_log, write .surql files.

    Returns:
        (raw_path, filtered_path)
    Raises:
        RuntimeError on non-200 response.
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    raw_path = yadgar_home / f"vacuum_export_{ts}.surql"
    filtered_path = yadgar_home / f"vacuum_export_{ts}.filtered.surql"

    from yadgar._shared.config import get_settings as _get_settings

    _import_timeout = float(_get_settings().BACKEND_IMPORT_TIMEOUT_SEC)
    print(f"[vacuum] phase 1: GET {backend_url}/export ...", flush=True)
    resp = httpx.get(
        f"{backend_url}/export",
        headers=_surreal_headers(),
        timeout=_import_timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Export failed: HTTP {resp.status_code}\n{resp.text[:500]}")

    raw_surql = resp.text
    raw_path.write_text(raw_surql, encoding="utf-8")
    print(f"[vacuum] export saved: {raw_path} ({len(raw_surql):,} bytes)", flush=True)

    filtered = strip_action_log(raw_surql)
    filtered_path.write_text(filtered, encoding="utf-8")
    print(f"[vacuum] filtered:     {filtered_path} ({len(filtered):,} bytes)", flush=True)

    return raw_path, filtered_path


@observe(tier="stage")
def _vacuum_snapshot_and_drop(
    db_path: Path,
    yadgar_home: Path,
    svc: ServiceController,
    before_bytes: int,
) -> Path:
    """Phase 2 (v5.69 P2): stop the real backend, then snapshot a QUIESCED DB.

    Order is STOP-then-COPY (flipped from the pre-P2 copy-then-stop): copying a
    live, lock-held surrealkv dir can capture a torn segment (the BC-F3 hazard).
    The canonical ``surreal_db`` is NOT renamed or emptied here — the P2 side-path
    build + verified atomic swap (Phase 3) is the only thing that touches it, and
    only after a fully-verified compacted DB exists on a side path.  Every abort
    path therefore leaves the canonical untouched; the ``.pre-vacuum-<ts>``
    snapshot below is belt-and-suspenders.

    SCOPE (task:0111 / ADR-0188): ``stop_backend()``, NOT ``stop()``.  The CORE
    stays up for the whole vacuum.  ``ServiceController.stop()`` is
    ``("yadgar", "yadgar-backend")``, and stopping both was inherited from the
    2026-05-12 manual DB-rebuild ritual (``docs/PLAN_V4_8.md``), where the
    canonical was renamed out from under a live backend.  Every rationale that
    survives the P2 redesign is BACKEND-scoped: the torn-segment hazard above is
    about the process holding the store open, ``_assert_backend_quiesced`` polls
    the SurrealDB port, ADR-0090's corrupt-on-reopen is the backend's stop, and
    ``_verify_live_store_coherence`` only scans ``surreal … start`` processes.
    The core holds no fd into the store at all — it reaches the DB over HTTP
    (ADR-0078) — so a live core neither trips the quiescence gate nor strands an
    inode across the swap.  Stopping it bought nothing and cost ~68 s of engine
    downtime per run, dropping every connected MCP session.

    NOTE this scope change is only half the fix on a systemd host: ``Requires=``
    propagates stop, so a core unit rendered BEFORE the ``Requires=``→``Wants=``
    flip (``scripts/install/yadgar.service.in``,
    ``yadgar/core/daemon/systemd.py``) still goes down with the backend.  That is
    why the abort-path/restore ``start_yadgar()`` belts are retained.

    Returns:
        snapshot_path — the quiesced pre-vacuum snapshot.
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    snapshot_path = yadgar_home / f"surreal_db.pre-vacuum-{ts}"

    print(
        "[vacuum] phase 2: stopping the backend (quiesce before snapshot); core stays up ...",
        flush=True,
    )
    svc.stop_backend()

    print(
        f"[vacuum] phase 2: snapshot {db_path} → {snapshot_path} "
        f"({before_bytes / 1024 / 1024:.0f} MB) ...",
        flush=True,
    )
    shutil.copytree(str(db_path), str(snapshot_path))

    # NOTE: surreal_db is NOT renamed/emptied here. Phase 3 (side-build + atomic
    # swap) is the only step that touches the canonical, and only post-verify.

    return snapshot_path


# ---------------------------------------------------------------------------
# Atomic swap + crash-mid-swap recovery (v5.69 P2)
#
# Pure filesystem operations (same-dir os.rename) — not patched by tests, so
# they live here.  Re-exported from yadgar.vacuum.__init__ so callers/tests can
# reference them as yadgar.vacuum._atomic_swap / _recover_interrupted_swap.
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _atomic_swap(db_path: Path, side_path: Path) -> Path:
    """Atomically swap the verified side DB in for the canonical DB.

    Same-directory renames only (same filesystem -> atomic per-rename):
      1. canonical  -> surreal_db.old-<ts>
      2. side_path  -> canonical
    If step 2 fails, immediately rename .old back to canonical (rollback).

    A SIGKILL/OOM/power-loss BETWEEN the two renames leaves canonical ABSENT +
    .old/.new present -- see _recover_interrupted_swap (runs at vacuum start).

    Returns:
        The .old-<ts> path (the previous canonical, retained for rollback /
        retention pruning).
    Raises:
        on a rename failure that could not be rolled back (caller surfaces it).
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    old_path = db_path.parent / f"surreal_db.old-{ts}"

    print(f"[vacuum] swap: {db_path} -> {old_path} ...", flush=True)
    os.rename(str(db_path), str(old_path))  # rename 1: canonical out of the way
    try:
        print(f"[vacuum] swap: {side_path} -> {db_path} ...", flush=True)
        os.rename(str(side_path), str(db_path))  # rename 2: side in as canonical
    except OSError as exc:
        # rename 2 failed -- canonical is absent. Roll back rename 1 immediately.
        print(
            f"[vacuum] ERROR: swap rename-2 failed ({exc}); rolling back {old_path} "
            f"-> {db_path} ...",
            file=sys.stderr,
        )
        os.rename(str(old_path), str(db_path))
        raise
    return old_path


@observe(tier="stage")
def _rmtree_globs(yadgar_home: Path, *patterns: str) -> None:
    """Best-effort remove every dir matching any of *patterns* under yadgar_home."""
    for pat in patterns:
        for stale in yadgar_home.glob(pat):
            shutil.rmtree(str(stale), ignore_errors=True)


@observe(tier="stage")
def _sweep_stale_building(yadgar_home: Path) -> None:
    """Discard any UNVERIFIED `surreal_db.building-*` staging (never promotable).

    Runs at vacuum start before the current run creates its own `.building`, so
    anything found is an orphan from a crashed prior build.  M2: a `.building-*`
    is unverified by construction — recovery must NEVER promote it.
    """
    stale_building = sorted(yadgar_home.glob("surreal_db.building-*"))
    if stale_building:
        print(
            f"[vacuum] RECOVERY: discarding UNVERIFIED side-build staging "
            f"{[p.name for p in stale_building]} (never promotable).",
            file=sys.stderr,
        )
        _rmtree_globs(yadgar_home, "surreal_db.building-*")


@observe(tier="stage")
def _recover_interrupted_swap(yadgar_home: Path, db_path: Path) -> None:
    """Complete or roll back a swap interrupted by a crash between the two renames.

    Crash-mid-swap end-state: canonical ABSENT, surreal_db.old-<ts> (the
    original) and surreal_db.new-<ts> (the verified compacted DB) both present.
    Because .new was already EXACT-count-verified before the crash window opened
    (the swap only begins post-verify), the deterministic resolution is to
    COMPLETE the swap: promote .new -> canonical, then retire .old.  If no .new
    exists (crash before side-build finished, or .new already promoted) we ROLL
    BACK the newest .old -> canonical.

    M2 — `.building-*` is the UNVERIFIED staging name (side-build in progress); a
    crash mid-build leaves an `.building-*` partial that recovery MUST NEVER
    promote (only the structurally-verified `.new-*` is promotable).  Any stale
    `.building-*` is swept FIRST — before the canonical-present early-return —
    because recovery runs once at vacuum start, before the current run creates its
    own `.building`, so anything found here is an orphan from a crashed prior run.

    Assumes a SINGLE in-flight swap (vacuum is not concurrent -- P3's lock + the
    nightly serialization enforce this).  If multiple .old/.new pairs exist
    (multiple prior crashes) we pick the NEWEST by timestamp and warn.

    NO-OP when canonical is present -- nothing to recover.

    RESIDUAL WINDOW (documented, not silent): this runs at VACUUM start.  Wiring
    it into daemon start is deferred to a follow-up; until then, a crash mid-swap
    leaves the canonical absent until the next vacuum run invokes this routine.
    The backend will not start on an absent canonical in that window -- operator-
    visible (fails fast), not silent data loss.
    """
    # Sweep stale `.building-*` (UNVERIFIED partials) FIRST — before the
    # canonical-present early-return — and NEVER promote one.
    _sweep_stale_building(yadgar_home)

    if db_path.exists():
        return  # canonical present -> no interrupted swap

    olds = sorted(yadgar_home.glob("surreal_db.old-*"))
    news = sorted(yadgar_home.glob("surreal_db.new-*"))
    if not olds and not news:
        return  # canonical absent but no swap staging -- not our case

    print(
        f"[vacuum] RECOVERY: canonical {db_path} is ABSENT with swap staging "
        f"present (old={[p.name for p in olds]} new={[p.name for p in news]}). "
        "Completing/rolling back interrupted swap ...",
        file=sys.stderr,
    )
    if len(olds) > 1 or len(news) > 1:
        print(
            "[vacuum] RECOVERY WARNING: multiple swap-staging dirs found -- "
            "assuming single in-flight swap, using the NEWEST pair.",
            file=sys.stderr,
        )

    if news:
        # .new was verified pre-crash -> COMPLETE the swap.
        newest_new = news[-1]
        print(f"[vacuum] RECOVERY: promoting {newest_new.name} -> {db_path.name}", file=sys.stderr)
        os.rename(str(newest_new), str(db_path))
        _rmtree_globs(yadgar_home, "surreal_db.old-*", "surreal_db.new-*", "surreal_db.building-*")
    elif olds:
        # No .new -> roll back the original.
        newest_old = olds[-1]
        print(
            f"[vacuum] RECOVERY: rolling back {newest_old.name} -> {db_path.name}", file=sys.stderr
        )
        os.rename(str(newest_old), str(db_path))
        _rmtree_globs(yadgar_home, "surreal_db.old-*", "surreal_db.building-*")

    print(f"[vacuum] RECOVERY: canonical restored at {db_path}.", file=sys.stderr)
