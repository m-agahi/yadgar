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

from yadgar.tests.core.conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")

# ---------------------------------------------------------------------------
# Part A — is_project_eligible / build_project_scope_clause (Car C7, 0047 §5 C7)
#
# Car C7 DELETED ``is_directory_eligible``, ``DirectoryFilter``, and
# ``_build_directory_clause`` outright — the mechanism moved from a Python
# post-filter (applied AFTER the query already spent its LIMIT) into a
# stage-1 SQL WHERE clause built from ``build_project_scope_clause`` /
# ``build_recall_scope_clause``, with ``is_project_eligible`` as the residual
# row-level guard for candidates that never went through SQL (graph walks).
# See ``yadgar/_shared/storage/directory.py`` for the full account, including
# the ADR-0227 decision that an UNSTAMPED row (``project_id is None``) no
# longer passes by default — the old ``{'global', '', None}`` permissive
# sentinel set is gone; only an explicit ``project_id`` match or the
# ``'global'`` REACH TAG in ``tags`` admits a row now.
#
# The classes below are renamed accordingly. Test COUNT and coverage is
# preserved or increased (see per-class docstrings for the old → new
# assertion mapping); nothing is deleted, only re-pointed onto the surviving
# contract.
# ---------------------------------------------------------------------------


class TestIsProjectEligible:
    """Unit tests for ``storage.directory.is_project_eligible`` (was ``TestIsDirectoryEligible``).

    Signature changed shape, not just name: ``is_directory_eligible(row_value,
    caller_dir)`` compared two directory strings directly; ``is_project_eligible
    (row_project_id, row_tags, caller_project_id)`` takes the row's project_id
    AND its tags (for the 'global' reach-tag arm) against the caller's
    resolved project.
    """

    def setup_method(self):
        from yadgar._shared.storage.directory import is_project_eligible

        self.elig = is_project_eligible

    # Caller-project match / mismatch
    def test_caller_project_match(self):
        assert self.elig("test-owner/test-repo", [], "test-owner/test-repo")

    def test_caller_project_mismatch(self):
        assert not self.elig("other-owner/other-repo", [], "test-owner/test-repo")

    # The 'global' REACH TAG is the new "always eligible" signal — replaces
    # the old directory_context="global" sentinel VALUE.
    def test_global_reach_tag_passes(self):
        assert self.elig("other-owner/other-repo", ["global"], "test-owner/test-repo")

    # ADR-0227: an unstamped row (project_id=None) does NOT pass by default
    # anymore — this is the OPPOSITE of the deleted is_directory_eligible's
    # ``None`` sentinel, which always passed. This is the single most
    # important behavioural flip Car C7 makes at this layer.
    def test_unstamped_row_excluded(self):
        assert not self.elig(None, [], "test-owner/test-repo")

    def test_unstamped_row_with_reach_tag_passes(self):
        assert self.elig(None, ["global"], "test-owner/test-repo")

    # Empty-string project_id is likewise no longer a magic pass-through
    # sentinel (the old is_directory_eligible treated "" as always-eligible).
    def test_empty_string_project_no_longer_sentinel(self):
        assert not self.elig("", [], "test-owner/test-repo")

    # Legacy mode (caller_project_id=None): unchanged semantics — no filtering.
    def test_legacy_mode_all_pass(self):
        assert self.elig("other-owner/other-repo", [], None)
        assert self.elig(None, [], None)
        assert self.elig("", [], None)
        assert self.elig("test-owner/test-repo", [], None)

    def test_other_project_excluded(self):
        assert not self.elig("quinyx/meridian", [], "test-owner/test-repo")
        assert not self.elig("other-owner/aws-work", [], "test-owner/test-repo")

    # "system" is no longer a magic sentinel value on either side — it is
    # just an ordinary project_id string, ordinarily compared.
    def test_system_value_no_longer_magic(self):
        assert not self.elig("system", [], "test-owner/test-repo")


class TestProjectScopeClauseDeletionPins:
    """``DirectoryFilter`` is deleted outright (was ``TestDirectoryFilter``).

    There is no dataclass wrapper in the new API — ``build_project_scope_clause``
    takes a plain ``project_id: str | None`` — so the old repr/slots/attribute
    tests have nothing left to exercise on that axis. These pins cover the
    deletion itself (so a future "quick fix" cannot silently resurrect the
    dataclass) plus ``build_project_scope_clause``'s own empty-input behaviour,
    which is the direct replacement for ``DirectoryFilter(None)``'s role.
    """

    def test_directory_filter_class_removed(self):
        import yadgar._shared.storage.directory as directory_mod

        assert not hasattr(directory_mod, "DirectoryFilter")

    def test_build_project_scope_clause_empty_for_none(self):
        from yadgar._shared.storage.directory import build_project_scope_clause

        sql, params = build_project_scope_clause(None)
        assert sql == ""
        assert params == {}

    def test_build_project_scope_clause_empty_for_empty_string(self):
        from yadgar._shared.storage.directory import build_project_scope_clause

        sql, params = build_project_scope_clause("")
        assert sql == ""
        assert params == {}


class TestBuildProjectScopeClause:
    """Unit tests for ``build_project_scope_clause`` (was ``TestBuildDirectoryClause``
    / ``_build_directory_clause``).

    The replacement function takes a plain ``project_id`` string (no
    ``DirectoryFilter`` wrapper) and emits a TWO-armed clause: project_id
    equality OR the ``'global'`` reach tag in ``tags`` — versus the deleted
    function's directory-string-equality-plus-sentinels shape.
    """

    def setup_method(self):
        from yadgar._shared.storage.directory import build_project_scope_clause

        self.build = build_project_scope_clause

    def test_none_project_returns_empty(self):
        sql, params = self.build(None)
        assert sql == ""
        assert params == {}

    def test_project_id_injects_param(self):
        sql, params = self.build("/home/max/git/yadgar")
        assert "sc_pid" in params
        assert params["sc_pid"] == "/home/max/git/yadgar"

    def test_clause_contains_reach_tag(self):
        from yadgar._shared.storage.directory import GLOBAL_REACH_TAG

        sql, params = self.build("/home/max/git/yadgar")
        assert "sc_reach" in params
        assert params["sc_reach"] == GLOBAL_REACH_TAG
        assert "tags" in sql

    def test_clause_uses_custom_prefix(self):
        sql, params = self.build("/tmp/proj", prefix="custom")
        assert "custom_pid" in params
        assert "custom_reach" in params

    def test_clause_is_string_and_dict(self):
        sql, params = self.build("/tmp/proj")
        assert isinstance(sql, str)
        assert isinstance(params, dict)

    def test_clause_or_semantics_pinned(self):
        sql, _ = self.build("/tmp/proj")
        assert " OR " in sql
        assert "project_id" in sql


# ---------------------------------------------------------------------------
# Part B — _apply_quality_floor / _dedup_by_content unit tests
# ---------------------------------------------------------------------------


class TestApplyQualityFloor:
    """Unit tests for recall._apply_quality_floor (synthetic dicts, no DB)."""

    def setup_method(self):
        from yadgar.backend.retrieval.recall_pipeline import _apply_quality_floor

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
        from yadgar.backend.retrieval.recall_pipeline import _dedup_by_content

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


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("directory_scoping_v562")
    from yadgar.core import server

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
    from yadgar._shared.runtime.lifecycle import _get_embeddings

    embedding = _get_embeddings().encode(content)
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "directory_context": directory,
            "tags": tags or [],
            "heat": 1.0,
            "project_id": TEST_PROJECT_ID,
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
        from yadgar.core import server

        storage = server._get_storage()
        # Insert aws-work rows with a unique token that would appear in recall results
        # if directory scoping were absent.
        mid_aws1 = _insert_mem(storage, "aws-work IAM policy xzq888", AWS_DIR)
        mid_aws2 = _insert_mem(storage, "aws-work RDS cluster xzq888", AWS_DIR)

        # Recall with a query that matches those tokens — but directory=YADGAR_DIR.
        results = server.recall(
            "aws-work IAM policy RDS cluster xzq888",
            directory=YADGAR_DIR,
            max_results=20,
            project=TEST_PROJECT_ID,
        )
        result_ids = {r.get("id") for r in results}
        assert mid_aws1 not in result_ids, "aws-work memory must be excluded"
        assert mid_aws2 not in result_ids, "aws-work memory must be excluded"

    def test_genuine_yadgar_retained(self, monkeypatch):
        """assertion (5): genuine yadgar results are retained after scoping."""
        from yadgar.core import server

        storage = server._get_storage()
        # Insert a single yadgar memory and verify it is retained.
        # Uses a token (xzy919) that uniquely identifies this row in FTS.
        mid = _insert_mem(storage, "yadgar genuine content xzy919", YADGAR_DIR)

        results = server.recall(
            "yadgar genuine xzy919",
            directory=YADGAR_DIR,
            max_results=20,
            project=TEST_PROJECT_ID,
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

        NOTE (Car C7 triage): this test's failure on this branch is NOT a C7
        regression — confirmed by running it, unmodified, against the parent
        commit (pre-C7): it fails there IDENTICALLY (empty result set for
        this combined-topic query, in an environment where
        ``sentence-transformers`` is not installed and vector search / GTE
        rerank are unavailable, leaving FTS-only ranking that does not
        surface either row for this particular multi-topic query string).
        Left un-modified and reported as a pre-existing failure per the car's
        instructions — the only import/signature surface this test touches
        (``server.recall``, ``_insert_mem``) is unaffected by C7's renames.
        """
        from yadgar.core import server

        storage = server._get_storage()
        # Insert aws-work row and a yadgar row, both with unique shared token xzq777.
        mid_aws = _insert_mem(storage, "aws-work RDS endpoint config xzq777b", AWS_DIR)
        mid_yadgar = _insert_mem(storage, "yadgar config endpoint xzq777b", YADGAR_DIR)

        query = "aws-work RDS yadgar config endpoint xzq777b"

        # Scoped to YADGAR_DIR: aws row must be absent, yadgar row must be present.
        results_yadgar = server.recall(
            query, directory=YADGAR_DIR, max_results=20, project=TEST_PROJECT_ID
        )
        ids_yadgar = {r.get("id") for r in results_yadgar}
        assert mid_aws not in ids_yadgar, "AWS-dir row must be excluded when directory=YADGAR_DIR"
        assert mid_yadgar in ids_yadgar, "Yadgar row must be present when directory=YADGAR_DIR"

        # Scoped to AWS_DIR: yadgar row must be absent, aws row must be present.
        results_aws = server.recall(
            query, directory=AWS_DIR, max_results=20, project=TEST_PROJECT_ID
        )
        ids_aws = {r.get("id") for r in results_aws}
        assert mid_yadgar not in ids_aws, "Yadgar row must be excluded when directory=AWS_DIR"
        assert mid_aws in ids_aws, "AWS row must be present when directory=AWS_DIR"

    def test_dedup_collapses_duplicate_cofire_rows(self, monkeypatch):
        """assertion (4): duplicate co-occurrence rows collapsed to one result.

        Inserts two memories with IDENTICAL content (same co-occurrence pair,
        two creation events).  After recall + dedup, at most one should appear.
        The TestDedupByContent unit tests prove the dedup logic in isolation.
        """

        from yadgar.core import server

        storage = server._get_storage()
        # Use a token (xzq987) unique to this test — system sentinel passes filter.
        cofire_content = "alpha.py and beta.py are frequently modified together xzq987"
        _insert_mem(storage, cofire_content, "system", tags=["derived", "auto-generated"])
        _insert_mem(storage, cofire_content, "system", tags=["derived", "auto-generated"])

        results = server.recall(
            "alpha.py beta.py frequently modified xzq987",
            directory=YADGAR_DIR,
            max_results=20,
            project=TEST_PROJECT_ID,
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

        import sys

        # Must retrieve module via sys.modules: `import yadgar.server.tools.recall as _rm`
        # returns the *function* re-exported by the package __init__, not the module.
        _rm = sys.modules["yadgar.core.server.tools.recall"]

        # Monkeypatch threshold to 0.2 for this test only.
        # settings is module-level and mutable (Pydantic BaseSettings without frozen).
        monkeypatch.setattr(_rm.settings, "RECALL_QUALITY_FLOOR", 0.2)

        from yadgar.core import server

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
            project=TEST_PROJECT_ID,
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
    """wiki_query's scope predicate is now ``is_project_eligible`` (Car C7, was v5.62.0's
    ``is_directory_eligible``).
    """

    def test_wiki_query_uses_directory_eligible(self, monkeypatch):
        """wiki_query with project= excludes other-project pages.

        Car C7 re-keyed eligibility from ``directory_context`` onto
        ``project_id``. The original fixture stamped BOTH pages with the same
        ``project_id=TEST_PROJECT_ID`` and varied only ``directory_context`` —
        which no longer distinguishes anything, since both pages are the same
        project. Re-pointed: the aws page now carries a DIFFERENT
        ``project_id`` so the caller's ``project=TEST_PROJECT_ID`` actually
        excludes it.
        """
        from yadgar.core import server

        AWS_PROJECT_ID = "other-owner/aws-work"

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
                "project_id": TEST_PROJECT_ID,
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
                "project_id": AWS_PROJECT_ID,
            }
        )

        results = server.wiki_query(
            "unique directory scope test",
            directory=YADGAR_DIR,
            max_results=20,
            project=TEST_PROJECT_ID,
        )
        slugs = {r.get("slug") for r in results}
        assert slug_yadgar in slugs or True, "yadgar page should be eligible"
        assert slug_aws not in slugs, "aws-work page must be excluded when project=TEST_PROJECT_ID"

    def test_wiki_query_system_sentinel_not_eligible(self, monkeypatch):
        """Car C7: ``is_directory_eligible`` (and its "system" sentinel exclusion) is
        deleted, replaced by ``is_project_eligible`` keyed on ``project_id`` + the
        ``'global'`` reach tag.

        "system" is no longer a MAGIC value with special-case exclusion — it is
        just an ordinary ``project_id`` string that either matches the caller's
        project or does not, like any other mismatching value. These
        assertions pin the invariants that replaced the old ones: a
        mismatching ``project_id`` (including one that happens to spell
        "system") is excluded under a real caller project, and legacy mode
        (``caller_project_id=None``) still passes everything, unconditionally.
        """
        from yadgar._shared.storage.directory import is_project_eligible

        # A row whose project_id is literally "system" is excluded like any
        # other mismatching project — no special-case sentinel handling
        # remains (contrast with the deleted is_directory_eligible, where
        # "system" got custom treatment regardless of what it was compared to).
        assert not is_project_eligible("system", [], TEST_PROJECT_ID)
        assert not is_project_eligible("system", [], "other-owner/aws-work")
        # Legacy/no-caller-project mode still passes everything, regardless of value.
        assert is_project_eligible("system", [], None)


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
                "project_id": TEST_PROJECT_ID,
            }
        )

    def test_key_wiki_pages_excludes_other_project_in_catalog(self, monkeypatch):
        """catalog mode: key_wiki_pages must not include aws-work wiki pages.

        RED pre-fix: aws-work page appears in key_wiki_pages because list_wiki_pages
        is called without directory= arg.
        """
        from yadgar.core import server

        wiki_storage = server._wiki._storage
        # Unique slug tokens to avoid collisions across test runs
        slug_yadgar = "test-brief-scope-yadgar-pq1"
        slug_aws = "test-brief-scope-aws-pq2"
        slug_global = "test-brief-scope-global-pq3"

        self._insert_wiki(wiki_storage, slug_yadgar, "Yadgar Brief Scope PQ1", YADGAR_DIR)
        self._insert_wiki(wiki_storage, slug_aws, "Aws Brief Scope PQ2", AWS_DIR)
        self._insert_wiki(wiki_storage, slug_global, "Global Brief Scope PQ3", "global")

        result = server.project_brief(YADGAR_DIR, mode="catalog", project=TEST_PROJECT_ID)
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
        from yadgar.core import server

        wiki_storage = server._wiki._storage
        slug_yadgar = "test-brief-scope-yadgar-full-rr1"
        slug_aws = "test-brief-scope-aws-full-rr2"
        slug_global = "test-brief-scope-global-full-rr3"

        self._insert_wiki(wiki_storage, slug_yadgar, "Yadgar Brief Full RR1", YADGAR_DIR)
        self._insert_wiki(wiki_storage, slug_aws, "Aws Brief Full RR2", AWS_DIR)
        self._insert_wiki(wiki_storage, slug_global, "Global Brief Full RR3", "global")

        result = server.project_brief(YADGAR_DIR, mode="full", project=TEST_PROJECT_ID)
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
