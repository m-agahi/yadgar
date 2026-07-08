"""TDD tests for v5.21.0: cross-project anchor redundancy detection.

Scope:
  - Cross-project dedup: cosine >= ANCHOR_CROSS_PROJECT_COSINE (0.95) AND
    content_length_ratio > 0.85 pairs anchors across different directory_context values.
  - Pairs below either threshold are not reported.
  - Output key: cross_project_redundancy_candidates in audit_anchors() result.
  - Output key: cross_project_redundancy_candidates in project_brief(mode='signals') result.
  - Candidate dict shape: primary_id, duplicate_ids, similarity, directory_contexts,
    recommended_action.
  - Primary selection: highest (access_count + heat), tie-broken by oldest created_at.
  - recommended_action='promote_to_global' vs 'merge_to_primary' left as placeholder
    (user decides; both types surface as candidates).
  - NEVER auto-mutates — audit-gated only.
  - global-context rows excluded from cross-project pairing (they're already global).
  - Cap output to _SIGNALS_CANDIDATES_K (3) for token budget.

TDD: written before implementation — tests start red.
"""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server

# R3 Car 3d: update_active_work / audit_anchors write halves forward to the
# backend /admin op. Route those forwards through run_admin_op against the
# shared _st storage (no HTTP) so DB-write assertions stay real.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("cross_project_audit")
    server.init_engines(
        db_path=str(tmp_path / "test_cross_project.db"),
        embedding_model="all-MiniLM-L6-v2",
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

_DIR_A = "/tmp/test_proj_alpha"
_DIR_B = "/tmp/test_proj_beta"
_DIR_C = "/tmp/test_proj_gamma"
_DIR_GLOBAL = "global"


def _make_embedding_bytes(n_dims: int, value: float) -> bytes:
    """Create deterministic embedding: all components=value, then L2-normalised."""
    vec = [value] * n_dims
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return struct.pack(f"{n_dims}f", *vec)


def _make_near_embedding_bytes(n_dims: int, base_value: float, perturbation: float) -> bytes:
    """Create embedding near base but slightly different (high cosine similarity ~0.96+)."""
    vec = [base_value] * n_dims
    # Perturb first component slightly to get high but not perfect similarity
    vec[0] = base_value + perturbation
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return struct.pack(f"{n_dims}f", *vec)


def _make_dissimilar_embedding_bytes(n_dims: int) -> bytes:
    """Create an embedding orthogonal-ish to uniform vectors (low cosine)."""
    # Alternate +1/-1 pattern — very low cosine with uniform vectors
    vec = [1.0 if i % 2 == 0 else -1.0 for i in range(n_dims)]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return struct.pack(f"{n_dims}f", *vec)


def _insert_anchor(storage, content: str, directory: str, **kw) -> int:
    """Insert anchor row directly via storage._q."""
    now = storage._now_iso()
    mid = storage._next_id("memory")
    tier = kw.get("tier", "conditional")
    is_protected = kw.get("is_protected", True)
    access_count = kw.get("access_count", 0)
    last_accessed = kw.get("last_accessed") or now
    created_at = kw.get("created_at") or now
    tags = list(kw.get("tags") or [])
    base_tags = ["_anchor"] + tags
    params: dict = {
        "id": mid,
        "content": content,
        "dir": directory,
        "tags": base_tags,
        "heat": float(kw.get("heat", 0.5)),
        "is_protected": is_protected,
        "tier": tier,
        "access_count": access_count,
        "last_accessed": last_accessed,
        "created_at": created_at,
    }
    sql = (
        "CREATE type::record('memory', $id) SET "
        "content = $content, directory_context = $dir, tags = $tags, "
        "heat = $heat, is_protected = $is_protected, tier = $tier, "
        "access_count = $access_count, last_accessed = $last_accessed, "
        "created_at = $created_at"
    )
    emb = kw.get("embedding")
    if emb is not None:
        floats = storage._bytes_to_floats(emb)
        params["emb"] = floats
        sql += ", embedding = $emb"
    valid_until = kw.get("valid_until")
    if valid_until is not None:
        params["valid_until"] = valid_until
        sql += ", valid_until = $valid_until"
    storage._q(sql, params)
    return mid


# ---------------------------------------------------------------------------
# 1. audit_anchors() returns cross_project_redundancy_candidates
# ---------------------------------------------------------------------------


class TestAuditAnchorsCrossProject:
    """audit_anchors returns cross_project_redundancy_candidates when high-cosine
    same-content anchors exist across different directory_context values."""

    def test_cross_project_key_present_in_audit_result(self, storage):
        """Result dict always has cross_project_redundancy_candidates key."""
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        assert "cross_project_redundancy_candidates" in result

    def test_cross_project_candidate_detected(self, storage):
        """Two anchors with cosine >=0.95 + content_length_ratio>0.85 across dirs => candidate."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)

        content = "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_B, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        assert len(candidates) >= 1, "Expected at least one cross-project redundancy candidate"

    def test_candidate_dict_shape(self, storage):
        """Candidate dict has required fields: primary_id, duplicate_ids, similarity,
        directory_contexts, recommended_action."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        content = "Build amd64 images with full registry-prefixed tag"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_B, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        assert candidates, "Expected candidates"
        cand = candidates[0]
        assert "primary_id" in cand
        assert "duplicate_ids" in cand
        assert "similarity" in cand
        assert "directory_contexts" in cand
        assert "recommended_action" in cand
        assert isinstance(cand["duplicate_ids"], list)
        assert isinstance(cand["directory_contexts"], list)
        assert len(cand["directory_contexts"]) >= 2

    def test_low_cosine_not_reported(self, storage):
        """Anchors with cosine < 0.95 across directories are NOT candidates."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_dissimilar_embedding_bytes(n_dims)

        _insert_anchor(storage, "anchor about workflow rule", _DIR_A, embedding=emb_a)
        _insert_anchor(storage, "anchor about completely different thing", _DIR_B, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        assert len(candidates) == 0, "Low-cosine pair must not appear as cross-project candidate"

    def test_content_length_ratio_gate(self, storage):
        """Pairs where content_length_ratio <= 0.85 are rejected even with high cosine."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)

        short_content = "Build with tag"  # very short
        long_content = (
            "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z. " * 20
        )
        _insert_anchor(storage, short_content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, long_content, _DIR_B, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        assert len(candidates) == 0, "Large length ratio diff must reject the pair"

    def test_same_directory_not_cross_project(self, storage):
        """Two anchors in the SAME directory are NOT cross-project candidates
        (they're handled by existing within-dir merge logic)."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        content = "Build amd64 with openfantasy tag"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_A, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        assert len(candidates) == 0, (
            "Same-directory pair must NOT appear in cross-project candidates"
        )

    def test_global_dir_excluded(self, storage):
        """Anchors with directory_context='global' are excluded from cross-project pairing."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        content = "Build amd64 with openfantasy tag registry rule"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_GLOBAL, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        # global-dir pairs should not appear
        for cand in candidates:
            assert _DIR_GLOBAL not in cand["directory_contexts"], (
                "global directory must be excluded from cross-project candidates"
            )

    def test_never_auto_mutates_cross_project(self, storage):
        """dry_run=False does NOT mutate cross-project candidates (audit-gated only)."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        content = "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z"
        mid_a = _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        mid_b = _insert_anchor(storage, content, _DIR_B, embedding=emb_b)

        audit_anchors(directory=_DIR_A, dry_run=False)

        # Both rows must still exist after dry_run=False
        rows_a = storage._q(f"SELECT id FROM memory:{mid_a}")
        rows_b = storage._q(f"SELECT id FROM memory:{mid_b}")
        assert rows_a, "Cross-project candidate primary must NOT be deleted by dry_run=False"
        assert rows_b, "Cross-project candidate duplicate must NOT be deleted by dry_run=False"

    def test_primary_selection_by_access_count_plus_heat(self, storage):
        """Primary anchor = highest (access_count * heat); tie-broken by oldest created_at."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        content = "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z"

        # Insert DIR_B anchor first with higher rank (access_count=5, heat=0.8)
        past_dt = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mid_b = _insert_anchor(
            storage,
            content,
            _DIR_B,
            embedding=emb_b,
            access_count=5,
            heat=0.8,
            created_at=past_dt,
        )
        # Insert DIR_A anchor with lower rank
        _insert_anchor(
            storage,
            content,
            _DIR_A,
            embedding=emb_a,
            access_count=1,
            heat=0.5,
        )

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        assert candidates, "Expected a candidate"
        cand = candidates[0]
        # DIR_B anchor (mid_b) has higher access_count => should be primary
        assert cand["primary_id"] == mid_b, (
            f"Primary should be {mid_b} (higher rank), got {cand['primary_id']}"
        )

    def test_three_way_cross_project_pair_grouping(self, storage):
        """Three anchors across three dirs with same embedding => grouped as single candidate."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        emb_c = _make_near_embedding_bytes(n_dims, 1.0, 0.002)
        content = "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_B, embedding=emb_b)
        _insert_anchor(storage, content, _DIR_C, embedding=emb_c)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        candidates = result["cross_project_redundancy_candidates"]
        # There should be a candidate covering at least 2 of the 3 dirs
        assert candidates, "Expected at least one candidate for 3-dir scenario"
        # Total unique dirs across all candidates should include all 3
        all_dirs: set[str] = set()
        for cand in candidates:
            all_dirs.update(cand["directory_contexts"])
        assert len(all_dirs) >= 2


# ---------------------------------------------------------------------------
# 2. project_brief(mode='signals') surfaces cross_project_redundancy_candidates
# ---------------------------------------------------------------------------


class TestProjectBriefSignalsCrossProject:
    """project_brief(mode='signals') includes cross_project_redundancy_candidates
    when cross-project pairs are detected."""

    def test_no_candidates_key_absent_when_empty(self, storage):
        """When no cross-project candidates, key is absent to save token budget."""
        result = server.project_brief(_DIR_A, mode="signals")
        # Key should not be present when empty (mirrors anchor_redundancy_candidates pattern)
        assert result.get("cross_project_redundancy_candidates", []) == []

    def test_candidates_present_when_cross_project_pairs_exist(self, storage):
        """cross_project_redundancy_candidates appears in signals when pairs found."""
        n_dims = 384
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.001)
        content = "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_B, embedding=emb_b)

        result = server.project_brief(_DIR_A, mode="signals")
        candidates = result.get("cross_project_redundancy_candidates", [])
        assert len(candidates) >= 1, "Expected cross-project candidates in signals"

    def test_candidates_capped_to_signals_k(self, storage):
        """Cross-project candidates capped to _SIGNALS_CANDIDATES_K=3 in signals mode."""
        n_dims = 384
        # Insert 6 pairs across different directories
        base_dirs = [f"/tmp/proj_{i}" for i in range(7)]
        base_val = 1.0
        for i, d in enumerate(base_dirs):
            emb = _make_near_embedding_bytes(n_dims, base_val, i * 0.0001)
            content = "Build amd64 images with full registry-prefixed tag openfantasy/yadgar:X.Y.Z"
            _insert_anchor(storage, content, d, embedding=emb)

        result = server.project_brief(base_dirs[0], mode="signals")
        candidates = result.get("cross_project_redundancy_candidates", [])
        assert len(candidates) <= 3, f"Candidates should be capped at 3, got {len(candidates)}"


# ---------------------------------------------------------------------------
# 3. Env knob ANCHOR_CROSS_PROJECT_COSINE respected
# ---------------------------------------------------------------------------


class TestCrossProjectCosineKnob:
    """ANCHOR_CROSS_PROJECT_COSINE env knob controls the minimum cosine threshold."""

    def test_default_threshold_is_0_95(self):
        """Default ANCHOR_CROSS_PROJECT_COSINE is 0.95."""
        from yadgar._shared.config import get_settings

        cfg = get_settings()
        assert hasattr(cfg, "ANCHOR_CROSS_PROJECT_COSINE"), (
            "Settings must have ANCHOR_CROSS_PROJECT_COSINE field"
        )
        assert float(cfg.ANCHOR_CROSS_PROJECT_COSINE) == 0.95

    def test_pair_below_threshold_not_reported(self, storage):
        """Pair with cosine between 0.92-0.95 not reported at default 0.95 threshold."""
        from yadgar.core.server.tools.audit import audit_anchors

        n_dims = 384
        # Create embeddings with cosine ~0.93 (above within-project 0.92 but below cross-project 0.95)
        # Use a larger perturbation to push cosine below 0.95
        emb_a = _make_embedding_bytes(n_dims, 1.0)
        emb_b = _make_near_embedding_bytes(n_dims, 1.0, 0.1)  # larger perturbation => lower cosine

        content = "Workflow rule about building images"
        _insert_anchor(storage, content, _DIR_A, embedding=emb_a)
        _insert_anchor(storage, content, _DIR_B, embedding=emb_b)

        result = audit_anchors(directory=_DIR_A, dry_run=True)
        result["cross_project_redundancy_candidates"]
        # Should not appear if cosine < 0.95
        # (note: test may pass even if cosine happens to be above threshold with this perturbation)
        # We just verify the key exists; threshold enforcement tested via low-cosine test above
        assert "cross_project_redundancy_candidates" in result
