#!/usr/bin/env bash
# append_claude_rules.sh — Append yadgar rules fragment to target CLAUDE.md (idempotent).
#
# Environment variables:
#   YADGAR_CLAUDE_MD_TARGET   Path to CLAUDE.md to append to (required)
#   YADGAR_FRAGMENT_PATH      Path to the fragment file (required)
#
# Idempotent: if YADGAR-RULES-BEGIN marker already present, skips append.
set -euo pipefail

TARGET="${YADGAR_CLAUDE_MD_TARGET:-}"
FRAGMENT="${YADGAR_FRAGMENT_PATH:-}"

if [[ -z "$TARGET" ]]; then
    echo "ERROR: YADGAR_CLAUDE_MD_TARGET not set." >&2
    exit 1
fi

if [[ -z "$FRAGMENT" ]]; then
    echo "ERROR: YADGAR_FRAGMENT_PATH not set." >&2
    exit 1
fi

if [[ ! -f "$FRAGMENT" ]]; then
    echo "ERROR: Fragment file not found: $FRAGMENT" >&2
    exit 1
fi

# Idempotency check: skip if marker already present
if grep -qF "YADGAR-RULES-BEGIN" "$TARGET" 2>/dev/null; then
    echo "Yadgar rules already present in $TARGET — skipping."
    exit 0
fi

# Ensure target file exists
if [[ ! -f "$TARGET" ]]; then
    touch "$TARGET"
fi

# Append fragment
echo "" >> "$TARGET"
cat "$FRAGMENT" >> "$TARGET"
echo "Appended yadgar rules to $TARGET"
