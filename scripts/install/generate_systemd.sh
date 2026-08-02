#!/usr/bin/env bash
# Marshal the install environment, resolve a renderer, delegate. RENDERS NOTHING.
#
# task:0110 Stage D (ADR-0190). This script used to `sed`-render nine units from
# nine `.in` templates, which is why the repo carried four cross-generator
# invariant tests: two renderers emitting the same units drift, and the guard
# tax grows with every unit. The unit text now lives in ONE place for the
# non-nix Linux surfaces — yadgar/core/daemon/{unit_model,units,maintenance_units}.py
# — and this wrapper keeps only what is genuinely shell: the documented env
# contract, runtime detection, the version-skew assertion and the invocation.
#
# NOT converged, and out of reach from here: flake.nix builds its own systemd
# user units declaratively at nix eval time (a DIFFERENT set — eight units with
# per-unit Install.WantedBy and no yadgar.target). The *_cross_generator.py
# suites are what keep that arm honest.
#
# Environment variables (all have defaults):
#   YADGAR_SYSTEMD_OUTPUT_DIR   Target dir (default: ~/.config/systemd/user)
#   YADGAR_RUNTIME              Container runtime: podman|docker (default: auto-detected)
#   YADGAR_INSTALL_PREFIX       Data dir mounted at /data (default: ~/.local/share/yadgar)
#   YADGAR_SECRETS_ENV_FILE     Path to secrets.env (default: ~/.config/yadgar/secrets.env)
#   YADGAR_BACKEND_IMAGE        Backend image tag (default: openfantasy/yadgar-backend:latest)
#   YADGAR_CORE_IMAGE           Core image tag (default: openfantasy/yadgar:latest)
#   YADGAR_STATE_DIR            XDG state dir bound into the core container for the
#                               vacuum trigger (default: ~/.local/state/yadgar)
#   YADGAR_BACKEND_SURREAL_PORT Host port SurrealDB is published on, loopback-only
#                               (default: 8000). Override when :8000 is occupied.
#   YADGAR_HOST_CLI             Explicit path to the `yadgar` host CLI (escape hatch)
#   YADGAR_HOST_NIGHTLY_CLI     Explicit path to the `yadgar-nightly-cycle` host CLI
#   YADGAR_RENDERER_CLI         Explicit renderer command (escape hatch; word-split,
#                               so `python3 -m yadgar` is valid). Distinct from
#                               YADGAR_HOST_CLI, which is baked into the vacuum unit's
#                               ExecStart rather than executed now.
#
# Exits non-zero if:
#   - YADGAR_RUNTIME not detected
#   - no renderer resolves, or the resolved one is older than MIN_UNIT_SCHEMA
#   - existing units are nix-managed symlinks (DP5; enforced by the renderer)
#   - no host yadgar CLI resolves (the maintenance units would fail at 4am)
#
# RECOVERY. The renderer stages all nine units and validates them before moving
# any into place, so every abort above leaves the PREVIOUS units on disk and
# running. If a set is ever left broken:
#     systemctl --user stop yadgar.target
#     rm -f ~/.config/systemd/user/yadgar{,-backend}.service \
#           ~/.config/systemd/user/yadgar.target \
#           ~/.config/systemd/user/yadgar-vacuum.{service,timer} \
#           ~/.config/systemd/user/yadgar-vacuum-trigger.{path,service} \
#           ~/.config/systemd/user/yadgar-nightly-cycle.{service,timer}
#     systemctl --user daemon-reload && yadgar-setup
# Units are regenerated wholesale on every install and never migrated, so
# re-running an OLDER yadgar-setup is a full repair rather than a patch-up.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The lowest rendered-unit schema this wrapper is willing to install. Bumped only
# on a breaking shape change, in lockstep with UNIT_SCHEMA_VERSION in
# yadgar/core/daemon/unit_install.py.
MIN_UNIT_SCHEMA=1

RUNTIME="${YADGAR_RUNTIME:-}"

# ── Runtime detection (if not set) ───────────────────────────────────────────

if [[ -z "${RUNTIME}" ]]; then
    if [[ -x "${SCRIPT_DIR}/detect_runtime.sh" ]]; then
        RUNTIME="$(bash "${SCRIPT_DIR}/detect_runtime.sh" 2>/dev/null)" || {
            echo "ERROR: Could not detect container runtime. Install podman or docker." >&2
            exit 1
        }
    else
        # Fallback inline detection
        if command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
            RUNTIME="podman"
        elif command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
            RUNTIME="docker"
        else
            echo "ERROR: No container runtime found. Install podman or docker." >&2
            exit 1
        fi
    fi
fi
export YADGAR_RUNTIME="${RUNTIME}"

# ── Renderer resolution ──────────────────────────────────────────────────────
#
# Prefer the CO-SHIPPED renderer (plan §7). This script installs to
# <prefix>/share/yadgar/scripts/ via pyproject's shared-data mapping, so the
# `yadgar` console script from the SAME install is at ../../../bin/yadgar.
# Resolving that first means wrapper and renderer are one install in the common
# case and version skew never arises at all; `command -v` is the fallback for a
# curl-piped installer running against a separately installed CLI.

RENDERER=()

_resolve_renderer() {
    if [[ -n "${YADGAR_RENDERER_CLI:-}" ]]; then
        read -r -a RENDERER <<< "${YADGAR_RENDERER_CLI}"
        return 0
    fi
    local co_shipped="${SCRIPT_DIR}/../../../bin/yadgar"
    if [[ -x "${co_shipped}" && -f "${co_shipped}" ]]; then
        RENDERER=("${co_shipped}")
        return 0
    fi
    local found
    if found="$(command -v yadgar 2>/dev/null)"; then
        RENDERER=("${found}")
        return 0
    fi
    return 1
}

_fail_renderer() {
    echo "ERROR: $1" >&2
    echo "  This installer renders no unit files itself (ADR-0190); the unit" >&2
    echo "  definitions ship with the yadgar package, and unit schema >= ${MIN_UNIT_SCHEMA}" >&2
    echo "  is required." >&2
    echo "  Tried: \$YADGAR_RENDERER_CLI, ${SCRIPT_DIR}/../../../bin/yadgar, 'command -v yadgar'." >&2
    echo "  Install or upgrade one with:" >&2
    echo "      pipx install --force yadgar" >&2
    echo "  ...then re-run setup. Or point \$YADGAR_RENDERER_CLI at a current install." >&2
    echo "  Nothing was written — the existing units are untouched." >&2
    exit 1
}

_resolve_renderer || _fail_renderer "no yadgar renderer found."

# THREE arms, not one. A genuinely old renderer does not implement
# --print-schema AT ALL: it exits non-zero on an argparse error, or prints
# something that is not a number. A naive `if schema < MIN` lets exactly the case
# this check exists for fall straight through. Note the assignment is NOT
# `local schema=$(...)` — `local` swallows the command's exit status, which would
# silently re-open the same hole.
#
# `tail -n 1` because only the LAST line is the answer: a renderer whose startup
# ever writes to stdout (a deprecation notice, an observability banner) would
# otherwise look unparseable and abort a perfectly healthy install.
schema=""
if ! schema="$("${RENDERER[@]}" daemon render-units --print-schema 2> /dev/null | tail -n 1)"; then
    _fail_renderer "${RENDERER[0]} does not support 'daemon render-units --print-schema' (too old)."
fi
schema="${schema//[[:space:]]/}"
if [[ ! "${schema}" =~ ^[0-9]+$ ]]; then
    _fail_renderer "${RENDERER[0]} reported an unparseable unit schema: '${schema}' (too old)."
fi
if ((schema < MIN_UNIT_SCHEMA)); then
    _fail_renderer "${RENDERER[0]} reports unit schema ${schema}, below the required ${MIN_UNIT_SCHEMA}."
fi

# ── Delegate ─────────────────────────────────────────────────────────────────
# The renderer reads the same environment contract documented above, resolves
# @VACUUM_EXEC@ / @NIGHTLY_EXEC@, enforces the DP5 nix guard, stages + validates
# all nine units and only then moves them into place.

exec "${RENDERER[@]}" daemon render-units
