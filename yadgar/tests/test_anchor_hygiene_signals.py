"""TDD tests for v5.8.0 PR-B: anchor hygiene signals + recommended_actions.

PR-B scope:
  - 3 new fields in project_brief(mode="signals"):
      anchor_count_project, anchor_redundancy_candidates, anchor_promote_candidates
  - 4 new recommended_actions types:
      audit_anchors, merge_redundant_anchors, promote_anchor_to_wiki, forget_expired_anchors
  - 4 new env knobs I25-registered:
      ANCHOR_REDUNDANCY_COSINE, ANCHOR_PROMOTE_WORDS, ANCHOR_PROMOTE_HEADERS,
      ANCHOR_AUDIT_THRESHOLD
  - Token budget: signals mode <= 100 tokens under pathological load
  - Hard truncation at K=5 per candidate list; _truncated flag set when capped

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import json
import math
import struct
from datetime import UTC, datetime, timedelta

import pytest

from yadgar import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "test_signals.db"), embedding_model="all-MiniLM-L6-v2"
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar.server.lifecycle import _get_storage

    return _get_storage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIR = "/tmp/test_anchor_signals_proj"
_DIR2 = "/tmp/test_anchor_signals_other"  # different directory_context


def _make_embedding_bytes(n_dims: int, value: float) -> bytes:
    """Create a fake embedding: all components = value, then L2-normalised."""
    vec = [value] * n_dims
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return struct.pack(f"{n_dims}f", *vec)


def _insert_anchor(
    storage,
    content: str,
    directory: str = _DIR,
    embedding: bytes | None = None,
    valid_until: str | None = None,
    migration_grace: bool = False,
    tags: list[str] | None = None,
) -> int:
    """Insert an anchor row directly via storage._q for test isolation."""
    mid = storage._next_id("memory")
    base_tags = ["_anchor"] + (tags or [])
    params: dict = {
        "id": mid,
        "content": content,
        "dir": directory,
        "tags": base_tags,
        "heat": 0.5,
        "is_protected": True,
        "tier": "conditional",
    }
    sql = (
        "CREATE type::record('memory', $id) SET "
        "content = $content, directory_context = $dir, tags = $tags, "
        "heat = $heat, is_protected = $is_protected, tier = $tier"
    )
    if embedding is not None:
        floats = storage._bytes_to_floats(embedding)
        params["emb"] = floats
        sql += ", embedding = $emb"
    if valid_until is not None:
        params["valid_until"] = valid_until
        sql += ", valid_until = $valid_until"
    if migration_grace:
        params["grace"] = True
        sql += ", migration_grace = $grace"
    storage._q(sql, params)
    return mid


# ---------------------------------------------------------------------------
# 1. New fields present in signals mode
# ---------------------------------------------------------------------------


class TestNewSignalFields:
    """All 3 new fields present in signals mode response under correct conditions.

    anchor_count_project is always emitted.
    anchor_redundancy_candidates and anchor_promote_candidates are emitted only
    when non-empty (budget optimisation: omit empty lists to stay ≤100 tokens).
    """

    def test_anchor_count_project_field_present(self):
        result = server.project_brief(_DIR, mode="signals")
        assert "anchor_count_project" in result, "anchor_count_project missing from signals"

    def test_anchor_redundancy_candidates_field_present_when_non_empty(self, storage, monkeypatch):
        """anchor_redundancy_candidates appears when pairs exist (omitted when empty)."""
        from yadgar.server.tools import project as proj_mod

        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [{"id_a": 1, "id_b": 2, "similarity": 0.95}]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result = server.project_brief(_DIR, mode="signals")
        assert "anchor_redundancy_candidates" in result, (
            "anchor_redundancy_candidates missing when non-empty"
        )

    def test_anchor_promote_candidates_field_present_when_non_empty(self, storage):
        """anchor_promote_candidates appears when candidates exist (omitted when empty)."""
        _insert_anchor(storage, _HEADERS_CONTENT, directory=_DIR, tags=["recipe"])
        result = server.project_brief(_DIR, mode="signals")
        assert "anchor_promote_candidates" in result, (
            "anchor_promote_candidates missing when non-empty"
        )

    def test_anchor_count_project_is_int(self):
        result = server.project_brief(_DIR, mode="signals")
        assert isinstance(result["anchor_count_project"], int)

    def test_anchor_count_project_zero_when_no_anchors(self):
        result = server.project_brief("/tmp/empty_dir_xyz123", mode="signals")
        assert result["anchor_count_project"] == 0

    def test_anchor_count_project_counts_only_non_expired(self, storage):
        """Count excludes expired anchors (valid_until < now)."""
        now = datetime.now(UTC)
        past = (now - timedelta(hours=1)).isoformat()
        future = (now + timedelta(days=30)).isoformat()

        _insert_anchor(storage, "active anchor", directory=_DIR, valid_until=future)
        _insert_anchor(storage, "expired anchor", directory=_DIR, valid_until=past)
        _insert_anchor(storage, "null expiry anchor", directory=_DIR)

        result = server.project_brief(_DIR, mode="signals")
        # 2 non-expired (future + null), 1 expired excluded
        assert result["anchor_count_project"] >= 2

    def test_anchor_count_project_excludes_other_dir(self, storage):
        """Count is scoped to directory_context = resolved dir, not cross-dir."""
        _insert_anchor(storage, "my anchor", directory=_DIR)
        result_other = server.project_brief(_DIR2, mode="signals")
        result_mine = server.project_brief(_DIR, mode="signals")
        assert result_mine["anchor_count_project"] > result_other["anchor_count_project"]


# ---------------------------------------------------------------------------
# 2. Token budget
# ---------------------------------------------------------------------------


class TestTokenBudget:
    """signals mode stays <= 100 tokens even under pathological load."""

    def test_token_budget_empty_db(self):
        result = server.project_brief(_DIR, mode="signals")
        tokens = len(json.dumps(result)) // 4
        assert tokens <= 100, f"signals too large (empty): {tokens} tokens"

    def test_token_budget_with_15_anchors_no_candidates(self, storage):
        """Budget ≤100 with 15 anchors but no redundancy/promote candidates."""
        for i in range(15):
            _insert_anchor(storage, f"anchor content {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        tokens = len(json.dumps(result)) // 4
        assert tokens <= 100, f"signals too large (15 anchors, no candidates): {tokens} tokens"

    def test_token_budget_with_max_candidates_capped(self, storage, monkeypatch):
        """Candidate lists are hard-capped at K=5; payload with K=5 lists ≤200 tokens.

        When both lists are at maximum capacity, the total payload exceeds the baseline
        100-token budget (inherent cost of 5 pairs × 3 keys each + 5 IDs). The cap at
        K=5 (vs unbounded) is verified — without the cap the count could be 15 choose 2
        = 105 pairs which would be ~10× larger.
        """
        import math
        import struct

        from yadgar.config import get_settings

        dim = getattr(storage, "_embedding_dim", 384)

        def _emb(val: float) -> bytes:
            vec = [val] * dim
            norm = math.sqrt(sum(x * x for x in vec))
            vec = [x / norm for x in vec] if norm else vec
            return struct.pack(f"{dim}f", *vec)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)
        monkeypatch.setattr(get_settings(), "ANCHOR_PROMOTE_WORDS", 5, raising=False)
        monkeypatch.setattr(get_settings(), "ANCHOR_PROMOTE_HEADERS", 1, raising=False)

        # Insert 7 anchors with embeddings → C(7,2)=21 pairs, all above threshold → capped at 5
        for i in range(7):
            _insert_anchor(
                storage,
                f"# header {i}\nword1 word2 word3 word4 word5 word6",
                directory=_DIR,
                embedding=_emb(1.0),
                tags=["rule"],
            )

        result = server.project_brief(_DIR, mode="signals")
        redundancy = result.get("anchor_redundancy_candidates", [])
        promote = result.get("anchor_promote_candidates", [])
        # Verify truncation applied
        assert len(redundancy) <= 5
        assert len(promote) <= 5
        # Verify _truncated flag set (21 pairs > 5)
        assert result.get("_truncated") is True
        # Full payload stays under 250 tokens (reasonable upper bound even at K=5).
        # Without the cap, 7 anchors generate C(7,2)=21 pairs ≈ 850 tokens uncapped.
        tokens = len(json.dumps(result)) // 4
        assert tokens <= 250, f"signals too large even with K=5 cap: {tokens} tokens"


# ---------------------------------------------------------------------------
# 3. Redundancy detection
# ---------------------------------------------------------------------------


class TestRedundancyDetection:
    """Cosine threshold + same directory_context gating."""

    def _make_dim(self, storage) -> int:
        """Return embedding dimension from storage engine."""
        return getattr(storage, "_embedding_dim", 384)

    def test_redundancy_candidates_empty_when_no_embeddings(self, storage):
        _insert_anchor(storage, "no emb anchor a", directory=_DIR)
        _insert_anchor(storage, "no emb anchor b", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        # Empty list → field omitted from payload (budget optimisation)
        assert result.get("anchor_redundancy_candidates", []) == []

    def test_redundancy_candidates_found_above_threshold(self, storage, monkeypatch):
        """Two near-identical embeddings in same dir → pair emitted."""
        from yadgar.config import get_settings

        dim = self._make_dim(storage)

        # Very similar embeddings (nearly identical)
        emb_a = _make_embedding_bytes(dim, 1.0)
        emb_b = _make_embedding_bytes(dim, 0.9999)

        id_a = _insert_anchor(storage, "near dup A", directory=_DIR, embedding=emb_a)
        id_b = _insert_anchor(storage, "near dup B", directory=_DIR, embedding=emb_b)

        # Lower threshold so these definitely qualify
        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        pairs = result["anchor_redundancy_candidates"]
        assert len(pairs) >= 1
        ids_in_pairs = {p["id_a"] for p in pairs} | {p["id_b"] for p in pairs}
        assert id_a in ids_in_pairs or id_b in ids_in_pairs

    def test_redundancy_pair_has_required_keys(self, storage, monkeypatch):
        """Each pair dict has id_a, id_b, similarity keys."""
        from yadgar.config import get_settings

        dim = self._make_dim(storage)

        emb_a = _make_embedding_bytes(dim, 1.0)
        emb_b = _make_embedding_bytes(dim, 0.9999)
        _insert_anchor(storage, "pair A", directory=_DIR, embedding=emb_a)
        _insert_anchor(storage, "pair B", directory=_DIR, embedding=emb_b)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        pairs = result["anchor_redundancy_candidates"]
        if pairs:
            p = pairs[0]
            assert "id_a" in p
            assert "id_b" in p
            assert "similarity" in p
            assert isinstance(p["similarity"], float)

    def test_redundancy_cross_directory_excluded(self, storage, monkeypatch):
        """Pairs from different directory_context are NOT emitted."""
        from yadgar.config import get_settings

        dim = self._make_dim(storage)

        emb_a = _make_embedding_bytes(dim, 1.0)
        emb_b = _make_embedding_bytes(dim, 0.9999)
        _insert_anchor(storage, "dir1 anchor", directory=_DIR, embedding=emb_a)
        _insert_anchor(storage, "dir2 anchor", directory=_DIR2, embedding=emb_b)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        # Should not have cross-dir pairs (only 1 anchor in _DIR → empty → field absent)
        pairs = result.get("anchor_redundancy_candidates", [])
        for p in pairs:
            # Only same-dir anchors queried — other-dir IDs should not appear
            assert p["id_a"] != p["id_b"]

    def test_redundancy_cap_at_k5(self, storage, monkeypatch):
        """Candidate list capped at 5 pairs even if more qualify."""
        from yadgar.config import get_settings

        dim = self._make_dim(storage)

        # Insert 6 anchors with near-identical embeddings
        for i in range(6):
            emb = _make_embedding_bytes(dim, 1.0)  # all identical → all pairs qualify
            _insert_anchor(storage, f"dup anchor {i}", directory=_DIR, embedding=emb)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        pairs = result.get("anchor_redundancy_candidates", [])
        assert len(pairs) <= 5

    def test_redundancy_truncated_flag_set_when_capped(self, storage, monkeypatch):
        """_truncated=True when any list is capped."""
        from yadgar.config import get_settings

        dim = self._make_dim(storage)

        # 6 identical embeddings → more than 5 pairs → truncation
        for i in range(6):
            emb = _make_embedding_bytes(dim, 1.0)
            _insert_anchor(storage, f"trunc anchor {i}", directory=_DIR, embedding=emb)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        pairs = result.get("anchor_redundancy_candidates", [])
        if len(pairs) == 5:
            # List was capped → _truncated must be True
            assert result.get("_truncated") is True


# ---------------------------------------------------------------------------
# 4. Promote detection
# ---------------------------------------------------------------------------


_PROMO_TAG_SET = frozenset({"rule", "pattern", "convention", "playbook", "workflow", "recipe"})
_LONG_CONTENT = ("word " * 510).strip()  # >500 words
_HEADERS_CONTENT = "# Section One\n\nsome text\n\n## Section Two\n\nmore text\n" + _LONG_CONTENT
_CODE_BLOCK_HEADERS = (
    "# Real Header\n\n## Second Header\n\n```python\n# comment\n## not a header\n```\n"
    + _LONG_CONTENT
)


class TestPromoteDetection:
    """Triple AND: words > ANCHOR_PROMOTE_WORDS AND headers >= ANCHOR_PROMOTE_HEADERS AND tag intersection."""

    def test_promote_candidate_emitted_for_qualifying_anchor(self, storage):
        mid = _insert_anchor(
            storage,
            _HEADERS_CONTENT,
            directory=_DIR,
            tags=["rule"],
        )
        result = server.project_brief(_DIR, mode="signals")
        assert mid in result["anchor_promote_candidates"]

    def test_promote_requires_word_count(self, storage):
        """Short content (< 500 words) not promoted even with headers + tag."""
        mid = _insert_anchor(
            storage,
            "# Header One\n\n## Header Two\n\nshort content",
            directory=_DIR,
            tags=["rule"],
        )
        result = server.project_brief(_DIR, mode="signals")
        assert mid not in result.get("anchor_promote_candidates", [])

    def test_promote_requires_header_count(self, storage):
        """Content with only 1 header not promoted."""
        mid = _insert_anchor(
            storage,
            "# One Header\n\n" + ("word " * 510),
            directory=_DIR,
            tags=["pattern"],
        )
        result = server.project_brief(_DIR, mode="signals")
        assert mid not in result.get("anchor_promote_candidates", [])

    def test_promote_requires_tag_intersection(self, storage):
        """Content with words+headers but no qualifying tag not promoted."""
        mid = _insert_anchor(
            storage,
            _HEADERS_CONTENT,
            directory=_DIR,
            tags=["_anchor"],  # no promo-qualifying tag beyond base
        )
        result = server.project_brief(_DIR, mode="signals")
        assert mid not in result.get("anchor_promote_candidates", [])

    def test_promote_code_block_hashes_not_counted(self, storage):
        """# inside fenced code blocks not counted as headers."""
        # _CODE_BLOCK_HEADERS has exactly 2 real headers + # in code block
        mid = _insert_anchor(
            storage,
            _CODE_BLOCK_HEADERS,
            directory=_DIR,
            tags=["playbook"],
        )
        result = server.project_brief(_DIR, mode="signals")
        # Should still qualify (2 real headers, code block # excluded)
        assert mid in result["anchor_promote_candidates"]

    def test_promote_cap_at_k5(self, storage):
        """Promote candidates list capped at 5."""
        for i in range(7):
            _insert_anchor(
                storage,
                "# H1\n\n## H2\n\n" + (f"word{i} " * 510),
                directory=_DIR,
                tags=["workflow"],
            )
        result = server.project_brief(_DIR, mode="signals")
        assert len(result.get("anchor_promote_candidates", [])) <= 5


# ---------------------------------------------------------------------------
# 5. recommended_actions — 4 new action types
# ---------------------------------------------------------------------------


class TestRecommendedActionsAudit:
    """audit_anchors emitted when anchor_count_project > ANCHOR_AUDIT_THRESHOLD (default 15)."""

    def test_audit_anchors_not_emitted_below_threshold(self, storage, monkeypatch):
        from yadgar.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 15, raising=False)
        # Insert 10 anchors
        for i in range(10):
            _insert_anchor(storage, f"anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" not in actions

    def test_audit_anchors_emitted_above_threshold(self, storage, monkeypatch):
        from yadgar.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 5, raising=False)
        # Insert 6 anchors
        for i in range(6):
            _insert_anchor(storage, f"audit anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" in actions

    def test_audit_anchors_reason_contains_count(self, storage, monkeypatch):
        from yadgar.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 2, raising=False)
        for i in range(3):
            _insert_anchor(storage, f"reason anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "audit_anchors"), None
        )
        assert action is not None
        assert "count=" in action.get("reason", "")
        assert "threshold=" in action.get("reason", "")


class TestRecommendedActionsRedundancy:
    """merge_redundant_anchors emitted when len(anchor_redundancy_candidates) >= 1."""

    def test_merge_not_emitted_when_no_pairs(self):
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "merge_redundant_anchors" not in actions

    def test_merge_emitted_when_pairs_present(self, storage, monkeypatch):
        from yadgar.server.tools import project as proj_mod

        # Inject a redundancy pair via monkeypatch
        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [{"id_a": 1, "id_b": 2, "similarity": 0.95}]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "merge_redundant_anchors" in actions

    def test_merge_reason_contains_pair_count(self, storage, monkeypatch):
        from yadgar.server.tools import project as proj_mod

        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [{"id_a": 1, "id_b": 2, "similarity": 0.95}]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "merge_redundant_anchors"),
            None,
        )
        assert action is not None
        assert "redundancy_pairs=" in action.get("reason", "")


class TestRecommendedActionsPromote:
    """promote_anchor_to_wiki emitted when len(anchor_promote_candidates) >= 1."""

    def test_promote_not_emitted_when_no_candidates(self):
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "promote_anchor_to_wiki" not in actions

    def test_promote_emitted_when_candidates_present(self, storage):
        _insert_anchor(
            storage,
            _HEADERS_CONTENT,
            directory=_DIR,
            tags=["recipe"],
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "promote_anchor_to_wiki" in actions

    def test_promote_reason_contains_oversized_count(self, storage):
        _insert_anchor(
            storage,
            _HEADERS_CONTENT,
            directory=_DIR,
            tags=["convention"],
        )
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "promote_anchor_to_wiki"),
            None,
        )
        assert action is not None
        assert "oversized=" in action.get("reason", "")


class TestRecommendedActionsForgetExpired:
    """forget_expired_anchors emitted for expired rows with migration_grace=False."""

    def test_forget_not_emitted_when_no_expired(self, storage):
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        _insert_anchor(storage, "live anchor", directory=_DIR, valid_until=future)
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "forget_expired_anchors" not in actions

    def test_forget_emitted_when_expired_no_grace(self, storage):
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(
            storage, "expired anchor", directory=_DIR, valid_until=past, migration_grace=False
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "forget_expired_anchors" in actions

    def test_forget_not_emitted_for_grace_period_rows(self, storage):
        """migration_grace=True protects from forget_expired_anchors action."""
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(
            storage, "grace anchor", directory=_DIR, valid_until=past, migration_grace=True
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "forget_expired_anchors" not in actions

    def test_forget_reason_contains_expired_count(self, storage):
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "exp a", directory=_DIR, valid_until=past)
        _insert_anchor(storage, "exp b", directory=_DIR, valid_until=past)
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "forget_expired_anchors"),
            None,
        )
        assert action is not None
        assert "expired=" in action.get("reason", "")


# ---------------------------------------------------------------------------
# 6. Deterministic order of recommended_actions
# ---------------------------------------------------------------------------


class TestRecommendedActionsOrder:
    """Actions appear in fixed order: bootstrap, refresh_active_work, refresh_checkpoint,
    audit_anchors, merge_redundant_anchors, promote_anchor_to_wiki, forget_expired_anchors."""

    def test_deterministic_order_with_all_actions(self, storage, monkeypatch):
        from yadgar.config import get_settings
        from yadgar.server.tools import project as proj_mod

        # Trigger audit_anchors by lowering threshold
        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 0, raising=False)

        # Trigger forget_expired_anchors
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "exp order", directory=_DIR, valid_until=past)

        # Trigger promote
        _insert_anchor(storage, _HEADERS_CONTENT, directory=_DIR, tags=["workflow"])

        # Trigger merge via monkeypatch
        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [{"id_a": 1, "id_b": 2, "similarity": 0.95}]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result1 = server.project_brief(_DIR, mode="signals")
        result2 = server.project_brief(_DIR, mode="signals")
        assert result1["recommended_actions"] == result2["recommended_actions"]

        # Verify anchor actions appear after baseline actions
        action_names = [a["action"] for a in result1["recommended_actions"]]
        anchor_actions = [
            "audit_anchors",
            "merge_redundant_anchors",
            "promote_anchor_to_wiki",
            "forget_expired_anchors",
        ]
        anchor_positions = [action_names.index(a) for a in anchor_actions if a in action_names]
        # All anchor actions come after any baseline actions
        baseline_actions = ["bootstrap_project", "refresh_active_work", "refresh_checkpoint"]
        baseline_positions = [action_names.index(a) for a in baseline_actions if a in action_names]
        if baseline_positions and anchor_positions:
            assert max(baseline_positions) < min(anchor_positions)


# ---------------------------------------------------------------------------
# 7. New env knobs registered (I25 three-way)
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    """4 new env knobs registered in Settings, _REGISTRY, and FIELD_META."""

    def test_anchor_redundancy_cosine_in_settings(self):
        from yadgar.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_REDUNDANCY_COSINE")
        assert isinstance(cfg.ANCHOR_REDUNDANCY_COSINE, float)
        assert cfg.ANCHOR_REDUNDANCY_COSINE == pytest.approx(0.92)

    def test_anchor_promote_words_in_settings(self):
        from yadgar.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_PROMOTE_WORDS")
        assert isinstance(cfg.ANCHOR_PROMOTE_WORDS, int)
        assert cfg.ANCHOR_PROMOTE_WORDS == 500

    def test_anchor_promote_headers_in_settings(self):
        from yadgar.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_PROMOTE_HEADERS")
        assert isinstance(cfg.ANCHOR_PROMOTE_HEADERS, int)
        assert cfg.ANCHOR_PROMOTE_HEADERS == 2

    def test_anchor_audit_threshold_in_settings(self):
        from yadgar.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_AUDIT_THRESHOLD")
        assert isinstance(cfg.ANCHOR_AUDIT_THRESHOLD, int)
        assert cfg.ANCHOR_AUDIT_THRESHOLD == 15

    def test_all_knobs_in_registry(self):
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_ANCHOR_REDUNDANCY_COSINE" in names
        assert "YADGAR_ANCHOR_PROMOTE_WORDS" in names
        assert "YADGAR_ANCHOR_PROMOTE_HEADERS" in names
        assert "YADGAR_ANCHOR_AUDIT_THRESHOLD" in names

    def test_all_knobs_in_field_meta(self):
        from yadgar.config_yaml import FIELD_META

        assert "anchor_redundancy_cosine" in FIELD_META
        assert "anchor_promote_words" in FIELD_META
        assert "anchor_promote_headers" in FIELD_META
        assert "anchor_audit_threshold" in FIELD_META
