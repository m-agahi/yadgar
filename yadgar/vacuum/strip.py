"""Strip action_log from SurrealDB /export response."""

from __future__ import annotations

import re

# Strip the TABLE: action_log block (schema definition + surrounding dashes)
_ACTION_LOG_SCHEMA_BLOCK_RE = re.compile(
    r"-- -{20,}\n"  # opening dashes line
    r"-- TABLE: action_log\n"  # TABLE comment
    r"-- -{20,}\n"  # closing dashes line
    r"\n?"  # optional blank line
    r".*?"  # schema content (DEFINE TABLE, indexes...)
    r"(?=\n-- -{20,}\n|\Z)",  # stop at next dashes block or EOF
    re.DOTALL,
)

# Strip the TABLE DATA: action_log block (data rows + surrounding dashes)
_ACTION_LOG_DATA_BLOCK_RE = re.compile(
    r"-- -{20,}\n"  # opening dashes line
    r"-- TABLE DATA: action_log\n"  # TABLE DATA comment
    r"-- -{20,}\n"  # closing dashes line
    r"\n?"  # optional blank line
    r".*?"  # INSERT [...]; statement(s)
    r"(?=\n-- -{20,}\n|\Z)",  # stop at next dashes block or EOF
    re.DOTALL,
)

# Fallback: match the single DEFINE TABLE action_log line (test fixtures
# may not use the full dashes format).
_ACTION_LOG_DEFINE_RE = re.compile(
    r"^DEFINE TABLE action_log\b[^\n]*;\s*\n?",
    re.MULTILINE,
)

# Fallback: match a bare TABLE DATA: action_log header (test fixtures)
_ACTION_LOG_DATA_BARE_RE = re.compile(
    r"-- TABLE DATA: action_log\b[^\n]*\n"  # bare header line
    r".*?"  # rows
    r"(?=\n-- TABLE DATA:|\Z)",  # stop at next TABLE DATA or EOF
    re.DOTALL,
)


def strip_action_log(surql: str) -> str:
    """Remove the action_log section from a SurrealDB /export response.

    Handles both the real SurrealDB v3.0.5 dashes-block format and the
    simpler per-line format used in test fixtures.

    The action_log rows contain raw shell-command text that breaks the
    SurrealQL re-parser on import. Both the TABLE: (schema) block and the
    TABLE DATA: (rows) block are stripped.

    Args:
        surql: Raw .surql export content from SurrealDB /export.

    Returns:
        Filtered content safe to POST to /import.
    """
    result = surql

    # -- Real SurrealDB v3.0.5 format: dashes-framed blocks --
    result = _ACTION_LOG_SCHEMA_BLOCK_RE.sub("", result)
    result = _ACTION_LOG_DATA_BLOCK_RE.sub("", result)

    # -- Fallback for test fixtures / older format --
    result = _ACTION_LOG_DEFINE_RE.sub("", result)
    result = _ACTION_LOG_DATA_BARE_RE.sub("", result)

    return result
