"""The FK-ordering invariant's NEVER-SKIP half — pure stdlib, source-level.

WHY THIS FILE EXISTS AND IS NOT A DUPLICATE.
``test_mariadb_migrations.py`` opens with ``pytest.importorskip("alembic")``,
so its render-based twin
(``test_every_inline_fk_references_an_already_created_table``) evaporates the
moment the ``sql`` extra is absent — and ``yadgar/tests/skip_inventory.json``
sanctions exactly that skip (``engine2-alembic-extra-absent-01``), noting that
``yadgar-ci`` "has no auto-sync pipeline". The CI image tag is a repo variable,
so nothing IN the repository can prove the running image carries the extra.

A gate that may not run is the vacuous pass this train exists to kill (ADR-0080:
a gate that cannot fail is worse than no gate). Car H set the precedent and its
inventory note states the principle outright — "this car exists to kill vacuous
passes, so its own coverage must not evaporate when an extra is missing". So the
invariant gets a second, independent route to the same fact: the rendered DDL is
one witness, the revision SOURCE is another, and this half imports nothing
outside the standard library.

The two halves are deliberately NOT shared code. A single parser reused twice
would fail identically twice; two independent readings of the same invariant is
the point.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Reached by path, not by import: ``yadgar._shared.storage.sql.migrate``
# imports alembic at module scope, which is precisely what this half refuses to
# depend on.
_VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "_shared" / "storage" / "sql" / "migrations" / "versions"
)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings.

    Revisions name their tables through constants (``CLIENT_TABLE = "client"``),
    so the table argument of ``op.create_table`` is often a ``Name``.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _literal_str(node: ast.AST, constants: dict[str, str]) -> str | None:
    """Resolve a string-valued expression, or ``None`` when it cannot be.

    Handles the three shapes the revisions actually use: a plain literal, a
    module constant, and an f-string built from them — ``004`` writes its FK
    target as ``f"{CLIENT_TABLE}.name"``. ``None`` is never treated as "no FK":
    the tests below hard-fail on an unresolved name rather than skipping it,
    because a parser that quietly sees nothing passes vacuously.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            resolved = (
                _literal_str(value.value, constants)
                if isinstance(value, ast.FormattedValue)
                else _literal_str(value, constants)
            )
            if resolved is None:
                return None
            parts.append(resolved)
        return "".join(parts)
    return None


def _call_name(node: ast.Call) -> str:
    """``op.create_table`` → ``create_table``; bare ``foo()`` → ``foo``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _inline_fk_targets(call: ast.Call, constants: dict[str, str]) -> list[str | None]:
    """Parent table names of every ``sa.ForeignKeyConstraint`` inside a create_table.

    The second positional argument is the referent list — ``["client.name"]``.
    The table half is everything before the first dot. ``None`` marks a target
    the resolver could not read, which the caller must treat as a failure.
    """
    targets: list[str | None] = []
    for node in ast.walk(call):
        if not isinstance(node, ast.Call) or _call_name(node) != "ForeignKeyConstraint":
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.List):
            targets.append(None)
            continue
        for element in node.args[1].elts:
            referent = _literal_str(element, constants)
            targets.append(None if referent is None else referent.split(".")[0].strip("`"))
    return targets


def _revision_chain() -> list[Path]:
    """Revision files ordered by ``down_revision``, root first.

    The filenames sort correctly today, but the chain is the actual contract —
    ``alembic upgrade head`` walks ``down_revision``, not the directory listing.
    """
    by_revision: dict[str, tuple[Path, str | None]] = {}
    for path in _VERSIONS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        revision: str | None = None
        down: str | None = None
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            if node.value is None:
                continue
            if node.target.id == "revision":
                revision = _literal_str(node.value, {})
            elif node.target.id == "down_revision":
                down = _literal_str(node.value, {})
        assert revision, f"{path.name} declares no revision id"
        by_revision[revision] = (path, down)

    children = {down: rev for rev, (_, down) in by_revision.items()}
    ordered: list[Path] = []
    cursor: str | None = None  # the root's down_revision is None
    while cursor in children:
        revision = children[cursor]
        ordered.append(by_revision[revision][0])
        cursor = revision
    assert len(ordered) == len(by_revision), (
        f"chain walk reached {len(ordered)} of {len(by_revision)} revisions — "
        "a fork or a broken down_revision link"
    )
    return ordered


def _created_tables_and_inline_fks() -> list[tuple[str, list[str | None]]]:
    """``(table, [parent, ...])`` per ``op.create_table``, in chain order.

    ``op.create_foreign_key`` — the ``003`` shape — is deliberately not read:
    it emits an ``ALTER TABLE`` that runs after both CREATEs, so it is exempt
    for the same reason the render half's scan stops at CREATE-TABLE bodies.
    """
    blocks: list[tuple[str, list[str | None]]] = []
    for path in _revision_chain():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        upgrade = next(
            (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"),
            None,
        )
        assert upgrade is not None, f"{path.name} has no upgrade()"
        calls = [n for n in ast.walk(upgrade) if isinstance(n, ast.Call)]
        calls.sort(key=lambda n: (n.lineno, n.col_offset))
        for call in calls:
            if _call_name(call) != "create_table" or not call.args:
                continue
            table = _literal_str(call.args[0], constants)
            assert table is not None, f"{path.name}: unresolved create_table name"
            blocks.append((table.lower(), _inline_fk_targets(call, constants)))
    return blocks


# The resolver is asserted on synthetic source as well as on the live chain.
# Mutation-tested during C1: disabling the f-string branch left every
# chain-level test GREEN, because C0's fix moved 004's ``f"{CLIENT_TABLE}.name"``
# into op.create_foreign_key and the corpus now contains no f-string referent at
# all. A branch no input exercises is a branch that rots silently, and the next
# revision to write one would be read as "no FK here".


def _parse_expr(source: str) -> ast.expr:
    body = ast.parse(source).body[0]
    assert isinstance(body, ast.Expr)
    return body.value


def test_the_resolver_reads_a_plain_literal():
    assert _literal_str(_parse_expr('"client.name"'), {}) == "client.name"


def test_the_resolver_reads_a_module_constant():
    assert _literal_str(_parse_expr("CLIENT_TABLE"), {"CLIENT_TABLE": "client"}) == "client"


def test_the_resolver_reads_an_f_string_built_from_a_constant():
    """``004`` wrote its referent this way before C0 moved it to an ALTER."""
    resolved = _literal_str(_parse_expr('f"{CLIENT_TABLE}.name"'), {"CLIENT_TABLE": "client"})
    assert resolved == "client.name"


def test_the_resolver_refuses_what_it_cannot_read():
    """A computed name resolves to None — which callers must treat as a failure."""
    assert _literal_str(_parse_expr('"".join(parts)'), {}) is None
    assert _literal_str(_parse_expr("UNKNOWN_CONSTANT"), {}) is None


def test_the_fk_reader_resolves_an_f_string_referent():
    """End-to-end over a synthetic create_table, not just the resolver."""
    call = _parse_expr(
        'op.create_table(TBL, sa.ForeignKeyConstraint(["client"], [f"{CLIENT_TABLE}.name"]))'
    )
    assert isinstance(call, ast.Call)
    assert _inline_fk_targets(call, {"TBL": "agent_pattern_model", "CLIENT_TABLE": "client"}) == [
        "client"
    ]


def test_the_fk_reader_reports_an_unreadable_referent_as_none():
    call = _parse_expr('op.create_table("t", sa.ForeignKeyConstraint(["c"], [some_name()]))')
    assert isinstance(call, ast.Call)
    assert _inline_fk_targets(call, {}) == [None]


def test_the_source_parser_reads_the_chain_it_claims_to_check():
    """Coverage assertion — a parser that sees nothing passes vacuously.

    Pins the two facts the ordering test below is worthless without: the chain
    walk reaches every revision file, and the FK reader finds real inline
    constraints rather than an empty list.
    """
    assert len(_revision_chain()) == len(list(_VERSIONS_DIR.glob("*.py")))
    blocks = _created_tables_and_inline_fks()
    assert {"task", "adr", "client", "agent_pattern_model"} <= {t for t, _ in blocks}
    assert sum(len(fks) for _, fks in blocks) >= 6, "no inline FKs parsed — the reader is blind"


def test_every_inline_fk_target_resolves():
    """An unreadable target is a failure, never a silently-skipped FK.

    ``004`` writes its referent as ``f"{CLIENT_TABLE}.name"``; if the f-string
    branch of the resolver regressed, that FK would vanish from the check and
    the ordering test below would go green on a broken chain.
    """
    unresolved = [table for table, fks in _created_tables_and_inline_fks() if None in fks]
    assert not unresolved, f"unresolved FK target in create_table for: {sorted(unresolved)}"


def test_every_inline_fk_references_an_already_created_table():
    """Same invariant as the render half, read from the revision SOURCE.

    InnoDB rejects a CREATE TABLE whose inline FK names a table that does not
    exist yet (errno 150), so ``alembic upgrade head`` dies at backend boot.
    """
    created: set[str] = set()
    violations: list[tuple[str, str]] = []
    for table, parents in _created_tables_and_inline_fks():
        for parent in parents:
            assert parent is not None  # test_every_inline_fk_target_resolves owns this
            if parent.lower() != table and parent.lower() not in created:
                violations.append((table, parent.lower()))
        created.add(table)
    assert not violations, (
        "inline FK references a table created LATER in the chain (InnoDB errno "
        "150) — create the parent first, or add the FK with op.create_foreign_key "
        "after both tables exist (the 003 shape): "
        + ", ".join(f"{child} → {parent}" for child, parent in violations)
    )
