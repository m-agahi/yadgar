"""Tests for v6 Phase 0.2 data-quality stats functions.

Covers:
  - _query_data_quality() in yadgar/cli/stats.py
  - StatsData data-quality fields
  - _build_json_output() includes data_quality section
  - _print_table_output() includes DATA QUALITY section

Follows TDD pattern: tests written first (scope = application logic, not infra).
No live DB required — mocks _count() and db.query() calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_stats_data(**kwargs):
    """Build a StatsData with provided field overrides."""
    from yadgar.core.cli.stats import StatsData

    sd = StatsData()
    for k, v in kwargs.items():
        setattr(sd, k, v)
    return sd


def _mock_db(query_responses: dict[str, list]) -> MagicMock:
    """Return a mock db that dispatches query() calls by SQL prefix."""
    db = MagicMock()

    def _dispatch(sql, *args, **kwargs):
        for prefix, response in query_responses.items():
            if prefix.lower() in sql.lower():
                return response
        return [[{"count": 0}]]

    db.query.side_effect = _dispatch
    return db


# ── StatsData fields ──────────────────────────────────────────────────────────


class TestStatsDataFields:
    def test_has_data_quality_fields(self):
        from yadgar.core.cli.stats import StatsData

        sd = StatsData()
        assert hasattr(sd, "dq_null_embedding_count")
        assert hasattr(sd, "dq_embedding_valid_ratio")
        assert hasattr(sd, "dq_duplicate_rate")
        assert hasattr(sd, "dq_zombie_rate")
        assert hasattr(sd, "dq_domain_coverage")
        assert hasattr(sd, "dq_surprise_p50")
        assert hasattr(sd, "dq_surprise_p95")

    def test_data_quality_defaults(self):
        from yadgar.core.cli.stats import StatsData

        sd = StatsData()
        assert sd.dq_null_embedding_count == 0
        assert sd.dq_embedding_valid_ratio == 0.0
        assert sd.dq_duplicate_rate == 0.0
        assert sd.dq_zombie_rate == 0.0
        assert sd.dq_domain_coverage == 0.0
        assert sd.dq_surprise_p50 is None
        assert sd.dq_surprise_p95 is None


# ── _query_data_quality ────────────────────────────────────────────────────────


class TestQueryDataQuality:
    def test_no_op_when_total_zero(self):
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=0)
        db = MagicMock()
        _query_data_quality(db, sd)
        # DB should not have been queried
        db.query.assert_not_called()
        assert sd.dq_embedding_valid_ratio == 0.0

    def test_null_embedding_ratio(self):
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=100, stale=0)

        db = _mock_db(
            {
                "embedding is none": [[{"count": 20}]],
                "memory_similarity_link": [[{"count": 0}]],
                "domain is not none": [[{"count": 0}]],
                "surprise_score": [[]],
            }
        )

        _query_data_quality(db, sd)

        assert sd.dq_null_embedding_count == 20
        assert abs(sd.dq_embedding_valid_ratio - 0.80) < 1e-6

    def test_full_embedding_validity(self):
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=50, stale=0)
        db = _mock_db(
            {
                "embedding is none": [[{"count": 0}]],
                "memory_similarity_link": [[{"count": 0}]],
                "domain is not none": [[{"count": 50}]],
                "surprise_score": [[]],
            }
        )
        _query_data_quality(db, sd)

        assert sd.dq_null_embedding_count == 0
        assert sd.dq_embedding_valid_ratio == 1.0

    def test_duplicate_rate_computed(self):
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=100, stale=0)
        db = _mock_db(
            {
                "embedding is none": [[{"count": 0}]],
                "memory_similarity_link": [[{"count": 30}]],
                "domain is not none": [[{"count": 100}]],
                "surprise_score": [[]],
            }
        )
        _query_data_quality(db, sd)

        assert abs(sd.dq_duplicate_rate - 0.30) < 1e-6

    def test_zombie_rate_uses_stale(self):
        from yadgar.core.cli.stats import _query_data_quality

        # 80 active, 20 stale
        sd = _make_stats_data(total=80, stale=20)
        db = _mock_db(
            {
                "embedding is none": [[{"count": 0}]],
                "memory_similarity_link": [[{"count": 0}]],
                "domain is not none": [[{"count": 80}]],
                "surprise_score": [[]],
            }
        )
        _query_data_quality(db, sd)

        # zombie = 20 / (80 + 20) = 0.2
        assert abs(sd.dq_zombie_rate - 0.20) < 1e-6

    def test_domain_coverage(self):
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=200, stale=0)
        db = _mock_db(
            {
                "embedding is none": [[{"count": 0}]],
                "memory_similarity_link": [[{"count": 0}]],
                "domain is not none": [[{"count": 150}]],
                "surprise_score": [[]],
            }
        )
        _query_data_quality(db, sd)

        assert abs(sd.dq_domain_coverage - 0.75) < 1e-6

    def test_surprise_p50_p95(self):
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=50, stale=0)
        # 30 rows with surprise scores
        scores = [float(i) / 10 for i in range(1, 31)]  # 0.1, 0.2, ..., 3.0
        rows = [{"surprise_score": s} for s in scores]

        db = _mock_db(
            {
                "embedding is none": [[{"count": 0}]],
                "memory_similarity_link": [[{"count": 0}]],
                "domain is not none": [[{"count": 50}]],
                "surprise_score": [rows],
            }
        )
        _query_data_quality(db, sd)

        assert sd.dq_surprise_p50 is not None
        assert sd.dq_surprise_p95 is not None
        # Median of 0.1..3.0 (30 values) = average of 15th and 16th = (1.5 + 1.6) / 2 = 1.55
        assert abs(sd.dq_surprise_p50 - 1.55) < 0.01
        # p95 should be higher than p50
        assert sd.dq_surprise_p95 > sd.dq_surprise_p50

    def test_surprise_p50_with_few_scores(self):
        """With <20 scores, p95 falls back to max()."""
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=20, stale=0)
        rows = [{"surprise_score": s} for s in [0.1, 0.2, 0.5, 0.9, 1.5]]

        db = _mock_db(
            {
                "embedding is none": [[{"count": 0}]],
                "memory_similarity_link": [[{"count": 0}]],
                "domain is not none": [[{"count": 20}]],
                "surprise_score": [rows],
            }
        )
        _query_data_quality(db, sd)

        assert sd.dq_surprise_p50 is not None
        assert sd.dq_surprise_p95 == 1.5  # fallback to max

    def test_null_embedding_count_includes_explicit_null_rows(self):
        """G2 item 2: ``dq_null_embedding_count`` must count NONE *and* NULL.

        SurrealDB's ``NONE`` (field absent) and ``NULL`` (explicit null) are
        DISTINCT values — ``IS NONE`` is FALSE for a row whose embedding is an
        explicit NULL (same trap Car F1 fixed for the brute-force vector-search
        arms in ``yadgar/_shared/storage/vector.py::search_vectors``, which
        guards with ``IS NOT NONE AND IS NOT NULL``). A metric whose whole job
        is finding rows with no usable embedding must use the mirror-image
        positive form, ``IS NONE OR IS NULL``, or it silently undercounts
        exactly the NULL-embedding rows it exists to surface.

        This mock simulates that real SurrealDB distinction: a query that only
        guards ``embedding IS NONE`` sees 2 rows (the NONE ones); a query that
        ALSO guards ``embedding IS NULL`` sees all 5 (2 NONE + 3 explicit
        NULL). Asserting the true total (5) fails against the old
        NONE-only query — it would return 2 — and passes once both arms are
        present, so this is not a trivially-passing assertion.
        """
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=100, stale=0)

        def _dispatch(sql, *args, **kwargs):
            low = sql.lower()
            if "embedding" in low and "count" in low:
                if "is null" in low:
                    return [[{"count": 5}]]  # 2 NONE + 3 explicit NULL
                return [[{"count": 2}]]  # NONE-only guard misses the 3 NULL rows
            return [[{"count": 0}]]

        db = MagicMock()
        db.query.side_effect = _dispatch

        _query_data_quality(db, sd)

        assert sd.dq_null_embedding_count == 5, (
            "dq_null_embedding_count under-counted — the query is still "
            "guarding 'embedding IS NONE' alone and missing explicit-NULL rows"
        )

    def test_db_error_silently_swallowed(self):
        """DB errors should not raise — graceful degradation."""
        from yadgar.core.cli.stats import _query_data_quality

        sd = _make_stats_data(total=50, stale=0)
        db = MagicMock()
        db.query.side_effect = RuntimeError("DB unavailable")

        # Should not raise
        _query_data_quality(db, sd)

        # Defaults unchanged
        assert sd.dq_null_embedding_count == 0
        assert sd.dq_embedding_valid_ratio == 0.0


# ── _build_json_output ────────────────────────────────────────────────────────


class TestBuildJsonOutput:
    def test_data_quality_section_present(self):
        from yadgar.core.cli.stats import _build_json_output

        sd = _make_stats_data(
            total=100,
            stale=5,
            dq_null_embedding_count=3,
            dq_embedding_valid_ratio=0.97,
            dq_duplicate_rate=0.05,
            dq_zombie_rate=0.048,
            dq_domain_coverage=0.80,
            dq_surprise_p50=0.25,
            dq_surprise_p95=0.75,
        )
        out = _build_json_output(sd)

        assert "data_quality" in out
        dq = out["data_quality"]
        assert dq["null_embedding_count"] == 3
        assert abs(dq["embedding_valid_ratio"] - 0.97) < 0.0001
        assert abs(dq["duplicate_rate"] - 0.05) < 0.0001
        assert abs(dq["zombie_rate"] - 0.048) < 0.0001
        assert abs(dq["domain_coverage"] - 0.80) < 0.0001
        assert abs(dq["surprise_p50"] - 0.25) < 0.0001
        assert abs(dq["surprise_p95"] - 0.75) < 0.0001

    def test_data_quality_null_surprise_is_none(self):
        from yadgar.core.cli.stats import _build_json_output

        sd = _make_stats_data(total=0)
        out = _build_json_output(sd)

        assert out["data_quality"]["surprise_p50"] is None
        assert out["data_quality"]["surprise_p95"] is None


# ── _print_table_output ───────────────────────────────────────────────────────


class TestPrintTableOutput:
    def test_data_quality_section_in_output(self, capsys):
        from yadgar.core.cli.stats import _print_table_output

        sd = _make_stats_data(
            total=100,
            stale=5,
            dq_null_embedding_count=2,
            dq_embedding_valid_ratio=0.98,
            dq_duplicate_rate=0.03,
            dq_zombie_rate=0.047,
            dq_domain_coverage=0.90,
            dq_surprise_p50=0.20,
            dq_surprise_p95=0.60,
        )
        _print_table_output(sd, project=None)

        captured = capsys.readouterr()
        assert "DATA QUALITY" in captured.out
        assert "Null embeddings" in captured.out
        assert "Duplicate rate" in captured.out
        assert "Zombie rate" in captured.out
        assert "Domain coverage" in captured.out
        assert "Surprise p50" in captured.out

    def test_data_quality_section_no_surprise(self, capsys):
        from yadgar.core.cli.stats import _print_table_output

        sd = _make_stats_data(total=10, stale=0)
        _print_table_output(sd, project=None)

        captured = capsys.readouterr()
        assert "DATA QUALITY" in captured.out
        assert "n/a" in captured.out
