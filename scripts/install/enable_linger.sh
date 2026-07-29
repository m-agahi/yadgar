#!/usr/bin/env bash
# enable_linger.sh — shared helper: enable systemd lingering for the invoking user.
#
# Called by both yadgar-setup.sh (_step_enable_units / _run_doctor) and the
# Makefile (enable-units, _enable-units-auto) — same DRY shape as
# install_runtime.sh, so the two install surfaces cannot drift apart.
#
# WHY THIS EXISTS
#   All three yadgar units are systemd *user* units with
#   `WantedBy=default.target`. Without lingering, the per-user systemd manager
#   (user@$UID.service) is torn down when the user's last session ends and is
#   never started at boot. So the units are correctly enabled and *still* do not
#   persist: the daemon dies at logout and never returns after a reboot. On a
#   workstation with a permanent graphical session this is invisible; on the
#   headless server someone actually installs into over SSH, the install
#   silently does not persist.
#
# WHY IT IS ATTEMPTED BY DEFAULT (and never prompts)
#   `loginctl enable-linger` for *your own* user maps to the polkit action
#   org.freedesktop.login1.set-self-linger, which is allow_any=yes /
#   allow_inactive=yes / allow_active=yes — no sudo, no polkit auth, no TTY, no
#   interactive agent. (set-user-linger, i.e. somebody *else's* user, is the
#   auth_admin_keep one; this helper never needs it.)
#
#   Do NOT read install_runtime.sh's default-to-hint behaviour as a precedent
#   against acting here. That helper defaults to printing a hint because it
#   shells out to `sudo apt-get install`, and sudo can hang or fail without a
#   TTY. Auth-free self-linger has no such constraint. Same flag *shape*,
#   deliberately different default.
#
#   The one real failure path is a host with no reachable polkitd, as a non-root
#   user. That is a warning with an exact remediation line — never a fatal.
#
# Modes:
#   (default)   attempt to enable lingering for the invoking user
#   --check     probe and report only; NEVER mutates (used by --doctor)
#
# Environment:
#   YADGAR_ENABLE_LINGER=0      Skip entirely (Makefile opt-out / --no-enable-linger).
#   YADGAR_LINGER_DRYRUN=1      Print the command that would run; do not run it.
#   YADGAR_LINGER_TIMEOUT=<s>   Per-loginctl-call timeout, default 5 (R6: logind
#                               present but not running must not hang the install).
#   YADGAR_TEST_LOGINCTL=<bin>  Override the loginctl binary (test seam).
#                               Empty string = simulate "loginctl not installed".
#
# Exit codes:
#   Always 0. A linger failure must never abort an install — yadgar-setup.sh
#   runs under `set -euo pipefail`, so a non-zero exit here would take the whole
#   install down. Callers additionally guard with `|| true` (belt and braces).

# NOTE: deliberately no `set -e`. Every probe below is a command substitution
# whose failure is a *handled* outcome (no logind, wedged logind, denied
# polkit); under `set -e` a failing assignment would abort the script before it
# could print the remediation, which is precisely the bug this helper fixes.
set -uo pipefail

# ── helpers ───────────────────────────────────────────────────────────────────

info() { echo "    $*"; }
log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }

# ── mode ──────────────────────────────────────────────────────────────────────

MODE="enable"
for _arg in "$@"; do
    case "$_arg" in
        --check) MODE="check" ;;
    esac
done

USER_NAME="$(id -un 2>/dev/null || echo "${USER:-}")"

_remediation() {
    echo "      yadgar's user units will NOT survive logout or start at boot." >&2
    echo "      Fix with:  sudo loginctl enable-linger ${USER_NAME}" >&2
    echo "      Skip this check next time with: yadgar-setup --no-enable-linger" >&2
}

# ── early exits (enable mode only) ────────────────────────────────────────────

if [ "$MODE" = "enable" ]; then
    if [ "${YADGAR_ENABLE_LINGER:-1}" = "0" ]; then
        info "Skipping systemd lingering (YADGAR_ENABLE_LINGER=0)."
        exit 0
    fi

    # Dryrun prints the intended command *before* probing, so the printed plan
    # does not depend on whether this particular host happens to have logind.
    if [ "${YADGAR_LINGER_DRYRUN:-0}" = "1" ]; then
        echo "[dryrun] loginctl enable-linger ${USER_NAME}"
        exit 0
    fi
fi

# ── loginctl resolution ───────────────────────────────────────────────────────

# `-` not `:-`: an explicitly empty YADGAR_TEST_LOGINCTL means "absent", which is
# distinguishable from the variable being unset.
_LOGINCTL="${YADGAR_TEST_LOGINCTL-loginctl}"

if [ -z "$_LOGINCTL" ] || ! command -v "$_LOGINCTL" > /dev/null 2>&1; then
    # Not a warning: a host without systemd-logind (non-systemd distro, container
    # without logind, macOS) has nothing to linger. Informational only.
    info "loginctl not available — skipping systemd lingering (no systemd-logind on this host)."
    exit 0
fi

_TIMEOUT_S="${YADGAR_LINGER_TIMEOUT:-5}"
_TIMEOUT_BIN="$(command -v timeout 2> /dev/null || true)"

_run_loginctl() {
    if [ -n "$_TIMEOUT_BIN" ]; then
        "$_TIMEOUT_BIN" "$_TIMEOUT_S" "$_LOGINCTL" "$@"
    else
        "$_LOGINCTL" "$@"
    fi
}

_linger_state() {
    local out
    if ! out=$(_run_loginctl show-user "$USER_NAME" -p Linger 2>/dev/null); then
        echo "unknown"
        return 0
    fi
    case "$out" in
        *=yes*) echo "yes" ;;
        *=no*)  echo "no" ;;
        *)      echo "unknown" ;;
    esac
}

STATE="$(_linger_state)"

# ── check mode (doctor) — read-only, never mutates ───────────────────────────

if [ "$MODE" = "check" ]; then
    case "$STATE" in
        yes)
            info "OK: systemd lingering enabled for '${USER_NAME}' (units survive logout + reboot)."
            ;;
        no)
            warn "systemd lingering is DISABLED for user '${USER_NAME}'."
            _remediation
            ;;
        *)
            warn "could not determine systemd lingering state for user '${USER_NAME}'."
            _remediation
            ;;
    esac
    exit 0
fi

# ── enable mode ───────────────────────────────────────────────────────────────

if [ "$STATE" = "yes" ]; then
    info "systemd lingering already enabled for '${USER_NAME}' — skipping."
    exit 0
fi

log "Enabling systemd lingering for '${USER_NAME}' (so units survive logout + reboot)..."
if _run_loginctl enable-linger "$USER_NAME" > /dev/null 2>&1; then
    info "OK: lingering enabled for '${USER_NAME}'."
    exit 0
fi

# Failure path: no reachable polkitd as a non-root user, logind wedged, or the
# call timed out. Loud, actionable, and non-fatal.
warn "could not enable systemd lingering for user '${USER_NAME}'."
_remediation
exit 0
