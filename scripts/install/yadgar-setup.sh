#!/usr/bin/env bash
# yadgar-setup — distribution-side setup entrypoint for pipx/brew/nix users.
# Parallels `make setup` for users without a repo checkout.
# Option C (v5.46.0): standalone script, NOT a yadgar CLI subcommand.
#
# Usage:
#   yadgar-setup [--noninteractive] [--dryrun] [--doctor]
#
# Flags:
#   --noninteractive   Use defaults; no interactive prompts.
#   --dryrun           Print commands without executing them.
#   --doctor           Run verification probes (macOS launchd + metrics endpoint).
#   --no-enable-linger Skip systemd lingering (units then die at logout).
#   --no-code-graph    Skip the codebase-memory-mcp install AND persist
#                      code_graph.enabled=false (both halves, coherently).
#
# Environment:
#   INSTALL_NONINTERACTIVE=1   Equivalent to passing --noninteractive. Exported
#                              to child install scripts (bootstrap_secrets.sh
#                              etc.) so credential prompts don't try to read
#                              from a non-TTY stdin (task 64, car C3).
#
# Exit codes:
#   0  success
#   1  setup failure (message printed to stderr)
#
# Install asset resolution:
#   Locates wheel-shipped install_assets/ via importlib.resources (sys.prefix fallback).
#   Works under pipx, brew virtualenv, and nix profile installs.
#
# Idempotency:
#   Re-runnable. Each building block is idempotent. Skips already-applied steps.

set -euo pipefail

# ── helper bundle check (fail-fast) ──────────────────────────────────────────
# v5.46.10: guard against incomplete wheel bundles (e.g. pipx installs before
# v5.46.10 where only yadgar-setup.sh was shipped, not its helper scripts).
# This check runs before flag parsing so --help still works (--help exits 0
# from the argparse block without calling any helper scripts).
#
# Exit code 2 = bundle gap (distinct from exit 1 = runtime/setup failure).
#
# NOTE: This check is skipped when invoked with --help or -h (no helpers needed).
_SETUP_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REQUIRED_HELPERS=(
    detect_runtime.sh
    detect_os.sh
    install_runtime.sh
    generate_systemd.sh
    generate_launchd.sh
    bootstrap_secrets.sh
    append_claude_rules.sh
)

# Only run the bundle check when NOT in --help mode
_IS_HELP_MODE=0
for _arg in "$@"; do
    case "$_arg" in --help|-h) _IS_HELP_MODE=1 ;; esac
done

if [[ "$_IS_HELP_MODE" -eq 0 ]]; then
    for _h in "${_REQUIRED_HELPERS[@]}"; do
        if [[ ! -f "$_SETUP_SCRIPTS_DIR/$_h" ]]; then
            echo "ERROR: yadgar-setup wheel bundle is incomplete — missing helper '$_h'." >&2
            echo "  This is a yadgar packaging bug (affects pipx installs before v5.46.10)." >&2
            echo "  Workarounds:" >&2
            echo "    1. Upgrade:       pipx upgrade yadgar   (requires yadgar >= v5.46.10)" >&2
            echo "    2. Repo checkout: git clone https://github.com/m-agahi/yadgar && cd yadgar && make setup" >&2
            echo "    3. Report at:     https://github.com/m-agahi/yadgar/issues" >&2
            exit 2
        fi
    done
fi
unset _SETUP_SCRIPTS_DIR _REQUIRED_HELPERS _IS_HELP_MODE _arg _h

# ── flag parsing ──────────────────────────────────────────────────────────────

NONINTERACTIVE=0
DRYRUN=0
DOCTOR=0
INSTALL_RUNTIME_FLAG=0    # 0=default, 1=--install-runtime, 2=--no-install-runtime
# Systemd lingering is attempted by default (see scripts/install/enable_linger.sh
# for why that is safe: self-linger is polkit allow_any=yes, so no sudo/TTY).
# Opt-out only — there is deliberately NO --enable-linger opt-IN flag: an opt-in
# flag for default-on behaviour is a no-op and pushes scripted installs onto the
# negative form (the --code-graph defect removed one car earlier in this train).
ENABLE_LINGER=1
# code_graph (codebase-memory-mcp) is provisioned by default, matching
# `yadgar setup` and the Makefile's YADGAR_CODE_GRAPH=1. Opt-out only, for the
# same reason as ENABLE_LINGER above. Note the opt-out does NOT merely skip the
# step: it runs `yadgar code-graph install --no-code-graph`, which persists
# code_graph.enabled=false — a plain skip would leave the feature ON with no
# binary, which is the bug this step exists to fix, inverted.
CODE_GRAPH=1

for arg in "$@"; do
    case "$arg" in
        --noninteractive)      NONINTERACTIVE=1 ;;
        --dryrun)              DRYRUN=1 ;;
        --doctor)              DOCTOR=1 ;;
        --install-runtime)     INSTALL_RUNTIME_FLAG=1 ;;
        --no-install-runtime)  INSTALL_RUNTIME_FLAG=2 ;;
        --no-enable-linger)    ENABLE_LINGER=0 ;;
        --no-code-graph)       CODE_GRAPH=0 ;;
        --help|-h)
            cat <<'EOF'
Usage: yadgar-setup [--noninteractive] [--dryrun] [--doctor]
                    [--install-runtime] [--no-install-runtime]
                    [--no-enable-linger] [--no-code-graph]

  --noninteractive       Use defaults; skip interactive prompts.
  --dryrun               Print commands without executing them.
  --doctor               Run verification probes (metrics, launchd, etc.).
  --install-runtime      Auto-install podman without prompting (yes-mode).
  --no-install-runtime   Skip podman install; print hint and exit 1 if not found.
  --no-enable-linger     Do not enable systemd lingering. Yadgar's user units
                         will then stop at logout and not start at boot.
  --no-code-graph        Opt out of code_graph: skip the codebase-memory-mcp
                         host-binary install AND persist code_graph.enabled=false,
                         so the flag and the binary stay coherent.
  --help                 Show this message.

yadgar-setup configures Yadgar for users installed via pipx, Homebrew, or nix profile.
Parallels `make setup` for users without a repo checkout.

Building blocks (in order):
  1. detect_runtime + detect_os
  2. pull-images
  3. bootstrap-secrets
  4. generate_systemd (Linux) / generate_launchd (macOS)
  5. enable-units (systemctl / launchctl)
  6. install-hooks
  7. install-agents
  8. config-sync
  9. install-rules (append CLAUDE.md fragment)
  10. seed-anchors
  11. seed-agent-prompts
  12. code-graph install (codebase-memory-mcp binary + code_graph.enabled)

See https://github.com/m-agahi/yadgar for full documentation.
EOF
            exit 0
            ;;
        *)
            echo "ERROR: Unknown flag: $arg" >&2
            echo "Run 'yadgar-setup --help' for usage." >&2
            exit 1
            ;;
    esac
done

# ── helpers ───────────────────────────────────────────────────────────────────

log()  { echo "==> $*"; }
info() { echo "    $*"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

run() {
    # In dryrun mode: print the command. Otherwise: execute it.
    if [ "$DRYRUN" -eq 1 ]; then
        echo "[dryrun] $*"
    else
        "$@"
    fi
}

run_sh() {
    # Run a shell script with run() wrapping (prints command in dryrun).
    if [ "$DRYRUN" -eq 1 ]; then
        echo "[dryrun] bash $*"
    else
        bash "$@"
    fi
}

# ── asset resolution ──────────────────────────────────────────────────────────

# Resolve the venv python interpreter via the yadgar shim's shebang line.
#
# Problem: bare ``python3`` resolves to /usr/bin/python3 on Rocky Linux / bare
# Debian. That interpreter has sys.prefix=/usr, so any path constructed from
# sys.prefix won't find wheel assets (which live in the pipx venv, not /usr).
#
# Solution: read the shebang of the ``yadgar`` shim installed by pipx/brew/nix.
# The shebang points directly to the venv python, e.g.
#   #!/root/.local/share/pipx/venvs/yadgar/bin/python
# which resolves sys.prefix to the pipx venv where the wheel assets are shipped.
#
# Fallback: echo "python3" when the yadgar shim is absent (repo-checkout dev).
_get_venv_python() {
    local yadgar_shim
    yadgar_shim=$(command -v yadgar 2>/dev/null) || { echo "python3"; return; }
    [ -f "$yadgar_shim" ] || { echo "python3"; return; }
    head -1 "$yadgar_shim" | sed 's|^#!||'
}

# Locate wheel-shipped install_assets via importlib.resources (sys.prefix path).
# Fallback: check if we are running from a repo checkout (SCRIPTS_DIR/../install_assets).
_locate_install_assets() {
    # Use venv python (via _get_venv_python) so sys.prefix resolves to the pipx
    # venv rather than /usr (the system python prefix on Rocky Linux / bare Debian).
    # Fix for v5.46.14: same class of bug as v5.46.11 (_resolve_yadgar_version).
    local venv_python share_path
    venv_python=$(_get_venv_python)
    share_path=$("$venv_python" -c "
import sys, os
prefix = sys.prefix
candidate = os.path.join(prefix, 'share', 'yadgar', 'install_assets')
print(candidate)
" 2>/dev/null || true)

    if [ -n "$share_path" ] && [ -d "$share_path" ]; then
        echo "$share_path"
        return
    fi

    # Fallback: repo checkout (script lives at scripts/install/yadgar-setup.sh)
    local repo_candidate
    repo_candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/install_assets"
    if [ -d "$repo_candidate" ]; then
        echo "$repo_candidate"
        return
    fi

    die "Cannot locate install_assets/. Is yadgar installed correctly? (sys.prefix=$share_path)"
}

_locate_setup_scripts() {
    # Locate the scripts/install/ dir (contains detect_runtime.sh, etc.)
    # The wheel only ships yadgar-setup.sh; all other building-block scripts require
    # a repo checkout. In pipx/brew/nix installs those scripts are called via
    # the `yadgar` CLI shim (pipx-aware, shebang points to venv python) instead.

    # Try repo checkout (script lives at scripts/install/yadgar-setup.sh)
    local repo_scripts
    repo_scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$repo_scripts/detect_runtime.sh" ]; then
        echo "$repo_scripts"
        return
    fi

    # Not a repo checkout — building-block scripts unavailable (expected for pipx/brew/nix)
    echo ""
}

# Resolve the installed yadgar core version.
#
# v5.46.18+: primary path uses `yadgar --version` (new flag) and extracts the
# core version with awk.  This avoids the shim-shebang workaround needed on
# systems where /usr/bin/python3 cannot see the pipx venv.
#
# Fallback (staged upgrades where installed yadgar is pre-5.46.18):
# read the shebang of the `yadgar` shim installed by pipx to locate the venv
# python, then import yadgar.__version__ directly.
#
# Final fallback: "latest" so docker pull falls back to :latest rather than
# failing.
_resolve_yadgar_version() {
    local version
    # Primary: yadgar --version (v5.46.18+)
    version=$(yadgar --version 2>/dev/null | awk '/^yadgar[[:space:]]+core/ {print $3}')
    if [ -n "$version" ] && [ "$version" != "unknown" ]; then
        echo "$version"
        return
    fi
    # Fallback: shim-shebang approach (pre-5.46.18 installs)
    local yadgar_shim venv_python
    yadgar_shim=$(command -v yadgar 2>/dev/null) || yadgar_shim=""
    if [ -n "$yadgar_shim" ] && [ -f "$yadgar_shim" ]; then
        venv_python=$(head -1 "$yadgar_shim" | sed 's|^#!||')
        if [ -n "$venv_python" ] && [ -x "$venv_python" ] && \
           "$venv_python" -c "import sys" 2>/dev/null; then
            version=$("$venv_python" -c "import yadgar; print(yadgar.__version__)" 2>/dev/null || echo "latest")
        else
            version="latest"
        fi
    else
        version="latest"
    fi
    echo "$version"
}

# Resolve the installed yadgar backend image version.
#
# v5.46.18+: primary path uses `yadgar --version` + awk to extract backend.
# Canonical source: yadgar/__init__.py BACKEND_VERSION constant.
#
# Fallback: shim-shebang approach for pre-5.46.18 installs.
# Final fallback: "5.4.0" (current backend track).
_resolve_backend_version() {
    local backend_version
    # Primary: yadgar --version (v5.46.18+)
    backend_version=$(yadgar --version 2>/dev/null | awk '/^yadgar[[:space:]]+backend/ {print $3}')
    if [ -n "$backend_version" ] && [ "$backend_version" != "unknown" ]; then
        echo "$backend_version"
        return
    fi
    # Fallback: shim-shebang approach (pre-5.46.18 installs)
    local yadgar_shim venv_python
    yadgar_shim=$(command -v yadgar 2>/dev/null) || yadgar_shim=""
    if [ -n "$yadgar_shim" ] && [ -f "$yadgar_shim" ]; then
        venv_python=$(head -1 "$yadgar_shim" | sed 's|^#!||')
        if [ -n "$venv_python" ] && [ -x "$venv_python" ] && \
           "$venv_python" -c "import sys" 2>/dev/null; then
            backend_version=$("$venv_python" -c "import yadgar; print(yadgar.BACKEND_VERSION)" 2>/dev/null || echo "5.4.0")
        else
            backend_version="5.4.0"
        fi
    else
        backend_version="5.4.0"
    fi
    echo "$backend_version"
}

# ── runtime + OS detection ────────────────────────────────────────────────────

_detect_runtime() {
    # Check YADGAR_CONTAINER_RUNTIME override first
    if [ -n "${YADGAR_CONTAINER_RUNTIME:-}" ]; then
        echo "$YADGAR_CONTAINER_RUNTIME"
        return
    fi

    local scripts_dir
    scripts_dir="$(_locate_setup_scripts)"

    if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/detect_runtime.sh" ]; then
        bash "$scripts_dir/detect_runtime.sh"
    else
        # Inline detection (used when building-block scripts unavailable)
        if command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
            echo "podman"
        elif command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
            echo "docker"
        else
            die "No container runtime found. Install podman or docker, or set YADGAR_CONTAINER_RUNTIME."
        fi
    fi
}

_offer_install_runtime() {
    # Called when _detect_runtime fails. Delegates to install_runtime.sh shared helper
    # (same helper the Makefile install-runtime target uses — DRY).
    local scripts_dir
    scripts_dir="$(_locate_setup_scripts)"

    local install_sh=""
    if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/install_runtime.sh" ]; then
        install_sh="$scripts_dir/install_runtime.sh"
    fi

    if [ -z "$install_sh" ]; then
        # Fallback when install_runtime.sh not available (pipx/nix install without scripts_dir)
        die "No container runtime found. Install podman or docker, then re-run: yadgar-setup"
    fi

    # Map yadgar-setup flags to install_runtime.sh flags + env
    local install_args=()
    case "$INSTALL_RUNTIME_FLAG" in
        1) install_args+=("--install-runtime") ;;
        2) install_args+=("--no-install-runtime") ;;
    esac

    INSTALL_NONINTERACTIVE="$NONINTERACTIVE" \
        bash "$install_sh" "${install_args[@]+"${install_args[@]}"}" || \
        die "Runtime detection failed. Install podman or docker, then re-run: yadgar-setup"
}

_detect_os() {
    if [ -n "${YADGAR_TEST_OS_MARKER:-}" ]; then
        echo "$YADGAR_TEST_OS_MARKER"
        return
    fi

    local scripts_dir
    scripts_dir="$(_locate_setup_scripts)"

    if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/detect_os.sh" ]; then
        bash "$scripts_dir/detect_os.sh"
    else
        # Inline detection
        if [ -f /etc/NIXOS ] || command -v nixos-version &>/dev/null 2>&1; then
            echo "linux-nixos"
        elif [ "$(uname -s)" = "Darwin" ]; then
            echo "macos"
        else
            echo "linux"
        fi
    fi
}

# ── setup steps ───────────────────────────────────────────────────────────────

_step_detect() {
    log "Step 1/12: Detecting runtime + OS..."

    # Try to detect runtime; on failure offer to install
    if ! RUNTIME=$(_detect_runtime 2>/dev/null); then
        _offer_install_runtime
        # After successful install, re-run detection
        RUNTIME=$(_detect_runtime) || die "Runtime still not found after install attempt."
    fi

    OS=$(_detect_os)
    info "Runtime: $RUNTIME"
    info "OS: $OS"

    if [ "$OS" = "linux-nixos" ]; then
        die "NixOS detected. Use the nix flake install instead: nix profile install github:m-agahi/yadgar && yadgar-setup"
    fi
}

_step_pull_images() {
    log "Step 2/12: Pulling container images..."
    local version backend_version
    version=$(_resolve_yadgar_version)
    backend_version=$(_resolve_backend_version)
    log "  core=${version}  backend=${backend_version}"
    # Stop running containers before pull so new image is picked up on restart.
    for ctr in yadgar yadgar-backend; do
        if "$RUNTIME" ps --format '{{.Names}}' 2>/dev/null | grep -qx "$ctr"; then
            log "  Stopping running container: $ctr"
            "$RUNTIME" stop "$ctr" 2>/dev/null || true
        fi
    done
    run "$RUNTIME" pull "docker.io/openfantasy/yadgar:${version}"
    run "$RUNTIME" pull "docker.io/openfantasy/yadgar-backend:${backend_version}"
}

_step_bootstrap_secrets() {
    log "Step 3/12: Bootstrapping secrets..."
    local scripts_dir
    scripts_dir="$(_locate_setup_scripts)"

    if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/bootstrap_secrets.sh" ]; then
        INSTALL_NONINTERACTIVE="$NONINTERACTIVE" \
            run_sh "$scripts_dir/bootstrap_secrets.sh"
    else
        warn "bootstrap_secrets.sh not found in scripts dir; skipping (run manually)"
        info "Manual: set ANTHROPIC_API_KEY in ~/.config/yadgar/secrets.env"
        if [ "$DRYRUN" -eq 1 ]; then
            echo "[dryrun] bash bootstrap_secrets.sh (INSTALL_NONINTERACTIVE=${NONINTERACTIVE})"
        fi
    fi
}

_step_inject_secrets() {
    # macOS only: run op inject to resolve 1Password secrets into secrets.env.
    # Requires op CLI and an interactive session (biometric/Touch ID prompt).
    # Skipped on Linux (systemd uses EnvironmentFile= natively; no inject needed).
    [ "$OS" = "macos" ] || return 0

    local scripts_dir
    scripts_dir="$(_locate_setup_scripts)"
    local activation="${scripts_dir}/launchd/yadgar-secrets-activation.sh"

    if [ ! -f "$activation" ]; then
        warn "yadgar-secrets-activation.sh not found; skipping op inject step"
        return 0
    fi

    if ! command -v op &>/dev/null; then
        info "1Password CLI (op) not installed — skipping secrets injection."
        info "  Install: brew install 1password-cli, then re-run yadgar-setup."
        return 0
    fi

    run bash "$activation"
}

_step_generate_units() {
    log "Step 4/12: Generating daemon units (${OS})..."
    local scripts_dir
    scripts_dir="$(_locate_setup_scripts)"
    local yadgar_dir="${YADGAR_DIR:-${HOME}/.local/share/yadgar}"
    local version backend_version
    version=$(_resolve_yadgar_version)
    backend_version=$(_resolve_backend_version)

    case "$OS" in
        linux|linux-other)
            local systemd_dir="${YADGAR_SYSTEMD_OUTPUT_DIR:-${HOME}/.config/systemd/user}"
            if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/generate_systemd.sh" ]; then
                run env \
                    YADGAR_RUNTIME="$RUNTIME" \
                    YADGAR_INSTALL_PREFIX="$yadgar_dir" \
                    YADGAR_SECRETS_ENV_FILE="${HOME}/.config/yadgar/secrets.env" \
                    YADGAR_BACKEND_IMAGE="docker.io/openfantasy/yadgar-backend:${backend_version}" \
                    YADGAR_CORE_IMAGE="docker.io/openfantasy/yadgar:${version}" \
                    YADGAR_SYSTEMD_OUTPUT_DIR="$systemd_dir" \
                    bash "$scripts_dir/generate_systemd.sh"
            else
                warn "generate_systemd.sh not found; skipping unit generation"
                if [ "$DRYRUN" -eq 1 ]; then
                    echo "[dryrun] generate_systemd.sh (systemd to $systemd_dir)"
                fi
            fi
            ;;
        macos)
            local launchd_dir="${YADGAR_LAUNCHD_OUTPUT_DIR:-${HOME}/Library/LaunchAgents}"
            # Canonical secrets path: XDG ~/.config/yadgar/secrets.env (Q3 unification).
            local secrets_env="${YADGAR_SECRETS_ENV_FILE:-${HOME}/.config/yadgar/secrets.env}"
            if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/generate_launchd.sh" ]; then
                run env \
                    YADGAR_RUNTIME="$RUNTIME" \
                    YADGAR_INSTALL_PREFIX="$yadgar_dir" \
                    YADGAR_SECRETS_ENV_FILE="$secrets_env" \
                    YADGAR_BACKEND_IMAGE="docker.io/openfantasy/yadgar-backend:${backend_version}" \
                    YADGAR_CORE_IMAGE="docker.io/openfantasy/yadgar:${version}" \
                    YADGAR_LAUNCHD_OUTPUT_DIR="$launchd_dir" \
                    bash "$scripts_dir/generate_launchd.sh"
            else
                warn "generate_launchd.sh not found; skipping plist generation"
                if [ "$DRYRUN" -eq 1 ]; then
                    echo "[dryrun] generate_launchd.sh (launchd to $launchd_dir)"
                fi
            fi
            ;;
        *)
            die "Unsupported OS: $OS"
            ;;
    esac
}

_step_pre_create_dirs() {
    # Pre-create XDG directories before service start.
    # Prevents first-run mkdir failures inside the container on hostile
    # filesystems (e.g. Rocky Linux SELinux enforcing before relabel).
    local yadgar_data="${YADGAR_DIR:-${HOME}/.local/share/yadgar}"
    local yadgar_config="${HOME}/.config/yadgar"
    local yadgar_state="${HOME}/.local/state/yadgar"
    log "Pre-creating XDG dirs (data/config/state)..."
    run mkdir -p "${yadgar_data}/logs"
    run chmod 700 "${yadgar_data}/logs"
    run mkdir -p "${yadgar_config}"
    run chmod 700 "${yadgar_config}"
    run mkdir -p "${yadgar_state}"
    run chmod 700 "${yadgar_state}"
}

# Systemd lingering — delegate to the shared helper the Makefile also calls, so
# the two install surfaces cannot drift apart (R5). Full rationale lives in
# scripts/install/enable_linger.sh; the short version is that all yadgar units
# are systemd *user* units, so without lingering they die at logout and never
# come back at boot.
#
# $1: optional mode passed through to the helper ("--check" = read-only probe).
_run_enable_linger() {
    local mode="${1:-}"

    if [ -z "$mode" ] && [ "$ENABLE_LINGER" -eq 0 ]; then
        info "Skipping systemd lingering (--no-enable-linger)."
        return 0
    fi

    local scripts_dir linger_sh
    scripts_dir="$(_locate_setup_scripts)"
    [ -n "$scripts_dir" ] || scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    linger_sh="${scripts_dir}/enable_linger.sh"

    if [ ! -f "$linger_sh" ]; then
        # Do not fail silently: a missing helper is exactly the silent
        # non-persistence this car exists to fix.
        warn "enable_linger.sh helper not found — systemd lingering was NOT enabled."
        warn "  Fix with:  sudo loginctl enable-linger $(id -un)"
        return 0
    fi

    # `|| true` is load-bearing: this script runs under `set -euo pipefail`, and
    # a linger failure must never abort an otherwise-successful install.
    YADGAR_LINGER_DRYRUN="$DRYRUN" bash "$linger_sh" ${mode:+"$mode"} || true
}

_step_enable_units() {
    log "Step 5/12: Enabling daemon units..."
    case "$OS" in
        linux|linux-other)
            _run_enable_linger
            run systemctl --user daemon-reload
            run systemctl --user enable yadgar.target
            # Reinstall scenario: if target already active, restart so regenerated
            # unit file (new image tag, :Z mount flag, etc.) takes effect immediately.
            if systemctl --user is-active --quiet yadgar.target 2>/dev/null; then
                log "  Reinstall detected — restarting yadgar.target"
                run systemctl --user restart yadgar.target
            else
                run systemctl --user start yadgar.target
            fi
            ;;
        macos)
            local launchd_dir="${YADGAR_LAUNCHD_OUTPUT_DIR:-${HOME}/Library/LaunchAgents}"
            local macos_major
            macos_major=$(sw_vers -productVersion 2>/dev/null | cut -d. -f1 || echo "11")
            for plist in \
                "${launchd_dir}/com.openfantasy.yadgar.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-backend.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-vacuum.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-nightly-cycle.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-vacuum-trigger.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-worktree-sweep.plist"; do
                if [ ! -f "$plist" ] && [ "$DRYRUN" -eq 0 ]; then
                    warn "$plist not found — did generate_launchd.sh fail?"
                    continue
                fi
                run launchctl unload "$plist" 2>/dev/null || true
                if [ "${macos_major}" -ge 11 ] 2>/dev/null; then
                    run launchctl bootstrap "gui/$(id -u)" "$plist"
                else
                    run launchctl load -w "$plist"
                fi
            done
            ;;
    esac
}

_step_install_hooks() {
    log "Step 6/12: Installing Claude Code git hooks..."
    # Car 7 (2026-07-26): the legacy `yadgar install-hooks` CLI was hard-removed.
    # The unified `yadgar install --client claude-code --hooks --scope global`
    # is now the single canonical path; --hooks is default-on so this single
    # invocation wires MCP + rules + hooks in one shot. Use --no-hooks if you
    # want to skip the hooks surface.
    run yadgar install --client claude-code --hooks --scope global
}

_step_install_agents() {
    log "Step 7/12: Installing subagent templates..."
    run yadgar install-subagents
}

_step_config_sync() {
    log "Step 8/12: Syncing config..."
    local yadgar_dir="${YADGAR_DIR:-${HOME}/.local/share/yadgar}"
    local config_file="${YADGAR_CONFIG_FILE:-${HOME}/.config/yadgar/config.yaml}"
    if [ ! -f "$config_file" ]; then
        log "  config.yaml not found — running 'yadgar config init' first"
        run yadgar config init
    fi
    run yadgar config sync
}

_step_install_rules() {
    # Car 3: route through the unified install command (rules_render.write_rules).
    # The old fragment-based path (append_claude_rules.sh + CLAUDE.md.fragment)
    # is retired in favour of the descriptor-driven generator so setup-time and
    # session-time rules agree.  Back-compat: if `yadgar install` is unavailable
    # (e.g. very old install running setup before upgrade) we fall back to the
    # legacy fragment path and warn.
    log "Step 9/12: Installing Claude Code rules via yadgar install..."
    if command -v yadgar > /dev/null 2>&1 && yadgar install --help 2>&1 | grep -q -- '--rules'; then
        run yadgar install --client claude-code --rules
    else
        # Legacy fallback — will be removed in a future release.
        warn "yadgar install not available; falling back to legacy CLAUDE.md.fragment path"
        local assets_dir
        assets_dir="$(_locate_install_assets)"
        local fragment="${assets_dir}/CLAUDE.md.fragment"
        local claude_md="${HOME}/.claude/CLAUDE.md"

        if [ ! -f "$fragment" ]; then
            warn "CLAUDE.md.fragment not found at $fragment; skipping"
            return
        fi

        local scripts_dir
        scripts_dir="$(_locate_setup_scripts)"
        if [ -n "$scripts_dir" ] && [ -f "$scripts_dir/append_claude_rules.sh" ]; then
            run env \
                YADGAR_CLAUDE_MD_TARGET="$claude_md" \
                YADGAR_FRAGMENT_PATH="$fragment" \
                bash "$scripts_dir/append_claude_rules.sh"
        else
            if [ "$DRYRUN" -eq 1 ]; then
                echo "[dryrun] append_claude_rules.sh ($fragment -> $claude_md)"
            else
                warn "append_claude_rules.sh not found; skipping rules install"
            fi
        fi
    fi
}

_wait_for_daemon() {
    # Poll localhost:8765/health until it responds or timeout (seconds) elapses.
    # On Linux: attempt to start yadgar.target via systemctl --user first.
    # On macOS: probe only (launchctl auto-start deferred to v5.46.16).
    # v5.46.20: default timeout bumped 30 → 120s (embed model load + SurrealDB
    # schema migration can take 60s+ on cold start). Progress log every 10s.
    local timeout="${1:-120}"

    # Try to start daemon if not already active (Linux only)
    case "${OS:-}" in
        linux|linux-other)
            systemctl --user is-active yadgar.target >/dev/null 2>&1 || \
                systemctl --user start yadgar.target >/dev/null 2>&1 || true
            ;;
        macos)
            # macOS: daemons auto-start via launchctl bootstrap in _step_enable_units.
            # Nothing to do here — _wait_for_daemon polls the health endpoint.
            : ;;
    esac

    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if curl -fsS "http://localhost:8765/health" >/dev/null 2>&1; then
            log "  Daemon ready after ${elapsed}s."
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        if [ $((elapsed % 10)) -eq 0 ]; then
            log "  Waiting for daemon... (${elapsed}s / ${timeout}s)"
        fi
    done
    # Health check timed out — print diagnostic hints for common failure modes.
    warn "Daemon /health did not respond within ${timeout}s."
    info "Diagnose with:"
    info "  journalctl --user -u yadgar-backend.service -n 30"
    info "  journalctl --user -u yadgar.service -n 30"
    info "  systemctl --user status yadgar.target"
    return 1
}

_step_seed_anchors() {
    log "Step 10/12: Seeding canonical anchors..."
    local assets_dir
    assets_dir="$(_locate_install_assets)"
    local anchors_yaml="${assets_dir}/seeds/anchors.yaml"

    if [ ! -f "$anchors_yaml" ] && [ "$DRYRUN" -eq 0 ]; then
        warn "anchors.yaml not found at $anchors_yaml; skipping"
        return 0
    fi

    # Ensure daemon is running before attempting seed (v5.46.15)
    # v5.46.20: timeout bumped to 120s to allow embed model load + schema migration.
    if [ "$DRYRUN" -eq 0 ]; then
        if ! _wait_for_daemon 120; then
            warn "Daemon failed to start in 120s. Skipping anchor seed."
            info "After daemon starts, run manually:"
            info "  yadgar seed --anchors $anchors_yaml"
            return 0
        fi
    fi

    run yadgar seed --anchors "$anchors_yaml"
}

_step_seed_agent_prompts() {
    log "Step 11/12: Seeding built-in starter agent-prompts..."
    if [ "$DRYRUN" -eq 0 ]; then
        if ! _wait_for_daemon 120; then
            warn "Daemon failed to start in 120s. Skipping agent-prompt seed."
            info "After daemon starts, run manually:"
            info "  yadgar seed --agent-prompts"
            return 0
        fi
    fi
    run yadgar seed --agent-prompts
}

# Provision code_graph: the codebase-memory-mcp host binary AND the
# code_graph.enabled runtime-config row, together, via the shared
# `yadgar code-graph install` seam the Makefile also calls.
#
# Why this step exists: `yadgar setup` (the Python subcommand) has provisioned
# code_graph by default since 7cd74ea0, but THIS script never invokes it — it
# runs its own building-block chain. Since code_graph.enabled defaults to true
# with no row (ADR-0163), a pipx/brew/nix-profile install produced a machine
# with code_graph ON and ~/.local/bin/codebase-memory-mcp absent.
#
# Deliberately LAST: the daemon is already warm from steps 10/11, so unlike
# `yadgar setup` (which runs before `yadgar daemon start`) the runtime-config
# persist can actually land here. No existing step body moves.
_step_code_graph() {
    log "Step 12/12: Provisioning code_graph via 'yadgar code-graph install'..."

    # Feature-probe before calling, mirroring _step_install_rules: a staged
    # upgrade can pair a NEW yadgar-setup.sh with an OLDER installed yadgar that
    # has no `code-graph install` subcommand. Warn and skip, never abort.
    if ! command -v yadgar > /dev/null 2>&1 || ! yadgar code-graph install --help > /dev/null 2>&1; then
        warn "'yadgar code-graph install' unavailable — code_graph was NOT provisioned."
        info "  Upgrade yadgar, then run:  yadgar code-graph install"
        return 0
    fi

    # NO _wait_for_daemon gate, deliberately. Steps 10/11 skip themselves on a
    # daemon timeout; copying that here would mean daemon-down -> no binary ->
    # the exact divergence this step exists to remove survives. The BINARY
    # install needs no daemon; only the persist does, and that already fails
    # soft with a printed remediation. Side benefit: no third 120s stall on a
    # machine whose daemon is broken.
    #
    # `|| true` is load-bearing: this script runs under `set -euo pipefail` and a
    # failed optional provision must never abort an otherwise-successful install.
    if [ "$CODE_GRAPH" -eq 0 ]; then
        # NOT a plain skip: skipping would leave code_graph.enabled at its true
        # default with no binary — the original bug, inverted. The opt-out has to
        # RUN so the `false` row lands.
        run yadgar code-graph install --no-code-graph || true
    else
        run yadgar code-graph install || true
    fi
}

# ── doctor probes ─────────────────────────────────────────────────────────────

# R6: generate_systemd.sh resolves the host CLI at RENDER time and bakes the
# result into ExecStart. That path can go stale afterwards (venv deleted, pipx
# reinstall relocating the shim), and the unit then fails on its next scheduled
# fire with nobody watching. Execute what the units actually reference.
_probe_host_cli() {
    local unit_dir="${YADGAR_SYSTEMD_OUTPUT_DIR:-${HOME}/.config/systemd/user}"
    local unit exec_line
    for unit in yadgar-vacuum.service yadgar-nightly-cycle.service; do
        [ -f "${unit_dir}/${unit}" ] || continue
        # `|| true` is load-bearing under `set -euo pipefail`: a unit with no
        # ExecStart makes grep exit 1, which would abort the whole doctor run.
        exec_line=$(grep -m1 '^ExecStart=' "${unit_dir}/${unit}" 2>/dev/null | cut -d= -f2- || true)
        # First field only: `yadgar vacuum --service-mode=...` → `yadgar`.
        # shellcheck disable=SC2086
        set -- ${exec_line}
        if [ -z "${1:-}" ]; then
            warn "${unit}: no ExecStart program"
        elif [ "$1" = "python3" ] || [ "$1" = "python" ]; then
            # `python3 -m <module>` branch: field one ALWAYS resolves, so testing
            # it proves nothing — the failure mode here is the package going
            # away, not the interpreter. Re-run the same isolated import the
            # generator used. $3 is the module ("$1 -m $3 ...").
            if "$1" -I -c "import ${3:-yadgar}" > /dev/null 2>&1; then
                info "OK: ${unit} host CLI resolves ($1 -m ${3:-yadgar})"
            else
                warn "${unit}: '${3:-yadgar}' is no longer importable by $1 —"
                warn "  this unit fails on its next fire. Fix: pipx install yadgar, then re-run setup."
            fi
        elif command -v "$1" > /dev/null 2>&1 || [ -x "$1" ]; then
            info "OK: ${unit} host CLI resolves ($1)"
        else
            warn "${unit}: host CLI '$1' is GONE — this unit fails on its next fire."
            warn "  Fix with 'pipx install yadgar' then re-run setup to re-render."
        fi
    done
}

# Ledger task 306: the installer emits a fixed set of managed Claude Code hook
# entries and nothing ever reported whether they reached the live settings.json.
# Measured 2026-08-21: PostToolUse carried post-tool-capture but NOT
# block-reflect, and PreCompact was an empty array, so pre-compact-drain never
# fired either -- two managed hooks silently unwired for want of anyone asking.
# Read-only: `yadgar verify-hooks` REPORTS divergence and never rewrites a hook
# another tool installed (nix hand-rolls this wiring with jq; reconciling that
# is a nix-repo change, not something this probe may do behind the user's back).
#
# Host-coupling escape hatch (car C3 / task 324, bug-bag-2 train 2026-08-23):
# the test suite needs to exercise the dispatch arm without depending on
# whichever yadgar build is on PATH or what the live ~/.claude/settings.json
# contains. Two env overrides let the test inject its own:
#   YADGAR_TEST_YADGAR_BIN       path to a yadgar shim the test controls
#   YADGAR_TEST_SETTINGS_JSON    path to a settings.json fixture
# Both default unset; production callers see no behaviour change.
_probe_managed_hooks() {
    local yadgar_bin="${YADGAR_TEST_YADGAR_BIN:-yadgar}"
    # Absolute path → check it exists directly; bare name → resolve through PATH.
    # The override is meant to let tests point at a shim that lives outside
    # PATH, so do not gate it on `command -v`.
    if [[ "$yadgar_bin" == */* ]]; then
        [ -x "$yadgar_bin" ] || {
            warn "YADGAR_TEST_YADGAR_BIN=$yadgar_bin is not executable - cannot verify managed-hook wiring"
            return
        }
    elif ! command -v "$yadgar_bin" > /dev/null 2>&1; then
        warn "yadgar CLI not on PATH (YADGAR_TEST_YADGAR_BIN=$yadgar_bin) - cannot verify managed-hook wiring"
        return
    fi
    if ! "$yadgar_bin" verify-hooks --help > /dev/null 2>&1; then
        warn "this yadgar has no 'verify-hooks' - upgrade to check hook wiring"
        return
    fi
    # `|| true` is load-bearing under `set -euo pipefail`: divergence exits 1 by
    # design, and that must print the report rather than abort the doctor run.
    local report rc
    rc=0
    if [ -n "${YADGAR_TEST_SETTINGS_JSON:-}" ]; then
        report=$("$yadgar_bin" verify-hooks --settings "$YADGAR_TEST_SETTINGS_JSON" 2>&1) || rc=$?
    else
        report=$("$yadgar_bin" verify-hooks 2>&1) || rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
        info "OK: every yadgar-managed hook is wired"
    else
        warn "yadgar-managed hooks are MISSING from the live settings.json -"
        warn "  a missing hook fires never and reports nothing. Details:"
        echo "$report" >&2
    fi
}

_run_doctor() {
    log "Doctor: Running verification probes..."

    case "$OS" in
        macos)
            local launchd_dir="${YADGAR_LAUNCHD_OUTPUT_DIR:-${HOME}/Library/LaunchAgents}"
            for plist in \
                "${launchd_dir}/com.openfantasy.yadgar.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-backend.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-vacuum.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-nightly-cycle.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-vacuum-trigger.plist" \
                "${launchd_dir}/com.openfantasy.yadgar-worktree-sweep.plist"; do
                if [ -f "$plist" ]; then
                    run plutil -lint "$plist" && info "OK: $plist"
                else
                    warn "Plist not found: $plist"
                fi
            done
            run launchctl list | grep com.openfantasy.yadgar && info "OK: launchd agents listed" || warn "launchd agents not found"
            # Lint-only is not enough: a rendered-but-never-loaded maintenance
            # job is exactly the drift this train is closing. Assert each one is
            # actually registered with launchd.
            for label in \
                com.openfantasy.yadgar-vacuum \
                com.openfantasy.yadgar-nightly-cycle \
                com.openfantasy.yadgar-vacuum-trigger \
                com.openfantasy.yadgar-worktree-sweep; do
                if launchctl print "gui/$(id -u)/${label}" > /dev/null 2>&1; then
                    info "OK: ${label} loaded"
                else
                    warn "${label} is NOT loaded — it will never fire. Run: make enable-units-macos"
                fi
            done
            ;;
        linux|linux-other)
            run systemctl --user --no-pager status yadgar.target 2>&1 | head -5 || warn "yadgar.target not active"
            # Timers are pulled in by yadgar.target's Wants=, so `is-enabled`
            # reports "disabled" for them by design — probe list-timers/is-active
            # instead. A never-activated timer is otherwise completely invisible.
            info "Maintenance timers:"
            run systemctl --user --no-pager list-timers 'yadgar-*' 2>&1 | head -6 \
                || warn "no yadgar timers scheduled — background maintenance will never run"
            if systemctl --user is-active --quiet yadgar-vacuum-trigger.path 2>/dev/null; then
                info "OK: yadgar-vacuum-trigger.path active"
            else
                warn "yadgar-vacuum-trigger.path is NOT active — MCP vacuum_now() would be a no-op"
            fi
            # R6: the maintenance units bake in a host CLI path resolved at
            # render time. A pipx reinstall or deleted venv breaks it silently
            # until the unit fires at 4am; surface it here instead.
            _probe_host_cli
            # Read-only linger probe: reports state, never mutates it.
            _run_enable_linger --check
            ;;
    esac

    # Managed-hook wiring probe (task 306) - read-only, never repairs.
    _probe_managed_hooks

    # Metrics endpoint probe
    info "Checking metrics endpoint (http://localhost:8765/metrics)..."
    if command -v curl &>/dev/null; then
        if curl -sf http://localhost:8765/metrics >/dev/null 2>&1; then
            info "OK: /metrics responding"
        else
            warn "/metrics not responding — is the daemon running?"
        fi
    fi
}

# ── main ──────────────────────────────────────────────────────────────────────

main() {
    if [ "$DRYRUN" -eq 1 ]; then
        echo "=== yadgar-setup --dryrun ==="
        echo "    Commands will be printed but NOT executed."
        echo ""
    fi

    # Phase 1: Detect runtime and OS
    _step_detect

    if [ "$DOCTOR" -eq 1 ]; then
        _run_doctor
        exit 0
    fi

    # Phase 2: Pull images
    _step_pull_images

    # Phase 3: Bootstrap secrets
    _step_bootstrap_secrets

    # Phase 3b: op inject — resolve 1Password secrets into secrets.env (macOS only)
    _step_inject_secrets

    # Phase 4: Generate daemon units
    _step_generate_units

    # Phase 4b: Pre-create data directories (before unit start)
    _step_pre_create_dirs

    # Phase 5: Enable units
    _step_enable_units

    # Phase 6-11: Application-level setup (yadgar CLI building blocks)
    _step_install_hooks
    _step_install_agents
    _step_config_sync
    _step_install_rules
    _step_seed_anchors
    _step_seed_agent_prompts
    _step_code_graph

    echo ""
    if [ "$DRYRUN" -eq 1 ]; then
        echo "==> [dryrun] yadgar-setup complete — no changes made."
    else
        echo "==> Yadgar setup complete!"
        echo "    Run 'yadgar-setup --doctor' to verify."
    fi
}

main
