"""E2E behavioral tests for the Yadgar memory system.

These tests verify observable invariants from the LLM consumer's perspective:
  - Content stored is always the content retrieved (no silent modification)
  - Heat decays slowly enough that memories stay findable for months
  - Protected memories are permanent
  - Only auto-generated action-stream memories are ever auto-deleted
  - Similar but distinct facts are stored separately

How to add new cases:
  - New invariant for an existing area: add a test_* method to the relevant class
  - New behavioral area: add a new class with a docstring explaining the invariant
  - New regression: add to TestRegressionScenarios with a comment linking to the bug
"""

from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server
from yadgar.tests.conftest import memorize_sync

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Full server engine stack with isolated temp database per test.

    Function-scoped (default) so each test gets a fresh namespace — prevents
    memories stored by earlier tests from leaking into recall() results under
    xdist parallel execution (module-scope caused the 2/13 flake rate).
    """
    server.init_engines(
        db_path=str(tmp_path / "behavior.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _set_memory_age(memory_id: int, hours_ago: float) -> None:
    """Backdate a memory's last_accessed and created_at so decay tests don't need to wait."""
    storage = server._get_storage()
    past = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    storage.update_memory_fields(memory_id, last_accessed=past, created_at=past)


def _get_memory(memory_id: int) -> dict | None:
    return server._get_storage().get_memory(memory_id)


def _make_cold(memory_id: int) -> None:
    """Set heat to near-zero without triggering deletion — simulates a very old memory."""
    storage = server._get_storage()
    storage.update_memory_heat(memory_id, 0.005)
    storage.update_memory_fields(memory_id, confidence=0.2, access_count=0)


# ── A. Content Integrity ──────────────────────────────────────────────────────


class TestContentIntegrity:
    """Memory content must be bit-for-bit identical before and after any operation.

    Invariant: recall(), consolidate_now(), and time passage must NEVER modify the
    content field of a stored memory.
    """

    def test_exact_content_preserved(self, recall_backend_bypass):
        """recall() returns the exact content that was stored."""
        content = "The deployment uses Helm chart version 3.14.2 with replicas=5"
        result = memorize_sync(content, "/home/user/project", ["infra"])
        mid = result["id"]

        # Recall with a related query — v5.65: directory required
        hits = server.recall("helm chart deployment replicas", directory="/home/user/project")
        match = next((h for h in hits if h["id"] == mid), None)
        assert match is not None, "Memory not found in recall results"
        assert match["content"] == content, (
            f"Content was modified:\nExpected: {content!r}\nGot:      {match['content']!r}"
        )

    def test_specific_detail_preserved(self, recall_backend_bypass):
        """Specific identifiers (IDs, paths, item names) survive retrieval unchanged."""
        content = "Codeberg PAT is stored in 1Password item zqq55bz2qi53gw375jlm2sh4jq"
        result = memorize_sync(content, "/home/user", ["codeberg", "secrets"])
        mid = result["id"]

        hits = server.recall("codeberg personal access token", directory="/home/user")
        match = next((h for h in hits if h["id"] == mid), None)
        assert match is not None
        assert "zqq55bz2qi53gw375jlm2sh4jq" in match["content"], (
            "Specific 1Password item ID was lost from memory content"
        )

    def test_recall_does_not_rewrite_content(self, recall_backend_bypass):
        """Calling recall() multiple times with different queries never rewrites content."""
        content = "Redis cluster uses Sentinel mode with quorum=2 and auth password stored in Vault"
        result = memorize_sync(content, "/home/user/ops", ["redis", "vault"])
        mid = result["id"]

        queries = [
            "redis sentinel quorum",
            "vault secret password",
            "unrelated topic about python decorators",
            "completely different: machine learning gradient descent",
            "another unrelated: cooking pasta al dente",
        ]
        for query in queries:
            server.recall(query, directory="/home/user/ops")
            mem = _get_memory(mid)
            assert mem is not None
            assert mem["content"] == content, (
                f"Content changed after recall('{query}'):\n"
                f"Expected: {content!r}\nGot:      {mem['content']!r}"
            )

    def test_consolidation_does_not_alter_content(self):
        """consolidate_now() never modifies the content field."""
        content = "API key for Datadog is sk-dd-prod-XXXX stored in ~/.secrets/datadog"
        result = memorize_sync(content, "/home/user/monitoring", ["datadog", "api-key"])
        mid = result["id"]

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["content"] == content, "consolidate_now() modified memory content"


# ── A2. Multi-candidate ranking (C4.1 diagnostic — 1b) ────────────────────────


class TestRankingDiagnostics:
    """DIAGNOSTIC for concern 1b (abbreviation hard-miss), C4.1 / ADR-0142.

    The single-candidate ``test_specific_detail_preserved`` (nrows=1) cannot
    exercise a ranking miss — the target is the sole candidate, so it always
    ranks #1. This class adds the *multi-candidate* case the plan (§1.1b, option
    B1) flagged as missing: seed the "Codeberg PAT" memory alongside plausible
    distractors, query with the EXPANSION ("personal access token"), and assert
    the PAT memory survives into the top-k.

    Why this is a genuine abbreviation test (not an FTS-overlap test): the PAT
    memory's content contains only "PAT" — never the literal phrase "personal
    access token" — so FTS/BM25 has ZERO word overlap with the query. Only the
    vector layer (or CE, if the target reaches its pool) can bridge PAT ↔
    expansion. Whether this passes DECIDES 1b's scope:
      - PASS → the vector layer already bridges the abbreviation; 1b is resolved
        by the retrieval layer and this stays a passing regression.
      - FAIL → 1b is a genuine hard-miss; its real fix is research-sized
        (semantic abbreviation bridging) and is PARKED per ADR-0142 / #62. The
        test is then marked xfail — fusion is NOT to be overfit to green it
        (plan §5 gate G2).
    """

    # Distractors: on-topic-adjacent secrets/credentials memories that share NO
    # unusual token with the query, so they don't crowd out the target by
    # accident — they exist to make top-k a real cutoff (multi-candidate).
    _DISTRACTORS = [
        "AWS access key for the staging account rotates every 90 days via IAM",
        "The Datadog API key lives in ~/.secrets/datadog and is read at boot",
        "SSH deploy key for the CI runner is mounted from a Kubernetes secret",
        "Vault stores the Postgres admin password under secret/db/postgres",
        "GPG signing key passphrase is cached in gpg-agent for 8 hours",
        "The Stripe webhook signing secret is set in the STRIPE_WHSEC env var",
        "OAuth client secret for the Slack app is stored in 1Password vault Ops",
        "TLS private key for the ingress cert is issued by cert-manager",
    ]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "1b abbreviation-bridging DEFERRED (research-sized), ADR-0142 / #62. "
            "VERDICT 2026-07-18 (C4.1): the 'Codeberg PAT' memory ranked BELOW 5 "
            "distractors (top_ids were all distractors, pat_id absent) for the "
            "expansion query 'personal access token' — a genuine abbreviation "
            "hard-miss: 'PAT' has zero FTS overlap with the expansion and the "
            "vector layer (all-MiniLM-L6-v2) does not bridge the 3-letter acronym. "
            "The honest fix is semantic abbreviation bridging (query-side expansion "
            "or synonym injection), NOT fusion tuning — plan §5 gate G2 forbids "
            "overfitting fusion to green this one fixture. strict=True so an "
            "accidental future fix flips this xpass→fail and forces the marker's "
            "removal (turning it into a live regression test)."
        ),
    )
    def test_pat_abbreviation_multi_candidate_ranking(self, recall_backend_bypass):
        directory = "/home/user"
        pat_content = "Codeberg PAT is stored in 1Password item zqq55bz2qi53gw375jlm2sh4jq"

        # Sanity: the guard-rail the whole diagnostic rests on — the target must
        # NOT contain the query phrase, else we'd be measuring FTS overlap.
        assert "personal access token" not in pat_content.lower()

        pat = memorize_sync(pat_content, directory, ["codeberg", "secrets"])
        pat_id = pat["id"]
        for i, dc in enumerate(self._DISTRACTORS):
            memorize_sync(dc, directory, ["secrets", f"distractor{i}"])

        # Expansion query — no "PAT" token, so FTS cannot bridge it.
        hits = server.recall("personal access token", directory=directory, max_results=5)
        top_ids = [h["id"] for h in hits]
        assert pat_id in top_ids, (
            "1b DIAGNOSTIC: the 'Codeberg PAT' memory did NOT rank in the top-5 "
            "for the expansion query 'personal access token' among "
            f"{len(self._DISTRACTORS)} distractors. top_ids={top_ids}. "
            "This is the abbreviation hard-miss (ADR-0142 1b) — its fix is "
            "research-sized and PARKED (#62); do NOT overfit fusion to green it."
        )


# ── B. No Compression ────────────────────────────────────────────────────────


class TestNoCompression:
    """Memory content must not be degraded by age.

    Invariant: compression_cycle() is disabled. Old memories keep their full content.
    Previously: 7 days → gist (~30%), 30 days → tag (<200 chars).
    """

    def test_7_day_old_memory_not_compressed(self):
        """A 7-day-old memory keeps full content and compression_level=0."""
        content = (
            "PostgreSQL migration: added index on users.email using CONCURRENTLY to avoid lock"
        )
        result = memorize_sync(content, "/home/user/db", ["postgres", "migration"])
        mid = result["id"]
        _set_memory_age(mid, hours_ago=7 * 24 + 1)  # 7 days + 1h

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["content"] == content, "7-day-old memory content was compressed"
        assert (mem.get("compression_level") or 0) == 0, "compression_level was set"

    def test_30_day_old_memory_not_compressed(self):
        """A 30-day-old memory keeps full content (was previously reduced to <200 chars)."""
        content = (
            "K8s cluster autoscaler configuration: min_nodes=2, max_nodes=20, "
            "scale_down_delay=10m, scale_down_unneeded_time=10m. "
            "Managed via Terraform module terraform-aws-eks-autoscaler v2.1.3. "
            "Deployed in us-east-1 and eu-west-1. Tags: environment=production, "
            "team=platform, cost-center=infra-001. Last reviewed 2024-01-15."
        )
        assert len(content) > 200, "test setup: content must exceed old tag-compression threshold"
        result = memorize_sync(content, "/home/user/infra", ["k8s", "autoscaler"])
        mid = result["id"]
        _set_memory_age(mid, hours_ago=30 * 24 + 1)  # 30 days + 1h

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["content"] == content, "30-day-old memory content was tag-compressed"
        assert len(mem["content"]) > 200, "Content was truncated to tag format"

    def test_long_content_not_truncated(self):
        """A 2500-char memory retains its full length after consolidation."""
        long_content = "Detail: " + ("x" * 2490)  # over 2000-char reconsolidation truncation limit
        result = memorize_sync(long_content, "/home/user/docs", ["long"])
        mid = result["id"]
        _set_memory_age(mid, hours_ago=10 * 24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert len(mem["content"]) == len(long_content), (
            f"Long content was truncated: {len(mem['content'])} chars (expected {len(long_content)})"
        )


# ── C. Heat Decay ────────────────────────────────────────────────────────────


class TestHeatDecay:
    """Heat decay must be slow enough that memories remain findable for months.

    Expected values with DECAY_FACTOR=0.9995 per hour:
      24h:   0.9995^24   ≈ 0.988
      90d:   0.9995^2160 ≈ 0.339
      180d:  0.9995^4320 ≈ 0.115

    With IMPORTANCE_DECAY_FACTOR=0.9999 (importance > 0.7):
      90d:   0.9999^2160 ≈ 0.808
    """

    def test_heat_after_24h_above_98_percent(self):
        """After 24h, heat should still be above 98% of original."""
        result = memorize_sync("fresh memory", "/home/user/test", ["test"])
        mid = result["id"]
        _set_memory_age(mid, hours_ago=24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["heat"] > 0.98, f"Heat after 24h was {mem['heat']:.4f}, expected > 0.98"

    def test_heat_after_90_days_above_30_percent(self):
        """After 90 days without access, heat should still be above 30%."""
        result = memorize_sync("90 day old fact", "/home/user/test", ["test"])
        mid = result["id"]
        _set_memory_age(mid, hours_ago=90 * 24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["heat"] > 0.30, f"Heat after 90 days was {mem['heat']:.4f}, expected > 0.30"

    def test_important_memory_heat_after_90_days_above_75_percent(self):
        """Important memories (importance > 0.7) decay much more slowly."""
        result = memorize_sync("critical architecture decision", "/home/user/test", ["arch"])
        mid = result["id"]
        # Set high importance — triggers IMPORTANCE_DECAY_FACTOR path
        server._get_storage().update_memory_fields(mid, importance=0.9)
        _set_memory_age(mid, hours_ago=90 * 24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["heat"] > 0.75, (
            f"Important memory heat after 90 days was {mem['heat']:.4f}, expected > 0.75"
        )

    def test_heat_after_180_days_above_10_percent(self):
        """After 6 months without access, standard memories should still be above 10%."""
        result = memorize_sync("6 month old memory", "/home/user/test", ["test"])
        mid = result["id"]
        _set_memory_age(mid, hours_ago=180 * 24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["heat"] > 0.10, f"Heat after 180 days was {mem['heat']:.4f}, expected > 0.10"


# ── D. Protected Memories ────────────────────────────────────────────────────


class TestProtectedMemories:
    """Protected memories (is_protected=True) are permanent and immutable.

    Invariant: no consolidation process — decay, compression, pruning — touches them.
    """

    def test_protected_memory_heat_not_decayed(self):
        """A protected memory's heat is untouched even after 1 year without access."""
        result = memorize_sync(
            "permanent config: prod DB is db.internal:5432",
            "/home/user/infra",
            ["db", "config"],
            is_protected=True,
        )
        mid = result["id"]
        original_heat = result["heat"]
        _set_memory_age(mid, hours_ago=365 * 24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["heat"] == original_heat, (
            f"Protected memory heat changed: {original_heat} → {mem['heat']}"
        )

    def test_protected_memory_not_pruned(self):
        """A cold protected memory is never pruned."""
        result = memorize_sync(
            "protected cold memory that must survive",
            "/home/user/test",
            ["important"],
            is_protected=True,
        )
        mid = result["id"]
        # Simulate cold + low confidence + no access (pruning conditions)
        storage = server._get_storage()
        storage.update_memory_heat(mid, 0.001)
        storage.update_memory_fields(mid, confidence=0.1, access_count=0)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None, "Protected memory was deleted by consolidation"

    def test_protected_memory_content_not_modified(self):
        """A 60-day-old protected memory's content is identical after consolidation."""
        content = "SSH key fingerprint for bastion host: SHA256:ABCDEF1234567890"
        result = memorize_sync(content, "/home/user/ssh", ["ssh", "bastion"], is_protected=True)
        mid = result["id"]
        _set_memory_age(mid, hours_ago=60 * 24)

        server.consolidate_now()

        mem = _get_memory(mid)
        assert mem is not None
        assert mem["content"] == content


# ── E. Auto-Deletion Rules ───────────────────────────────────────────────────


class TestAutoDeletion:
    """Only auto-generated _action_stream memories may be auto-deleted.

    Invariant: _memify_prune() skips any memory without the _action_stream tag,
    regardless of how cold or unaccessed it is.
    """

    def test_user_memory_not_auto_deleted(self):
        """A cold, unaccessed, low-confidence user memory is never auto-pruned."""
        result = memorize_sync(
            "important fact that should never be deleted",
            "/home/user/project",
            ["user-stored"],
        )
        mid = result["id"]
        _make_cold(mid)

        server.consolidate_now()

        assert _get_memory(mid) is not None, (
            "User-stored memory was auto-deleted during consolidation"
        )

    def test_action_stream_cold_memory_pruned(self):
        """An _action_stream memory that went cold and was never accessed gets pruned."""
        storage = server._get_storage()
        embeddings = server._get_embeddings()
        content = "Session activity [Read(3), Bash(1)]: 4 tool calls"
        emb = embeddings.encode(content)
        mid = storage.insert_memory(
            {
                "content": content,
                "embedding": emb,
                "tags": ["_action_stream", "_auto"],
                "directory_context": "/home/user/project",
                "heat": 0.005,
                "is_stale": False,
                "file_hash": None,
                "embedding_model": embeddings.get_model_name(),
            }
        )
        storage._q(
            "UPDATE type::record('memory', $id) SET confidence = 0.15, access_count = 0",
            {"id": mid},
        )

        server.consolidate_now()

        assert _get_memory(mid) is None, (
            "_action_stream cold memory was NOT pruned during consolidation"
        )

    def test_action_stream_insert_has_zero_confidence(self):
        """Action stream memories created by the consolidation pipeline have confidence=0.0.

        This is required for _memify_prune to be able to delete them when they go cold
        and were never accessed. A NULL confidence coerces to 1.0 in the prune check,
        blocking deletion permanently.
        """
        storage = server._get_storage()
        embeddings = server._get_embeddings()
        mid = storage.insert_memory(
            {
                "content": "Session activity [Bash(3)]: 3 tool calls",
                "embedding": embeddings.encode("Session activity [Bash(3)]: 3 tool calls"),
                "tags": ["_action_stream", "_auto"],
                "directory_context": "/home/user/project",
                "heat": 0.4,
                "confidence": 0.0,
                "is_stale": False,
                "file_hash": None,
                "embedding_model": embeddings.get_model_name(),
            }
        )
        mem = storage.get_memory(mid)
        assert (mem.get("confidence") or 0.0) < 0.3, (
            "action stream memory should have confidence=0.0 to be eligible for pruning"
        )

    def test_protected_memory_never_deleted_even_with_action_stream_tag(self):
        """is_protected wins over _action_stream — protected memories are never deleted."""
        storage = server._get_storage()
        embeddings = server._get_embeddings()
        content = "Protected action stream entry that must survive"
        emb = embeddings.encode(content)
        mid = storage.insert_memory(
            {
                "content": content,
                "embedding": emb,
                "tags": ["_action_stream", "_auto"],
                "directory_context": "/home/user/project",
                "heat": 0.005,
                "is_stale": False,
                "file_hash": None,
                "embedding_model": embeddings.get_model_name(),
            }
        )
        storage.update_memory_fields(mid, confidence=0.1, access_count=0, is_protected=True)

        server.consolidate_now()

        assert _get_memory(mid) is not None, (
            "Protected _action_stream memory was deleted — is_protected should win"
        )


# ── F. Curation Threshold ────────────────────────────────────────────────────


class TestCurationThreshold:
    """Similar but distinct facts must remain as separate memories.

    Invariant: CURATION_SIMILARITY_THRESHOLD=0.95 means only near-exact duplicates
    are merged on ingest. Moderately related facts create separate memories.
    """

    def test_distinct_facts_stored_as_separate_memories(self):
        """Two related but distinct facts produce two separate memory IDs."""
        r1 = memorize_sync(
            "Production database host is prod-db-primary.internal port 5432",
            "/home/user/infra",
            ["db", "prod"],
        )
        r2 = memorize_sync(
            "Staging database host is staging-db.internal port 5433",
            "/home/user/infra",
            ["db", "staging"],
        )
        assert r1["id"] != r2["id"], "Distinct DB facts were merged into one memory"

    def test_near_duplicate_content_merged_on_ingest(self):
        """Storing the exact same content twice results in only one memory."""
        content = "AWS account ID for production environment is 123456789012"
        r1 = memorize_sync(content, "/home/user/aws", ["aws", "prod"])
        r2 = memorize_sync(content, "/home/user/aws", ["aws", "prod"])
        # Merged: both should resolve to the same memory ID
        assert r1["id"] == r2["id"], (
            "Identical content produced two separate memories instead of merging"
        )


# ── G. Deduplication ─────────────────────────────────────────────────────────


class TestDeduplication:
    """Consolidation removes exact duplicates, keeps distinct memories.

    Invariant: _merge_duplicates() deletes the lower-heat copy when cosine > 0.95.
    Memories at ~0.85 cosine similarity are kept separate.
    """

    def test_exact_duplicate_removed_by_consolidation(self):
        """Two memories with identical content → consolidation keeps only one."""
        content = "Duplicate fact that should be deduplicated"
        r1 = memorize_sync(content, "/home/user/test", ["test"])
        # Force a second distinct ID by inserting directly
        storage = server._get_storage()
        embeddings = server._get_embeddings()
        emb = embeddings.encode(content)
        mid2 = storage.insert_memory(
            {
                "content": content,
                "embedding": emb,
                "tags": ["test"],
                "directory_context": "/home/user/test",
                "heat": 0.5,
                "is_stale": False,
                "file_hash": None,
                "embedding_model": embeddings.get_model_name(),
            }
        )

        server.consolidate_now()

        # One of them should be gone; the higher-heat one (r1 with heat=1.0) survives
        m1 = _get_memory(r1["id"])
        m2 = _get_memory(mid2)
        assert (m1 is None) != (m2 is None), (
            "Exact duplicate was not removed: both memories still exist"
        )

    def test_similar_but_distinct_memories_both_kept(self):
        """Two distinct memories at moderate similarity are both kept after consolidation."""
        r1 = memorize_sync(
            "The Python asyncio event loop runs coroutines cooperatively",
            "/home/user/test",
            ["python"],
        )
        r2 = memorize_sync(
            "JavaScript uses an event loop for non-blocking I/O operations",
            "/home/user/test",
            ["javascript"],
        )

        server.consolidate_now()

        assert _get_memory(r1["id"]) is not None, "First memory was incorrectly deleted"
        assert _get_memory(r2["id"]) is not None, "Second memory was incorrectly deleted"


# ── H. Regression Scenarios ──────────────────────────────────────────────────


class TestRegressionScenarios:
    """Real-world failure cases that prompted behavioral changes.

    Each test reproduces a specific bug. The bug reference is in the docstring.
    """

    def test_1password_item_name_survives_recall(self, recall_backend_bypass):
        """Regression: specific 1Password item ID was lost during reconsolidation.

        Bug: reconsolidate() would replace memory content with the query string
        when mismatch was high (different query context → extinction).
        Fix: reconsolidation disabled — recall() is now read-only.
        """
        content = "Codeberg PAT is stored in 1Password item zqq55bz2qi53gw375jlm2sh4jq under Personal vault"
        result = memorize_sync(content, "/home/user", ["codeberg", "1password", "pat"])
        mid = result["id"]

        # Recall with very different contexts that previously triggered extinction
        unrelated_queries = [
            "git push authentication token",
            "api credentials for version control",
            "personal access token storage location",
            "password manager vault item",
        ]
        for query in unrelated_queries:
            server.recall(query, directory="/home/user")

        mem = _get_memory(mid)
        assert mem is not None
        assert "zqq55bz2qi53gw375jlm2sh4jq" in mem["content"], (
            f"1Password item ID was lost from memory content after recall.\n"
            f"Current content: {mem['content']!r}"
        )

    def test_credentials_not_corrupted_by_cross_project_recall(self, recall_backend_bypass):
        """Cross-directory recall must not corrupt credential memories.

        Previously: directory mismatch contributed to mismatch score, potentially
        triggering reconsolidation when recalling from a different project context.
        """
        # Use a short fake token (< 20 chars after prefix) so it doesn't trigger
        # the v5.10.2 secret gate (ghp_[A-Za-z0-9_]{20,}).
        content = "GitHub token for CI: ghp_FAKE1234567890"
        result = memorize_sync(content, "/home/user/projectA", ["github", "ci", "token"])
        mid = result["id"]

        # Recall from a completely different project — v5.65: directory required
        server.recall("github token authentication", directory="/home/user/projectB")
        server.recall("CI deployment secrets", directory="/home/user/projectB")

        mem = _get_memory(mid)
        assert mem is not None
        assert "ghp_FAKE1234567890" in mem["content"], (
            "Token was corrupted during cross-project recall"
        )
