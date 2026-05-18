"""Strip infrastructure state from a SurrealDB /export response.

Strips both the action_log table (data that breaks re-import) and user/access
definitions (infrastructure state owned by the backend entrypoint, not user
data).  Importing user definitions would overwrite freshly-bootstrapped users
with stale password hashes, causing authentication failures on restart.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# action_log stripping (original — preserves backward compat)
# ---------------------------------------------------------------------------

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

# Fallback: match a bare TABLE DATA: action_log header (test fixtures).
# Consumes: the header line + any following UPSERT/INSERT action_log rows.
# Stops before: a blank line, next "-- TABLE DATA:" header, or EOF.
# Using [^\n]* per-line matching (no DOTALL) to avoid spanning non-action_log rows.
_ACTION_LOG_DATA_BARE_RE = re.compile(
    r"-- TABLE DATA: action_log\b[^\n]*\n"  # bare header line
    r"(?:[^\n]*action_log[^\n]*\n)*"  # zero or more action_log rows
    r"\n?",  # optional trailing blank line
)

# ---------------------------------------------------------------------------
# User / access definition stripping (v5.1.3)
#
# These are infrastructure state owned by the backend entrypoint script.
# Importing them overwrites freshly-bootstrapped users with stale password
# hashes — causing 401 Unauthorized on the next yadgar startup.
#
# Patterns are anchored to start-of-line (MULTILINE) and use [^;]* (no
# newline crossing, no statement boundary skipping) so that memory content
# which merely *mentions* DEFINE USER is not stripped.
# ---------------------------------------------------------------------------

# DEFINE USER <name> ON (ROOT|NAMESPACE|NS|DATABASE|DB) ... ;
_DEFINE_USER_RE = re.compile(
    r"^DEFINE\s+USER\s+\S+\s+ON\s+(?:ROOT|NAMESPACE|NS|DATABASE|DB)\b[^;]*;\s*\n?",
    re.MULTILINE | re.IGNORECASE,
)

# DEFINE ACCESS <name> ... ; (SurrealDB v2+ combined user+token syntax)
_DEFINE_ACCESS_RE = re.compile(
    r"^DEFINE\s+ACCESS\s+\S+[^;]*;\s*\n?",
    re.MULTILINE | re.IGNORECASE,
)

# REMOVE USER <name> ... ; (defensive — also infra state)
_REMOVE_USER_RE = re.compile(
    r"^REMOVE\s+USER\s+\S+[^;]*;\s*\n?",
    re.MULTILINE | re.IGNORECASE,
)


def strip_export_for_vacuum(surql: str) -> str:
    """Remove infrastructure state from a SurrealDB /export response.

    Strips:
    - action_log table schema and data (breaks re-import).
    - DEFINE USER ... ON ROOT/NAMESPACE/DATABASE statements (stale hash risk).
    - DEFINE ACCESS statements (SurrealDB v2+ user/token syntax).
    - REMOVE USER statements (defensive — also infra state).

    Preserves all other content including table definitions, indexes, fields,
    and all data rows.  Content inside INSERT/UPSERT rows that merely mentions
    DEFINE USER in a string value is NOT stripped — the ^ anchor ensures only
    start-of-line SQL statements are matched.

    Args:
        surql: Raw .surql export content from SurrealDB /export.

    Returns:
        Filtered content safe to POST to /import.
    """
    result = surql

    # -- action_log: real SurrealDB v3.0.5 dashes-block format --
    result = _ACTION_LOG_SCHEMA_BLOCK_RE.sub("", result)
    result = _ACTION_LOG_DATA_BLOCK_RE.sub("", result)

    # -- action_log: fallback for test fixtures / older format --
    result = _ACTION_LOG_DEFINE_RE.sub("", result)
    result = _ACTION_LOG_DATA_BARE_RE.sub("", result)

    # -- User / access definitions --
    result = _DEFINE_USER_RE.sub("", result)
    result = _DEFINE_ACCESS_RE.sub("", result)
    result = _REMOVE_USER_RE.sub("", result)

    return result


def strip_action_log(surql: str) -> str:
    """Back-compat alias for strip_export_for_vacuum.

    .. deprecated::
        Use :func:`strip_export_for_vacuum` directly.  This alias exists so
        callers that imported ``strip_action_log`` continue to work, but now
        also strips user/access definitions.
    """
    return strip_export_for_vacuum(surql)
