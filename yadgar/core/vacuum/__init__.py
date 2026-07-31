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
  1. Preflight: confirm surreal_db/ exists, backend reachable, a host-side
     `surreal` binary is resolvable (Phase 3 spawns one), free space (~2.5x).
     The last two are SKIPs (exit 0, named reason) — never a destructive abort.
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
  6. Finalize: start yadgar, verify core health + post-swap inode coherence
               (both HARD gates → rollback), run check_invariants as an ADVISORY
               signal, retire .old, prune snapshots.
  7. Report: measure the canonical AFTER finalize, log before/after bytes,
             duration, insert consolidation_log row.  A rolled-back run reports
             saved_bytes=0 — never the pre-rollback figure (task:0045).

_restore_db is now a THIN FALLBACK (only if the post-swap backend won't come up).

Public entry point:
    cmd_vacuum_impl(args) -> int   (0 = success, non-zero = failure)

The top-level cmd_vacuum(args) in __main__.py delegates here.
"""

from __future__ import annotations

import os
import re
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

# Core route this module POSTs during finalize.  Kept as a named constant so the
# route-existence guard (yadgar/tests/core/test_vacuum_finalize_verification.py)
# can assert it against the daemon's REAL registered route table.  This URL was
# written here and served NOWHERE for months (task:0045) — it is now registered
# in yadgar/core/server/routes/admin_ops.py.
_CHECK_INVARIANTS_PATH = "/api/check_invariants"

# Car 0046: number of PRIOR vacuum_export_* pairs (raw + filtered) to retain as
# a backstop against unbounded accumulation.  ADR-0076 D2 deletes the CURRENT
# run's own pair on a retained swap but intentionally KEEPS it on any abort
# (forensics) — that retention had no ceiling, and 1.4 GB of scratch built up
# on the workstation before a manual sweep.  2 prior pairs (4 files) is enough
# to diagnose a fix-in-progress (this run's failure plus the one before it)
# without keeping every historical failure forever; older pairs carry no
# additional diagnostic value once the two most recent are on hand.
_VACUUM_EXPORT_KEEP_RUNS = 2

# Car 0092: named SKIP reasons.  A skip is exit 0 and reclaims nothing, so the
# reason is the only thing telling an operator what to do about it — a missing
# binary is a broken install that will never self-heal, low disk is transient.
# They must not look identical in stderr or in the consolidation_log row.
_SKIP_NO_SURREAL = "no_surreal_binary"
_SKIP_LOW_DISK = "low_disk"

# Hard bound on the advisory `surreal version` probe — this runs inside the
# nightly unit and must never be able to hang it.
_SURREAL_VERSION_TIMEOUT_SEC = 10.0

# Resolved-binary -> version string.  The probe is a subprocess spawn; a vacuum
# run only ever resolves one binary, and caching keeps repeated in-process runs
# (the test suite) from re-spawning it.
_SURREAL_VERSION_CACHE: dict[str, str] = {}


@observe(tier="stage")
def _reap_stale_pre_vacuum_snapshots(yadgar_home: Path, keep_n: int) -> None:
    """Car 0092: bound ``surreal_db.pre-vacuum-*`` accumulation on EVERY exit path.

    This prune used to live ONLY in ``_vacuum_finalize`` — which no abort path
    reaches (``_cmd_vacuum_body`` returns 1 first).  So each failed night left
    one full-size DB copy on disk forever, until ``_has_free_space`` started
    returning False, and that is a ``return 0`` SKIP rather than a failure: the
    accumulated snapshots silently converted vacuum into a permanent no-op with
    a green timer.  Pruning keeps the ``keep_n`` MOST RECENT, so the aborting
    run's own snapshot always survives for forensics.
    """
    _run_cleanup_script(yadgar_home, "surreal_db.pre-vacuum-*", keep_n)


@observe(tier="stage")
def _reap_stale_export_scratch(yadgar_home: Path) -> None:
    """Car 0046: bound vacuum_export_* accumulation to _VACUUM_EXPORT_KEEP_RUNS
    prior pairs.  Called on every ``_vacuum_finalize`` exit path — the previous
    "kept forever on abort" retention (ADR-0076 D2) had no ceiling.
    """
    _run_cleanup_script(yadgar_home, "vacuum_export_*", _VACUUM_EXPORT_KEEP_RUNS * 2)


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
def _assert_backend_quiesced(backend_url: str) -> bool:
    """True iff NOTHING answers GET <backend_url>/health — the store is quiesced.

    P0 #37 item 6 (the RCA §4 root defect): ``svc.stop()`` runs in Phase 2,
    but the swap happens minutes later (export write-out, snapshot copytree,
    and the side-build /import all sit in between). Nothing re-verified the
    backend was still DOWN at swap time, so any external (re)start in that
    window — a nix deploy, a manual ``systemctl start``, systemd recovery —
    re-opens the ORIGINAL canonical, and the swap then renames the dir under
    a live store: the live inode becomes ``.old`` while the path holds a
    stale decoy (the 07-09 16 h split-brain). The swap must verify quiescence
    or ABORT with the canonical untouched.

    Any HTTP answer (even 5xx) means SOMETHING holds the port → not quiesced.
    """
    try:
        r = httpx.get(f"{backend_url}/health", timeout=2.0)
    except Exception:
        return True  # connection refused / timeout → nothing serving → quiesced
    print(
        f"[vacuum] ERROR: backend at {backend_url} is LIVE (HTTP {r.status_code}) at swap "
        "time — an external restart re-opened the canonical during the side-build window.",
        file=sys.stderr,
    )
    return False


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

    The statement enumerates its fields, so a key added to ``row`` and NOT added
    here is silently dropped — the whole /sql call still returns 200.  That is a
    quiet-failure trap: the tests that assert on the row dict all patch this
    function, so they would never notice.  ``rolled_back`` / ``exit_code``
    (task:0045) are therefore inlined as SurrealQL LITERALS rather than bound
    params: ``params=`` values cross the wire as query strings, and
    ``<bool> "false"`` is not reliably ``false`` across SurrealDB versions — a
    rolled-back run recorded as ``rolled_back: true`` would be a new lie in the
    place the old one lived.  Both values are code-controlled (a bool and a small
    int, coerced here), so there is no injection surface.

    ``skip_reason`` (Car 0092) IS a bound param — it is a string, so the
    ``params=``-crosses-as-string caveat above does not apply to it.  It and its
    companion ``skipped: true`` literal are appended to the statement only when
    the row describes a SKIP, so every non-skip row is unchanged.
    """
    backend_url = row.pop(
        "_backend_url",
        os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8000"),
    )
    rolled_back = bool(row.pop("rolled_back", False))
    exit_code = int(row.pop("exit_code", 0))
    # Car 0092: a SKIPped run reclaims nothing and must say WHY.  ``skip_reason``
    # is a string, so unlike ``rolled_back``/``exit_code`` it crosses the wire
    # safely as a bound param.  The fields are emitted ONLY on a skip, so a
    # normal vacuum row is byte-identical to what it was before this change.
    skip_reason = row.get("skip_reason")
    skip_fields = ",skipped: true,skip_reason: $skip_reason" if skip_reason else ""
    if not skip_reason:
        row.pop("skip_reason", None)
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
            "saved_pct: $saved_pct,"
            f"rolled_back: {'true' if rolled_back else 'false'},"
            f"exit_code: {exit_code}"
            f"{skip_fields}"
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
def _restart_services_after_abort(svc: ServiceController, *, backend: bool = True) -> None:
    """Bring BOTH units back after a vacuum abort — backend first, then core.

    task:0027a.  Phase 2's ``svc.stop()`` stops ``yadgar`` AND ``yadgar-backend``
    (``ops.ServiceController.SERVICES``), but every abort path between that stop
    and finalize used to restart only the backend — and the quiescence-gate abort
    restarted nothing at all.  ``systemctl --user stop`` is an EXPLICIT stop, so
    ``Restart=on-failure``/``always`` will not bring core back: any of those paths
    left the memory engine down until a human noticed.

    Each start gets its OWN try/except deliberately.  Sharing one would mean a
    raising ``start_backend()`` (masked unit, docker daemon gone, manual mode)
    skips the core start — i.e. the exact failure this function exists to
    prevent.  Both failures are logged and swallowed; the caller is already on an
    error path and returns non-zero.

    No health wait here: an abort must stay fast.  ``start_yadgar()`` is
    idempotent under systemd/docker (starting a running unit is a no-op), so the
    finalize path's own ``start_yadgar()`` is unaffected.

    ``ManualModeError`` is not a failure: ``--service-mode=manual`` prints the
    commands and raises by contract, and it is the one mode where a human is
    reading the output — a CRITICAL "core is DOWN" there would be a false alarm.

    Args:
        svc: the service controller.
        backend: start the backend too.  False when the caller already did it
            (``_restore_db`` starts the backend on the restored DB itself).
    """
    from yadgar.core.ops import ManualModeError  # noqa: PLC0415

    if backend:
        try:
            svc.start_backend()
        except ManualModeError:
            pass  # instructions already printed by the controller
        except Exception as exc:
            print(
                f"[vacuum] WARNING: could not restart yadgar-backend after abort: {exc}",
                file=sys.stderr,
            )
    try:
        svc.start_yadgar()
    except ManualModeError:
        pass  # instructions already printed by the controller
    except Exception as exc:
        print(
            f"[vacuum] CRITICAL: could not restart yadgar CORE after abort: {exc}\n"
            "[vacuum] The memory engine is DOWN — run `systemctl --user start yadgar`.",
            file=sys.stderr,
        )


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
    # task:0027a — core must be running whichever way the restore went.  One
    # caller reaches here with core still stopped (the post-swap backend-health
    # fallback, before finalize ever starts it); the other with core already up
    # (the finalize rollback).  start_yadgar() is idempotent, so an unconditional
    # call is correct for both and costs nothing.  Outside the try above on
    # purpose: a failed rename must not also cost us the core restart.
    _restart_services_after_abort(svc, backend=False)


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
        # Backend AND core — Phase 2 stopped both (task:0027a).
        _restart_services_after_abort(svc)

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

    # 3b-pre: QUIESCENCE GATE (P0 #37 item 6). svc.stop() ran minutes ago in
    # Phase 2; an external restart in the side-build window would have
    # re-opened the ORIGINAL canonical. Renaming under a live store is exactly
    # the 07-09 path/inode split-brain — verify quiescence or ABORT.
    if not _assert_backend_quiesced(backend_url):
        shutil.rmtree(str(side_path), ignore_errors=True)
        print(
            "[vacuum] ABORT: the real backend is LIVE at swap time — refusing to rename "
            "the canonical under an open store (07-09 split-brain guard). "
            "Canonical untouched; the running backend stays on the original DB.",
            file=sys.stderr,
        )
        # task:0027a — this path used to restart NOTHING.  The gate can fire with
        # the backend externally revived while CORE is still stopped from Phase 2,
        # so both starts are needed (start_backend is a no-op when it is already
        # up, which is exactly the case that trips this gate).
        _restart_services_after_abort(svc)
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


# ---------------------------------------------------------------------------
# P0 #37 item 5a — live-store inode-coherence check (host-side /proc fd scan)
# ---------------------------------------------------------------------------

#: Matches the surreal_db dir-name variant inside an fd link target. Container
#: fd targets are container-ns paths (/data/surreal_db/...), host processes use
#: host paths — either way the DIR NAME identifies canonical vs staging.
_STORE_DIR_RE = re.compile(
    r"/(surreal_db(?:\.(?:old|new|building|pre-vacuum|CORRUPT)-[^/]+)?)(?:/|$)"
)


@observe(span=False)
def _fd_store_dir_names(fd_dir: Path) -> set[str]:
    """surreal_db* dir-name variants referenced by the fd links under *fd_dir*."""
    names: set[str] = set()
    try:
        fds = list(fd_dir.iterdir())
    except OSError:
        return names
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        m = _STORE_DIR_RE.search(target)
        if m:
            names.add(m.group(1))
    return names


@observe(tier="stage")
def _surreal_open_dir_names(proc_root: Path = Path("/proc")) -> set[str]:
    """Scan *proc_root* for live ``surreal start`` processes; return the set of
    surreal_db* DIR-name variants their open fds resolve into.

    Works for the rootless-podman backend: its processes are user-owned, so
    ``/proc/<pid>/fd`` is readable from the host, and the fd link targets show
    the container-ns path (``/data/surreal_db.old-*/...``) whose dir name
    still identifies canonical vs staging. Unreadable pids are skipped.
    """
    names: set[str] = set()
    for pid_dir in proc_root.glob("[0-9]*"):
        try:
            argv = (pid_dir / "cmdline").read_bytes().split(b"\x00")
        except OSError:
            continue
        if not argv or b"surreal" not in argv[0] or b"start" not in argv:
            continue
        names |= _fd_store_dir_names(pid_dir / "fd")
    return names


@observe(tier="stage")
def _verify_live_store_coherence(proc_root: Path = Path("/proc")) -> tuple[bool, set[str]]:
    """Post-swap invariant (P0 #37 item 5a): the store a live surreal has OPEN
    must be the CANONICAL ``surreal_db`` path — never a ``.old``/staging dir.

    The 07-09 incident was exactly this state persisting silently for 16 h:
    the live inode sat at ``surreal_db.old-*`` while the canonical path held a
    stale decoy. Returns ``(coherent, dir_names)``; no scannable surreal at
    all → coherent (absence must not false-alarm — the in-container guard loop
    covers the long tail).
    """
    names = _surreal_open_dir_names(proc_root)
    bad = {n for n in names if n != "surreal_db"}
    return (not bad, bad if bad else names)


@observe(tier="stage")
def _rollback_swap_on_finalize_failure(
    reason: str,
    old_path: Path,
    db_path: Path,
    svc: ServiceController,
    backend_url: str,
) -> None:
    """P0 #37 item 3: NEVER retain a half-swapped state — roll the swap back.

    The 07-09 incident: the ``.old`` was merely RETAINED while the running
    backend kept writing the original inode (= ``.old``) for 16 h — path/inode
    split-brain.  On a HARD finalize-gate failure the swap is therefore ROLLED
    BACK so the path and the live inode re-converge: the compacted canonical is
    discarded (the ``.pre-vacuum`` snapshot and the re-promoted original keep the
    data safe) and ``.old`` is promoted back to canonical.

    Callers (task:0045 D2): core-health timeout and post-swap inode incoherence
    ONLY.  A non-ok ``check_invariants`` no longer reaches here — that call was
    404ing against a route that did not exist, and even served correctly it
    answers a question about global data-model consistency rather than about
    this swap.  See ``_vacuum_finalize`` for the full rationale.
    """
    print(
        f"[vacuum] CRITICAL: {reason}\n"
        f"[vacuum] ROLLING BACK the swap: discarding the unverified compacted canonical and "
        f"promoting {old_path.name} back to {db_path.name} so the canonical path and the live "
        "store re-converge (07-09 split-brain guard — never retain a half-swapped state).",
        file=sys.stderr,
    )
    _restore_db(old_path, db_path, svc, backend_url)


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
def _check_invariants_verified(yadgar_url: str) -> tuple[bool, str]:
    """POST the core check_invariants route; return (ok, detail).

    ADVISORY ONLY, and only in the vacuum finalize path (task:0045 D2) — the
    caller LOGS a non-ok result and proceeds; it no longer rolls the swap back.
    ``check_invariants`` remains a HARD signal everywhere else: the nightly
    consolidation tail still logs CRITICAL on violations
    (``yadgar/core/consolidation/orchestrator.py``).

    ANY non-ok outcome — non-2xx, 200 with ok=false, or a connection error —
    returns False plus a detail string naming what came back, including the
    ``violations`` list when the response carried one (the operator needs to
    know WHICH invariant failed, not merely that one did).
    """
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
            f"{yadgar_url}{_CHECK_INVARIANTS_PATH}",
            headers=_ci_headers,
            timeout=120.0,
        )
    except Exception as exc:
        return False, f"check_invariants request failed: {exc}"
    if ci_resp.status_code != 200:
        return False, (
            f"check_invariants returned HTTP {ci_resp.status_code}: {ci_resp.text[:300]}"
        )
    try:
        payload = ci_resp.json()
    except Exception as exc:
        return False, f"check_invariants returned unparseable body: {exc}"
    if payload.get("ok"):
        return True, "ok"
    return False, (
        f"check_invariants ok=false; violations={payload.get('violations')} "
        f"timeouts={payload.get('timeouts')}"
    )


@observe(tier="stage")
def _vacuum_finalize(  # noqa: PLR0913 — established finalize signature + db_path rollback target (P0 #37)
    backend_url: str,
    yadgar_home: Path,
    old_path: Path,
    snapshot_path: Path,
    svc: ServiceController,
    keep_n: int = 3,
    raw_path: Path | None = None,
    filtered_path: Path | None = None,
    db_path: Path | None = None,
) -> bool:
    """Start yadgar, gate the swapped-in DB, retire the .old dir — or ROLL BACK.

    ``old_path`` is the previous-canonical retained by the atomic swap
    (``surreal_db.old-<ts>``).

    TWO HARD GATES roll the swap back (P0 #37, unchanged): core does not become
    healthy on the swapped-in DB, or the post-swap inode-coherence check finds a
    live surreal holding open fds outside the canonical path.  On either, ``.old``
    is promoted back to canonical and the unverified compacted DB is discarded,
    rather than retaining a half-swapped state (the 07-09 silent split-brain).
    The vacuum then exits 2 so the nightly unit goes red.

    ``check_invariants`` is ADVISORY here and ONLY here (task:0045 D2).  It ran
    against a route that did not exist until this change, so it 404'd on every
    run and rolled back every vacuum for a month — but making the call real is
    not enough on its own: it legitimately returns ok=false on a healthy host
    when the data model carries a pre-existing violation a vacuum neither causes
    nor fixes, which would leave the gate permanently unsatisfiable.  The swap is
    NOT unguarded without it: the EXACT per-table count comparison already ran
    PRE-swap in ``_build_and_verify_side_db`` (a partial import can never be
    swapped in), and inode coherence runs POST-swap above.  ``check_invariants``
    was an additional — and never-functioning — third gate answering a different
    question (global data-model self-consistency).  A non-ok result is logged
    loudly, naming which invariants failed, and the run proceeds.

    Scope note: this narrowing applies to the vacuum finalize path alone.
    ``check_invariants`` stays a HARD signal elsewhere — the consolidation tail
    (``yadgar/core/consolidation/orchestrator.py``) still logs CRITICAL on
    violations, unchanged.

    ``raw_path`` / ``filtered_path``: the vacuum export scratch files written by
    ``_vacuum_export`` (ADR-0076 D2).  This run's own pair is deleted whenever
    the swap is RETAINED; kept on the rollback paths for forensics, but bounded
    to ``_VACUUM_EXPORT_KEEP_RUNS`` PRIOR pairs on every outcome (Car 0046) —
    the old unbounded "kept on failure" retention accumulated 1.4 GB.

    ``db_path``: the canonical DB path (rollback target). Defaults to
    ``yadgar_home / "surreal_db"``.

    Age-backstop (ADR-0076 D1): surreal_db.old-* dirs older than
    VACUUM_OLD_MAX_AGE_DAYS are reaped on every finalize, regardless of
    outcome.  ``old_path`` (the current-run .old) is always exempted.

    Returns:
        True when the swap was RETAINED (with or without an advisory
        check_invariants failure); False when a hard gate rolled it back.
    """
    yadgar_url = f"http://127.0.0.1:{os.environ.get('YADGAR_PORT', '8765')}"
    if db_path is None:
        db_path = yadgar_home / "surreal_db"

    print("[vacuum] starting yadgar ...", flush=True)
    svc.start_yadgar()

    print(f"[vacuum] waiting for {yadgar_url}/health ...", flush=True)
    if not _wait_for_yadgar_health(yadgar_url, timeout_s=180.0):
        _rollback_swap_on_finalize_failure(
            "yadgar core did not become healthy on the swapped-in DB (180s)",
            old_path,
            db_path,
            svc,
            backend_url,
        )
        # Age-backstop still runs — failure does not exempt stale .old dirs.
        _reap_stale_old_dirs(yadgar_home, old_path)
        _reap_stale_export_scratch(yadgar_home)
        return False

    # Wait up to 30s for API layer readiness before check_invariants.
    # The 180s wait above confirms /health=200 (process up); this short window
    # gives the API layer a moment to settle before the advisory call.  The
    # routes themselves ARE registered — they are import-time side effects in
    # yadgar/core/server/__init__.py, so /health and /api/* come up together.
    print(f"[vacuum] waiting up to 30s for {yadgar_url}/health (API readiness) ...", flush=True)
    if not _wait_for_yadgar_health(yadgar_url, timeout_s=30.0):
        print(
            "[vacuum] WARNING: core /health not ready after 30s — "
            "proceeding to check_invariants anyway",
            file=sys.stderr,
        )

    # P0 #37 item 5a: post-swap inode-coherence invariant — the store the live
    # surreal has OPEN must be the canonical path, never .old/staging.
    coherent, store_names = _verify_live_store_coherence()
    if not coherent:
        _rollback_swap_on_finalize_failure(
            f"store inode SPLIT-BRAIN detected post-swap — a live surreal holds open fds in "
            f"{sorted(store_names)} instead of the canonical surreal_db",
            old_path,
            db_path,
            svc,
            backend_url,
        )
        _reap_stale_old_dirs(yadgar_home, old_path)
        _reap_stale_export_scratch(yadgar_home)
        return False

    # ADVISORY gate (task:0045 D2) — logged, never a rollback trigger.  See the
    # docstring for why the swap is still guarded without it.
    ci_ok, detail = _check_invariants_verified(yadgar_url)
    if ci_ok:
        print("[vacuum] check_invariants: ok", flush=True)
    else:
        print(
            f"[vacuum] ADVISORY: check_invariants did not pass on the swapped-in DB: "
            f"{detail}\n[vacuum] The swap is KEPT anyway — check_invariants is advisory "
            "in the vacuum finalize path: the EXACT per-table count verification ran "
            "pre-swap and post-swap inode coherence passed above, so the compaction "
            "itself is verified.  A standing data-model violation is a separate "
            "problem that a vacuum neither causes nor fixes — investigate it, but do "
            "not expect a vacuum to clear it.",
            file=sys.stderr,
        )

    # The swap is RETAINED — both hard gates passed.  Retire the previous
    # canonical (this is the reclaim: .old holds the entire pre-vacuum DB) and
    # drop the export scratch.  The .pre-vacuum snapshot remains the last-resort
    # recovery anchor (pruned by keep_n below).
    print(f"[vacuum] removing previous DB dir: {old_path}", flush=True)
    shutil.rmtree(str(old_path), ignore_errors=True)
    _delete_export_scratch(raw_path, filtered_path)
    _reap_stale_export_scratch(yadgar_home)  # Car 0046: bound any older leaked pairs

    # D1: Age-backstop — reap stale .old dirs older than VACUUM_OLD_MAX_AGE_DAYS.
    # Runs unconditionally (retained OR rolled back).  current_old exempted.
    _reap_stale_old_dirs(yadgar_home, old_path)

    # Prune pre-vacuum snapshots (Car 0092: the abort paths now do this too).
    _reap_stale_pre_vacuum_snapshots(yadgar_home, keep_n)

    # The swap was RETAINED — the return tracks retention, NOT check_invariants.
    # Returning ci_ok here would re-arm the exact bug this change removes: a
    # sound, retained compaction reported as a failure (exit 2, saved_bytes=0)
    # because of an unrelated standing data-model violation.
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
def _surreal_version(binary: str) -> str:
    """Best-effort ``<binary> version`` string.  Never raises, never gates.

    Recorded in the preflight log line only.  Two ``surreal`` binaries commonly
    coexist on one host (a nix profile one that wins PATH and a shadowed
    ``~/.local/bin`` one), and the side build must use a version that can write a
    store the real backend can then open — so a run needs to say WHICH binary and
    WHICH version it used.  Enforcing a version match against the backend image
    is a separate design decision and is deliberately NOT made here.

    Bounded by a hard subprocess timeout: this runs inside the nightly unit, and
    a ``surreal version`` that hangs must not hang the vacuum.
    """
    cached = _SURREAL_VERSION_CACHE.get(binary)
    if cached is not None:
        return cached
    version = "unknown"
    try:
        import subprocess  # noqa: PLC0415 — version probe only; not a runtime dep

        proc = subprocess.run(  # noqa: S603 — binary resolved by shutil.which, no shell
            [binary, "version"],
            capture_output=True,
            text=True,
            timeout=_SURREAL_VERSION_TIMEOUT_SEC,
            check=False,
        )
        lines = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        if lines and lines[0].strip():
            version = lines[0].strip()
    except Exception as exc:  # noqa: BLE001 — advisory log field; never gates
        print(f"[vacuum] WARNING: could not read `surreal version`: {exc}", file=sys.stderr)
    _SURREAL_VERSION_CACHE[binary] = version
    return version


@observe(tier="stage")
def _has_surreal_binary() -> bool:
    """Return True iff a host-side ``surreal`` binary is resolvable on PATH.

    Car 0092.  Phase 3's side build spawns a THROWAWAY ``surreal start``
    host-side (``yadgar.core._surreal_runner.spawn_surreal`` — a bare
    PATH-resolved ``subprocess.Popen(["surreal", ...])``).  On a container
    install that binary exists only inside the ``yadgar-backend`` image, so the
    spawn raises ``FileNotFoundError`` — which ``_build_and_verify_side_db``
    swallows in its broad ``except Exception`` and turns into a plain abort.

    Without this preflight that abort landed at the WORST possible moment: after
    the full ``/export``, after BOTH units were stopped, and after the full-size
    ``.pre-vacuum`` ``copytree``.  Worse, it WEDGED: the abort path never reached
    ``_vacuum_finalize``, so nothing pruned the snapshot it had just made, and
    each failed night parked another full-size DB copy on disk until
    ``_has_free_space`` began returning False — a ``return 0`` SKIP.  End state:
    a permanent silent no-op reporting exit 0 with a green timer.

    Asking the question BEFORE any destructive step turns that into a clean,
    loud, NAMED skip.  This is an EXISTENCE check only — a version-compatibility
    gate belongs with the full fix (running the side build in a one-shot backend
    container), not here.

    On failure the caller SKIPs (exit 0, ``_SKIP_NO_SURREAL``) — modelled on the
    ``_has_free_space`` skip path, but distinguishable from it: a missing binary
    is a broken install that will never self-heal, low disk is transient.
    """
    binary = shutil.which("surreal")
    if binary is None:
        print(
            "[vacuum] SKIP: no `surreal` binary on PATH — the Phase 3 side-path "
            "build spawns a throwaway `surreal start` HOST-side, and on a "
            "container install that binary exists only inside the yadgar-backend "
            "image. Skipping this run (no destructive op performed: no export, "
            "no service stop, no snapshot). Install surreal on the host PATH, or "
            "run vacuum where the binary is available.",
            file=sys.stderr,
        )
        return False
    print(
        f"[vacuum] preflight: side-build binary {binary} ({_surreal_version(binary)})",
        flush=True,
    )
    return True


@observe(tier="stage")
def _log_vacuum_skip(  # noqa: PLR0913 — mirrors _vacuum_report_and_log's field row
    backend_url: str,
    started_ts: str,
    started_at: float,
    before_bytes: int,
    skip_reason: str,
    detail: str,
) -> None:
    """Report a SKIPped run loudly and record it with a NAMED reason.

    Car 0092.  A skip is exit 0 and reclaims nothing.  Before this, the only skip
    (low disk) wrote NO ``consolidation_log`` row at all, so a skipped night was
    indistinguishable in telemetry from a night the unit never ran — and once a
    second skip reason existed, the two would have been indistinguishable from
    each other.  "cannot vacuum, no surreal binary" (broken install, will never
    self-heal) and "cannot vacuum, low disk" (transient) demand different
    operator responses, so they get different named reasons in both places an
    operator looks: stderr and the row.
    """
    duration_s = round(time.monotonic() - started_at, 1)
    print(
        f"\n[vacuum] SKIPPED ({skip_reason}) — nothing reclaimed, canonical untouched.\n"
        f"  Reason:   {detail}\n"
        f"  DB size:  {before_bytes / 1024 / 1024:.1f} MB (unchanged)\n"
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
            "after_bytes": before_bytes,
            "saved_bytes": 0,
            "saved_pct": 0,
            "rolled_back": False,
            "exit_code": 0,
            "skip_reason": skip_reason,
        }
    )


@observe(tier="stage")
def _preflight_skip_reason(yadgar_home: Path, before_bytes: int) -> tuple[str, str] | None:
    """Return ``(skip_reason, detail)`` when a NON-destructive preflight says do
    not vacuum this run, else ``None``.

    Car 0092.  Both checks answer "is a side-path vacuum possible AT ALL",
    neither touches the canonical, and both are SKIPs (exit 0) rather than
    failures — so they belong together, and they must run BEFORE the export /
    service stop / full-size copytree.  Grouping them here also keeps the two
    named reasons adjacent, which is the point: a missing binary is a broken
    install that will never self-heal, low disk is transient, and an operator
    reading either the log or the ``consolidation_log`` row has to be able to
    tell them apart.
    """
    if not _has_surreal_binary():
        return (
            _SKIP_NO_SURREAL,
            "no `surreal` binary on PATH — the Phase 3 side build cannot be spawned",
        )
    if not _has_free_space(yadgar_home, before_bytes):
        return (
            _SKIP_LOW_DISK,
            f"insufficient free space — need ~{int(before_bytes * 2.5) / 1024 / 1024:.0f} MB "
            "(2.5x the DB) for an atomic side-path vacuum",
        )
    return None


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


@observe(tier="stage")
def _vacuum_report_and_log(  # noqa: PLR0913 — one row of report fields, no cohesive sub-struct
    backend_url: str,
    started_ts: str,
    started_at: float,
    before_bytes: int,
    after_bytes: int,
    rolled_back: bool,
    exit_code: int,
) -> None:
    """Print the completion report and insert a consolidation_log row (best-effort).

    ``rolled_back`` is load-bearing, not decoration (task:0045).  This function
    used to print "complete … Saved: N MB" unconditionally, from an
    ``after_bytes`` measured BEFORE finalize — so a run that swapped in a
    compacted DB and then rolled the whole thing back one minute later still
    reported a ~2 GB saving, and wrote that number into ``consolidation_log``.
    Seven consecutive rolled-back nightlies read as successes.  A rolled-back run
    reclaimed nothing and must say so.

    The saving is DERIVED here rather than passed in, so no caller can report a
    positive saving for a rolled-back run: on rollback it is hard-zeroed, not
    recomputed.  Re-measuring alone is not sufficient — the restored original is
    reopened and written to, so ``before - after`` is a small non-zero number on
    that path.

    Telemetry note: every ``consolidation_log`` vacuum row written before this
    change carries the fabricated pre-rollback figures.  Any baseline must be cut
    from post-fix rows only.
    """
    if rolled_back:
        saved_bytes = 0
        saved_pct = 0
    else:
        saved_bytes = before_bytes - after_bytes
        saved_pct = int(100 * saved_bytes / before_bytes) if before_bytes else 0
    duration_s = round(time.monotonic() - started_at, 1)
    headline = "ROLLED BACK — nothing reclaimed." if rolled_back else "complete."
    print(
        f"\n[vacuum] {headline}\n"
        f"  Before:   {before_bytes / 1024 / 1024:.1f} MB\n"
        f"  After:    {after_bytes / 1024 / 1024:.1f} MB\n"
        f"  Saved:    {saved_bytes / 1024 / 1024:.1f} MB ({saved_pct}%)\n"
        f"  Duration: {duration_s} s",
        flush=True,
    )
    if rolled_back:
        print(
            f"[vacuum] CRITICAL: the swap was rolled back — {before_bytes / 1024 / 1024:.1f} MB "
            "was NOT reclaimed and the canonical is the original (pre-vacuum) DB. "
            f"exit={exit_code}.",
            file=sys.stderr,
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
            "rolled_back": rolled_back,
            "exit_code": exit_code,
        }
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@observe(tier="boundary")
def cmd_vacuum_impl(args) -> int:  # type: ignore[no-untyped-def]
    """Implement the new vacuum flow. Returns exit code (0 = success).

    args attributes consumed:
      - backend_url (str)     default: YADGAR_DB_URL env, else http://127.0.0.1:8000
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
        "YADGAR_DB_URL", "http://127.0.0.1:8000"
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
    from yadgar.core.sensitive_lock import sensitive_lock  # noqa: PLC0415

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

    keep_n: int = getattr(settings, "VACUUM_SNAPSHOT_RETENTION", 3)

    # -- Non-destructive preflights (Car 0092): surreal binary, then free space. --
    # MUST run BEFORE the export / service stop / full-size copytree below.
    # Phase 3 spawns a throwaway `surreal start` host-side, and on a container
    # install that binary lives only inside the backend image; asked late (i.e.
    # not at all, pre-Car-0092) the FileNotFoundError surfaced only after both
    # units were stopped and a full DB copy had been made — and the abort path
    # never pruned that copy, wedging vacuum permanently.  Placed AFTER the
    # reachability check on purpose: the skip row goes to the backend over HTTP,
    # so a skip logged with the backend down would be half-silent.
    skip = _preflight_skip_reason(yadgar_home, before_bytes)
    if skip is not None:
        # A wedged host reaches the low-disk branch BECAUSE stale .pre-vacuum-*
        # dirs ate the headroom; prune before skipping so a later run can proceed.
        _reap_stale_pre_vacuum_snapshots(yadgar_home, keep_n)
        _log_vacuum_skip(backend_url, started_ts, started_at, before_bytes, *skip)
        return 0  # skip is not a failure — canonical untouched

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
        # Best-effort: bring BOTH units back on the untouched canonical.
        # svc.stop() may already have run inside _vacuum_snapshot_and_drop, so
        # core can be down here too (task:0027a).
        _restart_services_after_abort(svc)
        # Car 0092: a partial copytree may have left a .pre-vacuum dir behind;
        # finalize (the only pre-0092 prune site) is unreachable from here.
        _reap_stale_pre_vacuum_snapshots(yadgar_home, keep_n)
        return 1

    # -- Phase 3: side-build → verify → atomic swap → start on compacted DB. --
    # Returns the retained .old path on success, or None on ANY abort (in which
    # case the canonical is guaranteed untouched and the backend has been
    # restarted on the original DB).
    old_path = _side_build_swap_and_start(
        backend_url, filtered_path, db_path, yadgar_home, source_counts, svc
    )
    if old_path is None:
        # Car 0092: the ONLY .pre-vacuum prune used to live in _vacuum_finalize,
        # which this return never reaches — so every aborted night parked another
        # full-size DB copy on disk.  This run's own snapshot is the newest and
        # survives the keep_n prune (forensics); older ones go.
        _reap_stale_pre_vacuum_snapshots(yadgar_home, keep_n)
        return 1

    # -- Finalize --
    finalize_ok = _vacuum_finalize(
        backend_url,
        yadgar_home,
        old_path,
        snapshot_path,
        svc,
        keep_n,
        raw_path=raw_path,
        filtered_path=filtered_path,
        db_path=db_path,
    )

    # -- Report + consolidation_log (best-effort) --
    # Measure AFTER finalize (task:0045).  The old call site measured before it,
    # so a rolled-back run reported the compacted size it had already discarded.
    # The saving itself is derived inside _vacuum_report_and_log, which
    # hard-zeroes it on the rollback path.
    rolled_back = not finalize_ok
    after_bytes = _dir_bytes(db_path)

    # 2 = swap ROLLED BACK (a hard finalize gate failed) — data safe on the
    # original DB, compaction discarded; the nightly unit goes red (#37).
    exit_code = 2 if rolled_back else 0
    _vacuum_report_and_log(
        backend_url,
        started_ts,
        started_at,
        before_bytes,
        after_bytes,
        rolled_back,
        exit_code,
    )
    return exit_code
