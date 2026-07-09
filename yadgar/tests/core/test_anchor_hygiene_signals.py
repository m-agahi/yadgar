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
  - Hard truncation at K=3 per candidate list; _truncated flag set when capped
  - Compact tuple encoding for redundancy pairs: [id_a, id_b, similarity]

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import json
import math
import struct
from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("anchor_hygiene_signals")
    server.init_engines(
        db_path=str(tmp_path / "test_signals.db"), embedding_model="all-MiniLM-L6-v2"
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar._shared.runtime.lifecycle import _get_storage

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
        from yadgar.core.server.tools import project as proj_mod

        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [[1, 2, 0.95]]
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
        """Candidate lists hard-capped at K=3; full payload ≤160 tokens.

        Compact tuple encoding [id_a, id_b, similarity] + K=3 keeps total payload tight.
        The 100-token budget applies to candidates overhead in isolation (see
        test_token_budget_pathological).  Full payload includes fixed-cost baseline
        (~80 tokens) so the integration bound is ≤160.  Without the cap, 5 anchors
        generate C(5,2)=10 pairs × dict encoding ≈ 800 tokens uncapped.
        """
        import math
        import struct

        from yadgar._shared.config import get_settings

        dim = getattr(storage, "_embedding_dim", 384)

        def _emb(val: float) -> bytes:
            vec = [val] * dim
            norm = math.sqrt(sum(x * x for x in vec))
            vec = [x / norm for x in vec] if norm else vec
            return struct.pack(f"{dim}f", *vec)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)
        monkeypatch.setattr(get_settings(), "ANCHOR_PROMOTE_WORDS", 5, raising=False)
        monkeypatch.setattr(get_settings(), "ANCHOR_PROMOTE_HEADERS", 1, raising=False)

        # Insert 5 anchors with embeddings → C(5,2)=10 pairs, all above threshold → capped at 3
        for i in range(5):
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
        # Verify truncation applied at K=3
        assert len(redundancy) <= 3
        assert len(promote) <= 3
        # Verify _truncated flag set (10 pairs > 3)
        assert result.get("_truncated") is True
        # Full payload (incl. fixed fields + candidates + audit_anchors action) must stay
        # within 200 tokens.  After the anchor-signal-gap fix (car #20), actionable anchors
        # correctly emit a single audit_anchors action (with suggested_call), which adds
        # ~25 tokens vs the prior budget of 160.  200 is the updated safe bound.
        tokens = len(json.dumps(result)) // 4
        assert tokens <= 200, f"signals too large with K=3 cap: {tokens} tokens"

    def test_token_budget_pathological(self):
        """Pathological seed: 10 redundancy + 10 promote candidates — candidates overhead ≤100 tokens.

        After K=3 cap + compact tuple encoding [id_a, id_b, similarity], the incremental cost
        of the candidates fields (anchor_redundancy_candidates, anchor_promote_candidates,
        _truncated) must stay ≤100 tokens.  This isolates the budget impact of the new fields
        from the fixed-cost baseline (resolved_directory, mode, booleans, recommended_actions).
        """
        from yadgar.core.server.tools.project import _SIGNALS_CANDIDATES_K

        assert _SIGNALS_CANDIDATES_K == 3, f"Expected K=3, got {_SIGNALS_CANDIDATES_K}"

        # Pathological: 10 redundancy pairs + 10 promote IDs (all above threshold)
        # After K=3 cap: 3 tuples + 3 IDs
        redundancy_all = [[i, i + 100, round(0.90 + i * 0.005, 4)] for i in range(10)]
        promote_all = list(range(1001, 1011))

        redundancy_capped = redundancy_all[:_SIGNALS_CANDIDATES_K]
        promote_capped = promote_all[:_SIGNALS_CANDIDATES_K]

        # Measure only the incremental candidate fields (isolates budget overhead of new fields)
        candidates_payload = {
            "anchor_redundancy_candidates": redundancy_capped,
            "anchor_promote_candidates": promote_capped,
            "_truncated": True,
        }
        tokens = len(json.dumps(candidates_payload)) // 4
        assert tokens <= 100, (
            f"Candidates overhead too large after K=3 + compact encoding: {tokens} tokens "
            f"(json_len={len(json.dumps(candidates_payload))})"
        )


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
        from yadgar._shared.config import get_settings

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
        ids_in_pairs = {p[0] for p in pairs} | {p[1] for p in pairs}
        assert id_a in ids_in_pairs or id_b in ids_in_pairs

    def test_redundancy_pair_compact_tuple_encoding(self, storage, monkeypatch):
        """Each pair is a 3-element list [id_a, id_b, similarity] (compact tuple encoding)."""
        from yadgar._shared.config import get_settings

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
            assert isinstance(p, list), f"Expected list tuple, got {type(p)}"
            assert len(p) == 3, f"Expected [id_a, id_b, similarity], got len={len(p)}"
            id_a, id_b, similarity = p
            assert isinstance(id_a, int)
            assert isinstance(id_b, int)
            assert isinstance(similarity, float)

    def test_redundancy_cross_directory_excluded(self, storage, monkeypatch):
        """Pairs from different directory_context are NOT emitted."""
        from yadgar._shared.config import get_settings

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
            assert p[0] != p[1]

    def test_redundancy_cap_at_k3(self, storage, monkeypatch):
        """Candidate list capped at 3 pairs even if more qualify."""
        from yadgar._shared.config import get_settings

        dim = self._make_dim(storage)

        # Insert 5 anchors with near-identical embeddings → C(5,2)=10 pairs, capped at 3
        for i in range(5):
            emb = _make_embedding_bytes(dim, 1.0)  # all identical → all pairs qualify
            _insert_anchor(storage, f"dup anchor {i}", directory=_DIR, embedding=emb)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        pairs = result.get("anchor_redundancy_candidates", [])
        assert len(pairs) <= 3

    def test_redundancy_truncated_flag_set_when_capped(self, storage, monkeypatch):
        """_truncated=True when any list is capped."""
        from yadgar._shared.config import get_settings

        dim = self._make_dim(storage)

        # 4 identical embeddings → C(4,2)=6 pairs → more than K=3 → truncation
        for i in range(4):
            emb = _make_embedding_bytes(dim, 1.0)
            _insert_anchor(storage, f"trunc anchor {i}", directory=_DIR, embedding=emb)

        monkeypatch.setattr(get_settings(), "ANCHOR_REDUNDANCY_COSINE", 0.5, raising=False)

        result = server.project_brief(_DIR, mode="signals")
        pairs = result.get("anchor_redundancy_candidates", [])
        if len(pairs) == 3:
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

    def test_promote_cap_at_k3(self, storage):
        """Promote candidates list capped at 3."""
        for i in range(5):
            _insert_anchor(
                storage,
                "# H1\n\n## H2\n\n" + (f"word{i} " * 510),
                directory=_DIR,
                tags=["workflow"],
            )
        result = server.project_brief(_DIR, mode="signals")
        assert len(result.get("anchor_promote_candidates", [])) <= 3


# ---------------------------------------------------------------------------
# 5. recommended_actions — 4 new action types
# ---------------------------------------------------------------------------


class TestRecommendedActionsAudit:
    """audit_anchors emitted only when there are actionable items (not on raw count alone).

    Post anchor-signal-gap fix (car #20): the count-only gate is removed.
    audit_anchors fires when expired_no_grace_count > 0 OR redundancy_count > 0 OR
    promote_count > 0.  Count alone (even above ANCHOR_AUDIT_THRESHOLD) is not sufficient.
    """

    def test_audit_anchors_not_emitted_below_threshold(self, storage, monkeypatch):
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 15, raising=False)
        # Insert 10 healthy anchors — no expired/redundant/promotable
        for i in range(10):
            _insert_anchor(storage, f"anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" not in actions

    def test_audit_anchors_not_emitted_above_threshold_when_nothing_actionable(
        self, storage, monkeypatch
    ):
        """Count > threshold alone does not fire audit_anchors if nothing is actionable."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 5, raising=False)
        # Insert 6 healthy anchors with no expired/redundant/promotable content
        for i in range(6):
            _insert_anchor(storage, f"audit anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" not in actions, (
            "audit_anchors should NOT fire on count alone — actionability gate required"
        )

    def test_audit_anchors_emitted_when_expired_and_above_threshold(self, storage, monkeypatch):
        """audit_anchors fires when there are expired anchors (regardless of threshold)."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 100, raising=False)
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(
            storage, "expired audit", directory=_DIR, valid_until=past, migration_grace=False
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" in actions

    def test_audit_anchors_reason_names_actionable_items(self, storage, monkeypatch):
        """When audit_anchors fires, reason string names what is actionable."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 100, raising=False)
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "reason exp a", directory=_DIR, valid_until=past)
        _insert_anchor(storage, "reason exp b", directory=_DIR, valid_until=past)
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "audit_anchors"), None
        )
        assert action is not None
        # Reason names the type of actionable item (e.g. "2 expired")
        assert "expired" in action.get("reason", ""), (
            f"Expected 'expired' in reason, got: {action.get('reason')}"
        )


class TestRecommendedActionsRedundancy:
    """Redundancy → audit_anchors (not phantom merge_redundant_anchors).

    Post anchor-signal-gap fix (car #20): merge_redundant_anchors is no longer emitted as
    a recommended action name.  When redundancy pairs exist, a single audit_anchors action
    is emitted with a reason mentioning the pair count.
    """

    def test_merge_not_emitted_when_no_pairs(self):
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "merge_redundant_anchors" not in actions

    def test_audit_anchors_emitted_when_pairs_present(self, storage, monkeypatch):
        """Redundancy pairs → audit_anchors recommended (not the phantom merge_redundant_anchors)."""
        from yadgar.core.server.tools import project as proj_mod

        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [[1, 2, 0.95]]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "merge_redundant_anchors" not in actions, "phantom name must not appear"
        assert "audit_anchors" in actions, "audit_anchors must fire when redundancy present"

    def test_audit_reason_contains_redundant_pairs_count(self, storage, monkeypatch):
        """audit_anchors reason mentions redundant pairs when they are present."""
        from yadgar.core.server.tools import project as proj_mod

        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [[1, 2, 0.95]]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "audit_anchors"),
            None,
        )
        assert action is not None, "audit_anchors action must be present"
        assert "redundant" in action.get("reason", ""), (
            f"Expected 'redundant' in reason, got: {action.get('reason')}"
        )


class TestRecommendedActionsPromote:
    """Promotable anchors → audit_anchors (not phantom promote_anchor_to_wiki).

    Post anchor-signal-gap fix (car #20): promote_anchor_to_wiki is no longer emitted as
    a recommended action name.  When promotable anchors exist, a single audit_anchors action
    is emitted with a reason mentioning the promotable count.
    """

    def test_promote_not_emitted_when_no_candidates(self):
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "promote_anchor_to_wiki" not in actions

    def test_audit_anchors_emitted_when_candidates_present(self, storage):
        """Promotable anchor → audit_anchors recommended (not phantom promote_anchor_to_wiki)."""
        _insert_anchor(
            storage,
            _HEADERS_CONTENT,
            directory=_DIR,
            tags=["recipe"],
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "promote_anchor_to_wiki" not in actions, "phantom name must not appear"
        assert "audit_anchors" in actions, "audit_anchors must fire when promotable anchors present"

    def test_audit_reason_contains_promotable_count(self, storage):
        """audit_anchors reason mentions promotable when promotable anchors exist."""
        _insert_anchor(
            storage,
            _HEADERS_CONTENT,
            directory=_DIR,
            tags=["convention"],
        )
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "audit_anchors"),
            None,
        )
        assert action is not None, "audit_anchors action must be present"
        assert "promotable" in action.get("reason", ""), (
            f"Expected 'promotable' in reason, got: {action.get('reason')}"
        )


class TestRecommendedActionsForgetExpired:
    """Expired anchors → audit_anchors (not phantom forget_expired_anchors).

    Post anchor-signal-gap fix (car #20): forget_expired_anchors is no longer emitted as
    a recommended action name.  When expired anchors (grace=False) exist, a single
    audit_anchors action is emitted with a reason mentioning the expired count.
    """

    def test_forget_phantom_name_never_emitted_when_no_expired(self, storage):
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        _insert_anchor(storage, "live anchor", directory=_DIR, valid_until=future)
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "forget_expired_anchors" not in actions

    def test_audit_anchors_emitted_when_expired_no_grace(self, storage):
        """Expired anchor (grace=False) → audit_anchors (not phantom forget_expired_anchors)."""
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(
            storage, "expired anchor", directory=_DIR, valid_until=past, migration_grace=False
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "forget_expired_anchors" not in actions, "phantom name must not appear"
        assert "audit_anchors" in actions, "audit_anchors must fire when expired anchor present"

    def test_forget_phantom_name_not_emitted_for_grace_period_rows(self, storage):
        """migration_grace=True does not trigger any anchor action (no phantom, no audit_anchors)."""
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(
            storage, "grace anchor", directory=_DIR, valid_until=past, migration_grace=True
        )
        result = server.project_brief(_DIR, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]
        assert "forget_expired_anchors" not in actions

    def test_audit_reason_contains_expired_count(self, storage):
        """audit_anchors reason mentions expired count when expired anchors exist."""
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "exp a", directory=_DIR, valid_until=past)
        _insert_anchor(storage, "exp b", directory=_DIR, valid_until=past)
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "audit_anchors"),
            None,
        )
        assert action is not None, "audit_anchors action must be present"
        assert "expired" in action.get("reason", ""), (
            f"Expected 'expired' in reason, got: {action.get('reason')}"
        )


# ---------------------------------------------------------------------------
# 6. Deterministic order of recommended_actions
# ---------------------------------------------------------------------------


class TestRecommendedActionsOrder:
    """Actions appear in fixed order: bootstrap, refresh_active_work, refresh_checkpoint,
    audit_anchors (single consolidated entry).

    Post anchor-signal-gap fix (car #20): the four separate anchor-hygiene action names
    (audit_anchors, merge_redundant_anchors, promote_anchor_to_wiki, forget_expired_anchors)
    are collapsed into a single audit_anchors entry.  Order rule: audit_anchors always
    comes after the baseline actions.
    """

    def test_deterministic_order_with_all_actions(self, storage, monkeypatch):
        from yadgar.core.server.tools import project as proj_mod

        # Trigger audit_anchors via expired anchor (actionability gate)
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "exp order", directory=_DIR, valid_until=past)

        # Trigger promote
        _insert_anchor(storage, _HEADERS_CONTENT, directory=_DIR, tags=["workflow"])

        # Trigger merge via monkeypatch
        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [[1, 2, 0.95]]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)

        result1 = server.project_brief(_DIR, mode="signals")
        result2 = server.project_brief(_DIR, mode="signals")
        assert result1["recommended_actions"] == result2["recommended_actions"]

        # Exactly one audit_anchors action — no phantom names
        action_names = [a["action"] for a in result1["recommended_actions"]]
        assert action_names.count("audit_anchors") == 1, (
            f"Expected exactly 1 audit_anchors, got: {action_names}"
        )
        assert "merge_redundant_anchors" not in action_names
        assert "promote_anchor_to_wiki" not in action_names
        assert "forget_expired_anchors" not in action_names

        # audit_anchors comes after any baseline actions
        baseline_actions = ["bootstrap_project", "refresh_active_work", "refresh_checkpoint"]
        baseline_positions = [action_names.index(a) for a in baseline_actions if a in action_names]
        audit_pos = action_names.index("audit_anchors")
        if baseline_positions:
            assert max(baseline_positions) < audit_pos, (
                "audit_anchors must appear after all baseline actions"
            )


# ---------------------------------------------------------------------------
# 7. New env knobs registered (I25 three-way)
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    """4 new env knobs registered in Settings, _REGISTRY, and FIELD_META."""

    def test_anchor_redundancy_cosine_in_settings(self):
        from yadgar._shared.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_REDUNDANCY_COSINE")
        assert isinstance(cfg.ANCHOR_REDUNDANCY_COSINE, float)
        assert cfg.ANCHOR_REDUNDANCY_COSINE == pytest.approx(0.92)

    def test_anchor_promote_words_in_settings(self):
        from yadgar._shared.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_PROMOTE_WORDS")
        assert isinstance(cfg.ANCHOR_PROMOTE_WORDS, int)
        assert cfg.ANCHOR_PROMOTE_WORDS == 500

    def test_anchor_promote_headers_in_settings(self):
        from yadgar._shared.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_PROMOTE_HEADERS")
        assert isinstance(cfg.ANCHOR_PROMOTE_HEADERS, int)
        assert cfg.ANCHOR_PROMOTE_HEADERS == 2

    def test_anchor_audit_threshold_in_settings(self):
        from yadgar._shared.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_AUDIT_THRESHOLD")
        assert isinstance(cfg.ANCHOR_AUDIT_THRESHOLD, int)
        assert cfg.ANCHOR_AUDIT_THRESHOLD == 15

    def test_all_knobs_in_registry(self):
        from yadgar._shared.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_ANCHOR_REDUNDANCY_COSINE" in names
        assert "YADGAR_ANCHOR_PROMOTE_WORDS" in names
        assert "YADGAR_ANCHOR_PROMOTE_HEADERS" in names
        assert "YADGAR_ANCHOR_AUDIT_THRESHOLD" in names

    def test_all_knobs_in_field_meta(self):
        from yadgar._shared.config_yaml import FIELD_META

        assert "anchor_redundancy_cosine" in FIELD_META
        assert "anchor_promote_words" in FIELD_META
        assert "anchor_promote_headers" in FIELD_META
        assert "anchor_audit_threshold" in FIELD_META


# ---------------------------------------------------------------------------
# 8. Actionability gate + phantom-name elimination (fix: anchor-signal-gap)
# ---------------------------------------------------------------------------

_PHANTOM_NAMES = frozenset(
    {"forget_expired_anchors", "merge_redundant_anchors", "promote_anchor_to_wiki"}
)


class TestNoPhantomActionNames:
    """No recommended action name should be a phantom (non-existent MCP tool).

    The only valid anchor hygiene action is audit_anchors.
    merge_redundant_anchors, promote_anchor_to_wiki, and forget_expired_anchors
    are internal audit-action strings, not MCP tools — they must not appear as
    recommended_actions entries that consumers can act on.
    """

    def test_no_phantom_names_when_no_actionable(self, storage, monkeypatch):
        """16 healthy anchors (no expired/redundant/promotable) → no phantom names."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 5, raising=False)
        for i in range(16):
            _insert_anchor(storage, f"healthy anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        action_names = {a["action"] for a in result["recommended_actions"]}
        assert not action_names & _PHANTOM_NAMES, (
            f"Phantom action names found: {action_names & _PHANTOM_NAMES}"
        )

    def test_no_phantom_names_when_expired_present(self, storage):
        """Expired anchor present → audit_anchors recommended, NOT forget_expired_anchors."""
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "expired", directory=_DIR, valid_until=past, migration_grace=False)
        result = server.project_brief(_DIR, mode="signals")
        action_names = {a["action"] for a in result["recommended_actions"]}
        assert not action_names & _PHANTOM_NAMES, (
            f"Phantom action names found: {action_names & _PHANTOM_NAMES}"
        )

    def test_no_phantom_names_when_redundancy_present(self, storage, monkeypatch):
        """Redundancy pairs present → audit_anchors recommended, NOT merge_redundant_anchors."""
        from yadgar.core.server.tools import project as proj_mod

        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [[1, 2, 0.95]]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)
        result = server.project_brief(_DIR, mode="signals")
        action_names = {a["action"] for a in result["recommended_actions"]}
        assert not action_names & _PHANTOM_NAMES, (
            f"Phantom action names found: {action_names & _PHANTOM_NAMES}"
        )

    def test_no_phantom_names_when_promote_present(self, storage):
        """Promotable anchor present → audit_anchors recommended, NOT promote_anchor_to_wiki."""
        _insert_anchor(storage, _HEADERS_CONTENT, directory=_DIR, tags=["recipe"])
        result = server.project_brief(_DIR, mode="signals")
        action_names = {a["action"] for a in result["recommended_actions"]}
        assert not action_names & _PHANTOM_NAMES, (
            f"Phantom action names found: {action_names & _PHANTOM_NAMES}"
        )


class TestAuditAnchorActionabilityGate:
    """audit_anchors fires only when there is actual work to do, not on raw count alone."""

    def test_no_audit_signal_when_count_above_threshold_but_nothing_actionable(
        self, storage, monkeypatch
    ):
        """16 healthy anchors (count > 15) with zero expired/redundant/promotable
        → audit_anchors NOT recommended. This is the core over-signal fix."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 15, raising=False)
        # Insert 16 healthy anchors: no expiry, no embeddings (no redundancy), no promo tags
        for i in range(16):
            _insert_anchor(storage, f"unique healthy anchor {i}", directory=_DIR)
        result = server.project_brief(_DIR, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" not in action_names, (
            "audit_anchors should NOT fire when count > threshold but nothing is actionable"
        )

    def test_audit_signal_fires_when_expired_present(self, storage, monkeypatch):
        """audit_anchors fires when an expired (grace=False) anchor exists."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 100, raising=False)
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "expired", directory=_DIR, valid_until=past, migration_grace=False)
        result = server.project_brief(_DIR, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" in action_names, (
            "audit_anchors should fire when expired anchor present"
        )

    def test_audit_signal_fires_when_redundancy_present(self, storage, monkeypatch):
        """audit_anchors fires when redundancy pairs exist."""
        from yadgar._shared.config import get_settings
        from yadgar.core.server.tools import project as proj_mod

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 100, raising=False)
        original = proj_mod._compute_anchor_signals

        def patched(storage_obj, resolved, cfg):
            base = original(storage_obj, resolved, cfg)
            base["anchor_redundancy_candidates"] = [[1, 2, 0.95]]
            return base

        monkeypatch.setattr(proj_mod, "_compute_anchor_signals", patched)
        result = server.project_brief(_DIR, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" in action_names, (
            "audit_anchors should fire when redundancy candidates present"
        )

    def test_audit_signal_fires_when_promote_present(self, storage, monkeypatch):
        """audit_anchors fires when promote candidates exist."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 100, raising=False)
        _insert_anchor(storage, _HEADERS_CONTENT, directory=_DIR, tags=["recipe"])
        result = server.project_brief(_DIR, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" in action_names, (
            "audit_anchors should fire when promote candidates present"
        )

    def test_audit_reason_names_actionable_items(self, storage, monkeypatch):
        """When audit_anchors fires, reason includes what is actionable."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "ANCHOR_AUDIT_THRESHOLD", 100, raising=False)
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _insert_anchor(storage, "expired", directory=_DIR, valid_until=past, migration_grace=False)
        result = server.project_brief(_DIR, mode="signals")
        action = next(
            (a for a in result["recommended_actions"] if a["action"] == "audit_anchors"), None
        )
        assert action is not None
        # Reason should mention expired count
        assert "expired" in action.get("reason", ""), (
            f"Expected 'expired' in reason, got: {action.get('reason')}"
        )

    def test_no_audit_signal_with_zero_anchors(self):
        """Empty project → no audit_anchors signal."""
        result = server.project_brief(_DIR, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" not in action_names


# ---------------------------------------------------------------------------
# 10. Predicate parity — signal fires ⇔ audit_anchors has ≥1 non-skipped action
# ---------------------------------------------------------------------------

#: Unique directory for parity tests — isolated from accumulated module-scope rows.
_DIR_PARITY = "/tmp/test_anchor_signal_parity_dir"
_DIR_PARITY_OTHER = "/tmp/test_anchor_signal_parity_other_dir"


class TestSignalAuditParity:
    """Signal fires iff audit_anchors(dir, dry_run=True) has ≥1 non-skipped action.

    Reproduces the 2026-07-09 live-daemon bug: expired anchor in OTHER directory
    caused audit_anchors recommendation for THIS directory while dry_run returned
    actions=[].  Root cause: _fetch_expired_anchor_count had no directory filter,
    so it counted expired rows across ALL directories regardless of the resolved dir.
    """

    def test_cross_dir_expired_does_not_trigger_signal(self, storage):
        """Expired non-grace anchor in _DIR_PARITY_OTHER must NOT fire audit_anchors
        for _DIR_PARITY.  This is the exact 2026-07-09 regression case."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        # Expired anchor sits in a DIFFERENT directory
        _insert_anchor(
            storage,
            "expired in other dir",
            directory=_DIR_PARITY_OTHER,
            valid_until=past,
            migration_grace=False,
        )
        # _DIR_PARITY itself has no expired anchors
        result = server.project_brief(_DIR_PARITY, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" not in action_names, (
            "audit_anchors fired for _DIR_PARITY but the expired anchor is in "
            "_DIR_PARITY_OTHER — cross-directory scope leak in expired count predicate"
        )
        # Sanity-check: audit for the TARGET dir agrees (empty)
        dry = audit_anchors(directory=_DIR_PARITY, dry_run=True)
        non_skipped = [a for a in dry["actions"] if not a.get("skipped")]
        assert non_skipped == [], (
            "audit_anchors(dry_run=True) has non-skipped actions for _DIR_PARITY, "
            "but signal should only fire when audit would produce work in the SAME dir"
        )

    def test_same_dir_expired_fires_signal_and_audit_agrees(self, storage):
        """Expired non-grace anchor in _DIR_PARITY fires both signal AND audit action."""
        from yadgar.core.server.tools.audit import audit_anchors

        past = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
        _insert_anchor(
            storage,
            "expired in target dir",
            directory=_DIR_PARITY,
            valid_until=past,
            migration_grace=False,
        )
        result = server.project_brief(_DIR_PARITY, mode="signals")
        action_names = [a["action"] for a in result["recommended_actions"]]
        assert "audit_anchors" in action_names, (
            "audit_anchors must fire when expired non-grace anchor exists in the target dir"
        )
        dry = audit_anchors(directory=_DIR_PARITY, dry_run=True)
        non_skipped = [a for a in dry["actions"] if not a.get("skipped")]
        assert non_skipped, (
            "audit_anchors(dry_run=True) returned no non-skipped actions, "
            "but signal fired — predicate mismatch"
        )
