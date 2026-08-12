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

EXPECTED_HEAD = "004_agent_pattern_model_client"


# ── the chain itself ─────────────────────────────────────────────────────


def test_the_environment_ships_inside_the_package():
    """``packages = ["yadgar"]`` puts it in the wheel — no repo checkout needed."""
    assert SCRIPT_LOCATION.is_dir()
    assert (SCRIPT_LOCATION / "env.py").is_file()
    assert (SCRIPT_LOCATION / "versions").is_dir()
    assert "yadgar/_shared/storage/sql/migrations" in SCRIPT_LOCATION.as_posix()


def test_the_chain_has_exactly_one_head():
    """Two heads means a fork, and ``upgrade head`` then fails ambiguously.

    Car A of 0047 spine train inserted ``002_ledger_tables`` between
    ``0001_config`` and A0's ``003_project_registry``, but the chain stays
    single-headed.
    """
    assert heads() == (EXPECTED_HEAD,)


def test_002_ledger_tables_descends_from_0001_config():
    """``002_ledger_tables.down_revision == "0001_config"`` — D34 ordering.

    The first car that CREATES a ledger revision chains it off the existing
    chain head (``0001_config``); car A0's ``003_project_registry`` chains
    off ``002_ledger_tables`` and is the current head.
    """
    script = script_directory()
    rev_002 = script.get_revision("002_ledger_tables")
    assert rev_002.down_revision == "0001_config"


def test_003_project_registry_descends_from_002():
    """A0's ``003_project_registry`` chains off Car A's ``002_ledger_tables``."""
    script = script_directory()
    rev_003 = script.get_revision("003_project_registry")
    assert rev_003.down_revision == "002_ledger_tables"


def test_004_agent_pattern_model_client_descends_from_003():
    """Car I's ``004_agent_pattern_model_client`` chains off A0's ``003_project_registry``.

    §3.3 (task 0094): model tier is a property of (pattern × client), never of
    the pattern — the seed corpus's hardcoded ``DISPATCH: model=opus`` lines
    assume Claude Code tiers and are wrong for every other client.
    """
    script = script_directory()
    rev_004 = script.get_revision("004_agent_pattern_model_client")
    assert rev_004.down_revision == "003_project_registry"


def test_004_creates_agent_pattern_model_and_client(upgrade_sql):
    """Car I adds ``agent_pattern_model`` + ``client`` (§3.3) — model tier + lookup."""
    expected = {"agent_pattern_model", "client"}
    found = set(re.findall(r"CREATE TABLE\s+([A-Za-z_]+)", upgrade_sql, re.IGNORECASE))
    missing = expected - {name.lower() for name in found}
    assert not missing, f"004 missing CREATE TABLE for: {sorted(missing)}"


def test_004_agent_pattern_model_has_composite_pk(upgrade_sql):
    """``(pattern_name, client)`` composite PK — model tier is per-pattern × per-client."""
    body = _create_table_body(upgrade_sql, "agent_pattern_model")
    assert re.search(
        r"PRIMARY KEY\s*\(\s*`?pattern_name`?\s*,\s*`?client`?\s*\)",
        body,
        re.IGNORECASE,
    ), "agent_pattern_model missing PRIMARY KEY (pattern_name, client)"


def test_004_agent_pattern_model_fks_cascade(upgrade_sql):
    """CASCADE on both FKs — deleting a pattern or a client wipes its model tiers.

    RESTRICT would leave dangling rows that no resolver could reach; the
    whole reason the table exists is the model tier lookup, so a dangling
    row is a silent miss.
    """
    body = _create_table_body(upgrade_sql, "agent_pattern_model")
    assert re.search(
        r"FOREIGN KEY\s*\(\s*`?pattern_name`?\s*\)\s+REFERENCES\s+`?agent_pattern`?\s*\(\s*`?name`?\s*\)"
        r"\s+ON DELETE CASCADE",
        body,
        re.IGNORECASE,
    ), "agent_pattern_model.pattern_name FK must ON DELETE CASCADE"
    # The client FK is added by ALTER TABLE, not inline: `client` is created by
    # THIS revision, so an inline REFERENCES would be a forward reference and
    # InnoDB rejects it (errno 150). Searched over the whole chain rather than
    # the CREATE TABLE body for that reason — and the ORDER of the two
    # statements is what
    # test_every_inline_fk_references_an_already_created_table owns.
    assert re.search(
        r"ALTER TABLE\s+`?agent_pattern_model`?\s+ADD CONSTRAINT\s+`?fk_agent_pattern_model_client`?"
        r"\s+FOREIGN KEY\s*\(\s*`?client`?\s*\)\s+REFERENCES\s+`?client`?\s*\(\s*`?name`?\s*\)"
        r"\s+ON DELETE CASCADE",
        upgrade_sql,
        re.IGNORECASE,
    ), "agent_pattern_model.client FK must be added by ALTER TABLE and ON DELETE CASCADE"


def test_004_client_name_is_pk(upgrade_sql):
    """``client.name`` is the PK — lookup mirroring CLIENT_REGISTRY."""
    body = _create_table_body(upgrade_sql, "client")
    assert re.search(
        r"PRIMARY KEY\s*\(\s*`?name`?\s*\)",
        body,
        re.IGNORECASE,
    ), "client missing PRIMARY KEY (name)"


def test_config_is_the_first_revision():
    """Spine D33(a): ``config`` precedes the ledger tables via down_revision.

    The chain root (``0001_config``) is the engine-#2 chain head predating
    Car A0's ``003_project_registry`` and Car A's ``002_ledger_tables``.
    Asserted explicitly via ``script.get_revision("0001_config")`` rather
    than via the head, since the head changed when A0 added ``003``.
    """
    script = script_directory()
    first = script.get_revision("0001_config")
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
    0095's re-key window closes on the first ``config_set``. Car A extends
    the gate to every ledger revision (D35a — the seed is a separate one-shot
    admin op, NOT a migration step).
    """
    assert not re.search(r"\bINSERT\s+INTO\s+config\b", upgrade_sql, re.IGNORECASE)
    revision_bodies = [
        p.read_text(encoding="utf-8") for p in (SCRIPT_LOCATION / "versions").glob("*.py")
    ]
    for body in revision_bodies:
        assert "op.bulk_insert" not in body
        assert "INSERT INTO" not in body.upper()


# ── car A: ledger schema (002_ledger_tables) ──────────────────────────────


def test_002_creates_all_seven_tables(upgrade_sql):
    """Car A writes the seven-table ledger schema.

    Each table name is matched on its own CREATE TABLE statement — the test
    intentionally does NOT accept ``CREATE TABLE config, ledger, …`` style
    bulk creation (alembic emits one statement per op.create_table anyway).
    """
    expected = {
        "task",
        "adr",
        "agent_pattern",
        "agent_discipline",
        "task_blocked_by",
        "adr_supersedes",
        "agent_pattern_composes",
    }
    found = set(re.findall(r"CREATE TABLE\s+([A-Za-z_]+)", upgrade_sql, re.IGNORECASE))
    missing = expected - {name.lower() for name in found}
    assert not missing, f"002 missing CREATE TABLE for: {sorted(missing)}"


def test_task_and_adr_carry_project_id_without_fk(upgrade_sql):
    """``project_id`` ships as a plain VARCHAR(256) NOT NULL on task/adr.

    No FK on 002 — car A0's ``003_project_registry`` creates the ``project``
    table and adds the FKs. Asserted via the regex of the rendered DDL (the
    alembic offline render is the only artefact available without a DB).
    The CREATE TABLE body is matched via a balanced-paren walk so nested
    parens in column type declarations do not truncate the match.

    C6 (#17) widened 255 → 256 in place on this unreleased revision: the FK
    target ``project.key`` must agree with the two referencing columns, and
    256 is ADR-0202's slug cap, which the ``body_slug`` columns carry. The
    number is asserted rather than left free because a silent disagreement
    between the FK's two sides is a truncation bug nothing else catches.
    """
    for table in ("task", "adr"):
        body = _create_table_body(upgrade_sql, table)
        assert re.search(
            r"`?project_id`?\s+VARCHAR\(256\)\s+NOT NULL",
            body,
            re.IGNORECASE,
        ), f"{table} missing project_id VARCHAR(256) NOT NULL"
        assert "FOREIGN KEY" not in body.upper(), (
            f"{table} must NOT carry a FOREIGN KEY in 002 — 003_project_registry adds it"
        )


def test_task_and_adr_have_an_index_on_project_id(upgrade_sql):
    """Both ledger rows index ``project_id`` for the project-scoped reads.

    SQLAlchemy / alembic renders ``sa.Index(...)`` as a separate
    ``CREATE INDEX`` statement rather than embedding the index inside
    the CREATE TABLE body — searched at the table-name level.
    """
    for table in ("task", "adr"):
        # The CREATE INDEX statement immediately follows the CREATE TABLE.
        idx_match = re.search(
            rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+\S+\s+ON\s+{table}\s*\(\s*`?project_id`?\s*\)",
            upgrade_sql,
            re.IGNORECASE,
        )
        assert idx_match, f"{table} missing CREATE INDEX on project_id"


def test_agent_pattern_and_agent_discipline_carry_content_hash(upgrade_sql):
    """ADR-0209 / §14.3 — ``content_hash NOT NULL`` + ``baseline_hash NULL``.

    Without them, ``check_page_row_desync`` is a permanent stub and the
    cross-engine invariant check turns into a vacuous pass — the exact
    failure the arm exists to prevent. ``baseline_hash`` is nullable
    (rendered either as ``CHAR(64),`` with no NULL keyword, or as
    ``CHAR(64) NULL``); both pass.
    """
    for table in ("agent_pattern", "agent_discipline"):
        body = _create_table_body(upgrade_sql, table)
        assert re.search(
            r"`?content_hash`?\s+CHAR\(64\)\s+NOT NULL",
            body,
            re.IGNORECASE,
        ), f"{table} missing content_hash CHAR(64) NOT NULL"
        assert re.search(
            r"`?baseline_hash`?\s+CHAR\(64\)(?:\s+NULL)?(?=\s*,|\s*\)|\s*$)",
            body,
            re.IGNORECASE,
        ), f"{table} missing baseline_hash CHAR(64) NULL"


def test_id_is_bigint_auto_increment_pk(upgrade_sql):
    """ADR-0197 / §14.1 — ``id`` IS the AUTO_INCREMENT PK. No separate ``number``."""
    for table in ("task", "adr", "agent_pattern", "agent_discipline"):
        body = _create_table_body(upgrade_sql, table)
        assert re.search(
            r"`?id`?\s+BIGINT\s+UNSIGNED\s+NOT NULL\s+AUTO_INCREMENT",
            body,
            re.IGNORECASE,
        ), f"{table} missing id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT"
        assert re.search(
            r"PRIMARY KEY\s*\(\s*`?id`?\s*\)",
            body,
            re.IGNORECASE,
        ), f"{table} missing PRIMARY KEY (id)"


def test_no_separate_number_column(upgrade_sql):
    """``MAX+1 FOR UPDATE`` / ``UNIQUE(project_id,origin,number)`` all retired.

    Asserted via a name scan: ``number`` must not appear as a column on any
    ledger table. The word may appear in docstrings or comments (test
    corpus), so the regex is constrained to ``CREATE TABLE`` blocks.
    """
    for table in ("task", "adr"):
        body = _create_table_body(upgrade_sql, table)
        assert not re.search(r"\b`?number`?\s+(BIGINT|INT|INTEGER)\b", body, re.IGNORECASE), (
            f"{table} must NOT carry a `number` column — id IS the number"
        )


def test_task_carries_completed_at(upgrade_sql):
    """C15a item 1 — ``task.completed_at DATETIME NULL``.

    The sweep ages completed rows off this column instead of ``updated_at``,
    which bumps on every edit and so reset the 90-day clock on any touched
    row (§14.2). Nullable because a row that has not completed has no
    completion instant, and the sweep reads NULL as "clock never started"
    rather than "infinitely old".
    """
    body = _create_table_body(upgrade_sql, "task")
    assert re.search(
        r"`?completed_at`?\s+DATETIME(?:\s+NULL)?(?=\s*,|\s*\)|\s*$)",
        body,
        re.IGNORECASE,
    ), "task missing completed_at DATETIME NULL"
    assert not re.search(r"`?completed_at`?\s+DATETIME\s+NOT NULL", body, re.IGNORECASE), (
        "task.completed_at must be NULLable — a pending task has no completion instant"
    )


def test_adr_carries_superseded_at(upgrade_sql):
    """C15a item 2 — ``adr.superseded_at DATETIME NULL``.

    Aging superseded ADRs off ``created_at`` gave zero grace: an ADR written
    120 days ago and superseded today archived on the very next sweep. The
    column is nullable because ``rejected`` / ``deprecated`` rows are swept
    too and nothing supersedes them — those fall back to ``created_at``.
    """
    body = _create_table_body(upgrade_sql, "adr")
    assert re.search(
        r"`?superseded_at`?\s+DATETIME(?:\s+NULL)?(?=\s*,|\s*\)|\s*$)",
        body,
        re.IGNORECASE,
    ), "adr missing superseded_at DATETIME NULL"
    assert not re.search(r"`?superseded_at`?\s+DATETIME\s+NOT NULL", body, re.IGNORECASE), (
        "adr.superseded_at must be NULLable — rejected/deprecated rows never carry one"
    )


def test_task_is_unique_on_project_id_and_body_slug(upgrade_sql):
    """C15a item 3 — ``UNIQUE(project_id, body_slug)`` on ``task``.

    Parent-plan D8: one ledger row ↔ one wiki page is the only genuine 1:1,
    so the constraint belongs on ``body_slug`` rather than on the title (two
    tasks may share a name). MySQL permits repeated NULLs in a UNIQUE index,
    so an unslugged row (D4 — ``body_slug`` is NULL until the body page
    exists) is not blocked by it.

    Asserted on the rendered CREATE TABLE rather than on a round-trip
    INSERT because the offline render is the only artefact available without
    a live MariaDB — and it is the DDL, not a row, that carries the claim.
    """
    body = _create_table_body(upgrade_sql, "task")
    assert re.search(
        r"UNIQUE\s*\(\s*`?project_id`?\s*,\s*`?body_slug`?\s*\)",
        body,
        re.IGNORECASE,
    ), "task missing UNIQUE (project_id, body_slug)"


def _balanced_body(sql: str, open_idx: int) -> str | None:
    """Return the text between ``sql[open_idx]`` (a ``(``) and its partner.

    Non-greedy regexes truncate on the first inner ``)`` (column type
    expressions contain them — ``VARCHAR(255)``). A balanced-paren walk that
    skips quoted spans gives the exact body. ``None`` means unbalanced.
    """
    depth = 0
    quote: str | None = None
    i = open_idx
    while i < len(sql):
        c = sql[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return sql[open_idx + 1 : i]
        i += 1
    return None


def _create_table_body(sql: str, table: str) -> str:
    """Return the body of the ``CREATE TABLE <table>(...)`` block."""
    open_match = re.search(rf"CREATE TABLE\s+{table}\s*\(", sql, re.IGNORECASE)
    assert open_match, f"no CREATE TABLE {table} in the rendered chain"
    body = _balanced_body(sql, open_match.end() - 1)  # end()-1 is the '('
    assert body is not None, f"unbalanced parens in CREATE TABLE {table}"
    return body


# ── the chain-wide FK-ordering invariant ─────────────────────────────────
#
# NOT a 004-specific regression test. ``test_004_agent_pattern_model_fks_cascade``
# above regex-asserts the ``REFERENCES client(name) ON DELETE CASCADE`` TEXT and
# PASSES on the migration that InnoDB rejects with errno 150, because only the
# ORDER is wrong — the proof that a regex-on-render gate is insufficient. This
# invariant is positional and applies to every revision, present and future.
#
# An FK added by a later ``ALTER TABLE`` is exempt by construction: the scan only
# looks INSIDE ``CREATE TABLE`` bodies, so ``003_project_registry``'s
# create-table-then-``op.create_foreign_key`` shape — the prescribed fix — never
# reaches the check. That shape is legal precisely because the ALTER runs after
# both tables exist.

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE\s+`?(\w+)`?\s*\(", re.IGNORECASE)
_REFERENCES_RE = re.compile(r"\bREFERENCES\s+`?(\w+)`?", re.IGNORECASE)


def _create_table_blocks(sql: str) -> list[tuple[str, str]]:
    """``(table_name, body)`` for every ``CREATE TABLE``, in emission order.

    Names are lowercased and stripped of backticks: the render quotes only
    reserved words (``REFERENCES project (`key`)``), so both forms occur.
    """
    blocks: list[tuple[str, str]] = []
    for match in _CREATE_TABLE_RE.finditer(sql):
        body = _balanced_body(sql, match.end() - 1)
        assert body is not None, f"unbalanced parens in CREATE TABLE {match.group(1)}"
        blocks.append((match.group(1).lower(), body))
    return blocks


def inline_fk_ordering_violations(sql: str) -> list[tuple[str, str]]:
    """``(child, parent)`` for every inline FK whose parent is not yet created.

    A self-reference is not a violation — InnoDB accepts an FK onto the table
    being created, since the table exists by the time the constraint is applied.
    """
    created: set[str] = set()
    violations: list[tuple[str, str]] = []
    for table, body in _create_table_blocks(sql):
        for ref in _REFERENCES_RE.finditer(body):
            parent = ref.group(1).lower()
            if parent != table and parent not in created:
                violations.append((table, parent))
        created.add(table)
    return violations


def test_every_inline_fk_references_an_already_created_table(upgrade_sql):
    """InnoDB errno 150: an inline FK onto a table the chain creates LATER.

    ``alembic upgrade head`` dies at backend boot on such a chain, and — until
    car C1 of the PR #40 remediation made it fatal — that death was swallowed,
    so the daemon continued onto a schema-less database.
    """
    violations = inline_fk_ordering_violations(upgrade_sql)
    assert not violations, (
        "inline FK references a table created LATER in the chain "
        "(InnoDB errno 150) — create the parent first, or add the FK with "
        "op.create_foreign_key after both tables exist (the 003 shape): "
        + ", ".join(f"{child} → {parent}" for child, parent in violations)
    )


# The invariant's own parser is asserted on synthetic DDL: the live chain
# exercises neither backtick-quoted table names nor a self-reference, so those
# branches would otherwise ship untested — and a parser that silently matches
# nothing is the vacuous pass this gate exists to prevent.


def test_the_invariant_catches_a_forward_inline_fk():
    sql = (
        "CREATE TABLE child (\n  parent_id INTEGER,\n"
        "  CONSTRAINT fk FOREIGN KEY(parent_id) REFERENCES parent (id)\n);\n"
        "CREATE TABLE parent (\n  id INTEGER NOT NULL,\n  PRIMARY KEY (id)\n);\n"
    )
    assert inline_fk_ordering_violations(sql) == [("child", "parent")]


def test_the_invariant_accepts_the_same_two_tables_in_the_right_order():
    sql = (
        "CREATE TABLE parent (\n  id INTEGER NOT NULL,\n  PRIMARY KEY (id)\n);\n"
        "CREATE TABLE child (\n  parent_id INTEGER,\n"
        "  CONSTRAINT fk FOREIGN KEY(parent_id) REFERENCES parent (id)\n);\n"
    )
    assert inline_fk_ordering_violations(sql) == []


def test_the_invariant_reads_backtick_quoted_table_names():
    sql = (
        "CREATE TABLE `child` (\n  parent_id INTEGER,\n"
        "  CONSTRAINT fk FOREIGN KEY(parent_id) REFERENCES `parent` (`key`)\n);\n"
        "CREATE TABLE `parent` (\n  `key` VARCHAR(255) NOT NULL\n);\n"
    )
    assert inline_fk_ordering_violations(sql) == [("child", "parent")]


def test_a_self_reference_is_not_a_violation():
    """InnoDB accepts an FK onto the table being created."""
    sql = (
        "CREATE TABLE node (\n  id INTEGER NOT NULL,\n  parent_id INTEGER,\n"
        "  PRIMARY KEY (id),\n"
        "  CONSTRAINT fk FOREIGN KEY(parent_id) REFERENCES node (id)\n);\n"
    )
    assert inline_fk_ordering_violations(sql) == []


def test_an_alter_table_fk_is_exempt():
    """The 003 shape — the prescribed fix — must not be flagged.

    ``ALTER TABLE`` runs after the CREATE it follows, so an FK added there is
    ordered correctly even when the parent's CREATE comes later in the file
    than the child's.
    """
    sql = (
        "CREATE TABLE child (\n  parent_id INTEGER\n);\n"
        "CREATE TABLE parent (\n  id INTEGER NOT NULL,\n  PRIMARY KEY (id)\n);\n"
        "ALTER TABLE child ADD CONSTRAINT fk FOREIGN KEY(parent_id) REFERENCES parent (id);\n"
    )
    assert inline_fk_ordering_violations(sql) == []


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
