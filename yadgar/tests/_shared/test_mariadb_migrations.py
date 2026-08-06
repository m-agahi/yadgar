"""Engine-#2 Alembic chain — asserted WITHOUT a database (car D).

Every assertion here renders the chain offline (``alembic upgrade --sql``, which
needs only a dialect NAME) or reads the parsed revision graph. No connection, no
driver, no server — so the shape of ``config`` is pinned on the yadgar-ci image,
where the integration half cannot run at all.

The zero-rows assertion is the one to keep. ADR-0203 makes "schema only, zero
rows" load-bearing rather than tidy: task 0095's free-re-key window closes on the
first ``config_set``, and this train must leave it open. A prose rule decays; a
test that fails on any ``INSERT`` in the rendered chain does not.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
pytest.importorskip("alembic", reason="alembic not installed (sql extra)")

from yadgar._shared.storage.sql.migrate import (  # noqa: E402
    HEAD,
    SCRIPT_LOCATION,
    build_alembic_config,
    heads,
    render_sql,
    script_directory,
)

EXPECTED_HEAD = "0001_config"


# ── the chain itself ─────────────────────────────────────────────────────


def test_the_environment_ships_inside_the_package():
    """``packages = ["yadgar"]`` puts it in the wheel — no repo checkout needed."""
    assert SCRIPT_LOCATION.is_dir()
    assert (SCRIPT_LOCATION / "env.py").is_file()
    assert (SCRIPT_LOCATION / "versions").is_dir()
    assert "yadgar/_shared/storage/sql/migrations" in SCRIPT_LOCATION.as_posix()


def test_the_chain_has_exactly_one_head():
    """Two heads means a fork, and ``upgrade head`` then fails ambiguously."""
    assert heads() == (EXPECTED_HEAD,)


def test_config_is_the_first_revision():
    """Spine D33(a): ``config`` precedes the ledger tables via down_revision."""
    script = script_directory()
    first = script.get_revision(EXPECTED_HEAD)
    assert first.down_revision is None


def test_the_environment_declares_no_orm_metadata():
    """Hand-written revisions only — no second source of truth for the schema.

    Asserted against ``env.py``'s SOURCE rather than an imported attribute:
    alembic loads ``env.py`` by path with a live ``EnvironmentContext`` pushed,
    and its module body calls ``context.is_offline_mode()``, so importing it as
    an ordinary module raises. That is alembic's design, not a defect.
    """
    source = (SCRIPT_LOCATION / "env.py").read_text(encoding="utf-8")
    assert re.search(r"^target_metadata = None$", source, re.MULTILINE)


# ── the rendered DDL ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return render_sql(f"base:{HEAD}")


def test_upgrade_creates_the_config_table(upgrade_sql):
    assert re.search(r"CREATE TABLE\s+config\b", upgrade_sql, re.IGNORECASE)


def test_key_is_the_primary_key(upgrade_sql):
    """No surrogate id: key-as-PK makes a duplicate row unrepresentable."""
    assert re.search(r"PRIMARY KEY\s*\(\s*`?key`?\s*\)", upgrade_sql, re.IGNORECASE)
    assert re.search(r"`key`\s+VARCHAR\(64\)\s+NOT NULL", upgrade_sql, re.IGNORECASE)


def test_the_four_columns_and_only_those(upgrade_sql):
    """Spine schema §3.1 — key · value · default_value · updated_at."""
    body = upgrade_sql[upgrade_sql.upper().index("CREATE TABLE CONFIG") :]
    body = body[: body.index(";")]
    for column in ("`key`", "value", "default_value", "updated_at"):
        assert column in body, f"{column} missing from the config DDL"
    assert re.search(r"\bvalue\s+TEXT\s+NOT NULL", body, re.IGNORECASE)
    assert re.search(r"\bdefault_value\s+TEXT\s+NOT NULL", body, re.IGNORECASE)


def test_there_is_no_directory_column(upgrade_sql):
    """ADR-0198 / ADR-0207 D2: all knobs are global.

    The removal closes a live hole, so it is asserted rather than assumed —
    MariaDB unique indexes permit unlimited NULLs, so ``UNIQUE(key, directory)``
    never bound the global rows and concurrent writes produced duplicates that
    wedged every later read on ``MultipleResultsFound``.
    """
    assert "directory" not in upgrade_sql.lower()


def test_updated_at_maintains_itself(upgrade_sql):
    assert re.search(
        r"updated_at\s+DATETIME\s+NOT NULL\s+DEFAULT\s+CURRENT_TIMESTAMP"
        r"\s+ON UPDATE\s+CURRENT_TIMESTAMP",
        upgrade_sql,
        re.IGNORECASE,
    )


def test_the_chain_inserts_nothing(upgrade_sql):
    """ADR-0203's zero-rows rule, enforced mechanically rather than by prose.

    ``alembic_version`` is written by alembic itself at run time and never
    appears in a revision body, so the whole rendered chain may contain no
    ``INSERT`` at all. The gate this protects is scoped to ``config``: task
    0095's re-key window closes on the first ``config_set``.
    """
    assert not re.search(r"\bINSERT\s+INTO\s+config\b", upgrade_sql, re.IGNORECASE)
    revision_bodies = [
        p.read_text(encoding="utf-8") for p in (SCRIPT_LOCATION / "versions").glob("*.py")
    ]
    for body in revision_bodies:
        assert "op.bulk_insert" not in body
        assert "INSERT INTO" not in body.upper()


# ── reversibility ────────────────────────────────────────────────────────


def test_downgrade_drops_the_table():
    """Reversible, so it is written rather than excused."""
    sql = render_sql(f"{HEAD}:base", downgrade=True)
    assert re.search(r"DROP TABLE\s+config\b", sql, re.IGNORECASE)


# ── the environment's refusal ────────────────────────────────────────────


def test_online_without_a_connection_refuses_loudly():
    """No URL fallback: asyncmy is async-only and cannot back a sync engine.

    Driven through ``command.upgrade`` — alembic's real loader — so this
    exercises ``env.py`` exactly as the boot path does, minus the connection.
    PR #32 paired ``mysql+asyncmy://`` with a sync ``create_engine`` and failed
    at connect time; a named refusal at the top is the cheaper failure.
    """
    from alembic import command  # noqa: PLC0415

    cfg = build_alembic_config()
    with pytest.raises(RuntimeError, match="caller-supplied connection"):
        command.upgrade(cfg, HEAD)
