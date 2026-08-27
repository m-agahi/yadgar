#!/usr/bin/env bash
# Reap orphaned TEST surreal procs AND their leaked surrealkv data dirs,
# left behind by crashed/OOM-killed pytest runs.
#
# PROCESSES — filters by process NAME (comm == "surreal") AND data path
# (/tmp/pytest) so it:
#   - NEVER matches its own wrapper shell / make / pre-commit (comm = sh/make/...),
#     which is the bug that made `pkill -9 -f 'surreal start.*/tmp/pytest'`
#     SIGKILL its own `sh -c` (the pattern appears in the wrapper's own cmdline),
#     killing `make e2e` and breaking the pre-push gate.
#   - NEVER touches the production daemon (its surrealkv data lives under /data,
#     not /tmp/pytest).
#
# DIRECTORIES (task 307) — the half that was missing. Reaping the process left
# its surrealkv store on disk forever: 4838 dirs / 49GB accumulated between
# 2026-08-01 and 2026-08-27, and the pre-push `make e2e` gate was OOM-killed
# (Error 137) with 1132 of them (12GB) live.
#
# Note the two halves do NOT scope the same way, and that is deliberate: the
# session fixture's `mkdtemp(prefix="surreal_session_")` lands at the TOP of
# TMPDIR (`/tmp/surreal_session_XXXXXXXX`), which contains no `/tmp/pytest`, so
# the process filter above never matched those procs either. The directory
# sweep keys on the two basename prefixes the pytest fixtures own instead, with
# the same discipline preserved:
#   - prefix-gated to `surreal_session_*` / `surreal_respawn_*` under a tmp
#     root, so `/data` (production) and benchmark dirs are unreachable from here;
#   - IN-USE gated: any dir named in a live `comm == "surreal"` cmdline is left
#     alone, so a concurrent run's live database is never deleted;
#   - AGE gated (>1 min): covers the mkdtemp -> spawn window, during which a
#     brand-new dir is not yet in any cmdline.
#
# `--dirs-only` runs the directory sweep WITHOUT the process kill above. The
# sweep is safe against a concurrent run (in-use gated); the kill is not, so
# anything that only wants disk back — the test suite for this script included
# — asks for the half it needs rather than SIGKILLing another run's database.
#
# Always exits 0 — reaping is best-effort cleanup, never a failure.
set -u

dirs_only=0
[ "${1:-}" = "--dirs-only" ] && dirs_only=1

if [ "$dirs_only" -eq 0 ]; then
  ps -eo pid,comm,args 2>/dev/null \
    | awk '$2 == "surreal" && /\/tmp\/pytest/ { print $1 }' \
    | xargs -r kill -9 2>/dev/null || true
fi

# Data paths currently served by a LIVE surreal (comm-filtered, same as above).
live_args="$(ps -eo comm,args 2>/dev/null | awk '$1 == "surreal"')"

# BOTH roots, deduped. `$TMPDIR` alone is not enough: when it IS set, sweeping
# only it misses the top of `/tmp`, which is exactly where a TMPDIR-unset run
# (a bare `uv run pytest`) puts its stores — and where the 4838-dir backlog
# was found. Mirrors the union `sweep_orphan_surreal_data_dirs` takes.
tmp_root="${TMPDIR:-/tmp}"
tmp_root="${tmp_root%/}"
roots="$tmp_root"
[ "$tmp_root" = "/tmp" ] || roots="$tmp_root /tmp"

for root in $roots; do
  for dir in "$root"/surreal_session_* "$root"/surreal_respawn_*; do
    [ -d "$dir" ] || continue
    case "$live_args" in
      *"$dir"*) continue ;; # a live surreal is serving out of this store
    esac
    # Older than a minute only.
    [ -n "$(find "$dir" -maxdepth 0 -mmin +1 2>/dev/null)" ] || continue
    rm -rf -- "$dir" 2>/dev/null || true
  done
done

exit 0
