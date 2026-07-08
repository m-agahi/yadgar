#!/usr/bin/env bash
# Yadgar local dev environment bootstrap.
#
# Creates a Python 3.14 venv at ./.venv, installs yadgar in editable mode
# with the [test,ml,dev] extras, and wires up the pre-commit hook.
#
# Usage:
#   ./scripts/setup-dev.sh
#   ./scripts/setup-dev.sh --recreate   # nuke ./.venv and start fresh
#
# Requirements:
#   - python3.14 on PATH (on NixOS: `nix-shell -p python314`)
#   - git working tree with pyproject.toml

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
PY="${PYTHON:-python3.14}"

recreate=0
for arg in "$@"; do
  case "$arg" in
    --recreate) recreate=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY not on PATH. Install python 3.14 first." >&2
  exit 1
fi

if [ "$recreate" -eq 1 ] && [ -d "$VENV" ]; then
  echo "removing $VENV"
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  echo "creating venv at $VENV using $PY"
  "$PY" -m venv "$VENV"
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"

python -m pip install -U pip wheel

# Editable install + test, ml, dev extras. The dev extra already pulls in test,
# but listing it explicitly makes the intent obvious and survives extras refactors.
pip install -e "$ROOT[test,ml,dev]"

# Install the repo's pre-commit hooks if pre-commit is available
if command -v pre-commit >/dev/null 2>&1; then
  ( cd "$ROOT" && pre-commit install )
else
  echo "note: pre-commit not on PATH yet; re-run after activating .venv"
fi

echo
echo "Done. Activate the venv with:"
echo "  source $VENV/bin/activate"
echo
echo "Or install direnv and add a .envrc (already in repo) — it will auto-activate."
echo
echo "Smoke test:"
echo "  pytest yadgar/tests/backend/test_consolidation.py -k cooldown"
