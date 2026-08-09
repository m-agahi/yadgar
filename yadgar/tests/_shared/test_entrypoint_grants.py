"""D19 — least-privilege grant for the engine-#2 app account (Car A).

Car A of 0047 spine train narrows the ``yadgar_app`` user's grant from
``GRANT ALL PRIVILEGES ON ${MARIADB_DB}.*`` to a per-table list. The shape
is pinned here against ``entrypoint-backend.sh``'s heredoc rather than
against a live MariaDB, because (a) the yadgar-ci image does not have
mysqld and (b) the integration half already exists at
``yadgar/tests/integration/test_mariadb_restore_arm.py`` for the runtime
half.

Each ledger table is a separate ``GRANT`` so an omission surfaces as a
missing line — grep-able in code review and assertable in a unit test,
without needing the actual server to validate the statement is syntactically
correct SQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).parent.parent.parent.parent / "entrypoint-backend.sh"

# Ledger tables that the app user MUST have a per-table grant on. The list is
# the chokepoint's surface; a table missing from this set has no grant and
# the app cannot read or write it.
EXPECTED_GRANT_TABLES = (
    "alembic_version",
    "config",
    "task",
    "adr",
    "agent_pattern",
    "agent_discipline",
    "task_blocked_by",
    "adr_supersedes",
    "agent_pattern_composes",
    "project",
)


def _read_entrypoint_sql_block() -> str:
    """Return the ``SQLEOF`` heredoc body from ``_bootstrap_mariadb_accounts``.

    The heredoc contains the GRANT statements the app account receives.
    Pinning the body text lets a unit test detect drift.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    # The heredoc body sits between ``<<SQLEOF`` and the next ``SQLEOF``
    # terminator on its own line.
    start = text.index("<<SQLEOF") + len("<<SQLEOF")
    end = text.index("\nSQLEOF", start)
    return text[start:end]


def test_entrypoint_revokes_legacy_broad_grant():
    """D19 — the entrypoint REVOKEs the prior broad grant before the narrow GRANTs.

    MariaDB's GRANT is additive — a fresh narrow grant does NOT replace a
    prior broad one. The REVOKE removes ``ALL PRIVILEGES, GRANT OPTION``
    on the user itself (which clears every grant it owned) so the
    per-table list that follows is the COMPLETE grant set.
    """
    body = _read_entrypoint_sql_block()
    assert "REVOKE ALL PRIVILEGES, GRANT OPTION" in body, (
        "D19 entrypoint must REVOKE the legacy broad grant before the "
        "narrow per-table grants — additive GRANT semantics leave a stale "
        "ALL PRIVILEGES otherwise"
    )


def test_entrypoint_grants_are_per_table_not_database_wildcard():
    """The app account owns a per-table list, NOT ``${MARIADB_DB}.*``.

    The pre-car-A grant was ``GRANT ALL PRIVILEGES ON \\`${MARIADB_DB}\\`.*``
    — omnipotent across the database. Car A replaces that with one grant
    per ledger table.
    """
    body = _read_entrypoint_sql_block()
    # The heredoc is unquoted (``<<SQLEOF``), so the SQL identifier
    # backticks are escaped as ``\``...\.`` to suppress shell
    # command-substitution. The broad grant's pattern was the same shape.
    assert "GRANT ALL PRIVILEGES ON \\`${MARIADB_DB}\\`.*" not in body, (
        "D19 — must not carry a broad GRANT on the whole database"
    )
    # The restorecheck grant stays — its pattern (car G) is intentional.
    # Two backslashes in source per literal ``\``; the body has
    # ``\`` + ``_restorecheck`` + ``\`` + ``_%``.
    assert "GRANT ALL PRIVILEGES ON \\`${MARIADB_DB}\\\\_restorecheck\\\\_%\\`.*" in body


@pytest.mark.parametrize("table", EXPECTED_GRANT_TABLES)
def test_entrypoint_grants_a_per_table_privilege(table):
    """Each ledger table appears in its own GRANT statement.

    ``GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON \\`<db>\\`.table``
    is the per-table privilege shape. The narrow grant is deliberate —
    a missing table surfaces as a missing ``GRANT`` line, not as a runtime
    permission error.
    """
    body = _read_entrypoint_sql_block()
    # Backslash-backticks (``\``...\.``) in the heredoc — the file's
    # SQL identifiers escape the backticks for shell.
    expected = (
        f"GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON "
        f"\\`${{MARIADB_DB}}\\`.{table} TO '${{MARIADB_APP_USER}}'@'localhost';"
    )
    assert expected in body, (
        f"entrypoint-backend.sh must grant {table!r} to the app user; "
        f"the missing line lets the app account fail silently at first read"
    )
