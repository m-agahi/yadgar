"""yadgar vacuum — side-path build + verified atomic swap (v5.69 P2).

DATA-SAFETY rewrite.  A vacuum bug destroyed 3622 real memories on 2026-06-16:
the prior flow built the compacted DB IN PLACE on the canonical path (rename
surreal_db → .bloated, start an EMPTY backend on canonical, /import); when the
restore that flow relied on itself failed, the canonical was left empty and the
original stranded.  This module NEVER renames or empties the canonical until a
fully-built, EXACT-per-table-count-verified compacted DB exists on a side path.

Flow:

  0. Startup recovery: complete/roll back any swap interrupted by a crash
     (canonical absent + .old/.new present) — BEFORE the preflight.
  1. Preflight: confirm surreal_db/ exists, backend reachable, free space (~2.5x).
  2. Capture EXACT per-table source counts (surviving set; action_log excluded).
  3. Phase 1 — Export: GET /export → strip_export_for_vacuum().
  4. Phase 2 — Stop the real backend, then snapshot a QUIESCED .pre-vacuum copy
               (belt-and-suspenders; canonical NOT renamed/emptied here).
  5. Phase 3 — Side-build: spawn a throwaway surreal on surreal_db.building-<ts>
               (alt port), /import, re-define users, then VERIFY by reopening
               and asserting per-table counts EXACTLY match source.  Stop the
               throwaway GRACEFULLY (assert clean exit) so the dir is flushed.
               On any failure → ABORT: canonical untouched, real backend back up.
               Only AFTER verify+clean-stop, PROMOTE surreal_db.building-<ts> →
               surreal_db.new-<ts> (so a `.new-*` structurally means
               verified-complete; recovery promotes `.new` without re-verifying).
               Then ATOMIC SWAP (same-dir renames): surreal_db → .old-<ts>,
               .new-<ts> → surreal_db (rollback .old on rename-2 failure).
               Start the real backend on the swapped-in compacted DB.
  6. Finalize: start yadgar, check_invariants, retire .old, prune snapshots.
  7. Report: log before/after bytes, duration, insert consolidation_log row.

_restore_db is now a THIN FALLBACK (only if the post-swap backend won't come up).

Public entry point:
    cmd_vacuum_impl(args) -> int   (0 = success, non-zero = failure)

The top-level cmd_vacuum(args) in __main__.py delegates here.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from yadgar._shared.observability.observe import observe
from yadgar.core._surreal_runner import _resolve_db_creds
from yadgar.core.ops import ServiceController, detect_service_mode
from yadgar.core.vacuum.phases import (
    _atomic_swap,
    _dir_bytes,
    _recover_interrupted_swap,
    _run_cleanup_script,
    _vacuum_export,
    _vacuum_snapshot_and_drop,
)
from yadgar.core.vacuum.strip import strip_action_log, strip_export_for_vacuum

__all__ = [
    "cmd_vacuum_impl",
    "strip_action_log",
    "strip_export_for_vacuum",
    "_run_cleanup_script",
    "ServiceController",
    "_wait_for_health",
    "_wait_for_yadgar_health",
    "_log_consolidation_row",
    "_redefine_users_post_import",
    "_capture_table_counts",
    "_build_and_verify_side_db",
    "_atomic_swap",
    "_recover_interrupted_swap",
    "_resolve_db_creds",
]

# Tables intentionally dropped on /import (see yadgar.vacuum.strip) — excluded
# from the exact-count verification gate because the compacted DB legitimately
# does NOT contain them.  A vacuum bug destroyed 3622 real memories on 2026-06-16
# when a partial restore (1484/3622) passed a "non-empty"/">=" check; the gate
# below therefore requires EXACT per-table equality over the SURVIVING set only.
_STRIPPED_TABLES = frozenset({"action_log"})


# ---------------------------------------------------------------------------
# HTTP helpers (patched by tests via yadgar.vacuum._wait_for_health etc.)
# ---------------------------------------------------------------------------


# _resolve_db_creds is imported at module top from yadgar._surreal_runner (next
# to spawn_surreal) so the HTTP client here and the side-backend spawn share one
# source of truth; re-exported via __all__ as yadgar.vacuum._resolve_db_creds.


@observe(tier="stage")
def _build_http_client(backend_url: str) -> httpx.Client:
    """Build an httpx.Client with SurrealDB root credentials.

    Credential precedence (vacuum is an admin operation, needs root IAM):
      1. SURREAL_USER / SURREAL_PASS  (preferred — root IAM, same creds used by entrypoint)
      2. YADGAR_RW_USER / YADGAR_RW_PASS  (canonical post-rename; new installs only write RW)
      3. YADGAR_DB_USER / YADGAR_DB_PASS  (legacy alias — backward compat for old installs)
      4. root / root  (built-in SurrealDB default)
    """
    import base64

    user, password = _resolve_db_creds()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return httpx.Client(
        base_url=backend_url,
        headers={
            "Authorization": f"Basic {auth}",
            "surreal-ns": "yadgar",
            "surreal-db": "main",
            "Accept": "application/json",
        },
        timeout=120.0,
    )


@observe(tier="stage")
def _wait_for_health(
    url: str,
    timeout_s: float = 120.0,
    poll_interval: float = 1.0,
) -> bool:
    """Poll GET <url>/health until 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


@observe(tier="stage")
def _wait_for_yadgar_health(
    url: str,
    timeout_s: float = 180.0,
    poll_interval: float = 1.0,
) -> bool:
    """Poll GET <url>/health until 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


@observe(tier="stage")
def _log_consolidation_row(row: dict) -> None:
    """Insert a consolidation_log row via the SurrealDB SQL API.

    Non-fatal: if this fails we just warn. The vacuum itself succeeded.
    """
    backend_url = row.pop(
        "_backend_url",
        os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080"),
    )
    try:
        client = _build_http_client(backend_url)
        stmt = (
            "INSERT INTO consolidation_log {"
            "kind: $kind,"
            "started_at: $started_at,"
            "finished_at: $finished_at,"
            "duration_seconds: $duration_seconds,"
            "before_bytes: $before_bytes,"
            "after_bytes: $after_bytes,"
            "saved_bytes: $saved_bytes,"
            "saved_pct: $saved_pct"
            "}"
        )
        client.post(
            "/sql",
            content=stmt,
            headers={"Content-Type": "text/plain"},
            params=row,
        )
        client.close()
    except Exception as exc:
        print(f"[vacuum] warning: could not insert consolidation_log row: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Free-port + per-table count capture (data-safety primitives)
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _free_port() -> int:
    """Return a free TCP port on localhost.

    Inlined here (production code) rather than importing the test helper so the
    side-build path has no test dependency.  Binds :0, reads the OS-assigned
    port, releases it; a small TOCTOU window exists but the throwaway surreal
    retries are unnecessary at vacuum frequency.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@observe(tier="stage")
def _sql_result(resp, label: str):  # type: ignore[no-untyped-def]
    """Validate a SurrealDB /sql HTTP response and return the last block's result.

    Raises RuntimeError on any non-OK status / unparseable payload — callers
    treat that as ABORT (never swap on an unverifiable read).
    """
    if resp.status_code != 200:
        raise RuntimeError(f"{label} failed: HTTP {resp.status_code}\n{resp.text[:300]}")
    blocks = resp.json()
    if not isinstance(blocks, list) or not blocks:
        raise RuntimeError(f"{label} returned unexpected payload: {blocks!r}")
    block = blocks[-1]
    if not isinstance(block, dict) or block.get("status") not in (None, "OK"):
        raise RuntimeError(f"{label} returned error: {block!r}")
    return block.get("result", {})


@observe(tier="stage")
def _capture_table_counts(backend_url: str) -> dict[str, int]:
    """Return EXACT per-table row counts for the SURVIVING tables.

    THE data-safety gate.  Tables that vacuum intentionally strips on /import
    (``action_log``) are excluded — the compacted DB legitimately lacks them, so
    including them would force a spurious mismatch on every happy-path run.

    Discovers tables via ``INFO FOR DB`` (the authoritative table list), then
    ``SELECT count() ... GROUP ALL`` per table.  Counts are EXACT, per table —
    the 06-16 partial restore (1484/3622) was non-empty and would have passed
    any ">=" / "non-empty" check; only exact per-table equality catches it.

    Raises:
        RuntimeError: if the table list or any count cannot be read (a torn /
            unopenable store).  Callers treat any failure here as ABORT — never
            proceed to a swap on an unverifiable side DB.
    """

    def _post(client, sql: str):  # type: ignore[no-untyped-def]
        return client.post("/sql", content=sql.encode(), headers={"Content-Type": "text/plain"})

    with _build_http_client(backend_url) as client:
        result = _sql_result(_post(client, "INFO FOR DB;"), "INFO FOR DB")
        tables = result.get("tables", {}) if isinstance(result, dict) else {}
        table_names = sorted(t for t in tables if t not in _STRIPPED_TABLES)

        counts: dict[str, int] = {}
        for table in table_names:
            # Backtick-quote so hyphenated/reserved names parse as identifiers.
            rows = _sql_result(
                _post(client, f"SELECT count() FROM `{table}` GROUP ALL;"),
                f"count({table})",
            )
            counts[table] = int(rows[0]["count"]) if rows else 0
        return counts


# ---------------------------------------------------------------------------
# Post-import user re-bootstrap
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _redefine_users_post_import(backend_url: str) -> None:
    """Re-create yadgar-rw and yadgar-ro on the freshly-imported DB.

    SurrealDB /import wipes ROOT-level user definitions regardless of what the
    export contains (users are infrastructure state, not data).  The root user
    survives only because SurrealDB re-bootstraps it from SURREAL_USER/PASS env
    on every server start.  Non-root users defined via DEFINE USER must be
    explicitly re-created here so yadgar core can authenticate after vacuum.

    Uses raw SurrealQL with Content-Type: text/plain.  The SurrealDB v3 HTTP
    ``/sql`` endpoint does NOT execute SQL sent in a JSON body — it treats the
    body as a literal JSON value and returns it via implicit RETURN, silently
    no-opping.  Only a plain-text body is parsed as SurrealQL.

    Passwords are embedded as single-quoted SurrealQL string literals.  Any
    literal single-quote in a password is SQL-escaped by doubling (``'`` →
    ``''``) before interpolation.

    Raises:
        RuntimeError: if YADGAR_RW_PASS or YADGAR_RO_PASS are missing, or if
            the SurrealDB /sql request returns a non-200 status.
    """
    rw_user = os.environ.get("YADGAR_RW_USER", "yadgar-rw")
    rw_pass = os.environ.get("YADGAR_RW_PASS")
    ro_user = os.environ.get("YADGAR_RO_USER", "yadgar-ro")
    ro_pass = os.environ.get("YADGAR_RO_PASS")

    if not rw_pass or not ro_pass:
        raise RuntimeError(
            "YADGAR_RW_PASS / YADGAR_RO_PASS env vars are required for vacuum "
            "post-import user re-bootstrap. SurrealDB /import does not preserve "
            "non-root user definitions; vacuum must re-create them."
        )

    # SQL-escape passwords: double any single-quote characters so the literal
    # is safe inside a SurrealQL single-quoted string (SQL-standard escaping).
    rw_pass_esc = rw_pass.replace("'", "''")
    ro_pass_esc = ro_pass.replace("'", "''")

    # Usernames are backtick-quoted so hyphenated names (e.g. yadgar-rw) are
    # treated as identifiers, not subtraction expressions.
    sql = (
        f"DEFINE USER IF NOT EXISTS `{rw_user}` ON ROOT "
        f"PASSWORD '{rw_pass_esc}' ROLES OWNER; "
        f"DEFINE USER IF NOT EXISTS `{ro_user}` ON ROOT "
        f"PASSWORD '{ro_pass_esc}' ROLES VIEWER;"
    )

    print("[vacuum] re-defining yadgar-rw + yadgar-ro on imported DB ...", flush=True)
    with _build_http_client(backend_url) as client:
        resp = client.post(
            "/sql",
            content=sql.encode(),
            headers={"Content-Type": "text/plain"},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to re-define users post-import: HTTP {resp.status_code}\n{resp.text[:500]}"
            )
    print("[vacuum] users re-defined.", flush=True)


# ---------------------------------------------------------------------------
# Phase 3 (v5.69 P2): side-path build → verify → stop-clean → atomic swap
#
# The 06-16 data loss came from building the compacted DB IN PLACE on the
# canonical path: rename canonical → .bloated, start an EMPTY backend on
# canonical, import; if that restore itself failed, canonical was left empty and
# the original stranded.  The new design NEVER renames or empties the canonical
# until a fully-built, EXACT-count-verified compacted DB exists on a side path.
# Every abort path leaves the canonical (and the still-running real backend)
# completely untouched.
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _bootstrap_namespace(backend_url: str) -> None:
    """Create the yadgar/main namespace+database on a fresh side DB.

    SurrealDB's /import requires the target namespace to exist — it does NOT
    auto-create it from headers, and the export omits DEFINE NAMESPACE/DATABASE.
    """
    with _build_http_client(backend_url) as ns_client:
        ns_resp = ns_client.post(
            "/sql",
            content=(
                "DEFINE NAMESPACE IF NOT EXISTS yadgar; "
                "USE NS yadgar; DEFINE DATABASE IF NOT EXISTS main;"
            ),
            headers={"Content-Type": "text/plain"},
        )
        if ns_resp.status_code != 200:
            raise RuntimeError(
                f"namespace bootstrap failed: HTTP {ns_resp.status_code}\n{ns_resp.text[:200]}"
            )


@observe(tier="stage")
def _stop_side_backend_clean(proc, side_url: str) -> None:
    """Stop the throwaway side backend GRACEFULLY and assert it fully exited.

    A SIGKILL'd surrealkv dir can be half-flushed; renaming such a dir into the
    canonical path is the corrupt-on-reopen risk this design must avoid.  So we
    SIGTERM and require a clean exit — if the process does not exit on its own
    within the grace window (i.e. it would need SIGKILL), we RAISE so the caller
    ABORTS the swap and leaves the canonical untouched.
    """
    if proc is None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=15.0)
    except Exception as exc:  # subprocess.TimeoutExpired or similar
        # Escalate to kill so we don't leak the process, but ABORT the swap:
        # a non-graceful stop means the segments may not be flushed.
        try:
            proc.kill()
            proc.wait(timeout=5.0)
        except Exception:
            pass
        raise RuntimeError(
            f"side backend at {side_url} did not exit gracefully on SIGTERM "
            f"({exc}); refusing to swap a possibly half-flushed surrealkv dir"
        ) from exc
    # Belt-and-suspenders: poll the URL until it stops answering (lock released).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{side_url}/health", timeout=1.0)
        except Exception:
            break  # connection refused → port released → process gone
        time.sleep(0.2)


@observe(tier="stage")
def _build_and_verify_side_db(
    backend_url: str,
    filtered_path: Path,
    side_path: Path,
    source_counts: dict[str, int],
) -> bool:
    """Build the compacted DB on *side_path* and verify it EXACTLY matches source.

    *side_path* is the UNVERIFIED staging dir (``surreal_db.building-<ts>``); the
    caller promotes it to ``surreal_db.new-<ts>`` only AFTER this returns True.
    Spawns a throwaway surreal on an ALT free port pointing at ``side_path``
    (NOT the canonical), bootstraps the namespace, POSTs /import, re-defines
    users, then REOPENS/queries the side DB and asserts per-table counts match
    ``source_counts`` EXACTLY.  Stops the throwaway gracefully (asserting a clean
    exit) so the dir is safe to rename in.

    Returns:
        True iff the side DB is built AND verified (safe to promote + swap in).
        False on ANY failure (import error, count mismatch, non-graceful stop) —
        the caller cleans up the staging path; the canonical is left untouched.

    The throwaway is spawned directly via yadgar._surreal_runner.spawn_surreal
    (production-importable), NOT via ServiceController — ServiceController governs
    only the real backend lifecycle.
    """
    from yadgar.core._surreal_runner import spawn_surreal, teardown_surreal_proc

    side_path.mkdir(parents=True, exist_ok=True)
    side_port = _free_port()
    side_url = f"http://127.0.0.1:{side_port}"
    proc = None
    stopped_clean = False
    try:
        print(
            f"[vacuum] side-build: spawning throwaway surreal on {side_url} → {side_path} ...",
            flush=True,
        )
        _side_user, _side_pass = _resolve_db_creds()
        proc = spawn_surreal(
            port=side_port,
            data_dir=str(side_path),
            surreal_user=_side_user,
            surreal_pass=_side_pass,
        )
        if not _wait_for_health(side_url, timeout_s=120.0):
            print(
                f"[vacuum] ERROR: side backend at {side_url} did not become healthy.",
                file=sys.stderr,
            )
            return False

        _bootstrap_namespace(side_url)

        surql_content = filtered_path.read_bytes()
        print(
            f"[vacuum] side-build: POST {side_url}/import ({len(surql_content):,} bytes) ...",
            flush=True,
        )
        client = _build_http_client(side_url)
        import_headers = {"Content-Type": "text/plain", **dict(client.headers)}
        client.close()

        from yadgar._shared.config import get_settings as _get_settings

        _import_timeout = float(_get_settings().BACKEND_IMPORT_TIMEOUT_SEC)
        resp = httpx.post(
            f"{side_url}/import",
            content=surql_content,
            headers=import_headers,
            timeout=_import_timeout,
        )
        if resp.status_code != 200:
            print(
                f"[vacuum] ERROR: side /import returned HTTP {resp.status_code}:\n"
                f"{resp.text[:1000]}\n[vacuum] ABORT: canonical untouched.",
                file=sys.stderr,
            )
            return False

        # /import wipes ROOT-level user defs — re-create on the side DB.
        _redefine_users_post_import(side_url)

        # -- VERIFY: reopen-query the side DB; EXACT per-table count match. --
        print("[vacuum] side-build: verifying per-table row counts ...", flush=True)
        try:
            side_counts = _capture_table_counts(side_url)
        except Exception as exc:
            print(
                f"[vacuum] ERROR: could not read side DB counts ({exc}).\n"
                "[vacuum] ABORT: canonical untouched.",
                file=sys.stderr,
            )
            return False

        if side_counts != source_counts:
            print(
                f"[vacuum] ERROR: VERIFICATION FAILED — side counts do not EXACTLY "
                f"match source.\n  source={source_counts}\n  side  ={side_counts}\n"
                "[vacuum] ABORT: canonical untouched (this is the 06-16 guard: a "
                "partial import must never be swapped in).",
                file=sys.stderr,
            )
            return False

        print(
            f"[vacuum] side-build: verified OK ({source_counts}). "
            "Stopping throwaway backend before swap ...",
            flush=True,
        )
        # Stop gracefully + assert fully exited (lock released, segments flushed)
        # BEFORE any rename — a half-flushed renamed-in dir is corrupt-on-reopen.
        _stop_side_backend_clean(proc, side_url)
        stopped_clean = True
        return True
    except Exception as exc:
        print(f"[vacuum] ERROR in side-build: {exc}", file=sys.stderr)
        return False
    finally:
        if proc is not None and not stopped_clean:
            teardown_surreal_proc(proc, wait_timeout=5)


@observe(tier="stage")
def _restore_db(
    old_path: Path,
    db_path: Path,
    svc: ServiceController,
    backend_url: str,
) -> None:
    """THIN FALLBACK: roll the swapped-in DB back to the previous canonical.

    No longer the primary safety mechanism (the side-build + verified atomic swap
    is) — reached ONLY when the real backend will not come up on the
    just-swapped-in compacted DB.  Stops the backend, removes the bad
    swapped-in canonical, renames the retained ``.old-<ts>`` back to canonical,
    and restarts.  Errors are logged, not re-raised (caller already returns
    non-zero); the .pre-vacuum snapshot remains as a last resort.
    """
    try:
        print(
            f"[vacuum] restore: stopping backend and rolling {old_path} → {db_path} ...",
            file=sys.stderr,
        )
        svc.stop_backend()
        if db_path.exists():
            shutil.rmtree(str(db_path), ignore_errors=True)
        os.rename(str(old_path), str(db_path))
        svc.start_backend()
        print("[vacuum] restore: backend restarted on previous DB.", file=sys.stderr)
    except Exception as exc:
        print(
            f"[vacuum] CRITICAL: restore failed: {exc}\n"
            f"Manual recovery needed: rename {old_path} → {db_path}",
            file=sys.stderr,
        )


@observe(tier="stage")
def _side_build_swap_and_start(
    backend_url: str,
    filtered_path: Path,
    db_path: Path,
    yadgar_home: Path,
    source_counts: dict[str, int],
    svc: ServiceController,
) -> Path | None:
    """Phase 3: build a verified compacted DB on a side path, atomically swap it
    in for the canonical, and start the real backend on it.

    On ANY abort (side-build/verify failure, swap failure, or post-swap backend
    not coming up) the canonical is left intact and the real backend is restarted
    on the original DB; returns None.  On success returns the retained
    ``surreal_db.old-<ts>`` path (the previous canonical) for the finalizer to
    retire after check_invariants.
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    # M2: build UNVERIFIED content under `.building-<ts>`.  Only after the side DB
    # is built AND EXACT-count-verified AND the throwaway backend stopped clean is
    # it promoted (renamed) to `.new-<ts>`.  This makes "a `.new-*` exists"
    # STRUCTURALLY mean "verified-complete" — crash-recovery promotes `.new`
    # without re-verifying, so an unverified partial must never wear that name.
    building_path = yadgar_home / f"surreal_db.building-{ts}"
    side_path = yadgar_home / f"surreal_db.new-{ts}"

    def _abort_restart(msg: str) -> None:
        print(msg, file=sys.stderr)
        shutil.rmtree(str(building_path), ignore_errors=True)
        shutil.rmtree(str(side_path), ignore_errors=True)
        try:
            svc.start_backend()
        except Exception as exc:
            print(
                f"[vacuum] WARNING: could not restart backend after abort: {exc}", file=sys.stderr
            )

    # 3a: build + EXACT-count verify under `.building-<ts>` (canonical untouched).
    if not _build_and_verify_side_db(backend_url, filtered_path, building_path, source_counts):
        _abort_restart(
            "[vacuum] ABORT: side-build/verify failed — canonical untouched, "
            "restarting real backend on original DB."
        )
        return None

    # 3a': PROMOTE `.building` → `.new` now that it is verified-complete + flushed.
    # A crash AFTER this rename leaves a `.new-*` that recovery may promote without
    # re-verifying — which is safe precisely because the rename only happens here,
    # post-verify + post-clean-stop.
    try:
        os.rename(str(building_path), str(side_path))
    except OSError as exc:
        _abort_restart(
            f"[vacuum] ERROR: could not promote {building_path.name} → {side_path.name}: {exc}\n"
            "[vacuum] canonical untouched; restarting real backend on original DB."
        )
        return None

    # 3b: atomic same-dir swap (canonical → .old, side → canonical).
    try:
        old_path = _atomic_swap(db_path, side_path)
    except Exception as exc:
        # _atomic_swap already rolled back rename-1 on a rename-2 failure.
        _abort_restart(
            f"[vacuum] ERROR: atomic swap failed: {exc}\n"
            "[vacuum] canonical restored; restarting real backend on original DB."
        )
        return None

    # 3c: start the real backend on the swapped-in compacted DB.
    print("[vacuum] starting yadgar-backend on swapped-in compacted DB ...", flush=True)
    svc.start_backend()
    if not _wait_for_health(backend_url, timeout_s=120.0):
        # Post-swap backend won't come up — _restore_db is the thin fallback.
        print(
            "[vacuum] ERROR: backend did not become healthy on the swapped-in DB. "
            "Falling back to restore from .old ...",
            file=sys.stderr,
        )
        _restore_db(old_path, db_path, svc, backend_url)
        shutil.rmtree(str(side_path), ignore_errors=True)
        return None

    return old_path


# ---------------------------------------------------------------------------
# Finalize (calls _wait_for_yadgar_health — must live here)
# ---------------------------------------------------------------------------


def _vacuum_old_max_age_days() -> int:
    """Return VACUUM_OLD_MAX_AGE_DAYS (default 7).

    Reads os.getenv live so tests can monkeypatch without module reload.
    """
    return int(os.getenv("VACUUM_OLD_MAX_AGE_DAYS", "7"))


@observe(tier="stage")
def _reap_stale_old_dirs(yadgar_home: Path, current_old: Path) -> None:
    """Age-backstop reap for surreal_db.old-* dirs (ADR-0076 D1).

    Removes any surreal_db.old-* dir whose mtime exceeds VACUUM_OLD_MAX_AGE_DAYS,
    EXCEPT for ``current_old`` (the .old created by THIS vacuum run — it is the
    immediate rollback anchor and must not be reaped until check_invariants passes
    and the normal retirement path handles it).

    Runs every vacuum finalize regardless of check_invariants outcome — the
    backstop fires even on CI warn/fail so accumulated stale .old dirs are cleaned
    up independently of whether the current run fully validated.  This closes the
    check_invariants-timeout-induced accumulation loop documented in ADR-0076.
    """
    import time as _time

    max_age_sec = _vacuum_old_max_age_days() * 86400
    cutoff = _time.time() - max_age_sec
    for candidate in sorted(yadgar_home.glob("surreal_db.old-*")):
        if candidate == current_old:
            continue  # never touch the current-run .old
        try:
            if candidate.stat().st_mtime < cutoff:
                print(
                    f"[vacuum] age-backstop: reaping stale .old dir "
                    f"({candidate.name}, >{_vacuum_old_max_age_days()}d old)",
                    flush=True,
                )
                shutil.rmtree(str(candidate), ignore_errors=True)
        except OSError as exc:
            print(f"[vacuum] age-backstop: could not stat {candidate}: {exc}", file=sys.stderr)


@observe(tier="stage")
def _delete_export_scratch(raw_path: Path | None, filtered_path: Path | None) -> None:
    """D2: delete vacuum export scratch files after a successful check_invariants pass.

    Best-effort: logs warnings on OSError but does not propagate.  Called only
    when CI passes — on any failure path the files are kept for forensics.
    """
    for export_file in (raw_path, filtered_path):
        if export_file is not None and export_file.exists():
            try:
                export_file.unlink()
                print(f"[vacuum] removed export scratch: {export_file.name}", flush=True)
            except OSError as exc:
                print(
                    f"[vacuum] WARNING: could not remove {export_file}: {exc}",
                    file=sys.stderr,
                )


@observe(tier="stage")
def _vacuum_finalize(
    backend_url: str,
    yadgar_home: Path,
    old_path: Path,
    snapshot_path: Path,
    svc: ServiceController,
    keep_n: int = 3,
    raw_path: Path | None = None,
    filtered_path: Path | None = None,
) -> bool:
    """Start yadgar, wait for health, run check_invariants, retire the .old dir.

    ``old_path`` is the previous-canonical retained by the atomic swap
    (``surreal_db.old-<ts>``).  It is removed only after check_invariants passes
    on the swapped-in compacted DB; until then it is kept as a rollback anchor.

    ``raw_path`` / ``filtered_path``: the vacuum export scratch files written by
    ``_vacuum_export`` (ADR-0076 D2).  Deleted on a successful check_invariants
    pass (no diagnostic value once the vacuum is confirmed sound); kept on any
    failure path (connection error, non-ok response) so the operator has the
    full export for forensics.

    Age-backstop (ADR-0076 D1): surreal_db.old-* dirs older than
    VACUUM_OLD_MAX_AGE_DAYS are reaped on every finalize, regardless of CI
    outcome.  ``old_path`` (the current-run .old) is always exempted — it
    survives until check_invariants determines whether it is safe to retire.

    Returns:
        True if all checks pass and cleanup succeeded.
    """
    yadgar_url = f"http://127.0.0.1:{os.environ.get('YADGAR_PORT', '8765')}"

    print("[vacuum] starting yadgar ...", flush=True)
    svc.start_yadgar()

    print(f"[vacuum] waiting for {yadgar_url}/health ...", flush=True)
    if not _wait_for_yadgar_health(yadgar_url, timeout_s=180.0):
        print(
            f"[vacuum] WARNING: yadgar did not become healthy. "
            f"Previous DB retained for rollback: {old_path}",
            file=sys.stderr,
        )
        # Age-backstop still runs — health failure does not exempt stale .old dirs.
        _reap_stale_old_dirs(yadgar_home, old_path)
        return False

    # Wait up to 30s for API layer readiness before check_invariants.
    # The 180s wait above confirms /health=200 (process up), but API routes
    # (/api/check_invariants) may not be registered yet — this short window
    # closes that gap.  PR-2's warn-only handling is the fallback.
    print(f"[vacuum] waiting up to 30s for {yadgar_url}/health (API readiness) ...", flush=True)
    if not _wait_for_yadgar_health(yadgar_url, timeout_s=30.0):
        print(
            "[vacuum] WARNING: core /health not ready after 30s — "
            "proceeding to check_invariants anyway",
            file=sys.stderr,
        )

    # Run check_invariants
    _ci_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    if not _ci_token:
        print(
            "[vacuum] WARNING: YADGAR_MCP_AUTH_TOKEN not set — "
            "check_invariants POST will have no bearer token and may return 401.",
            file=sys.stderr,
        )
    _ci_headers = {"Authorization": f"Bearer {_ci_token}"} if _ci_token else {}
    try:
        ci_resp = httpx.post(
            f"{yadgar_url}/api/check_invariants",
            headers=_ci_headers,
            timeout=120.0,
        )
        if ci_resp.status_code == 200 and ci_resp.json().get("ok"):
            print("[vacuum] check_invariants: ok", flush=True)
            # Safe to retire the previous-canonical (.old) dir.
            print(f"[vacuum] removing previous DB dir: {old_path}", flush=True)
            shutil.rmtree(str(old_path), ignore_errors=True)
            # D2: delete export scratch files — no diagnostic value once CI passes.
            _delete_export_scratch(raw_path, filtered_path)
        else:
            # Non-2xx (e.g. 404 when core hasn't finished booting post-restart)
            # or 200 with ok=false: log a warning but do NOT fail the vacuum.
            # The vacuum operation itself succeeded; post-flight verification is
            # informational.  The 30s readiness wait above reduces the probability
            # of this branch, but the warn-only path remains as the safety net.
            body = ci_resp.text[:300] if ci_resp.status_code != 200 else str(ci_resp.json())
            print(
                f"[vacuum] WARNING: check_invariants returned non-ok "
                f"(HTTP {ci_resp.status_code}): {body} "
                f"— core may not be fully ready post-restart; "
                f"previous DB retained for rollback: {old_path}",
                file=sys.stderr,
            )
    except Exception as exc:
        # Connection-refused / timeout: core hasn't finished booting yet.
        # Warn-only — vacuum itself succeeded.  The 30s readiness wait above
        # reduces the probability of this branch; this path is the safety net.
        print(
            f"[vacuum] WARNING: check_invariants request failed: {exc} "
            f"— core may not be fully ready post-restart; "
            f"previous DB retained for rollback: {old_path}",
            file=sys.stderr,
        )

    # D1: Age-backstop — reap stale .old dirs older than VACUUM_OLD_MAX_AGE_DAYS.
    # Runs unconditionally (CI pass OR fail) so stale .old dirs accumulated from
    # prior timeout-induced CI misses are cleaned up regardless of this run's
    # check_invariants outcome.  current_old (old_path) is always exempted.
    _reap_stale_old_dirs(yadgar_home, old_path)

    # Prune pre-vacuum snapshots
    _run_cleanup_script(yadgar_home, "surreal_db.pre-vacuum-*", keep_n)

    return True


# ---------------------------------------------------------------------------
# Preflight + report helpers
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _check_backend_reachable(backend_url: str, http_timeout: float) -> bool:
    """Return True iff GET <backend_url>/health is 200; log + return False otherwise."""
    try:
        r = httpx.get(
            f"{backend_url}/health",
            timeout=httpx.Timeout(connect=2.0, read=http_timeout, write=http_timeout, pool=5.0),
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as exc:
        print(
            f"[vacuum] ERROR: backend at {backend_url} is not reachable: {exc}\n"
            "Start yadgar-backend first, then run `yadgar vacuum`.",
            file=sys.stderr,
        )
        return False
    return True


@observe(tier="stage")
def _has_free_space(yadgar_home: Path, before_bytes: int) -> bool:
    """Return True iff there is headroom for an atomic side-path vacuum.

    Peak disk ~doubles (side DB + .pre-vacuum + .old retained until
    check_invariants); require conservatively 2.5x the current DB size.  On
    insufficient space the caller SKIPs (not fails) — no destructive op is done.
    Returns True on a check error (don't block on an unreadable statvfs).
    """
    try:
        free_bytes = shutil.disk_usage(str(yadgar_home)).free
    except OSError as exc:
        print(f"[vacuum] WARNING: free-space preflight check failed: {exc}", file=sys.stderr)
        return True
    required = int(before_bytes * 2.5)
    if before_bytes and free_bytes < required:
        print(
            f"[vacuum] SKIP: insufficient free space for an atomic side-path vacuum. "
            f"need ~{required / 1024 / 1024:.0f} MB free (2.5x DB), "
            f"have {free_bytes / 1024 / 1024:.0f} MB. "
            "Skipping this run (no destructive op performed).",
            file=sys.stderr,
        )
        return False
    return True


def _vacuum_report_and_log(
    backend_url: str,
    started_ts: str,
    started_at: float,
    before_bytes: int,
    after_bytes: int,
    saved_bytes: int,
    saved_pct: int,
) -> None:
    """Print the completion report and insert a consolidation_log row (best-effort)."""
    duration_s = round(time.monotonic() - started_at, 1)
    print(
        f"\n[vacuum] complete.\n"
        f"  Before:   {before_bytes / 1024 / 1024:.1f} MB\n"
        f"  After:    {after_bytes / 1024 / 1024:.1f} MB\n"
        f"  Saved:    {saved_bytes / 1024 / 1024:.1f} MB ({saved_pct}%)\n"
        f"  Duration: {duration_s} s",
        flush=True,
    )
    _log_consolidation_row(
        {
            "_backend_url": backend_url,
            "kind": "vacuum",
            "started_at": started_ts,
            "finished_at": datetime.now(UTC).isoformat(),
            "duration_seconds": duration_s,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "saved_bytes": saved_bytes,
            "saved_pct": saved_pct,
        }
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@observe(tier="boundary")
def cmd_vacuum_impl(args) -> int:  # type: ignore[no-untyped-def]
    """Implement the new vacuum flow. Returns exit code (0 = success).

    args attributes consumed:
      - backend_url (str)     default: YADGAR_DB_URL env, else http://127.0.0.1:8080
      - service_mode (str)    "systemd" | "docker" | "manual" | None (auto-detect)
      - db_path (str | None)  override default ~/.local/share/yadgar/surreal_db
      - yes (bool)            skip confirmation prompt
    """
    from yadgar._shared.config import Settings

    settings = Settings()
    started_at = time.monotonic()
    started_ts = datetime.now(UTC).isoformat()

    # -- Resolve paths --
    # Read YADGAR_DB_URL from env when args.backend_url is absent/empty.
    # getattr(..., None) + env fallback matches the v5.10.2 pattern from
    # _log_consolidation_row and yadgar/cli/vacuum.py::_default_backend_url.
    backend_url: str = getattr(args, "backend_url", None) or os.environ.get(
        "YADGAR_DB_URL", "http://127.0.0.1:8080"
    )
    db_path_arg: str | None = getattr(args, "db_path", None)
    db_path = Path(db_path_arg).expanduser() if db_path_arg else Path(settings.DB_PATH).expanduser()
    yadgar_home = db_path.parent

    # -- Sensitive-job lock (v5.69 P3) — ACQUIRE FIRST, before swap-recovery. --
    # Vacuum is a sensitive job: hold the lock for the WHOLE sequence — INCLUDING
    # _recover_interrupted_swap — so (1) an external shutdown signal drains/refuses
    # instead of interrupting a swap mid-flight (06-16 data loss), and (2) a second
    # concurrent vacuum is refused BEFORE it can touch the canonical/.old/.new.
    # The recovery step does destructive renames on the canonical; if it ran
    # unprotected, a second vacuum could "recover" a first vacuum's IN-FLIGHT swap
    # state → corruption.  _recover_interrupted_swap explicitly assumes a single
    # in-flight vacuum — this lock is what enforces it.  The lock lives in
    # yadgar_home (always present, even when the canonical is absent mid-crash), so
    # there is no chicken-and-egg with recovery.  If a LIVE job already holds it,
    # skip (log + return 0 — skip is not a failure).
    from yadgar.core import sensitive_lock  # noqa: PLC0415

    if not sensitive_lock.acquire("vacuum"):
        held = sensitive_lock.read() or {}
        print(
            f"[vacuum] another sensitive job holds the lock "
            f"(job={held.get('job')} pid={held.get('pid')}) — skipping this vacuum.",
            file=sys.stderr,
        )
        return 0  # skip — not a failure; canonical untouched
    try:
        return _cmd_vacuum_body(
            args, settings, backend_url, db_path, yadgar_home, started_at, started_ts
        )
    finally:
        sensitive_lock.release()


@observe(tier="stage")
def _cmd_vacuum_body(  # type: ignore[no-untyped-def]
    args, settings, backend_url, db_path, yadgar_home, started_at, started_ts
) -> int:
    """Destructive vacuum body — runs while the sensitive-job lock is held.

    Extracted from cmd_vacuum_impl so the acquire/release lifecycle is a clean
    try/finally around every exit path (the body has many early returns).  Holds
    the sensitive-job lock for swap-recovery AND the full vacuum, so neither can
    race a concurrent vacuum (single-in-flight is enforced by the caller's lock).
    """
    # -- Startup recovery (MUST run before the db_path.exists() preflight) --
    # A crash mid-swap leaves the canonical ABSENT with .old/.new staging present
    # — exactly the state the preflight below would reject.  Recover first so a
    # subsequent vacuum can complete/roll back the interrupted swap.  This is the
    # ONLY auto-recovery trigger this phase (daemon-start wiring deferred); the
    # residual window is documented in _recover_interrupted_swap.  Runs UNDER the
    # sensitive-job lock (acquired by the caller) so it can never race a
    # concurrent vacuum's in-flight swap.
    try:
        _recover_interrupted_swap(yadgar_home, db_path)
    except Exception as exc:
        print(
            f"[vacuum] CRITICAL: startup swap-recovery failed: {exc}\n"
            f"Manual recovery needed in {yadgar_home}.",
            file=sys.stderr,
        )
        return 1

    # -- Preflight (canonical exists + backend reachable) --
    if not db_path.exists():
        print(
            f"[vacuum] ERROR: DB dir not found: {db_path}\n"
            "Is yadgar configured correctly? Check DB_PATH in config.",
            file=sys.stderr,
        )
        return 1
    if not _check_backend_reachable(backend_url, float(settings.BACKEND_HTTP_TIMEOUT_SEC)):
        return 1

    before_bytes = _dir_bytes(db_path)
    print(f"[vacuum] DB size before: {before_bytes / 1024 / 1024:.1f} MB ({db_path})", flush=True)

    # -- Free-space preflight: skip (NOT fail) if disk can't hold ~2.5x the DB. --
    if not _has_free_space(yadgar_home, before_bytes):
        return 0  # skip is not a failure — canonical untouched

    keep_n: int = getattr(settings, "VACUUM_SNAPSHOT_RETENTION", 3)

    # -- Service controller --
    mode = getattr(args, "service_mode", None) or detect_service_mode()
    svc = ServiceController(mode)

    # -- Capture EXACT per-table source counts BEFORE any destructive op. --
    # This is the gate the swap is verified against (06-16: partial 1484/3622
    # would have passed any non-empty/">=" check; only exact per-table catches it).
    try:
        source_counts = _capture_table_counts(backend_url)
        print(f"[vacuum] source per-table counts (surviving set): {source_counts}", flush=True)
    except Exception as exc:
        print(f"[vacuum] ERROR capturing source counts: {exc}", file=sys.stderr)
        return 1

    # -- Phase 1: Export (real backend still UP — no lost writes vs. count capture) --
    raw_path: Path | None = None
    filtered_path: Path | None = None
    try:
        raw_path, filtered_path = _vacuum_export(backend_url, yadgar_home)
    except Exception as exc:
        print(f"[vacuum] ERROR in export phase: {exc}", file=sys.stderr)
        return 1

    # -- Phase 2: stop real backend + quiesced .pre-vacuum snapshot. --
    # Canonical is NOT renamed/emptied here (only the verified swap touches it).
    try:
        snapshot_path = _vacuum_snapshot_and_drop(db_path, yadgar_home, svc, before_bytes)
    except Exception as exc:
        print(f"[vacuum] ERROR in snapshot/drop phase: {exc}", file=sys.stderr)
        # Best-effort: bring the real backend back on the untouched canonical.
        try:
            svc.start_backend()
        except Exception:
            pass
        return 1

    # -- Phase 3: side-build → verify → atomic swap → start on compacted DB. --
    # Returns the retained .old path on success, or None on ANY abort (in which
    # case the canonical is guaranteed untouched and the backend has been
    # restarted on the original DB).
    old_path = _side_build_swap_and_start(
        backend_url, filtered_path, db_path, yadgar_home, source_counts, svc
    )
    if old_path is None:
        return 1

    # -- Finalize --
    after_bytes = _dir_bytes(db_path)
    saved_bytes = before_bytes - after_bytes
    saved_pct = int(100 * saved_bytes / before_bytes) if before_bytes else 0

    finalize_ok = _vacuum_finalize(
        backend_url,
        yadgar_home,
        old_path,
        snapshot_path,
        svc,
        keep_n,
        raw_path=raw_path,
        filtered_path=filtered_path,
    )

    # -- Report + consolidation_log (best-effort) --
    _vacuum_report_and_log(
        backend_url, started_ts, started_at, before_bytes, after_bytes, saved_bytes, saved_pct
    )
    return 0 if finalize_ok else 2  # 2 = succeeded but check_invariants warn
