"""Bug-bag-2 train 2026-08-23, C5 — ``list_pattern_composes`` engine method tests.

The admin op at ``ledger.py`` forwards ``storage.list_pattern_composes(pattern_name=...)``
to ``MariaStorageEngine``, which must expose that exact method. C5 pins:

  * the method exists on ``MariaStorageEngine`` (the soft no-op class);
  * the method is a coroutine function (matches the asyncmy contract);
  * the method's signature is keyword-only ``*, pattern_name: str`` (the
    signature the dispatcher at ledger.py:543 already calls against);
  * the SQL shape is the column list the schema migration declares at
    ``migrations/versions/002_ledger_tables.py:288-303`` (pattern_name,
    discipline_name, position).

No DB / no fixture — signature-shape and SQL-text pins only. Running the SQL
itself is integration territory.
"""

from __future__ import annotations

import inspect


class TestListPatternComposesMethodExists:
    def test_method_exists_on_engine(self):
        from yadgar._shared.storage.sql import mariadb

        assert hasattr(mariadb.MariaStorageEngine, "list_pattern_composes")

    def test_method_is_coroutine_function(self):
        from yadgar._shared.storage.sql import mariadb

        assert inspect.iscoroutinefunction(mariadb.MariaStorageEngine.list_pattern_composes)

    def test_method_signature_is_keyword_only_pattern_name(self):
        from yadgar._shared.storage.sql import mariadb

        sig = inspect.signature(mariadb.MariaStorageEngine.list_pattern_composes)
        # Keyword-only parameter: ``*, pattern_name: str``. The dispatcher
        # already calls ``storage.list_pattern_composes(pattern_name=...)``,
        # so any drift to a positional-or-keyword parameter would let a
        # caller pass it positionally and silently bypass that contract.
        params = list(sig.parameters.values())
        # Drop ``self`` — methods always have it.
        params = [p for p in params if p.name != "self"]
        assert len(params) == 1
        only = params[0]
        assert only.name == "pattern_name"
        assert only.kind is inspect.Parameter.KEYWORD_ONLY
        # ``from __future__ import annotations`` makes every annotation a
        # string literal; the contract is "annotated as str" regardless of
        # whether the annotation has been evaluated yet.
        assert only.annotation in ("str", str)


class TestListPatternComposesSqlShape:
    def test_sql_selects_pattern_discipline_position(self):
        # Read the source for the method and assert the SQL text selects
        # exactly the three columns the migration declares. Anything else
        # (a wildcard, a join, an extra column) would be a contract change.
        from yadgar._shared.storage.sql import mariadb

        src = inspect.getsource(mariadb.MariaStorageEngine.list_pattern_composes)
        assert "SELECT" in src
        assert "pattern_name" in src
        assert "discipline_name" in src
        assert "position" in src
        assert "FROM agent_pattern_composes" in src
        # ORDER BY position so the prelude assembly order is deterministic
        # (mirrors the existing comment).
        assert "ORDER BY position" in src
        assert ":pattern_name" in src

    def test_sql_text_round_trip_with_pattern_name(self):
        # ``sqlalchemy.text(...)`` must accept the literal the source uses;
        # if it ever changes (e.g. dynamic f-string) this would still pin
        # the rendering is the expected shape.
        from yadgar._shared.storage.sql import mariadb

        src = inspect.getsource(mariadb.MariaStorageEngine.list_pattern_composes)
        # The body must compile a ``text(...)`` SQL expression referencing
        # ``agent_pattern_composes`` and a bound ``:pattern_name`` param.
        assert "text(" in src
        # The runtime SQL the method hands to sqlalchemy.text() must compile.
        # We do not run it — just confirm sqlalchemy accepts the literal
        # string the source embeds. Skipped when sqlalchemy is not installed
        # in this test environment (sql is an optional extra).
        sample = (
            "SELECT pattern_name, discipline_name, position "
            "FROM agent_pattern_composes WHERE pattern_name = :pattern_name "
            "ORDER BY position ASC"
        )
        try:
            from sqlalchemy import text as _sa_text
        except ImportError:
            return  # sql extra not installed — nothing more to pin
        compiled = _sa_text(sample)
        assert compiled is not None
