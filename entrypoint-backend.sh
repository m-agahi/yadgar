#!/bin/bash
# Backend container entrypoint: SurrealDB + embedding service.
#
# NOTE: no in-container backup loop. The previous `GET /export` cron triggered
# a stack overflow in a `surrealdb-worker` thread (the export's recursive value
# serializer blows the default ~2 MiB tokio stack on this dataset), aborting the
# whole process and putting the container in a restart loop. DB snapshots are
# handled outside the container by the systemd ExecStartPre `cp -r` of the
# surrealkv data dir. If a logical (.surql) backup is needed again, run
# `surreal export` from a one-off client against a *quiesced* DB, not on a hot
# server, and only after the upstream export-recursion issue is resolved.
set -e

# ---------------------------------------------------------------------------
# v5.6.7 PR-M: resolve log directory (YADGAR_LOG_DIR env knob)
# Default inside containers: /data/logs (bind-mounted by compose or systemd).
# Operators on Linux hosts can override to e.g. /var/log/yadgar for Alloy access.
# ---------------------------------------------------------------------------
YADGAR_LOG_DIR="${YADGAR_LOG_DIR:-/data/logs}"
export YADGAR_LOG_DIR
echo "yadgar-backend: log dir = ${YADGAR_LOG_DIR}" >&2
if ! mkdir -p "${YADGAR_LOG_DIR}" && chmod 0750 "${YADGAR_LOG_DIR}" 2>/dev/null; then
    echo "WARNING: could not create ${YADGAR_LOG_DIR}; falling back to /tmp/yadgar-logs" >&2
    YADGAR_LOG_DIR="/tmp/yadgar-logs"
    export YADGAR_LOG_DIR
    mkdir -p "${YADGAR_LOG_DIR}" || true
fi

# Fail fast if required credentials are missing.
# Use YADGAR_ALLOW_ROOT=1 in test/dev environments to bypass.
if [[ "${YADGAR_ALLOW_ROOT:-0}" != "1" ]]; then
    : "${SURREAL_USER:?SURREAL_USER is required — set via EnvironmentFile or docker -e}"
    : "${SURREAL_PASS:?SURREAL_PASS is required — set via EnvironmentFile or docker -e}"
fi

# Log level configuration — shared across SurrealDB and the embed service.
# YADGAR_BACKEND_LOG_LEVEL uses the SurrealDB convention (warn/info/debug/error).
# uvicorn uses "warning" instead of "warn", so we remap before passing it.
_LOG_LEVEL="${YADGAR_BACKEND_LOG_LEVEL:-warn}"
export SURREAL_LOG="${_LOG_LEVEL}"
_UVICORN_LOG_LEVEL="${_LOG_LEVEL}"
[ "$_UVICORN_LOG_LEVEL" = "warn" ] && _UVICORN_LOG_LEVEL="warning"

# Data root for the surrealkv store (bind-mounted; /data in production).
SURREAL_DATA_ROOT="${SURREAL_DATA_ROOT:-/data}"

# --- safe-stop begin (P0 #37 Option B: writers-first ordered stop) ---------
# SurrealKV never flushes the store on close upstream: surrealkv's
# `impl Drop for Tree` skips the async close when the tokio runtime is already
# torn down on SurrealDB's SIGTERM path (unconditional on v3.1.5; see
# docs/plans/surrealkv-safe-stop-2026-07-10.md §2). This block cannot fix that
# Drop ordering, but it:
#   (a) stops the WRITERS (uvicorn embed + wiki-backup + inode-guard loops)
#       BEFORE surreal so no HTTP write is mid-flight against the store;
#   (b) WAITS for surreal's own exit and captures its status, so the container
#       never exits while the store is still shutting down (and any future
#       upstream graceful close actually gets to run);
#   (c) writes a SURREAL_UNCLEAN_STOP marker on a non-zero exit or deadline
#       overrun, so a torn stop is DETECTABLE (feeds safe-start auto-restore).
TORN_STOP_MARKER="${YADGAR_LOG_DIR}/SURREAL_UNCLEAN_STOP"
SPLIT_BRAIN_MARKER="${YADGAR_LOG_DIR}/SURREAL_SPLIT_BRAIN"
# Internal stop deadline — must stay below podman --stop-timeout 30 so WE
# handle the overrun (marker + SIGKILL) instead of podman killing PID 1 blind.
SURREAL_STOP_DEADLINE="${SURREAL_STOP_DEADLINE:-25}"

_write_torn_stop_marker() {
    # $1 = reason (timeout | nonzero-exit), $2 = surreal exit status or "unknown"
    {
        echo "ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "reason=$1"
        echo "surreal_exit_status=$2"
    } > "${TORN_STOP_MARKER}" 2>/dev/null || true
    echo "SURREAL_UNCLEAN_STOP: reason=$1 exit_status=$2 (marker: ${TORN_STOP_MARKER})" >&2
}

_stop_writers() {
    # Writers stop FIRST. Bounded wait so a hung uvicorn cannot eat the stop
    # budget surreal needs for its own shutdown.
    kill "${EMBED_PID:-}" "${WIKI_BACKUP_PID:-}" "${INODE_GUARD_PID:-}" 2>/dev/null || true
    local _ticks=25  # 5s @ 0.2s
    while [ -n "${EMBED_PID:-}" ] && kill -0 "${EMBED_PID}" 2>/dev/null; do
        if [ "${_ticks}" -le 0 ]; then
            echo "entrypoint: embed service did not exit within 5s — SIGKILL (preserving surreal's stop budget)" >&2
            kill -9 "${EMBED_PID}" 2>/dev/null || true
            break
        fi
        sleep 0.2
        _ticks=$(( _ticks - 1 ))
    done
    wait "${EMBED_PID:-}" 2>/dev/null || true
}

_stop_surreal_and_wait() {
    # SIGTERM surreal, then WAIT for its own exit (bounded). Returns 0 on a
    # clean (status 0) exit; writes the torn-stop marker and returns 1 on a
    # non-zero exit or deadline overrun.
    kill "${SURREAL_PID}" 2>/dev/null || true
    local _ticks=$(( SURREAL_STOP_DEADLINE * 5 ))  # 0.2s granularity
    while kill -0 "${SURREAL_PID}" 2>/dev/null; do
        if [ "${_ticks}" -le 0 ]; then
            _write_torn_stop_marker "timeout" "unknown"
            kill -9 "${SURREAL_PID}" 2>/dev/null || true
            wait "${SURREAL_PID}" 2>/dev/null || true
            return 1
        fi
        sleep 0.2
        _ticks=$(( _ticks - 1 ))
    done
    local _status=0
    wait "${SURREAL_PID}" 2>/dev/null || _status=$?
    if [ "${_status}" -ne 0 ]; then
        _write_torn_stop_marker "nonzero-exit" "${_status}"
        return 1
    fi
    echo "entrypoint: surreal exited cleanly (status 0)" >&2
    return 0
}

cleanup() {
    echo "entrypoint: stop signal received — writers-first safe stop (P0 #37)" >&2
    _stop_writers
    if [ -z "${SURREAL_PID:-}" ]; then
        # SIGTERM before surreal was ever spawned — nothing to stop, no marker.
        exit 0
    fi
    if _stop_surreal_and_wait; then
        exit 0
    else
        exit 1
    fi
}
# --- safe-stop end ----------------------------------------------------------
trap cleanup TERM INT

# Worker-thread stack size. Default tokio stack (~2 MiB) overflows on deep
# queries (large transactions, long expression chains, deeply nested values),
# aborting the whole process. 32 MiB gives headroom. Overridable via env.
export SURREAL_RUNTIME_STACK_SIZE="${SURREAL_RUNTIME_STACK_SIZE:-33554432}"
export RUST_MIN_STACK="${RUST_MIN_STACK:-33554432}"

# --- safe-start (P0 #37 Option D: torn-manifest detection + auto-restore) ---
SURREAL_STARTUP_LOG="${YADGAR_LOG_DIR}/surreal-startup.log"

# Surface a previous torn stop (observability). The marker is cleared only
# after surreal reaches healthy on this start.
if [ -f "${TORN_STOP_MARKER}" ]; then
    echo "WARNING: previous stop was UNCLEAN — $(tr '\n' ' ' < "${TORN_STOP_MARKER}" 2>/dev/null)" >&2
fi

# 5b split-brain preflight: refuse to start when a leftover surreal_db.old-*
# contains writes NEWER than the canonical. Dir names + dir mtimes LIE
# (os.rename preserves them — RCA §4); inner-file mtime is the truth.
# Auto-resolving this state is risky, so a human decides (runbook).
# Fail-closed ONLY on a genuine detection (exit 4); fail-open on tool error.
set +e
python3 -m yadgar.backend.safe_start preflight --data-dir "${SURREAL_DATA_ROOT}"
_pf_status=$?
set -e
if [ "${_pf_status}" -eq 4 ]; then
    echo "FATAL: safe_start preflight refused startup (path/inode split-brain evidence)." >&2
    echo "Runbook: docs/plans/surrealkv-safe-stop-2026-07-10.md §6" >&2
    exit 1
elif [ "${_pf_status}" -ne 0 ]; then
    echo "WARNING: safe_start preflight errored (status ${_pf_status}) — continuing (fail-open on tool error)" >&2
fi

# Start SurrealDB — bind to all interfaces so the core container can reach it
# across the docker network. Security: the docker network is internal; the
# host-side port is only published to 127.0.0.1 via -p 127.0.0.1:8000:8000.
# Output is tee'd to SURREAL_STARTUP_LOG (truncated per start) so the
# torn-manifest failure signature is machine-readable on a startup crash.
_start_surreal() {
    : > "${SURREAL_STARTUP_LOG}" 2>/dev/null || true
    surreal start \
      --no-banner \
      --bind 0.0.0.0:8000 \
      --user "${SURREAL_USER}" \
      --pass "${SURREAL_PASS}" \
      --log "${SURREAL_LOG}" \
      "surrealkv://${SURREAL_DATA_ROOT}/surreal_db" > >(tee -a "${SURREAL_STARTUP_LOG}") 2>&1 &
    SURREAL_PID=$!
}
_start_surreal

_health_ok() {
    python3 - <<'PYEOF' 2>/dev/null
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1)
except Exception:
    sys.exit(1)
PYEOF
}

# Wait for SurrealDB to be ready. If surreal DIES during the wait (the torn-
# manifest crashloop signature — RCA §1), run the safe-start recovery ONCE:
# it verifies the failure signature, moves the corrupt canonical aside
# (NEVER deleted), restores the newest complete quiesced copy by INNER-file
# mtime, and removes the stale LOCK. Anything else fails LOUD with the
# runbook pointer instead of spinning until systemd's start-timeout kill.
_RESTORE_ATTEMPTED=0
until _health_ok; do
    if ! kill -0 "${SURREAL_PID}" 2>/dev/null; then
        _st=0
        wait "${SURREAL_PID}" 2>/dev/null || _st=$?
        echo "ERROR: surreal exited (status ${_st}) before becoming healthy" >&2
        if [ "${_RESTORE_ATTEMPTED}" -eq 0 ]; then
            _RESTORE_ATTEMPTED=1
            if python3 -m yadgar.backend.safe_start recover \
                --data-dir "${SURREAL_DATA_ROOT}" \
                --startup-log "${SURREAL_STARTUP_LOG}"; then
                echo "safe_start: auto-restore complete — retrying surreal start" >&2
                _start_surreal
                continue
            fi
        fi
        echo "FATAL: surreal cannot start — manual recovery required." >&2
        echo "Runbook: docs/plans/surrealkv-safe-stop-2026-07-10.md §6 (torn-manifest recovery)" >&2
        exit 1
    fi
    sleep 0.2
done

# Healthy start achieved — the torn-stop marker (if any) did its job.
rm -f "${TORN_STOP_MARKER}" 2>/dev/null || true

# Bootstrap database users (idempotent — IF NOT EXISTS).
# Required env vars: YADGAR_RW_USER, YADGAR_RW_PASS, YADGAR_RO_USER, YADGAR_RO_PASS.
# If any are missing, log a warning and skip (legacy mode — only ROOT user exists).
#
# Users are defined ON ROOT (not ON DATABASE) because SurrealDB v3 only supports
# HTTP Basic auth for ON ROOT and ON NAMESPACE users. ON DATABASE users must use
# the JWT /signin flow, which yadgar's StorageEngine does not implement. The
# tradeoff: these users have full-server access rather than DB-scoped access.
# If finer-grained isolation is needed, migrate StorageEngine to JWT auth.
if [[ -n "${YADGAR_RW_USER:-}" && -n "${YADGAR_RW_PASS:-}" && -n "${YADGAR_RO_USER:-}" && -n "${YADGAR_RO_PASS:-}" ]]; then
    echo "Bootstrapping yadgar-rw and yadgar-ro users..."
    # Use Authorization header to avoid credentials leaking via /proc/<pid>/cmdline.
    _b64_creds="$(printf '%s:%s' "${SURREAL_USER:-root}" "${SURREAL_PASS:-root}" | base64 -w0)"
    # SurrealDB v3 HTTP /sql does NOT execute SQL in a JSON body — it treats the
    # body as a literal JSON value and returns it via implicit RETURN (silent no-op).
    # Only Content-Type: text/plain bodies are parsed as SurrealQL.
    #
    # Passwords are embedded as single-quoted SurrealQL string literals.
    # SQL-escape any literal single-quote by doubling it (SQL standard: ' -> '').
    _rw_pass_esc="${YADGAR_RW_PASS//\'/''}"
    _ro_pass_esc="${YADGAR_RO_PASS//\'/''}"
    _bootstrap_sql="DEFINE USER IF NOT EXISTS \`${YADGAR_RW_USER}\` ON ROOT PASSWORD '${_rw_pass_esc}' ROLES OWNER; DEFINE USER IF NOT EXISTS \`${YADGAR_RO_USER}\` ON ROOT PASSWORD '${_ro_pass_esc}' ROLES VIEWER;"
    if curl -sf \
        -H "Authorization: Basic ${_b64_creds}" \
        -H "Content-Type: text/plain" \
        -X POST --data "${_bootstrap_sql}" \
        http://127.0.0.1:8000/sql >/dev/null; then
        echo "User bootstrap complete (yadgar-rw ROOT OWNER, yadgar-ro ROOT VIEWER)"
    else
        echo "WARNING: user bootstrap failed; backend may be running with only ROOT user" >&2
    fi
else
    echo "WARNING: YADGAR_RW_USER/PASS or YADGAR_RO_USER/PASS not set — skipping user bootstrap (legacy ROOT-only mode)" >&2
fi

# Start embedding service
python3 -m uvicorn yadgar.backend.embed_service:app \
  --host 0.0.0.0 \
  --port 8001 \
  --no-access-log \
  --log-level "${_UVICORN_LOG_LEVEL}" &
EMBED_PID=$!

# §16 Wiki backup loop — every 6 hours alongside main services.
#
# NOTE: We do NOT use SurrealDB's /export endpoint — it can trigger a
# stack overflow in surrealdb-worker on large datasets (the recursive
# value serialiser blows the default tokio stack). Instead we do a
# targeted SELECT * FROM wiki_page via /sql. wiki_page is small and
# bounded so this query is safe.
#
# Authorization uses a base64-encoded Basic auth header instead of
# -u / --netrc-file so credentials do NOT appear in /proc/<pid>/cmdline.
_wiki_backup_loop() {
    # ADR-0076 D3: output dir is /data/backups/wiki/ (D4 layout); cadence 24 h.
    mkdir -p /data/backups/wiki
    while true; do
        sleep 86400  # 24 hours (ADR-0076 D3: was 6 h)
        if [[ "${YADGAR_ALLOW_ROOT:-0}" == "1" ]] || \
           { [[ -n "${SURREAL_USER}" ]] && [[ -n "${SURREAL_PASS}" ]]; }; then
            _b64_creds="$(printf '%s:%s' "${SURREAL_USER:?SURREAL_USER must be set}" "${SURREAL_PASS:?SURREAL_PASS must be set}" | base64 -w0)"
            _snap_file="/data/backups/wiki/wiki_$(date +%Y%m%d_%H%M%S).jsonl"
            if curl -sf \
                -H "Authorization: Basic ${_b64_creds}" \
                -H "Surreal-NS: yadgar" -H "Surreal-DB: main" \
                -H "Content-Type: text/plain" \
                -X POST --data "SELECT * FROM wiki_page;" \
                -o "${_snap_file}" \
                http://127.0.0.1:8000/sql; then
                echo "wiki_snapshot: saved ${_snap_file}"
            else
                echo "WARNING: wiki snapshot failed" >&2
                rm -f "${_snap_file}"
            fi
            # Retention: prune snapshots older than 14 days
            find /data/backups/wiki -name 'wiki_*.jsonl' -mtime +14 -delete
        fi
    done
}
_wiki_backup_loop &
WIKI_BACKUP_PID=$!

# --- inode-guard begin (P0 #37 item 5a: split-brain detection loop) ---------
# The 07-09 incident: host-side vacuum renames left the LIVE store inode at
# surreal_db.old-* while the surreal_db path held a stale decoy — silently,
# for 16 hours. surreal opens by PATH at start, so a rename-under-live-store
# is only detectable AFTER the fact: scan surreal's open fds and flag any
# that resolve OUTSIDE the canonical ${SURREAL_DATA_ROOT}/surreal_db.
_check_store_inode_coherence() {
    local fd link bad=""
    for fd in /proc/"${SURREAL_PID}"/fd/*; do
        link=$(readlink "${fd}" 2>/dev/null) || continue
        case "${link}" in
            "${SURREAL_DATA_ROOT}"/surreal_db.old-*|"${SURREAL_DATA_ROOT}"/surreal_db.new-*|"${SURREAL_DATA_ROOT}"/surreal_db.building-*|"${SURREAL_DATA_ROOT}"/surreal_db.pre-vacuum-*|"${SURREAL_DATA_ROOT}"/surreal_db.CORRUPT-*)
                bad="${link}"
                ;;
        esac
    done
    if [ -n "${bad}" ]; then
        if [ ! -f "${SPLIT_BRAIN_MARKER}" ]; then
            {
                echo "ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
                echo "fd_target=${bad}"
            } > "${SPLIT_BRAIN_MARKER}" 2>/dev/null || true
        fi
        echo "ERROR: STORE_INODE_SPLIT_BRAIN — surreal (pid ${SURREAL_PID}) holds an open fd OUTSIDE the canonical store path: ${bad} (marker: ${SPLIT_BRAIN_MARKER})" >&2
        return 1
    fi
    rm -f "${SPLIT_BRAIN_MARKER}" 2>/dev/null || true
    return 0
}

_inode_guard_loop() {
    while true; do
        sleep "${SURREAL_INODE_GUARD_INTERVAL:-300}"
        _check_store_inode_coherence || true
    done
}
# --- inode-guard end ---------------------------------------------------------
_inode_guard_loop &
INODE_GUARD_PID=$!

wait -n "$SURREAL_PID" "$EMBED_PID"
