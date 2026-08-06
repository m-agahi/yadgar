#!/bin/bash
# Backend container entrypoint: SurrealDB + embedding service.
#
# HISTORY: the original in-container `GET /export` cron (dropped in 267a45c3,
# #43) triggered a stack overflow in a `surrealdb-worker` thread — the export's
# recursive value serializer blew the default ~2 MiB tokio stack on this dataset,
# aborting the process and putting the container in a restart loop. The same
# commit raised SURREAL_RUNTIME_STACK_SIZE to 32 MiB (see below), which is why
# `/export` is used again today.
#
# The comment that stood here until task 0121 carried three false claims and is
# recorded so nobody reinstates them: (1) "no in-container backup loop" — there
# is one, `_wiki_backup_loop` below; (2) "DB snapshots are handled outside the
# container by the systemd ExecStartPre `cp -r`" — NO generator has ever emitted
# such a directive (`git log -S 'ExecStartPre=cp -r' --all` returns nothing), and
# believing it is why task 0115 exists; (3) "do not run `surreal export` until
# the upstream export-recursion issue is resolved" — the nightly cycle calls
# `GET /export` against a live backend by design.
#
# What actually snapshots this DB, as of today:
#   * pre-vacuum physical — yadgar/core/vacuum/phases.py: the host-side core
#     process stops the service, then shutil.copytree()s the surrealkv data dir.
#   * nightly logical — yadgar/core/backup/backup.py via
#     yadgar/core/scripts/nightly_cycle.py: `GET /export` -> .surql, labelled
#     nightly-pre / nightly-post, transactionally consistent against a live server.
#   * in-container wiki — `_wiki_backup_loop` in THIS file: a targeted
#     `SELECT * FROM wiki_page` via /sql every 24 h, 14-day retention (ADR-0076
#     D3). Deliberately not `/export`.
#   * pre-migration — NONE. That gap is task 0115. Do not add a forward
#     reference to its mechanism here: 0115 is unlanded, so describing it would
#     be false in exactly the way this rewrite is repairing. 0115's own commit
#     adds its line.
# Guard: yadgar/tests/scripts/test_no_cp_execstartpre_cross_generator.py pins
# claim (2) against every unit generator.
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

# --- engine #2 (MariaDB, ADR-0195) -----------------------------------------
# Datadir is a SIBLING SUBDIRECTORY of the shared data root, never inside the
# surrealkv tree. Rationale, in order of what it buys:
#   * the root is a host bind-mount shared by the backend and core containers
#     (`~/.local/share/yadgar -> /data`), so it is writable in production and
#     reachable by host-side systemd units — no new volume, no new mount, no
#     unit-renderer change (task 0122's 4-5 divergent renderers stay untouched);
#   * a SEPARATE subdir keeps it out of everything the vacuum touches. Verified:
#     the vacuum operates exclusively on `surreal_db`-prefixed paths under the
#     root — `_dir_bytes(db_path)` / `copytree(db_path)` where `db_path =
#     yadgar_home/"surreal_db"` (core/vacuum/phases.py:265, core/vacuum/
#     __init__.py:1218,1940,2032) and every reap glob is `surreal_db.*` or
#     `vacuum_export_*`. A copytree of a live InnoDB datadir would be
#     corruption; ADR-0196 keeps engine #2 out of the vacuum pipeline entirely.
MARIADB_DATA_DIR="${MARIADB_DATA_DIR:-${SURREAL_DATA_ROOT}/mariadb}"
# Socket-only: NO listener on any port, not even loopback. Core never talks to
# engine #2 directly (ADR-0195/ADR-0200 — it forwards to backend admin ops), so
# a TCP listener would be attack surface with no consumer. asyncmy supports
# `unix_socket` transport and `read_default_file` (asyncmy/connection.pyx:390,
# :818), so the client side needs no env plumbing at all.
MARIADB_SOCKET="${MARIADB_DATA_DIR}/mysqld.sock"
MARIADB_CLIENT_CNF="${MARIADB_DATA_DIR}/client.cnf"
MARIADB_DB="${MARIADB_DB:-yadgar}"
# Two accounts, deliberately:
#   * ADMIN — created by mariadb-install-db as the socket-auth root equivalent,
#     named after the OS user (`--auth-root-socket-user` defaults to `--user`).
#     Used ONLY by this script, needs no password, and cannot be used by the
#     app: asyncmy implements mysql_native_password / ed25519 / caching_sha2
#     but NOT the unix_socket auth plugin (asyncmy/auth.py).
#   * APP — password auth, privileges scoped to the engine-#2 database alone.
#     This is what car C connects with.
MARIADB_ADMIN_USER="$(id -un 2>/dev/null || echo yadgar)"
MARIADB_APP_USER="${MARIADB_APP_USER:-yadgar_app}"

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

_writer_alive() {
    # True while ANY still-set writer pid is running.
    local _p
    for _p in "${EMBED_PID:-}" "${MARIADB_PID:-}"; do
        [ -n "${_p}" ] && kill -0 "${_p}" 2>/dev/null && return 0
    done
    return 1
}

_stop_writers() {
    # Writers stop FIRST. Bounded wait so a hung uvicorn cannot eat the stop
    # budget surreal needs for its own shutdown.
    #
    # mysqld (engine #2) is a writer too, and its SIGTERM path is a clean InnoDB
    # shutdown, so it is stopped here alongside uvicorn. The two are waited on
    # CONCURRENTLY inside the SAME 5s window the embed wait always had — the
    # total stop budget (5s writers + SURREAL_STOP_DEADLINE) is unchanged, which
    # is what keeps us inside podman's --stop-timeout 30. A SIGKILLed mysqld is
    # recoverable from the InnoDB redo log on next start; a SIGKILLed surrealkv
    # store is NOT (ADR-0090), which is why surreal's budget is the protected one.
    kill "${EMBED_PID:-}" "${MARIADB_PID:-}" "${WIKI_BACKUP_PID:-}" "${INODE_GUARD_PID:-}" 2>/dev/null || true
    local _ticks=25  # 5s @ 0.2s
    while _writer_alive; do
        if [ "${_ticks}" -le 0 ]; then
            echo "entrypoint: writers did not exit within 5s — SIGKILL (preserving surreal's stop budget)" >&2
            kill -9 "${EMBED_PID:-}" "${MARIADB_PID:-}" 2>/dev/null || true
            break
        fi
        sleep 0.2
        _ticks=$(( _ticks - 1 ))
    done
    wait "${EMBED_PID:-}" 2>/dev/null || true
    wait "${MARIADB_PID:-}" 2>/dev/null || true
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

# --- engine #2: MariaDB (ADR-0195) ------------------------------------------
# Started HERE — after surreal is healthy, before the embed service — on purpose:
#   * after surreal's health/auto-restore loop so nothing new can perturb the
#     most load-bearing logic in this file (safe-start recovery);
#   * before the embed service so mysqld is up by the time the container reports
#     healthy, without being ON the health path.
# It is deliberately NOT in the closing `wait -n` and NOT in the container
# HEALTHCHECK: a mysqld failure must not kill the container or flip it unhealthy
# while surreal + embed serve. Nothing reads engine #2 yet (the `config` table
# lands in car D; the read path is repointed by the knob train), so every
# failure below is a WARNING, never fatal.
_start_mariadb() {
    _MARIADB_INSTALL_LOG="${YADGAR_LOG_DIR}/mariadb-install-db.log"
    mkdir -p "${MARIADB_DATA_DIR}" || return 1
    chmod 0700 "${MARIADB_DATA_DIR}" 2>/dev/null || true
    if [ ! -d "${MARIADB_DATA_DIR}/mysql" ]; then
        echo "entrypoint: initialising MariaDB datadir at ${MARIADB_DATA_DIR}" >&2
        # The chown warnings this emits for /usr/lib/mysql/plugin/auth_pam_tool_dir
        # under a read-only rootfs are expected and non-fatal (PAM auth unused).
        #
        # DO NOT change this redirect to `>/dev/null`. Observed on 11.8.6-deb13u1:
        # run from THIS script, install-db with stdout on /dev/null fails its
        # internal `mariadbd --bootstrap` with `ERROR: 1290 ... --skip-grant-tables`
        # (2/2 runs), while the identical invocation with stdout on a file or on
        # the container's stdout succeeds (2/2). The same command with /dev/null
        # in a bare shell OUTSIDE this script succeeds, so it is not the redirect
        # alone. Mechanism not identified; a log file both avoids it and leaves
        # the install transcript on disk, which /dev/null never would.
        if ! mariadb-install-db \
            --user="${MARIADB_ADMIN_USER}" \
            --datadir="${MARIADB_DATA_DIR}" \
            --skip-test-db \
            --auth-root-authentication-method=socket \
            >>"${_MARIADB_INSTALL_LOG}" 2>&1; then
            echo "ERROR: mariadb-install-db failed — transcript: ${_MARIADB_INSTALL_LOG}" >&2
            tail -20 "${_MARIADB_INSTALL_LOG}" >&2 2>/dev/null || true
            return 1
        fi
    fi
    # --skip-networking: socket only, no listener on any port (not even
    # loopback). Explicit --socket/--pid-file/--log-error keep every writable
    # path inside volumes that exist in BOTH dev and prod, so `read_only: true`
    # on the container rootfs stays intact.
    # --user is REQUIRED, not cosmetic: production runs this container as root
    # (`--user root` in scripts/install/launchd/*.plist.in and the systemd unit —
    # the image's `USER yadgar` is overridden there), and mariadbd HARD-REFUSES
    # to start as root unless --user=root is passed explicitly. When the process
    # is unprivileged the flag is a no-op warning. Verified against a host
    # bind-mount running as root, which is the production shape.
    mariadbd \
        --user="${MARIADB_ADMIN_USER}" \
        --datadir="${MARIADB_DATA_DIR}" \
        --socket="${MARIADB_SOCKET}" \
        --pid-file="${MARIADB_DATA_DIR}/mysqld.pid" \
        --log-error="${YADGAR_LOG_DIR}/mariadb-error.log" \
        --tmpdir=/tmp \
        --skip-networking \
        --skip-name-resolve &
    MARIADB_PID=$!
}

_mariadb_ready() {
    # PING ONLY — never a write. A writing probe would break the zero-rows
    # property the knob train's re-key window (task 0095) depends on.
    mariadb-admin --socket="${MARIADB_SOCKET}" --protocol=socket \
        --user="${MARIADB_ADMIN_USER}" ping >/dev/null 2>&1
}

_bootstrap_mariadb_accounts() {
    # Idempotent, runs on EVERY start — same shape as the surreal
    # `DEFINE USER IF NOT EXISTS` block above. Creates the engine-#2 DATABASE
    # (empty: no tables, no rows — car D owns the schema, and the knob train
    # owns the first row) and the password-auth app account car C connects with.
    #
    # An existing password is REUSED from the option file so a restart does not
    # rotate credentials underneath a running client; ALTER USER then forces the
    # server to agree with the file, so a hand-edited or half-written file
    # self-heals rather than locking the app out.
    local _pass=""
    if [ -r "${MARIADB_CLIENT_CNF}" ]; then
        _pass="$(awk -F'[ \t]*=[ \t]*' '/^password/ {print $2; exit}' "${MARIADB_CLIENT_CNF}")"
    fi
    if [ -z "${_pass}" ]; then
        _pass="$(python3 -c 'import secrets; print(secrets.token_hex(24))')" || return 1
    fi
    # SQL (and therefore the password) goes in on STDIN, never on a command
    # line — it must not land in /proc/<pid>/cmdline, same rule as the surreal
    # bootstrap above. Escape any literal single-quote by doubling it.
    local _pass_esc="${_pass//\'/\'\'}"
    mariadb --socket="${MARIADB_SOCKET}" --protocol=socket \
        --user="${MARIADB_ADMIN_USER}" <<SQLEOF || return 1
CREATE DATABASE IF NOT EXISTS \`${MARIADB_DB}\`;
CREATE USER IF NOT EXISTS '${MARIADB_APP_USER}'@'localhost' IDENTIFIED BY '${_pass_esc}';
ALTER USER '${MARIADB_APP_USER}'@'localhost' IDENTIFIED BY '${_pass_esc}';
GRANT ALL PRIVILEGES ON \`${MARIADB_DB}\`.* TO '${MARIADB_APP_USER}'@'localhost';
SQLEOF
    # Client credentials as a MySQL option file: asyncmy reads user/password/
    # socket/database straight out of it via read_default_file
    # (asyncmy/connection.pyx:375-390), so car C needs no env var — no new
    # secret reaches the compose env block, /etc/yadgar/secrets.env or any of
    # task 0122's unit renderers.
    ( umask 077 && cat > "${MARIADB_CLIENT_CNF}" <<CNFEOF
[client]
socket = ${MARIADB_SOCKET}
user = ${MARIADB_APP_USER}
password = ${_pass}
database = ${MARIADB_DB}
CNFEOF
    ) || return 1
    echo "entrypoint: MariaDB bootstrap complete (db=${MARIADB_DB}, app user=${MARIADB_APP_USER}, creds: ${MARIADB_CLIENT_CNF})" >&2
}

set +e
_start_mariadb
_mariadb_start_status=$?
if [ "${_mariadb_start_status}" -eq 0 ]; then
    _ticks=150  # 30s @ 0.2s
    until _mariadb_ready; do
        if ! kill -0 "${MARIADB_PID}" 2>/dev/null; then
            echo "WARNING: mariadbd exited before becoming ready — engine #2 unavailable (see ${YADGAR_LOG_DIR}/mariadb-error.log)" >&2
            MARIADB_PID=""
            break
        fi
        if [ "${_ticks}" -le 0 ]; then
            echo "WARNING: mariadbd not ready after 30s — continuing without engine #2" >&2
            break
        fi
        sleep 0.2
        _ticks=$(( _ticks - 1 ))
    done
    if [ -n "${MARIADB_PID}" ] && _mariadb_ready; then
        echo "entrypoint: MariaDB ready (socket ${MARIADB_SOCKET})" >&2
        _bootstrap_mariadb_accounts || \
            echo "WARNING: MariaDB account bootstrap failed — engine #2 present but unusable by the app" >&2
    fi
else
    echo "WARNING: MariaDB start/bootstrap failed — continuing without engine #2" >&2
    MARIADB_PID=""
fi
set -e
# --- engine #2 end ----------------------------------------------------------

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
