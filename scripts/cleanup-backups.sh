#!/usr/bin/env bash
# cleanup-backups.sh — Yadgar backup retention helper
#
# Enforces three caps on the surreal_db_* snapshot ring:
#   1. Age cap  — drop snapshots older than YADGAR_BACKUP_MAX_AGE_DAYS
#   2. Count cap — keep at most YADGAR_BACKUP_MAX_COUNT newest snapshots
#   3. Size cap  — drop oldest until total real disk usage ≤ YADGAR_BACKUP_MAX_GIB GiB
#
# Whichever cap fires first wins; order: age → count → size.
# Idempotent — safe to run multiple times.
#
# Environment variables (all optional — defaults shown):
#   YADGAR_BACKUP_DIR               ~/.backups/yadgar/db
#   YADGAR_BACKUP_MAX_AGE_DAYS      7
#   YADGAR_BACKUP_MAX_COUNT         7
#   YADGAR_BACKUP_MAX_GIB           10
#   YADGAR_BACKUP_CLEANUP_VERBOSE   (set to 1 to print every removed path)
#
# Flags:
#   --dry-run   Print what would be removed but do not delete anything.

set -euo pipefail

# ── Parse flags ────────────────────────────────────────────────────────────────
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *) echo "cleanup-backups.sh: unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ── Configuration ──────────────────────────────────────────────────────────────
DIR="${YADGAR_BACKUP_DIR:-${HOME}/.backups/yadgar/db}"
MAX_AGE_DAYS="${YADGAR_BACKUP_MAX_AGE_DAYS:-7}"
MAX_COUNT="${YADGAR_BACKUP_MAX_COUNT:-7}"
MAX_GIB="${YADGAR_BACKUP_MAX_GIB:-10}"
VERBOSE="${YADGAR_BACKUP_CLEANUP_VERBOSE:-0}"

# ── Helpers ────────────────────────────────────────────────────────────────────
log_verbose() {
    if [ "$VERBOSE" = "1" ] || [ "$DRY_RUN" = "1" ]; then
        echo "$*" >&2
    fi
}

remove_snap() {
    local path="$1"
    local reason="$2"
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would remove (${reason}): ${path}"
    else
        log_verbose "removing (${reason}): ${path}"
        rm -rf "$path"
    fi
}

# ── Guard: nothing to do if dir absent or empty ───────────────────────────────
if [ ! -d "$DIR" ]; then
    log_verbose "cleanup-backups.sh: backup dir does not exist: $DIR — nothing to do"
    exit 0
fi

# Check for any matching snapshots (shopt nullglob means no matches = empty array)
shopt -s nullglob
snaps=("$DIR"/surreal_db_*)
shopt -u nullglob
if [ "${#snaps[@]}" -eq 0 ]; then
    log_verbose "cleanup-backups.sh: no surreal_db_* snapshots in $DIR — nothing to do"
    exit 0
fi

# ── Step 1: Age cap ────────────────────────────────────────────────────────────
# find -mtime +N matches directories whose mtime is *more than* N days ago.
# Collect paths first so we can report them under --dry-run.
age_candidates=()
while IFS= read -r -d '' p; do
    age_candidates+=("$p")
done < <(find "$DIR" -maxdepth 1 -name 'surreal_db_*' -type d -mtime +"${MAX_AGE_DAYS}" -print0 2>/dev/null)

for p in "${age_candidates[@]}"; do
    remove_snap "$p" "age>${MAX_AGE_DAYS}d"
done

# ── Step 2: Count cap ──────────────────────────────────────────────────────────
# Rebuild snapshot list (age step may have removed some) — sort newest-first.
shopt -s nullglob
remaining=("$DIR"/surreal_db_*)
shopt -u nullglob

if [ "${#remaining[@]}" -gt "$MAX_COUNT" ]; then
    # Sort by mtime descending (newest first), then slice off the tail
    mapfile -t sorted < <(
        for snap in "${remaining[@]}"; do
            printf '%s\t%s\n' "$(stat -c '%Y' "$snap" 2>/dev/null || echo 0)" "$snap"
        done | sort -rn | awk -F'\t' '{print $2}'
    )
    # Keep the first MAX_COUNT; remove the rest
    for (( i=MAX_COUNT; i<${#sorted[@]}; i++ )); do
        remove_snap "${sorted[$i]}" "count>${MAX_COUNT}"
    done
fi

# ── Step 3: Size cap ───────────────────────────────────────────────────────────
# Loop: while total real-block usage > MAX_GIB, remove the oldest snapshot.
# du -sBG: summarise in GiB units (rounds up to next GiB due to block sizes).
# We use awk to strip the trailing 'G' suffix.
while true; do
    # Refresh snapshot list each iteration (may have shrunk)
    shopt -s nullglob
    size_snaps=("$DIR"/surreal_db_*)
    shopt -u nullglob
    if [ "${#size_snaps[@]}" -eq 0 ]; then
        break
    fi

    used_raw="$(du -sBG "$DIR" 2>/dev/null | awk '{print $1}' | tr -d 'G')"
    # Handle empty/non-numeric output gracefully
    if ! [[ "$used_raw" =~ ^[0-9]+$ ]]; then
        break
    fi
    used_gib="$used_raw"

    if [ "$used_gib" -le "$MAX_GIB" ]; then
        break
    fi

    # Find the oldest snapshot (lowest mtime)
    oldest=""
    oldest_mtime=9999999999
    for snap in "${size_snaps[@]}"; do
        snap_mtime="$(stat -c '%Y' "$snap" 2>/dev/null || echo 9999999999)"
        if [ "$snap_mtime" -lt "$oldest_mtime" ]; then
            oldest_mtime="$snap_mtime"
            oldest="$snap"
        fi
    done

    if [ -z "$oldest" ]; then
        break
    fi

    remove_snap "$oldest" "size>${MAX_GIB}GiB"

    # Under dry-run we never actually delete, so we must break to avoid infinite loop
    if [ "$DRY_RUN" = "1" ]; then
        break
    fi
done

log_verbose "cleanup-backups.sh: done"
