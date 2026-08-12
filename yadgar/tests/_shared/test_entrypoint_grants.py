"""D19 — least-privilege grant for the engine-#2 app account (Car A).

Car A of 0047 spine train narrows the ``yadgar_app`` user's grant from
``GRANT ALL PRIVILEGES ON ${MARIADB_DB}.*`` to a per-table list. The shape
is pinned here against ``entrypoint-backend.sh``'s heredocs rather than
against a live MariaDB, because (a) the yadgar-ci image does not have
mysqld and (b) the integration half already exists at
``yadgar/tests/integration/test_mariadb_restore_arm.py`` for the runtime
half.

Each ledger table is a separate ``GRANT`` so an omission surfaces as a
missing line — grep-able in code review and assertable in a unit test,
without needing the actual server to validate the statement is syntactically
correct SQL.

WHAT A TEXT ASSERTION CANNOT SEE, AND WHY THAT MATTERS HERE
-----------------------------------------------------------
This file used to be the ONLY test of the grant block, and it asserted only
that the list NAMES every table. It could not notice two things that made the
named list unreachable:

  * the privilege set contains no ``CREATE``, so the migration that creates
    those tables could not run as the account being granted them;
  * MariaDB REJECTS a table-level ``GRANT`` on a table that does not exist
    (``ERROR 1146``), so the grants had to be applied AFTER the migration —
    and they were applied before it.

Both are ordering/behaviour facts, and only a live server can settle them:
``yadgar/tests/integration/test_mariadb_migrations.py`` replays these exact
heredocs against a real MariaDB. This file now pins the STRUCTURE that
ordering depends on (two named phases with the migration between them) so a
regression is caught in the fast suite as well.
"""

from __future__ import annotations

import pytest

from yadgar.tests import _entrypoint_sql as eps

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
    "agent_pattern_model",
    "client",
    "task_blocked_by",
    "adr_supersedes",
    "agent_pattern_composes",
    "project",
)

# DDL the runtime account must NEVER hold (D19).
FORBIDDEN_APP_PRIVILEGES = ("CREATE", "ALTER", "INDEX", "DROP")


def _read_entrypoint_sql_block() -> str:
    """Every bootstrap statement the entrypoint runs, both phases concatenated.

    Kept as one string for the assertions that only care about presence. The
    ORDERING assertions below deliberately do not use it.
    """
    return eps.heredoc(eps.ACCOUNTS_SENTINEL) + "\n" + eps.heredoc(eps.GRANTS_SENTINEL)


def test_entrypoint_revokes_legacy_broad_grant():
    """D19 — the entrypoint REVOKEs the prior broad grant before the narrow GRANTs.

    MariaDB's GRANT is additive — a fresh narrow grant does NOT replace a
    prior broad one. The REVOKE removes ``ALL PRIVILEGES, GRANT OPTION``
    on the user itself (which clears every grant it owned) so the
    per-table list that follows is the COMPLETE grant set.
    """
    body = eps.heredoc(eps.GRANTS_SENTINEL)
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
    # The heredoc is unquoted (``<<MDBGRANTSEOF``), so the SQL identifier
    # backticks are escaped as ``\``...\.`` to suppress shell
    # command-substitution. The broad grant's pattern was the same shape.
    assert (
        "GRANT ALL PRIVILEGES ON \\`${MARIADB_DB}\\`.* "
        "TO '${MARIADB_APP_USER}'@'localhost'" not in body
    ), "D19 — must not carry a broad GRANT on the whole database"
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
    body = eps.heredoc(eps.GRANTS_SENTINEL)
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


# ── the ordering the 1146 rejection forces ───────────────────────────────────


def test_the_bootstrap_is_split_into_two_named_phases():
    """Accounts and grants are separate heredocs, and both exist.

    Not cosmetic: they must be separated by a step that is not SQL (the
    Alembic chain), which a single heredoc cannot express. The distinct
    sentinels also keep the extractor honest — a naive "first heredoc in the
    file" lookup would silently read the wrong block after the split and keep
    passing.
    """
    assert eps.has_heredoc(eps.ACCOUNTS_SENTINEL)
    assert eps.has_heredoc(eps.GRANTS_SENTINEL)
    sentinels = eps.heredoc_sentinels()
    assert sentinels.index(eps.ACCOUNTS_SENTINEL) < sentinels.index(eps.GRANTS_SENTINEL)
    assert eps.LEGACY_SENTINEL not in sentinels, (
        "the combined SQLEOF heredoc is the pre-fix shape — grants applied "
        "before any table existed, so MariaDB rejected the first one with "
        "1146 and aborted every statement after it"
    )


def test_the_migration_runs_between_the_two_phases():
    """Phase B sits between A and C — the whole point of the split.

    Position, not presence. A migration step defined but invoked after the
    grants leaves the bug exactly where it was.
    """
    assert eps.runs_migration_between_the_phases(), (
        f"{eps.MIGRATION_COMMAND} must be invoked between the accounts heredoc "
        f"and the grants heredoc: MariaDB rejects GRANT on a table that does "
        f"not exist yet (1146), so the tables have to be created in between"
    )


def test_phase_a_grants_no_table_level_privilege():
    """Nothing in the accounts phase may name a table.

    Every statement there runs against a possibly-EMPTY database. One
    table-level GRANT is a 1146 that aborts the accounts themselves — the
    fresh-install failure mode: "engine #2 present but unusable by the app".
    """
    for stmt in eps.statements(eps.heredoc(eps.ACCOUNTS_SENTINEL)):
        if not stmt.upper().startswith("GRANT"):
            continue
        target = stmt.split(" ON ", 1)[1].split(" TO ", 1)[0].strip()
        assert target.endswith(".*"), (
            f"phase A grant must be database-scoped (no table can exist yet), got: {target}"
        )


def test_the_migration_account_exists_and_holds_ddl():
    """A THIRD account owns DDL, so the runtime account never has to.

    The app account cannot run migrations (no CREATE) and the admin account
    cannot be used by the driver at all (socket auth; asyncmy implements no
    ``unix_socket`` plugin). The migration account is the credential that
    closes that gap.
    """
    body = eps.heredoc(eps.ACCOUNTS_SENTINEL)
    assert "CREATE USER IF NOT EXISTS '${MARIADB_MIGRATE_USER}'@'localhost'" in body
    grant = next(
        stmt
        for stmt in eps.statements(body)
        if stmt.startswith("GRANT") and "${MARIADB_MIGRATE_USER}" in stmt
    )
    privileges = grant.split("GRANT", 1)[1].split(" ON ", 1)[0]
    for needed in ("CREATE", "ALTER", "INDEX", "DROP"):
        assert needed in privileges, f"the migration account needs {needed}"
    assert "GRANT OPTION" not in grant, "the migration account must not be able to grant"
    assert "ALL PRIVILEGES" not in privileges, (
        "spell the migration privileges out — 'run migrations, nothing else' "
        "should be readable from the statement"
    )


@pytest.mark.parametrize("privilege", FORBIDDEN_APP_PRIVILEGES)
def test_the_app_account_is_never_granted_ddl(privilege):
    """D19's actual invariant, asserted against BOTH phases.

    This is the mutation guard on the fix: making the migration work by
    handing the runtime account ``CREATE`` would turn every other test in this
    train green and delete the property the train exists to protect.
    """
    for sentinel in (eps.ACCOUNTS_SENTINEL, eps.GRANTS_SENTINEL):
        for stmt in eps.statements(eps.heredoc(sentinel)):
            if not stmt.startswith("GRANT") or "${MARIADB_APP_USER}" not in stmt:
                continue
            if "_restorecheck" in stmt:
                continue  # car G's throwaway restore schema — deliberately ALL
            privileges = stmt.split("GRANT", 1)[1].split(" ON ", 1)[0]
            assert privilege not in privileges, (
                f"D19 — the runtime account must never hold {privilege}: {stmt}"
            )


def test_phase_b_names_the_migration_option_file_explicitly():
    """The DDL run's credentials are chosen by the entrypoint, not by a ladder.

    The first cut of this fix set ``YADGAR_MARIADB_CLIENT_CNF`` — a variable
    ``default_migrate_option_file_path()`` does not read — and worked only
    because ``MARIADB_MIGRATE_CNF`` happened to be exported. Dropping that
    export would have silently resolved the DDL run's credentials through the
    APP account's ladder. Passing the path as an argument removes the choice.
    """
    command = eps.migration_command_line()
    assert "yadgar._shared.storage.sql.migrate" in command
    assert '"${MARIADB_MIGRATE_CNF}"' in command, (
        f"phase B must pass the migration option file as an argument; got: {command}"
    )
    assert "YADGAR_MARIADB_CLIENT_CNF" not in command, (
        "that variable names the APP account's option file and is not on the "
        "migration ladder at all"
    )
