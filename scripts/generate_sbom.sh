#!/usr/bin/env bash
# Generate CycloneDX 1.5 SBOM for yadgar.
#
# Uses cyclonedx-bom (installed via `pip install yadgar[sbom]` or `pip install cyclonedx-bom`).
# Note: cyclonedx-py on PyPI is an alias for cyclonedx-bom; use cyclonedx-bom directly.
#
# Usage:
#   ./scripts/generate_sbom.sh [--output <path>]
#
# Environment variables:
#   SBOM_OUTPUT   Output path (default: dist/yadgar-<version>-sbom.cdx.json)
#   SBOM_VERSION  Version string override (default: read from pyproject.toml)
#
# Exit codes:
#   0  success
#   1  cyclonedx-bom not found or generation failed

set -euo pipefail

# ── helpers ───────────────────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── argument parsing ──────────────────────────────────────────────────────────

OUTPUT_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) OUTPUT_ARG="$2"; shift 2 ;;
        --help|-h)
            cat <<'EOF'
Usage: generate_sbom.sh [--output <path>]

Generate a CycloneDX 1.5 SBOM for the current yadgar environment.

Options:
  --output <path>   Output file path (default: dist/yadgar-<version>-sbom.cdx.json)

Environment variables:
  SBOM_OUTPUT       Override output path
  SBOM_VERSION      Override version string

Prerequisites:
  pip install 'yadgar[sbom]'   (installs cyclonedx-bom==7.3.0)
EOF
            exit 0
            ;;
        *) die "Unknown argument: $1" ;;
    esac
done

# ── check prerequisites ───────────────────────────────────────────────────────

if ! command -v cyclonedx-bom &>/dev/null; then
    die "cyclonedx-bom not found. Install with: pip install 'yadgar[sbom]' (or cyclonedx-bom==7.3.0)"
fi

# ── version detection ─────────────────────────────────────────────────────────

if [ -n "${SBOM_VERSION:-}" ]; then
    VERSION="$SBOM_VERSION"
else
    VERSION=$(python3 -c "
import re, sys
toml = open('${REPO_ROOT}/pyproject.toml').read()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', toml, re.MULTILINE)
if not m:
    sys.exit(1)
print(m.group(1))
" 2>/dev/null) || die "Cannot read version from pyproject.toml"
fi

# ── output path ───────────────────────────────────────────────────────────────

if [ -n "${OUTPUT_ARG}" ]; then
    OUTPUT="$OUTPUT_ARG"
elif [ -n "${SBOM_OUTPUT:-}" ]; then
    OUTPUT="$SBOM_OUTPUT"
else
    OUTPUT="${REPO_ROOT}/dist/yadgar-${VERSION}-sbom.cdx.json"
fi

# Ensure dist/ exists
mkdir -p "$(dirname "$OUTPUT")"

# ── generate SBOM ─────────────────────────────────────────────────────────────

echo "==> Generating CycloneDX 1.5 SBOM for yadgar ${VERSION}..."
echo "    Output: ${OUTPUT}"

cyclonedx-bom environment \
    --output-format json \
    --schema-version 1.5 \
    --output-file "${OUTPUT}"

echo "==> SBOM generated: ${OUTPUT}"

# ── basic validation ──────────────────────────────────────────────────────────

python3 -c "
import json, sys
data = json.load(open('${OUTPUT}'))
if 'bomFormat' not in data and 'components' not in data:
    print('ERROR: Output does not look like a valid CycloneDX SBOM', file=sys.stderr)
    sys.exit(1)
print(f'    Validation: OK (bomFormat={data.get(\"bomFormat\", \"unknown\")}, components={len(data.get(\"components\", []))})')
"
