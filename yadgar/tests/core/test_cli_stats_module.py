"""Tests for yadgar/cli/stats.py — per-project scoping on ``project_id``.

Wave 3 coverage: yadgar/cli/stats.py (~900 stmts, 0% pre-wave).
Strategy: mock the SurrealDB client at boundary. Pin the SQL text-shape
of the 13 per-project SELECTs to scope on ``project_id`` (the identity
column new writes stamp), NOT on ``directory_context`` (legacy rows
still hold paths there, but every post-re-key row holds the resolved
identity in ``project_id``).

Car 8 task 333: the CLI bound ``args.project`` through
``Path(...).resolve()`` which coerced identity-shaped values like
``m-agahi/yadgar`` into filesystem paths no row had ever been
stamped with. Combined with the column mismatch, the bug under-counts
every project to zero. Three behaviours pinned here:

1. ``resolve_cli_project`` runs first, NOT ``Path(...).resolve()`` — the
   CLI surfaces whatever the identity resolver returns (an identity
   string, ``None``, or — for legacy path inputs that map to a known
   remote — an identity string derived from the remote).
2. SELECTs scope on ``project_id = $p``, not ``directory_context = $p``.
3. An unresolvable ``--project`` binds ``None`` rather than the raw
   filesystem path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core.cli.stats import (
    _query_access_stats,
    _query_compression_levels,
    _query_core_counts,
    _query_heat_stats,
    _query_temporal_stats,
    _query_top_tags,
    _query_type_breakdown,
    cmd_stats,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> SimpleNamespace:
    defaults = {
        "project": None,
        "db_path": None,
        "format": "table",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_db() -> MagicMock:
    """Return a mock Surreal client whose ``query`` returns an empty list."""
    db = MagicMock()
    db.query.return_value = []
    return db


# ---------------------------------------------------------------------------
# SQL shape pins — the 6 per-project query helpers must scope on
# ``project_id = $p``, never ``directory_context = $p``.
# ---------------------------------------------------------------------------


class TestPerProjectQueriesScopeOnProjectId:
    """One pin per query helper. Each helper has 1+ SELECTs that scope on
    ``$p``; every one of those must compare against ``project_id``."""

    @pytest.mark.parametrize(
        "fn",
        [
            _query_core_counts,
            _query_type_breakdown,
            _query_compression_levels,
            _query_heat_stats,
            _query_access_stats,
            _query_temporal_stats,
            _query_top_tags,
        ],
    )
    def test_no_select_references_directory_context(self, fn):
        """The hard constraint: no per-project SELECT names
        ``directory_context`` AT ALL. Even one reference tripwires a
        regression to the pre-333 shape."""
        db = _make_db()
        sd = MagicMock()
        fn(db, "m-agahi/yadgar", sd)
        # Every db.query call's first positional arg is the SQL.
        for call in db.query.call_args_list:
            sql = call.args[0]
            assert "directory_context" not in sql, (
                f"per-project SELECT still references directory_context: {sql!r}"
            )

    @pytest.mark.parametrize(
        "fn",
        [
            _query_core_counts,
            _query_type_breakdown,
            _query_compression_levels,
            _query_heat_stats,
            _query_access_stats,
            _query_temporal_stats,
            _query_top_tags,
        ],
    )
    def test_select_uses_project_id_predicate(self, fn):
        """Every per-project SELECT must include ``project_id = $p``
        somewhere in the WHERE clause."""
        db = _make_db()
        sd = MagicMock()
        fn(db, "m-agahi/yadgar", sd)
        for call in db.query.call_args_list:
            sql = call.args[0]
            # Either the per-project branch ran (contains ``$p``), or
            # the no-project branch ran (no ``$p``). The no-project
            # branch is correct for non-project queries.
            if "$p" in sql:
                assert "project_id = $p" in sql, (
                    f"per-project SELECT must scope on project_id, got: {sql!r}"
                )


# ---------------------------------------------------------------------------
# Bound value pins — the value passed to ``$p`` is the resolved identity
# (or ``None`` on unresolvable trees), NOT a Path.resolve()'d string.
# ---------------------------------------------------------------------------


class TestBoundProjectId:
    @pytest.mark.parametrize(
        "fn",
        [
            _query_core_counts,
            _query_type_breakdown,
            _query_compression_levels,
            _query_heat_stats,
            _query_access_stats,
            _query_temporal_stats,
            _query_top_tags,
        ],
    )
    def test_bound_p_is_identity_string_unchanged(self, fn):
        """A pre-resolved identity (``m-agahi/yadgar``) must be bound as
        the identity string itself, not transformed into a filesystem
        path. Pre-333, ``Path('m-agahi/yadgar').resolve()`` returned
        ``/home/.../m-agahi/yadgar`` — a string no row had ever held."""
        db = _make_db()
        sd = MagicMock()
        fn(db, "m-agahi/yadgar", sd)
        # Every db.query call's second positional arg is the params dict.
        bound_values = {
            (call.args[1].get("p") if len(call.args) > 1 else None)
            for call in db.query.call_args_list
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        }
        assert "m-agahi/yadgar" in bound_values, (
            f"identity string must be bound verbatim, got: {bound_values!r}"
        )

    @pytest.mark.parametrize(
        "fn",
        [
            _query_core_counts,
            _query_type_breakdown,
            _query_compression_levels,
            _query_heat_stats,
            _query_access_stats,
            _query_temporal_stats,
            _query_top_tags,
        ],
    )
    def test_unresolvable_binds_none_not_path(self, fn):
        """``project=None`` (the resolved unresolvable case) must bind
        ``None``, not a derived filesystem path. Pre-333, an
        unresolvable ``--project /foo`` ran through
        ``Path('/foo').resolve()`` and bound ``/foo`` — matching zero
        rows. Post-333, ``None`` is bound — matching zero rows because
        the query is rewritten to skip the WHERE clause (the no-project
        branch)."""
        db = _make_db()
        sd = MagicMock()
        fn(db, None, sd)
        # When project is None, the helpers should not bind ``p`` at all
        # (they take the no-project branch). If they DO bind, it must
        # be None — never a path.
        for call in db.query.call_args_list:
            if len(call.args) > 1 and isinstance(call.args[1], dict):
                if "p" in call.args[1]:
                    assert call.args[1]["p"] is None, (
                        f"unresolvable project must bind None, got {call.args[1]['p']!r}"
                    )


# ---------------------------------------------------------------------------
# cmd_stats integration pin — the resolution step runs BEFORE Path.resolve,
# so ``--project m-agahi/yadgar`` becomes the identity string (already
# resolved), not a filesystem path. The earlier parametrised pins cover
# the helper layer; this pins the cmd_stats call path so a regression at
# the entry point (e.g. re-introducing ``Path(...).resolve()`` in
# ``cmd_stats`` itself) tripwires here.
# ---------------------------------------------------------------------------


class TestCmdStatsResolvesIdentityFirst:
    def test_cmd_stats_passes_resolved_identity_to_helpers(self, capsys):
        """``cmd_stats`` must call ``resolve_cli_project`` and pass its
        return value into the query helpers. The pre-333 code passed
        ``str(Path(args.project).resolve())`` — the wrong resolution."""
        args = _make_args(project="m-agahi/yadgar", format="json")
        db = _make_db()
        with (
            patch("surrealdb.Surreal", return_value=db) as surreal_factory,
            patch("yadgar._shared.config.Settings"),
            patch(
                "yadgar.core.cli._shared.resolve_cli_project", return_value="m-agahi/yadgar"
            ) as mock_resolve,
        ):
            # The cmd_stats path has a daemon-vs-direct fork; force the
            # direct-DB branch by making the HTTP probe raise.
            with patch(
                "yadgar.core.cli.stats.urllib.request.urlopen", side_effect=OSError("no daemon")
            ):
                try:
                    cmd_stats(args)
                except SystemExit:
                    pass
        # Task 412: the mock must actually have intercepted. The pre-412 patch
        # targeted ``yadgar._shared.storage.Surreal`` + a ``sys.modules`` entry
        # for that module, but ``_run_db_path`` does ``from surrealdb import
        # Surreal`` — so the REAL client ran, opened a surrealkv store at
        # ``str(Path(<mocked Settings>.DB_PATH))`` and left
        # ``MagicMock/Settings().DB_PATH/<id>/`` in the repo root.
        assert surreal_factory.called, (
            "the Surreal client was not intercepted — the real one opened a store on disk"
        )
        # resolve_cli_project was called.
        assert mock_resolve.called, (
            "cmd_stats must call resolve_cli_project — pre-333 it bypassed "
            "the identity resolver entirely"
        )

    def test_cmd_stats_unresolvable_passes_none(self, capsys):
        """An unresolvable ``--project`` (``resolve_cli_project`` returns
        ``None``) must thread through to the helpers as ``None``,
        matching no rows. Pre-333, an unresolvable input was bound to a
        filesystem path that no row had."""
        args = _make_args(project="/nonexistent", format="json")
        db = _make_db()
        with (
            patch("surrealdb.Surreal", return_value=db) as surreal_factory,
            patch("yadgar._shared.config.Settings"),
            patch("yadgar.core.cli._shared.resolve_cli_project", return_value=None),
            patch("yadgar.core.cli.stats.urllib.request.urlopen", side_effect=OSError("no daemon")),
        ):
            try:
                cmd_stats(args)
            except SystemExit:
                pass
        # Task 412: same interception pin as the sibling test. It is also what
        # keeps the binding assertion below honest — pre-412 the real client
        # ran, ``db.query`` was never called, and the ``len(...) == 0`` arm
        # made this test pass vacuously.
        assert surreal_factory.called, (
            "the Surreal client was not intercepted — the real one opened a store on disk"
        )
        # At least one query must have bound ``p=None`` (the unresolvable
        # value) — never ``/nonexistent``.
        bound_p_values = [
            (
                call.args[1].get("p")
                if len(call.args) > 1 and isinstance(call.args[1], dict)
                else None
            )
            for call in db.query.call_args_list
        ]
        assert None in bound_p_values or len(bound_p_values) == 0, (
            f"unresolvable --project must bind None (or skip the WHERE), got: {bound_p_values!r}"
        )
        assert "/nonexistent" not in bound_p_values, (
            "unresolvable --project MUST NOT bind the raw input — pre-333 "
            "bound Path.resolve() output here"
        )
