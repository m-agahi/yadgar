"""Read the engine-#2 bootstrap SQL out of ``entrypoint-backend.sh``.

WHY THIS EXISTS
---------------
Two test files need the SAME bootstrap SQL and must not carry two copies of it.
``yadgar/tests/_shared/test_entrypoint_grants.py`` asserts the grant SHAPE
without a server; ``yadgar/tests/integration/test_mariadb_migrations.py``
REPLAYS the whole bootstrap against a live MariaDB so the migration chain runs
under production's actual privileges. A second hardcoded copy of the grant list
in the integration test would drift from the entrypoint, and a drifted copy
tests nothing — which is exactly how the DDL-less migration shipped: the
integration fixture provisioned its user with the stock image's
``MARIADB_USER=yadgar_app``, which grants ALL PRIVILEGES on the database. Same
username, opposite privileges, so every test passed against a privilege set
production has never had.

WHY IT PARSES SHELL RATHER THAN IMPORTING A CONSTANT
----------------------------------------------------
The entrypoint is the production artifact. Anything the tests read from
somewhere else can be right while the entrypoint is wrong. Parsing the shipped
file means the assertion is about what the container actually runs.

THE HEREDOCS ARE NAMED
----------------------
``_bootstrap_mariadb_accounts`` emits its SQL in two separate heredocs with
DISTINCT sentinels, because the two halves must be separated by a step that is
not SQL at all (the Alembic chain — see ``MIGRATION_COMMAND``):

    MDBACCOUNTSEOF   phase A — database + accounts (no table-level grants)
    MDBGRANTSEOF     phase C — the app account's per-table narrowing grants

Ordering is not cosmetic. MariaDB REJECTS a table-level ``GRANT`` naming a
table that does not exist (``ERROR 1146 (42S02)``), and the client runs without
``--force``, so one such statement aborts the rest of the heredoc. Phase C can
therefore only run AFTER the tables exist.

The legacy single-``SQLEOF`` shape is still recognised so a test can be written
against the shipped-and-broken entrypoint and watched to fail.
"""

from __future__ import annotations

import re
from pathlib import Path

ENTRYPOINT: Path = Path(__file__).resolve().parent.parent.parent / "entrypoint-backend.sh"

# Phase A / phase C sentinels, in execution order.
ACCOUNTS_SENTINEL = "MDBACCOUNTSEOF"
GRANTS_SENTINEL = "MDBGRANTSEOF"

# The pre-split shape: one heredoc carrying accounts AND grants together.
LEGACY_SENTINEL = "SQLEOF"

# Phase B — the shell function that runs the Alembic chain between the two
# heredocs. Named here so a test can assert it is INVOKED between them rather
# than merely defined.
MIGRATION_COMMAND = "_migrate_engine_two_schema"


def entrypoint_text() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def heredoc_sentinels() -> tuple[str, ...]:
    """Every heredoc sentinel in the file, in the order the bodies appear."""
    return tuple(
        m.group(1) for m in re.finditer(r"<<([A-Za-z_][A-Za-z0-9_]*)\b", entrypoint_text())
    )


def heredoc(sentinel: str) -> str:
    """The body of the ``<<SENTINEL`` heredoc, verbatim (no substitution).

    Raises:
        KeyError: the entrypoint has no heredoc with that sentinel.
    """
    text = entrypoint_text()
    opener = f"<<{sentinel}"
    start = text.find(opener)
    if start < 0:
        raise KeyError(f"entrypoint-backend.sh has no <<{sentinel} heredoc")
    body_start = text.index("\n", start + len(opener)) + 1
    end = text.index(f"\n{sentinel}", body_start)
    return text[body_start:end]


def has_heredoc(sentinel: str) -> bool:
    try:
        heredoc(sentinel)
    except KeyError:
        return False
    return True


def render(sql: str, substitutions: dict[str, str]) -> str:
    """Turn a heredoc body into SQL a client can execute.

    Two transformations, both of them undoing what the SHELL requires rather
    than editing the SQL:

    * ``${NAME}`` is replaced from *substitutions*. An unknown ``${NAME}``
      raises — a silently-unsubstituted variable would make the rendered SQL
      address a database literally called ``${MARIADB_DB}``, and the test would
      pass against the wrong schema.
    * ``\\`` becomes ``` ` ```. The heredoc is unquoted, so every SQL identifier
      backtick is backslash-escaped in the source to suppress shell command
      substitution.
    """
    unknown = {
        name
        for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", sql)
        if name not in substitutions
    }
    if unknown:
        raise KeyError(f"no substitution supplied for: {sorted(unknown)}")
    for name, value in substitutions.items():
        sql = sql.replace(f"${{{name}}}", value)
    return sql.replace("\\`", "`")


def statements(sql: str) -> list[str]:
    """Split rendered SQL into statements, dropping ``--`` comment lines."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def accounts_sql(substitutions: dict[str, str]) -> str:
    """Phase A — rendered. Falls back to the legacy combined heredoc."""
    sentinel = ACCOUNTS_SENTINEL if has_heredoc(ACCOUNTS_SENTINEL) else LEGACY_SENTINEL
    return render(heredoc(sentinel), substitutions)


def grants_sql(substitutions: dict[str, str]) -> str:
    """Phase C — rendered, or ``""`` when the entrypoint has not been split yet.

    The empty string is what makes a RED run meaningful: against the shipped
    single-heredoc entrypoint the whole bootstrap is phase A, and replaying it
    reproduces production's failure rather than a parse error.
    """
    if not has_heredoc(GRANTS_SENTINEL):
        return ""
    return render(heredoc(GRANTS_SENTINEL), substitutions)


def migration_command_line() -> str:
    """The command phase B actually runs, as written in the entrypoint.

    Returned so a test can assert the SHAPE the container invokes rather than
    a shape a test invented. The two diverged once already in this train: the
    entrypoint set ``YADGAR_MARIADB_CLIENT_CNF`` — a variable the migration
    ladder does not read — and worked only because a different variable
    happened to be exported.

    Raises:
        KeyError: the entrypoint defines no migration step.
    """
    text = entrypoint_text()
    marker = f"{MIGRATION_COMMAND}() {{"
    start = text.find(marker)
    if start < 0:
        raise KeyError(f"entrypoint-backend.sh defines no {MIGRATION_COMMAND}")
    body = text[start : text.index("\n}", start)]
    for line in body.splitlines():
        if "python3 -m" in line and not line.lstrip().startswith("#"):
            return line.strip()
    raise KeyError(f"{MIGRATION_COMMAND} runs no python3 -m command")


def runs_migration_between_the_phases() -> bool:
    """True when phase B is INVOKED between phase A's heredoc and phase C's.

    Position, not presence: a migration step defined but called after the
    grants would leave the 1146 ordering bug in place.
    """
    text = entrypoint_text()
    if not (has_heredoc(ACCOUNTS_SENTINEL) and has_heredoc(GRANTS_SENTINEL)):
        return False
    accounts_end = text.index(f"\n{ACCOUNTS_SENTINEL}")
    grants_start = text.index(f"<<{GRANTS_SENTINEL}")
    between = text[accounts_end:grants_start]
    # The definition line (``_migrate_engine_two_schema() {``) must not count.
    return any(
        MIGRATION_COMMAND in line and not line.lstrip().startswith(f"{MIGRATION_COMMAND}()")
        for line in between.splitlines()
    )
