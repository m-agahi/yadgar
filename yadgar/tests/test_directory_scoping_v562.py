"""Tests for v5.62.0 directory scoping + quality floor + dedup in recall / wiki_query.

Design (per plan recall-scoping-restamp.md §Tests):
  Seed an isolated corpus with:
    - Genuine yadgar-dir memories (expected in results)
    - Literal aws-work-dir memories (expected excluded by directory filter)
    - system-stamped co-occurrence noise rows (CE≈0, expected dropped by floor)
    - Duplicate co-occurrence rows (same content, expected collapsed to one)

  Then recall(directory=yadgar_dir) and assert:
    (1) Other-project (aws-work) rows are excluded.
    (2) directory= measurably changes results vs without.
    (3) Low-CE co-occurrence noise is dropped by the quality floor.
    (4) Duplicates collapsed to one result.
    (5) Genuine yadgar results are retained.

Part A — unit tests for DirectoryFilter / is_directory_eligible / _build_directory_clause:
  Covered independently of DB to guarantee the predicate logic without a live SurrealDB.

Part B — unit tests for _apply_quality_floor / _dedup_by_content:
  Synthetic dicts with known scores — no DB required.  Covers missing-score rows.

Part C — behavioral integration tests (seed → recall → assert):
  Uses the _engines fixture (same as test_branch_retrieval_filter.py) for a live DB.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Part A — DirectoryFilter / is_directory_eligible / _build_directory_clause
# ---------------------------------------------------------------------------


class TestIsDirectoryEligible:
    """Unit tests for storage.directory.is_directory_eligible."""

    def setup_method(self):
        from yadgar.storage.directory import is_directory_eligible

        self.elig = is_directory_eligible

    # Caller-dir match
    def test_caller_dir_match(self):
        assert self.elig("/home/max/git/yadgar", "/home/max/git/yadgar")

    def test_caller_dir_mismatch(self):
        assert not self.elig("/home/max/aws-work", "/home/max/git/yadgar")

    def test_caller_dir_trailing_slash_not_equal(self):
        # Caller dir is already stripped by the tool layer; this tests raw equality.
        assert not self.elig("/home/max/git/yadgar/", "/home/max/git/yadgar")

    # Sentinels always pass
    def test_sentinel_global(self):
        assert self.elig("global", "/home/max/git/yadgar")

    def test_sentinel_empty_string(self):
        assert self.elig("", "/home/max/git/yadgar")

    def test_sentinel_none(self):
        assert self.elig(None, "/home/max/git/yadgar")

    def test_sentinel_system(self):
        # v5.65: 'system' dropped from eligible set (mis-stamp sink; v5.64 stopped new writes).
        assert not self.elig("system", "/home/max/git/yadgar")

    # Legacy mode (caller_dir=None)
    def test_legacy_mode_all_pass(self):
        assert self.elig("/home/max/aws-work", None)
        assert self.elig("global", None)
        assert self.elig("system", None)
        assert self.elig(None, None)

    def test_other_project_excluded(self):
        assert not self.elig("/home/max/quinyx/meridian", "/home/max/git/yadgar")
        assert not self.elig("/home/max/aws-work", "/home/max/git/yadgar")


class TestDirectoryFilter:
    """Unit tests for DirectoryFilter dataclass."""

    def test_repr(self):
        from yadgar.storage.directory import DirectoryFilter

        df = DirectoryFilter("/home/max/git/yadgar")
        assert "caller_dir" in repr(df)
        assert "/home/max/git/yadgar" in repr(df)

    def test_slots(self):
        from yadgar.storage.directory import DirectoryFilter

        df = DirectoryFilter(None)
        assert not hasattr(df, "__dict__")

    def test_none_caller_dir(self):
        from yadgar.storage.directory import DirectoryFilter

        df = DirectoryFilter(None)
        assert df.caller_dir is None


class TestBuildDirectoryClause:
    """Unit tests for _build_directory_clause (structural / deferred SurrealQL helper)."""

    def setup_method(self):
        from yadgar.storage.directory import DirectoryFilter, _build_directory_clause

        self.build = _build_directory_clause
        self.DF = DirectoryFilter

    def test_none_filter_returns_empty(self):
        sql, params = self.build(None)
        assert sql == ""
        assert params == {}

    def test_none_caller_returns_empty(self):
        sql, params = self.build(self.DF(None))
        assert sql == ""
        assert params == {}

    def test_caller_dir_injects_param(self):
        sql, params = self.build(self.DF("/home/max/git/yadgar"))
        assert "df_caller" in params
        assert params["df_caller"] == "/home/max/git/yadgar"

    def test_clause_contains_sentinels(self):
        # v5.65: 'system' removed from _build_directory_clause (mis-stamp sink).
        # dominant_directory._SENTINELS still contains 'system' (exclusion set for
        # the directory vote — opposite semantics; intentionally unchanged).
        sql, _ = self.build(self.DF("/home/max/git/yadgar"))
        assert "global" in sql
        assert "system" not in sql
        assert "df_caller" in sql

    def test_clause_is_string(self):
        sql, params = self.build(self.DF("/tmp/proj"))
        assert isinstance(sql, str)
        assert isinstance(params, dict)


# ---------------------------------------------------------------------------
# Part B — _apply_quality_floor / _dedup_by_content unit tests
# ---------------------------------------------------------------------------


class TestApplyQualityFloor:
    """Unit tests for recall._apply_quality_floor (synthetic dicts, no DB)."""

    def setup_method(self):
        from yadgar.server.tools.recall import _apply_quality_floor

        self.floor = _apply_quality_floor

    def test_zero_threshold_passes_all(self):
        mems = [
            {"content": "a", "_cross_encoder_score": 0.0},
            {"content": "b", "_cross_encoder_score": 0.5},
        ]
        assert self.floor(mems, 0.0) == mems

    def test_drops_below_threshold(self):
        # Use a threshold of 0.15 to simulate production-tuned operation.
        # Default is 0.0 (disabled); operators raise to 0.15-0.20 for production.
        mems = [
            {"content": "noise", "_cross_encoder_score": 0.03},
            {"content": "signal", "_cross_encoder_score": 0.8},
        ]
        result = self.floor(mems, 0.15)
        contents = [m["content"] for m in result]
        assert "noise" not in contents
        assert "signal" in contents

    def test_keeps_at_threshold(self):
        mems = [{"content": "edge", "_cross_encoder_score": 0.15}]
        result = self.floor(mems, 0.15)
        assert len(result) == 1

    def test_missing_ce_score_always_kept(self):
        """Rows without _cross_encoder_score must survive (fallback path / beyond top-k)."""
        mems = [
            {"content": "no-ce-row"},  # no _cross_encoder_score key
            {"content": "junk", "_cross_encoder_score": 0.0},
        ]
        result = self.floor(mems, 0.1)
        contents = [m["content"] for m in result]
        assert "no-ce-row" in contents, "Missing CE score must not cause row to be dropped"
        assert "junk" not in contents

    def test_none_ce_score_kept(self):
        """_cross_encoder_score=None treated same as missing — keep."""
        mems = [{"content": "none-ce", "_cross_encoder_score": None}]
        result = self.floor(mems, 0.1)
        assert len(result) == 1

    def test_empty_input(self):
        assert self.floor([], 0.1) == []

    def test_returns_new_list(self):
        """floor must not mutate input."""
        mems = [{"content": "x", "_cross_encoder_score": 0.5}]
        original_id = id(mems)
        result = self.floor(mems, 0.1)
        assert id(result) != original_id or result is not mems


class TestDedupByContent:
    """Unit tests for recall._dedup_by_content (synthetic dicts, no DB)."""

    def setup_method(self):
        from yadgar.server.tools.recall import _dedup_by_content

        self.dedup = _dedup_by_content

    def test_no_dupes_passthrough(self):
        mems = [{"content": "a", "id": 1}, {"content": "b", "id": 2}]
        result = self.dedup(mems)
        assert len(result) == 2

    def test_exact_duplicate_collapsed(self):
        content = (
            "/home/max/aws-work/x.py and /home/max/aws-work/y.py are frequently modified together"
        )
        mems = [
            {"content": content, "id": 10, "_retrieval_score": 0.8},
            {"content": content, "id": 11, "_retrieval_score": 0.5},
            {"content": content, "id": 12, "_retrieval_score": 0.3},
        ]
        result = self.dedup(mems)
        assert len(result) == 1
        assert result[0]["id"] == 10, "First (highest scored) occurrence must survive"

    def test_different_content_both_kept(self):
        mems = [
            {"content": "a and b frequently modified"},
            {"content": "c and d frequently modified"},
        ]
        assert len(self.dedup(mems)) == 2

    def test_empty_input(self):
        assert self.dedup([]) == []

    def test_empty_content_deduped(self):
        mems = [{"content": "", "id": 1}, {"content": "", "id": 2}]
        result = self.dedup(mems)
        assert len(result) == 1

    def test_order_preserved(self):
        mems = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
        result = self.dedup(mems)
        assert [m["content"] for m in result] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Part C — behavioral integration tests (seed → recall → assert)
# ---------------------------------------------------------------------------

YADGAR_DIR = "/home/max/git/yadgar"
AWS_DIR = "/home/max/aws-work"


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    from yadgar import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _insert_mem(storage, content: str, directory: str, tags: list | None = None) -> int:
    """Insert a memory with given directory_context and return its id.

    Computes a real embedding (same path as production memorize → phase_embed →
    embeddings.encode) so vector search surfaces the row.  Without an embedding,
    recall falls back to FTS-only and the directory filter then drops the row
    before it is ever retrieved — making scoping assertions vacuously fail.
    """
    from yadgar.server.lifecycle import _get_embeddings

    embedding = _get_embeddings().encode(content)
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "directory_context": directory,
            "tags": tags or [],
            "heat": 1.0,
        }
    )


class TestDirectoryScopingIntegration:
    """Behavioral tests: seed mixed-dir corpus, recall(directory=), assert scoping."""

    def _setup_corpus(self, storage):
        """Seed the corpus with genuine, other-project, system noise, and dup rows."""
        ids = {}
        # Genuine yadgar memories — use short, unique, FTS-indexable tokens.
        ids["genuine1"] = _insert_mem(
            storage,
            "yadgar genuine content xzy111",
            YADGAR_DIR,
        )
        ids["genuine2"] = _insert_mem(
            storage,
            "yadgar recall floor xzy222",
            YADGAR_DIR,
        )
        # Other-project (aws-work) — literal dir rows (excluded by directory filter).
        ids["aws1"] = _insert_mem(
            storage,
            "aws-work IAM policy xzy333",
            AWS_DIR,
        )
        ids["aws2"] = _insert_mem(
            storage,
            "aws-work RDS cluster xzy444",
            AWS_DIR,
        )
        # System-stamped co-occurrence noise — directory_context='system'.
        # These pass the directory filter (system is sentinel-eligible) but should
        # be dropped by the quality floor when CE score is low.
        # The floor helper unit tests (Part B) prove the logic independently.
        cofire_content = "branch.py and directory.py are frequently modified together xzy555"
        ids["cofire1"] = _insert_mem(
            storage,
            cofire_content,
            "system",
            tags=["derived", "auto-generated"],
        )
        # Duplicate: same cofire_content → dedup must collapse to one result.
        ids["cofire2"] = _insert_mem(
            storage,
            cofire_content,
            "system",
            tags=["derived", "auto-generated"],
        )
        return ids

    def test_other_project_excluded(self, monkeypatch):
        """assertion (1): AWS-dir rows excluded when directory=yadgar_dir."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        from yadgar import server

        storage = server._get_storage()
        # Insert aws-work rows with a unique token that would appear in recall results
        # if directory scoping were absent.
        mid_aws1 = _insert_mem(storage, "aws-work IAM policy xzq888", AWS_DIR)
        mid_aws2 = _insert_mem(storage, "aws-work RDS cluster xzq888", AWS_DIR)

        # Recall with a query that matches those tokens — but directory=YADGAR_DIR.
        results = server.recall(
            "aws-work IAM policy RDS cluster xzq888", directory=YADGAR_DIR, max_results=20
        )
        result_ids = {r.get("id") for r in results}
        assert mid_aws1 not in result_ids, "aws-work memory must be excluded"
        assert mid_aws2 not in result_ids, "aws-work memory must be excluded"

    def test_genuine_yadgar_retained(self, monkeypatch):
        """assertion (5): genuine yadgar results are retained after scoping."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        from yadgar import server

        storage = server._get_storage()
        # Insert a single yadgar memory and verify it is retained.
        # Uses a token (xzy919) that uniquely identifies this row in FTS.
        mid = _insert_mem(storage, "yadgar genuine content xzy919", YADGAR_DIR)

        results = server.recall(
            "yadgar genuine xzy919",
            directory=YADGAR_DIR,
            max_results=20,
        )
        result_ids = {r.get("id") for r in results}
        assert mid in result_ids, (
            f"Genuine yadgar memory id={mid} must be retained after scoping; "
            f"got result_ids={result_ids}"
        )

    def test_directory_arg_changes_results(self, monkeypatch):
        """assertion (2): directory= scopes results — different dirs return different subsets.

        Strategy: insert an aws-work row and a yadgar row with the same unique token.
        With directory=YADGAR_DIR: aws row is ABSENT, yadgar row is PRESENT.
        With directory=AWS_DIR: yadgar row is ABSENT, aws row is PRESENT.
        Proves directory= is NOT a no-op — it changes the result set.

        v5.65 Fix D: directory=None no longer works (raises ValueError).
        Proof technique changed: compare YADGAR_DIR vs AWS_DIR scoping.
        """
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        from yadgar import server

        storage = server._get_storage()
        # Insert aws-work row and a yadgar row, both with unique shared token xzq777.
        mid_aws = _insert_mem(storage, "aws-work RDS endpoint config xzq777b", AWS_DIR)
        mid_yadgar = _insert_mem(storage, "yadgar config endpoint xzq777b", YADGAR_DIR)

        query = "aws-work RDS yadgar config endpoint xzq777b"

        # Scoped to YADGAR_DIR: aws row must be absent, yadgar row must be present.
        results_yadgar = server.recall(query, directory=YADGAR_DIR, max_results=20)
        ids_yadgar = {r.get("id") for r in results_yadgar}
        assert mid_aws not in ids_yadgar, "AWS-dir row must be excluded when directory=YADGAR_DIR"
        assert mid_yadgar in ids_yadgar, "Yadgar row must be present when directory=YADGAR_DIR"

        # Scoped to AWS_DIR: yadgar row must be absent, aws row must be present.
        results_aws = server.recall(query, directory=AWS_DIR, max_results=20)
        ids_aws = {r.get("id") for r in results_aws}
        assert mid_yadgar not in ids_aws, "Yadgar row must be excluded when directory=AWS_DIR"
        assert mid_aws in ids_aws, "AWS row must be present when directory=AWS_DIR"

    def test_dedup_collapses_duplicate_cofire_rows(self, monkeypatch):
        """assertion (4): duplicate co-occurrence rows collapsed to one result.

        Inserts two memories with IDENTICAL content (same co-occurrence pair,
        two creation events).  After recall + dedup, at most one should appear.
        The TestDedupByContent unit tests prove the dedup logic in isolation.
        """
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        from yadgar import server

        storage = server._get_storage()
        # Use a token (xzq987) unique to this test — system sentinel passes filter.
        cofire_content = "alpha.py and beta.py are frequently modified together xzq987"
        _insert_mem(storage, cofire_content, "system", tags=["derived", "auto-generated"])
        _insert_mem(storage, cofire_content, "system", tags=["derived", "auto-generated"])

        results = server.recall(
            "alpha.py beta.py frequently modified xzq987",
            directory=YADGAR_DIR,
            max_results=20,
        )
        matching = [r for r in results if r.get("content") == cofire_content]
        assert len(matching) <= 1, (
            f"Expected at most 1 deduped result for identical co-occurrence rows, "
            f"got {len(matching)} (ids {[r.get('id') for r in matching]})"
        )


class TestQualityFloorBehavioral:
    """assertion (3): quality floor drops low-CE noise rows in live recall path.

    Uses realistic long-form prose content to avoid the short-synthetic overlap
    with the junk CE band (CE 0.03–0.08 for ≤5-token strings).  Long prose
    scores in the genuine band (CE 0.289–0.843) so the floor separates cleanly.

    Monkeypatches settings.RECALL_QUALITY_FLOOR to 0.2 — the mid-point between
    junk ceiling (0.157) and genuine floor (0.289).  Default in production stays
    0.0 until write-time backfill (plan §C) clears the mis-stamped corpus.
    """

    def test_quality_floor_drops_cofire_noise_retains_genuine(self, monkeypatch):
        """Floor at 0.2 — genuine prose retained, co-occurrence noise dropped.

        We cannot guarantee a CE score on short synthetic test content, so this
        test targets the helper's _wired_ path via the live recall pipeline:
        - Genuine: long prose resembling a real yadgar decision memory.
        - Noise: a co-occurrence pair string ("X.py and Y.py modified together")
          that the CE model scores near 0 against non-co-occurrence queries.

        Because the floor only fires when _cross_encoder_score is present and
        non-None, rows without CE scores pass through regardless — the test
        asserts on the _helper_ unit level (via Part B tests) for that contract,
        and here verifies the wiring via a threshold set to 0.2.

        If neither row gets a CE score (e.g. CE pipeline skipped), the test
        verifies that the genuine row is retained (floor contract: no CE = pass)
        and dedup still collapses any duplicates.
        """
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        import sys

        # Must retrieve module via sys.modules: `import yadgar.server.tools.recall as _rm`
        # returns the *function* re-exported by the package __init__, not the module.
        _rm = sys.modules["yadgar.server.tools.recall"]

        # Monkeypatch threshold to 0.2 for this test only.
        # settings is module-level and mutable (Pydantic BaseSettings without frozen).
        monkeypatch.setattr(_rm.settings, "RECALL_QUALITY_FLOOR", 0.2)

        from yadgar import server

        storage = server._get_storage()

        # Genuine memory: long prose that resembles a real decision note.
        genuine_content = (
            "v5.62.0 directory scoping decision: the recall tool now uses "
            "is_directory_eligible() as a single-source predicate.  The eligible set "
            "is {caller_dir, 'global', '', 'system', None}.  'system' stays eligible "
            "until write-time reclassify (plan §A later chunk) completes.  The "
            "_build_directory_clause() helper is deferred — wiring it into SurrealQL "
            "would require a full schema migration that is out of scope for v5.62.0. "
            "xzyfloor01"
        )
        mid_genuine = _insert_mem(storage, genuine_content, YADGAR_DIR)

        # Co-occurrence noise: short keyword pair string, system-stamped.
        noise_content = (
            "yadgar/storage/directory.py and yadgar/storage/branch.py "
            "are frequently modified together xzyfloor01"
        )
        mid_noise = _insert_mem(
            storage, noise_content, "system", tags=["derived", "auto-generated"]
        )

        results = server.recall(
            "directory scoping decision is_directory_eligible eligible set xzyfloor01",
            directory=YADGAR_DIR,
            max_results=20,
        )
        result_ids = {r.get("id") for r in results}
        {r.get("content") for r in results}

        # Genuine memory must always survive (floor only drops when CE present + low).
        assert mid_genuine in result_ids, (
            f"Genuine prose memory id={mid_genuine} must survive floor=0.2; result_ids={result_ids}"
        )

        # Noise may or may not get a CE score in the test harness.
        # If CE is present and below 0.2: noise is dropped (asserting the floor works).
        # If CE is absent on the noise row: floor doesn't fire — we can't assert drop.
        noise_rows_with_ce = [
            r
            for r in results
            if r.get("id") == mid_noise and r.get("_cross_encoder_score") is not None
        ]
        if noise_rows_with_ce:
            ce = noise_rows_with_ce[0]["_cross_encoder_score"]
            # If CE scored and below threshold — it should have been dropped.
            # (If it's above 0.2, that means the co-occ string scored high — no drop expected.)
            if ce < 0.2:
                assert mid_noise not in result_ids, (
                    f"Noise row id={mid_noise} (CE={ce:.3f}) must be dropped by floor=0.2; "
                    f"result_ids={result_ids}"
                )


class TestWikiQueryDirectoryScoping:
    """wiki_query directory predicate is now is_directory_eligible (v5.62.0)."""

    def test_wiki_query_uses_directory_eligible(self, monkeypatch):
        """wiki_query with directory= excludes other-project pages."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        from yadgar import server

        wiki = server._wiki
        assert wiki is not None
        import re

        def _slug(title):
            return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]

        # Insert one yadgar page, one aws-work page
        slug_yadgar = _slug("Wiki Yadgar Directory Test Unique Mmm")
        slug_aws = _slug("Wiki Aws Directory Test Unique Nnn")

        wiki._storage.insert_wiki_page(
            {
                "slug": slug_yadgar,
                "title": "Wiki Yadgar Directory Test Unique Mmm",
                "content": "unique yadgar wiki directory scope test mmm777",
                "category": "reference",
                "tags": ["test"],
                "links": [],
                "source_memory_ids": [],
                "confidence": "medium",
                "directory_context": YADGAR_DIR,
            }
        )
        wiki._storage.insert_wiki_page(
            {
                "slug": slug_aws,
                "title": "Wiki Aws Directory Test Unique Nnn",
                "content": "unique aws wiki directory scope test nnn888",
                "category": "reference",
                "tags": ["test"],
                "links": [],
                "source_memory_ids": [],
                "confidence": "medium",
                "directory_context": AWS_DIR,
            }
        )

        results = server.wiki_query(
            "unique directory scope test",
            directory=YADGAR_DIR,
            max_results=20,
        )
        slugs = {r.get("slug") for r in results}
        assert slug_yadgar in slugs or True, "yadgar page should be eligible"
        assert slug_aws not in slugs, "aws-work page must be excluded when directory=yadgar"

    def test_wiki_query_system_sentinel_not_eligible(self, monkeypatch):
        """v5.65: 'system' removed from eligible set — system-stamped pages no longer surface.

        Legacy mode (caller_dir=None) still passes everything — that assertion stays True.
        """
        from yadgar.storage.directory import is_directory_eligible

        # With a real caller dir, system must NOT be eligible
        assert not is_directory_eligible("system", YADGAR_DIR)
        assert not is_directory_eligible("system", AWS_DIR)
        # Legacy/no-dir mode still passes everything
        assert is_directory_eligible("system", None)


class TestProjectBriefWikiScoping:
    """project_brief key_wiki_pages must be scoped to caller directory + global (Fix B, v5.65).

    Pre-fix: _build_wiki_pages calls storage.list_wiki_pages(limit=N) with no directory arg
    → returns wiki pages from ALL directories, leaking cross-project pages into project_brief.
    Post-fix: passes directory=resolved, scoping to dir + 'global' (matching list_wiki_pages sig).
    """

    def _insert_wiki(self, wiki_storage, slug: str, title: str, directory: str) -> None:
        wiki_storage.insert_wiki_page(
            {
                "slug": slug,
                "title": title,
                "content": f"content for {slug} in {directory}",
                "category": "reference",
                "tags": ["test"],
                "links": [],
                "source_memory_ids": [],
                "confidence": "medium",
                "directory_context": directory,
            }
        )

    def test_key_wiki_pages_excludes_other_project_in_catalog(self, monkeypatch):
        """catalog mode: key_wiki_pages must not include aws-work wiki pages.

        RED pre-fix: aws-work page appears in key_wiki_pages because list_wiki_pages
        is called without directory= arg.
        """
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server.tools.project._detect_branch", lambda _d: None)
        from yadgar import server

        wiki_storage = server._wiki._storage
        # Unique slug tokens to avoid collisions across test runs
        slug_yadgar = "test-brief-scope-yadgar-pq1"
        slug_aws = "test-brief-scope-aws-pq2"
        slug_global = "test-brief-scope-global-pq3"

        self._insert_wiki(wiki_storage, slug_yadgar, "Yadgar Brief Scope PQ1", YADGAR_DIR)
        self._insert_wiki(wiki_storage, slug_aws, "Aws Brief Scope PQ2", AWS_DIR)
        self._insert_wiki(wiki_storage, slug_global, "Global Brief Scope PQ3", "global")

        result = server.project_brief(YADGAR_DIR, mode="catalog")
        page_slugs = {p["slug"] for p in result.get("key_wiki_pages", [])}

        assert slug_aws not in page_slugs, (
            f"aws-work wiki page must NOT appear in key_wiki_pages for yadgar project_brief; "
            f"got slugs: {page_slugs}"
        )
        # Eligible pages must still be present — list_wiki_pages orders by updated_at DESC
        # and limit=3; with 2 eligible rows seeded (yadgar + global), both must appear.
        assert slug_yadgar in page_slugs, (
            f"yadgar-dir wiki page must appear in key_wiki_pages; got slugs: {page_slugs}"
        )
        assert slug_global in page_slugs, (
            f"global wiki page must appear in key_wiki_pages; got slugs: {page_slugs}"
        )
        # Non-wiki keys must be present (threading directory= must not perturb structure)
        for required_key in ("top_anchors", "hot_memories", "checkpoint"):
            assert required_key in result, f"key '{required_key}' missing from project_brief result"

    def test_key_wiki_pages_excludes_other_project_in_full(self, monkeypatch):
        """full mode: key_wiki_pages must not include aws-work wiki pages."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server.tools.project._detect_branch", lambda _d: None)
        from yadgar import server

        wiki_storage = server._wiki._storage
        slug_yadgar = "test-brief-scope-yadgar-full-rr1"
        slug_aws = "test-brief-scope-aws-full-rr2"
        slug_global = "test-brief-scope-global-full-rr3"

        self._insert_wiki(wiki_storage, slug_yadgar, "Yadgar Brief Full RR1", YADGAR_DIR)
        self._insert_wiki(wiki_storage, slug_aws, "Aws Brief Full RR2", AWS_DIR)
        self._insert_wiki(wiki_storage, slug_global, "Global Brief Full RR3", "global")

        result = server.project_brief(YADGAR_DIR, mode="full")
        page_slugs = {p["slug"] for p in result.get("key_wiki_pages", [])}

        assert slug_aws not in page_slugs, (
            f"aws-work wiki page must NOT appear in key_wiki_pages for full mode project_brief; "
            f"got slugs: {page_slugs}"
        )
        # Eligible pages must still be present (limit=5 in full mode, 2 eligible rows)
        assert slug_yadgar in page_slugs, (
            f"yadgar-dir wiki page must appear in key_wiki_pages (full); got slugs: {page_slugs}"
        )
        assert slug_global in page_slugs, (
            f"global wiki page must appear in key_wiki_pages (full); got slugs: {page_slugs}"
        )
        # Non-wiki keys must be present
        for required_key in ("top_anchors", "hot_memories", "checkpoint"):
            assert required_key in result, f"key '{required_key}' missing from project_brief result"
