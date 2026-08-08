"""TDD tests for v5.27.0 DuckDB analytics export.

Written BEFORE implementation (red-first per HARD RULE — Test-Driven).

§secret_gate note: v5.10.2 secret-gate operates at write-time (SecretLeakBlocked
raised by gate_or_reject before any row is stored). There is no `secret_flag`
column on memory rows. The --include-secrets flag is reserved for forward-compat
with future row-level tagging schemas; today its default (exclude) is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_duckdb(tmp_path: Path) -> Path:
    """Provide a path for a temp DuckDB file (not yet created)."""
    return tmp_path / "test_snapshot.duckdb"


@pytest.fixture()
def tmp_db(tmp_path: Path) -> str:
    """Provide a path for a temp SurrealDB instance (surrealkv)."""
    return str(tmp_path / "test_surreal.db")


@pytest.fixture()
def seeded_storage(tmp_db: str):
    """StorageEngine with one row in each exported table.

    This fixture uses the standard StorageEngine(path) pattern from the
    existing test suite (mirrors test_restoration.py, test_write_policy.py).
    """
    from yadgar._shared.storage import StorageEngine

    storage = StorageEngine(tmp_db, embedding_dim=384)

    # memory — core table, one row with a known embedding
    storage._q(
        "CREATE memory SET content = $c, heat = $h, tags = $t, "
        "embedding = $e, directory_context = $d, "
        "access_count = 1, useful_count = 1, "
        "created_at = time::now(), last_accessed = time::now(), "
        "is_stale = false, surprise_score = 0.5, importance = 0.7",
        {
            "c": "test memory content",
            "h": 0.8,
            "t": ["tag1", "tag2"],
            "e": [0.0] * 384,
            "d": "/test/project",
        },
    )
    # wiki_page
    storage._q(
        "CREATE wiki_page SET slug = $s, content = $c, title = $t, "
        "tags = ['wiki'], approved = true, created_at = time::now(), "
        "directory_context = '/test/sandbox'",
        {"s": "test-page", "c": "wiki content", "t": "Test Page"},
    )
    # wiki_crossref
    storage._q(
        "CREATE wiki_crossref SET from_slug = $f, to_slug = $t",
        {"f": "test-page", "t": "other-page"},
    )
    # action_log
    storage._q(
        "CREATE action_log SET tool = $tool, ts = time::now(), processed = false",
        {"tool": "recall"},
    )
    # consolidation_log
    storage._q(
        "CREATE consolidation_log SET timestamp = time::now(), "
        "memories_added = 1, memories_archived = 0, "
        "memories_updated = 0, memories_deleted = 0, duration_ms = 100",
    )
    # entity
    storage._q(
        "CREATE entity SET name = $n, entity_type = $t",
        {"n": "TestEntity", "t": "concept"},
    )
    # relationship
    storage._q(
        "CREATE relationship SET from_entity = 'entity:1', "
        "to_entity = 'entity:2', rel_type = 'relates_to'",
    )
    # causal_dag_edge
    storage._q(
        "CREATE causal_dag_edge SET source = 'memory:1', target = 'memory:2', weight = 0.5",
    )
    # memory_cluster
    storage._q(
        "CREATE memory_cluster SET cluster_id = 1, "
        "centroid_label = 'test cluster', member_count = 1",
    )
    # memory_archive
    storage._q(
        "CREATE memory_archive SET content = $c, archived_at = time::now()",
        {"c": "archived content"},
    )
    # memory_transition
    storage._q(
        "CREATE memory_transition SET memory_id = 'memory:1', "
        "from_tier = 'working', to_tier = 'archive', "
        "transitioned_at = time::now()",
    )
    # narrative_entry
    storage._q(
        "CREATE narrative_entry SET content = $c, created_at = time::now()",
        {"c": "narrative content"},
    )
    # derived_belief
    storage._q(
        "CREATE derived_belief SET belief = $b, confidence = 0.9, created_at = time::now()",
        {"b": "test belief"},
    )
    # user_profile
    storage._q(
        "CREATE user_profile SET attribute = $a, value = $v",
        {"a": "name", "v": "Test User"},
    )
    # memory_rule
    storage._q(
        "CREATE memory_rule SET rule_text = $r, is_active = true",
        {"r": "test rule"},
    )
    # prospective_memory
    storage._q(
        "CREATE prospective_memory SET content = $c, "
        "trigger_condition = $t, is_active = true, triggered_count = 0",
        {"c": "future task", "t": "when_needed"},
    )
    # memory_similarity_link — N1 fix v5.46.7: delete before insert to avoid
    # unique index memory_sim_link_pair_idx violation on repeated fixture use.
    storage._q(
        "DELETE memory_similarity_link WHERE source_memory_id = 'memory:1' "
        "AND target_memory_id = 'memory:2'",
    )
    storage._q(
        "CREATE memory_similarity_link SET source_memory_id = 'memory:1', "
        "target_memory_id = 'memory:2', similarity = 0.95",
    )

    yield storage
    storage.close()


@pytest.fixture()
def exported_db(seeded_storage, tmp_duckdb: Path, tmp_db: str):
    """Run the exporter against seeded_storage; return path to output .duckdb."""
    pytest.importorskip("duckdb", reason="duckdb not installed")

    from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

    cfg = ExportConfig(embedding_dim=384)
    exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
    exporter.run()
    return tmp_duckdb


# ---------------------------------------------------------------------------
# Table-level tests
# ---------------------------------------------------------------------------


class TestExporterCreatesTables:
    """Exported tables exist with expected row counts."""

    def test_memory_table_exists(self, exported_db: Path):
        import duckdb

        con = duckdb.connect(str(exported_db), read_only=True)
        rows = con.execute("SELECT count(*) FROM memory").fetchone()
        con.close()
        assert rows[0] == 1

    def test_wiki_page_table_exists(self, exported_db: Path):
        import duckdb

        con = duckdb.connect(str(exported_db), read_only=True)
        rows = con.execute("SELECT count(*) FROM wiki_page").fetchone()
        con.close()
        assert rows[0] == 1

    def test_all_exported_tables_present(self, exported_db: Path):
        import duckdb

        expected = {
            "memory",
            "wiki_page",
            "wiki_crossref",
            "action_log",
            "consolidation_log",
            "entity",
            "relationship",
            "causal_dag_edge",
            "memory_cluster",
            "memory_archive",
            "memory_transition",
            "narrative_entry",
            "derived_belief",
            "user_profile",
            "memory_rule",
            "prospective_memory",
            "schema_version",
            "memory_similarity_link",
        }
        con = duckdb.connect(str(exported_db), read_only=True)
        result = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
        ).fetchall()
        con.close()
        actual = {r[0] for r in result}
        missing = expected - actual
        assert not missing, f"Missing tables: {missing}"


class TestExporterSkippedTablesNotPresent:
    """Explicitly excluded tables must not appear in the output."""

    def test_excluded_tables_absent(self, exported_db: Path):
        import duckdb

        excluded = {
            "counter",
            "checkpoint",
            "engram_slot",
            "file_hash",
            "astrocyte_process",
            "memory_embedding_backup",
        }
        con = duckdb.connect(str(exported_db), read_only=True)
        result = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
        ).fetchall()
        con.close()
        actual = {r[0] for r in result}
        present = excluded & actual
        assert not present, f"Should be excluded but present: {present}"


# ---------------------------------------------------------------------------
# Secret-gate test
# ---------------------------------------------------------------------------


class TestExporterRespectsSecretGate:
    """--include-secrets flag forwarded; today is a no-op (write-time gate)."""

    def test_include_secrets_false_default(self, seeded_storage, tmp_duckdb, tmp_db):
        """Default run completes; no rows silently dropped (no secret_flag column)."""
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(include_secrets=False, embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()
        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        count = con.execute("SELECT count(*) FROM memory").fetchone()[0]
        con.close()
        assert count == 1

    def test_include_secrets_true_same_count(self, seeded_storage, tmp_duckdb, tmp_db):
        """With --include-secrets, same row count (no secret_flag to filter)."""
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(include_secrets=True, embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()
        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        count = con.execute("SELECT count(*) FROM memory").fetchone()[0]
        con.close()
        assert count == 1


# ---------------------------------------------------------------------------
# action_log windowing
# ---------------------------------------------------------------------------


class TestExporterActionLogWindow:
    """--action-log-since and --action-log-limit are honored."""

    def test_action_log_exported_by_default(self, exported_db: Path):
        """Recent action_log row appears in output."""
        import duckdb

        con = duckdb.connect(str(exported_db), read_only=True)
        count = con.execute("SELECT count(*) FROM action_log").fetchone()[0]
        con.close()
        assert count >= 1

    def test_action_log_limit_zero(self, seeded_storage, tmp_duckdb, tmp_db):
        """limit=0 → empty action_log table."""
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(action_log_limit=0, embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()
        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        count = con.execute("SELECT count(*) FROM action_log").fetchone()[0]
        con.close()
        assert count == 0

    def test_action_log_time_window_excludes_old_rows(self, tmp_db, tmp_duckdb):
        """action-log-since 30d: old row (60d ago) excluded, fresh row included."""
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar._shared.storage import StorageEngine

        storage = StorageEngine(tmp_db, embedding_dim=384)
        # Insert old row: 60 days ago (ISO string — SurrealDB accepts it)
        storage._q(
            "CREATE action_log SET tool = 'recall', processed = false, "
            "ts = <datetime>'2026-04-01T00:00:00Z'",
        )
        # Insert fresh row: now
        storage._q(
            "CREATE action_log SET tool = 'memorize', processed = false, ts = time::now()",
        )
        storage.close()

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(action_log_since="30d", embedding_dim=384, create_views=False)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()

        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        rows = con.execute("SELECT tool FROM action_log ORDER BY tool").fetchall()
        con.close()
        tools = [r[0] for r in rows]
        assert "memorize" in tools, "fresh row should be included"
        assert "recall" not in tools, "60-day-old row should be excluded by 30d window"


# ---------------------------------------------------------------------------
# Embedding roundtrip
# ---------------------------------------------------------------------------


class TestExporterEmbeddingRoundtrip:
    """Embedding stored as FLOAT[dim], cosine similarity computable."""

    def test_embedding_readable(self, exported_db: Path):
        import duckdb

        con = duckdb.connect(str(exported_db), read_only=True)
        row = con.execute("SELECT embedding FROM memory LIMIT 1").fetchone()
        con.close()
        assert row is not None
        emb = row[0]
        assert emb is not None, "embedding should not be NULL"
        assert len(emb) == 4, f"expected dim 4, got {len(emb)}"
        assert abs(emb[0] - 0.1) < 1e-4

    def test_cosine_similarity_computable(self, exported_db: Path):
        import duckdb

        con = duckdb.connect(str(exported_db), read_only=True)
        # array_cosine_similarity is available natively in DuckDB >= 0.10
        result = con.execute(
            f"SELECT list_cosine_similarity(embedding, {[0.0] * 384}::FLOAT[384]) "
            "FROM memory LIMIT 1"
        ).fetchone()
        con.close()
        assert result is not None
        assert result[0] is not None
        assert abs(result[0] - 1.0) < 0.05  # near-identical vectors → cosine ~1


# ---------------------------------------------------------------------------
# extra_fields
# ---------------------------------------------------------------------------


class TestExporterExtraFields:
    """Unknown field in SurrealDB row lands in extra_fields JSON, not lost."""

    def test_extra_fields_captured(self, seeded_storage, tmp_duckdb, tmp_db):
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import json

        import duckdb

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        # Insert a memory row with an unknown field
        seeded_storage._q(
            "CREATE memory SET content = $c, heat = 0.5, "
            f"embedding = {[0.0] * 384}, "
            "directory_context = '/test', "
            "created_at = time::now(), last_accessed = time::now(), "
            "tags = [], is_stale = false, "
            "unknown_future_field = 'surprise_value'",
            {"c": "extra field test"},
        )
        cfg = ExportConfig(action_log_since="all", embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()
        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        rows = con.execute(
            "SELECT extra_fields FROM memory WHERE content = 'extra field test' LIMIT 1"
        ).fetchall()
        con.close()
        assert rows, "row not found"
        extra = rows[0][0]
        assert extra is not None, "extra_fields should not be NULL for row with unknown field"
        parsed = json.loads(extra) if isinstance(extra, str) else extra
        assert "unknown_future_field" in parsed


# ---------------------------------------------------------------------------
# Missing table handling
# ---------------------------------------------------------------------------


class TestExporterHandlesMissingTable:
    """Table absent in SurrealDB → exporter warns + writes empty DuckDB table."""

    def test_missing_table_produces_empty_table(self, tmp_db, tmp_duckdb):
        """Export with only memory seeded; all other tables absent → empty, no error."""
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar._shared.storage import StorageEngine

        storage = StorageEngine(tmp_db, embedding_dim=384)
        storage._q(
            f"CREATE memory SET content = 'minimal', heat = 0.5, "
            f"embedding = {[0.0] * 384}, "
            "directory_context = '/test', "
            "created_at = time::now(), last_accessed = time::now(), "
            "tags = [], is_stale = false",
        )
        storage.close()

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(create_views=False, embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()  # must not raise

        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        # wiki_page was never seeded → empty table
        count = con.execute("SELECT count(*) FROM wiki_page").fetchone()[0]
        con.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class TestViewsCreated:
    """All 10 analytics views exist when --no-views not set."""

    def test_all_views_present(self, exported_db: Path):
        import duckdb

        expected_views = {
            "v_decay_distribution",
            "v_recall_efficacy_by_tag",
            "v_anchor_usage",
            "v_high_heat_memories",
            "v_domain_clustering",
            "v_consolidation_effect",
            "v_conflict_density",
            "v_wiki_coverage",
            "v_tool_call_volume",
        }
        con = duckdb.connect(str(exported_db), read_only=True)
        result = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'VIEW'"
        ).fetchall()
        con.close()
        actual = {r[0] for r in result}
        missing = expected_views - actual
        assert not missing, f"Missing views: {missing}"


class TestViewsExecutable:
    """Every view executes without error on a fixture-populated file."""

    @pytest.mark.parametrize(
        "view_name",
        [
            "v_decay_distribution",
            "v_recall_efficacy_by_tag",
            "v_anchor_usage",
            "v_high_heat_memories",
            "v_domain_clustering",
            "v_consolidation_effect",
            "v_conflict_density",
            "v_wiki_coverage",
            "v_tool_call_volume",
        ],
    )
    def test_view_executes(self, exported_db: Path, view_name: str):
        import duckdb

        con = duckdb.connect(str(exported_db), read_only=True)
        # Should not raise; ≥0 rows acceptable
        rows = con.execute(f"SELECT * FROM {view_name} LIMIT 10").fetchall()  # noqa: S608
        con.close()
        assert isinstance(rows, list)


class TestNoViewsFlag:
    """--no-views → views not created."""

    def test_no_views_skips_view_creation(self, seeded_storage, tmp_duckdb, tmp_db):
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(create_views=False, embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()
        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        result = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'VIEW'"
        ).fetchall()
        con.close()
        assert len(result) == 0


# ---------------------------------------------------------------------------
# CLI lazy-import / exit codes
# ---------------------------------------------------------------------------


class TestCliLazyImport:
    """CLI exits 2 with helpful message when duckdb not installed."""

    def test_missing_duckdb_exits_2(self, tmp_path, capsys):
        # Simulate ImportError by patching the import inside the run function
        from yadgar.core.export import duckdb_exporter

        with patch.object(
            duckdb_exporter,
            "_import_duckdb",
            side_effect=ImportError("No module named 'duckdb'"),
        ):
            with pytest.raises(SystemExit) as exc:
                from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

                cfg = ExportConfig()
                exporter = DuckDBExporter(
                    db_path="/nonexistent",
                    output_path=str(tmp_path / "out.duckdb"),
                    config=cfg,
                )
                exporter.run()
        assert exc.value.code == 2

    def test_cli_import_without_duckdb_does_not_crash(self):
        """Importing yadgar.cli.export must not import duckdb at module level."""
        with patch.dict(sys.modules, {"duckdb": None}):
            # Re-import to test
            import importlib

            import yadgar.core.cli.export as _mod  # noqa: F401

            importlib.reload(_mod)
            # If we reach here without ImportError, the test passes


class TestForceFlag:
    """Without --force, existing output file must cause exit non-zero."""

    def test_existing_file_no_force_fails(self, seeded_storage, tmp_duckdb, tmp_db):
        pytest.importorskip("duckdb", reason="duckdb not installed")

        # Create the file first
        tmp_duckdb.write_bytes(b"existing content")

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        with pytest.raises((SystemExit, FileExistsError)) as exc:
            cfg = ExportConfig(force=False, embedding_dim=384)
            exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
            exporter.run()

        if isinstance(exc.value, SystemExit):
            assert exc.value.code != 0

    def test_existing_file_with_force_overwrites(self, seeded_storage, tmp_duckdb, tmp_db):
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        tmp_duckdb.write_bytes(b"old content")

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(force=True, embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()

        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        count = con.execute("SELECT count(*) FROM memory").fetchone()[0]
        con.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Integration test (marked, opt-in only)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExportFullCorpusSmoke:
    """Spin up real SurrealDB with seed data; run full export; execute every view."""

    def test_full_corpus_smoke(self, seeded_storage, tmp_duckdb, tmp_db):
        pytest.importorskip("duckdb", reason="duckdb not installed")
        import duckdb

        from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

        cfg = ExportConfig(embedding_dim=384)
        exporter = DuckDBExporter(db_path=tmp_db, output_path=str(tmp_duckdb), config=cfg)
        exporter.run()

        con = duckdb.connect(str(tmp_duckdb), read_only=True)
        views = [
            "v_decay_distribution",
            "v_recall_efficacy_by_tag",
            "v_anchor_usage",
            "v_high_heat_memories",
            "v_domain_clustering",
            "v_consolidation_effect",
            "v_conflict_density",
            "v_wiki_coverage",
            "v_tool_call_volume",
        ]
        for view in views:
            rows = con.execute(f"SELECT * FROM {view} LIMIT 100").fetchall()  # noqa: S608
            assert isinstance(rows, list), f"View {view} failed"
        con.close()
