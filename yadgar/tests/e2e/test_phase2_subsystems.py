"""Phase-2 behavior-contract e2e tests — broader subsystems (v5.69, task #46).

Extends the Phase-1 real-path net (test_phase1_db_layer.py) to subsystems
beyond the critical-path core.  Same discipline:

    GREEN  test passes against the real isolated stack (real surreal, real
           embeddings, isolated tmp YADGAR_DATA_DIR).
    xfail(strict=True, reason=#NN)  known-broken / unwired; the SHALL is written
           as a FAILING spec.  When the linked fix lands, the test xpasses and
           strict turns the xpass into a signal to flip the marker.

ANTI-BENDING rule (review-enforced): every test asserts a real observable from
BEHAVIOR_CONTRACT.md.  Never weaken an assertion to go green; never assert
brokenness to force an xfail (that would not xpass when fixed).

Only the host-service boundary (systemctl/podman) is stubbed by conftest's
service_stub.  The unit under test is always driven real.

xfail-target imports live INSIDE the test function: a collection-time ImportError
is a hard ERROR, not an xfail.

Run: make e2e   (foreground, flock-locked)
Requires: surreal binary on PATH (or YADGAR_DB_URL set by the harness).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

_E2E_BRANCH = "feat/e2e-phase2"


# ---------------------------------------------------------------------------
# Helpers (mirror Phase-1 seeding style)
# ---------------------------------------------------------------------------


def _embed(e2e_engines, content: str) -> bytes:
    return e2e_engines["embeddings"].encode(content)


def _drain(e2e_engines) -> None:
    import yadgar.server._state as _st

    drainer = _st._queue_drainer
    if drainer is not None:
        drainer.drain_now()


def _insert_mem(
    e2e_engines,
    content: str,
    directory: str,
    *,
    heat: float = 0.8,
    tags: list[str] | None = None,
    last_accessed: str | None = None,
) -> int:
    """Insert a memory with a real embedding; return the row id (seeding only)."""
    storage = e2e_engines["storage"]
    emb = _embed(e2e_engines, content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        "heat": heat,
        "tags": tags or [],
        "last_accessed": last_accessed or now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
    }
    return storage.insert_memory(doc)


def _memorize_and_find(e2e_engines, content: str, directory: str, tags: list[str]) -> dict | None:
    """Drive the REAL memorize() → drain → lookup path; return the stored row or None."""
    server = e2e_engines["server"]
    storage = e2e_engines["storage"]

    result = server.memorize(content, directory, tags, branch_hint=_E2E_BRANCH)
    if not result.get("queued"):
        return result
    _drain(e2e_engines)

    try:
        rows = storage.search_memories_fts(content[:100], min_heat=0.0, limit=20)
        for row in rows:
            if row.get("content") == content and row.get("directory_context") == directory:
                return row
    except Exception:
        pass
    try:
        recent = storage.get_memories_by_heat(min_heat=0.0, limit=100)
        for row in recent:
            if row.get("content") == content and row.get("directory_context") == directory:
                return row
    except Exception:
        pass
    return None


# ===========================================================================
# GREEN — Predictive-coding write / surprise gate
# BC-A2 (write-gate stores novel, dedups near-identical) + BC-PCd2 (should_store
# gates redundant writes).
#
# WRITE_GATE_THRESHOLD defaults to 0.0 (gate disabled — store everything), so the
# full memorize() path won't gate by default.  The unit under test is the gate
# itself: a real WriteGate (real storage + real embeddings) with a live
# threshold.  We seed a directory, then assert the gate ranks a near-duplicate as
# LESS surprising than novel unrelated content, and that should_store() rejects
# the near-dup it stores the novel one.
# ===========================================================================


class TestBCA2_WriteGateSurprise:
    """BC-A2 / BC-PCd2: the surprise gate stores novel content, gates near-dups."""

    def _make_gate(self, e2e_engines, threshold: float):
        import yadgar.server._state as _st
        from yadgar.config import Settings
        from yadgar.predictive_coding import WriteGate

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        retriever = _st._retriever
        settings = Settings(
            DB_PATH=e2e_engines["db_path"],
            WRITE_GATE_THRESHOLD=threshold,
        )
        return WriteGate(storage, embeddings, retriever, settings)

    def test_near_dup_less_surprising_than_novel(self, e2e_engines):
        """A near-duplicate of an existing memory SHALL score lower surprisal
        than novel unrelated content (the gate's core discriminator).

        Surprisal is a weighted sum (embedding 0.4 + entity/temporal/structural);
        identical content still carries non-embedding mass, so the robust
        observable is the RELATIVE ordering, not an absolute boolean.
        """
        yadgar_dir = e2e_engines["yadgar_dir"]

        seeded = (
            "BC-A2 gate: the deployment pipeline pushes the container image to ECR "
            "then triggers a rolling update on the staging cluster xa2seed11001"
        )
        _insert_mem(e2e_engines, seeded, yadgar_dir, heat=0.9)

        gate = self._make_gate(e2e_engines, threshold=0.5)

        near_dup = (
            "BC-A2 gate: the deployment pipeline pushes the container image to ECR "
            "then triggers a rolling update on the staging cluster xa2seed11001"
        )
        novel = (
            "BC-A2 gate: quantum entanglement of migratory songbirds correlates with "
            "geomagnetic flux during autumnal navigation xa2novel22002 unrelated"
        )

        s_dup = gate.compute_surprisal(near_dup, yadgar_dir, [])
        s_novel = gate.compute_surprisal(novel, yadgar_dir, [])

        assert s_dup < s_novel, (
            "BC-A2: near-duplicate content MUST be less surprising than novel "
            f"unrelated content. near_dup={s_dup:.3f} novel={s_novel:.3f}"
        )

    def test_gate_stores_novel_rejects_near_dup(self, e2e_engines):
        """should_store SHALL accept novel content and reject a near-duplicate
        when an active threshold sits between their surprisal scores.

        Content carries no bypass triggers (no error/decision keywords, no
        important/critical tags) so the gate does not short-circuit to True.
        """
        yadgar_dir = e2e_engines["yadgar_dir"]

        seeded = (
            "BC-A2 store-gate: nightly backup snapshots the surrealkv dir to the "
            "rotating archive then verifies row counts per table xa2store33003"
        )
        _insert_mem(e2e_engines, seeded, yadgar_dir, heat=0.9)

        gate = self._make_gate(e2e_engines, threshold=0.5)

        near_dup = (
            "BC-A2 store-gate: nightly backup snapshots the surrealkv dir to the "
            "rotating archive then verifies row counts per table xa2store33003"
        )
        novel = (
            "BC-A2 store-gate: the chef braised the heirloom tomatoes in saffron "
            "broth while the orchestra rehearsed a baroque concerto xa2store44004"
        )

        s_dup = gate.compute_surprisal(near_dup, yadgar_dir, [])
        s_novel = gate.compute_surprisal(novel, yadgar_dir, [])
        # Pick a threshold strictly between the two so the ordering is the
        # discriminator, not the absolute scores.
        mid = (s_dup + s_novel) / 2.0
        gate._threshold = mid
        gate._settings.WRITE_GATE_THRESHOLD = mid

        ok_novel, _, _ = gate.should_store(novel, yadgar_dir, [])
        ok_dup, _, _ = gate.should_store(near_dup, yadgar_dir, [])

        assert ok_novel is True, (
            f"BC-A2: novel content (surprisal={s_novel:.3f}) MUST be stored when "
            f"threshold={mid:.3f}. should_store returned False."
        )
        assert ok_dup is False, (
            f"BC-A2: near-dup content (surprisal={s_dup:.3f}) MUST be gated when "
            f"threshold={mid:.3f}. should_store returned True."
        )


# ===========================================================================
# GREEN — Hook directory stamping (auto-capture)
# BC-H1 (tool-usage capture hook stamps caller cwd) + BC-AS1 (a captured action
# becomes a retrievable action-stream record stamped caller cwd).
#
# The host-side hook script (post-tool-capture.py) is HTTP-only and no-ops when
# the daemon is down — not deterministically drivable.  The REAL cwd→directory
# stamping happens in the daemon handler hook_auto_capture, which writes the
# action_log row.  We drive that handler in-process with a minimal Request
# (only the transport boundary is faked, not the unit).  The handler batches 5
# actions before flushing to insert_action_log, so we drive it 5×.
# ===========================================================================


class _FakeRequest:
    """Minimal Starlette-Request stand-in: only .json() is exercised."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class TestBCH1_AutoCaptureStampsCwd:
    """BC-H1 / BC-AS1: the auto-capture path stamps the caller cwd on the action_log row."""

    def test_action_log_stamped_with_caller_cwd(self, e2e_engines):
        """Five Write actions from cwd=D SHALL flush one action_log row stamped directory=D."""
        import yadgar.server._state as _st
        from yadgar.server.http import hook_auto_capture

        storage = e2e_engines["storage"]
        caller_cwd = e2e_engines["yadgar_dir"]
        session_id = "bc-h1-session-xh1cap55005"

        # Reset the module-global batch so a prior test can't carry over.
        _st._action_batch.clear()

        async def _drive():
            resp = None
            for i in range(5):
                payload = {
                    "tool_name": "Write",  # must be in _CAPTURE_TOOLS
                    "summary": f"BC-H1 edit number {i} xh1cap55005",
                    "directory": caller_cwd,
                    "session_id": session_id,
                }
                resp = await hook_auto_capture(_FakeRequest(payload))
            return resp

        final_resp = asyncio.run(_drive())
        # The 5th call flushes the batch.
        assert final_resp.status_code == 200, (
            f"BC-H1: auto-capture handler must return 200 on flush, got {final_resp.status_code}"
        )

        actions = storage.get_unprocessed_actions(limit=50)
        matching = [
            a
            for a in actions
            if a.get("session_id") == session_id and a.get("directory") == caller_cwd
        ]
        assert matching, (
            f"BC-H1/AS1: a captured action MUST become a retrievable action_log row "
            f"stamped directory={caller_cwd!r}. Got actions: "
            f"{[(a.get('directory'), a.get('session_id')) for a in actions]}"
        )
        # The flushed row records the directory of the captured tool calls.
        assert all(a.get("directory") == caller_cwd for a in matching), (
            "BC-H1: every flushed action row MUST carry the caller cwd as directory."
        )


# ===========================================================================
# GREEN — Hook install / sync (filesystem ops, fully deterministic)
# BC-HK1 (install_hooks writes hook config + idempotent re-run) +
# BC-HK2 (sync_instructions writes the instruction block, replaces a stale one).
# ===========================================================================


class TestBCHK1_InstallHooksIdempotent:
    """BC-HK1: install_hooks writes Claude Code hook config and is idempotent."""

    def test_install_then_reinstall_no_duplicate(self, e2e_engines, tmp_path):
        """install_hooks_impl SHALL write a settings.json with hooks; a second run
        SHALL produce identical content (no duplicate hook entries)."""
        import json

        from yadgar.install_hooks_lib import install_hooks_impl

        home = tmp_path / "home"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()

        r1 = install_hooks_impl(
            home_dir=home,
            scope="project",
            project_directory=str(project),
            dry_run=False,
        )
        assert r1.get("status") != "error", f"BC-HK1: install must not error, got {r1}"

        settings_file = project / ".claude" / "settings.json"
        assert settings_file.exists(), (
            f"BC-HK1: install_hooks MUST write {settings_file}. Got result: {r1}"
        )
        first = json.loads(settings_file.read_text())
        assert "hooks" in first and first["hooks"], (
            f"BC-HK1: settings.json MUST contain a non-empty hooks section. Got: {first}"
        )

        # Re-run — idempotent: hook config must be byte-stable.
        install_hooks_impl(
            home_dir=home,
            scope="project",
            project_directory=str(project),
            dry_run=False,
        )
        second = json.loads(settings_file.read_text())
        assert second["hooks"] == first["hooks"], (
            "BC-HK1: re-running install_hooks MUST NOT duplicate or mutate hook entries. "
            f"first={first['hooks']!r} second={second['hooks']!r}"
        )


class TestBCHK2_SyncInstructions:
    """BC-HK2: sync_instructions writes the Yadgar block; a stale block is replaced, not duplicated."""

    def test_sync_writes_and_replaces_stale_block(self, e2e_engines, tmp_path):
        """sync_instructions SHALL write the '## Memory System — Yadgar' section, and a
        second sync over a pre-existing (stale) block SHALL leave exactly one such section."""
        from yadgar.server.tools.misc import sync_instructions

        md_path = tmp_path / "CLAUDE.md"
        # Seed a CLAUDE.md with a STALE Yadgar section + a trailing section.
        md_path.write_text(
            "# Global Rules\n\n"
            "## Memory System — Yadgar v0.0.0-stale\n"
            "- stale line that MUST be replaced xhk2stale66006\n\n"
            "## Other Section\n"
            "- keep me xhk2keep77007\n"
        )

        result = sync_instructions(claude_md_path=str(md_path))
        assert result.get("status") != "skipped", (
            f"BC-HK2: sync_instructions must run against an existing dir, got {result}"
        )

        content = md_path.read_text()
        # Exactly one Yadgar section header.
        header_count = content.count("## Memory System — Yadgar")
        assert header_count == 1, (
            f"BC-HK2: exactly ONE Yadgar section must remain after sync, found {header_count}."
        )
        # The stale line is gone; the unrelated section survives.
        assert "xhk2stale66006" not in content, (
            "BC-HK2: the stale Yadgar block MUST be replaced, not kept."
        )
        assert "xhk2keep77007" in content, (
            "BC-HK2: sync_instructions MUST NOT clobber unrelated CLAUDE.md sections."
        )


# ===========================================================================
# GREEN — Wiki write → read → directory scoping
# BC-G1 (wiki_add stamps directory D) + BC-G3 (wiki_read resolves under D, not
# under another dir).
# ===========================================================================


class TestBCG1_WikiWriteReadScope:
    """BC-G1 / BC-G3: a wiki page added under dir D resolves via wiki_read(D) and is
    scoped to D (a read against another dir does NOT return it)."""

    def test_wiki_add_read_directory_scoped(self, e2e_engines):
        from yadgar.server.tools.wiki import wiki_add, wiki_read

        yadgar_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]

        add = wiki_add(
            title="BC-G1 Yadgar Scoped xg1yad88008 page",
            content="# BC-G1\n\nScoped to the yadgar project xg1yad88008.",
            directory=yadgar_dir,
            category="reference",
            tags=["e2e", "bc-g1"],
            branch_hint=_E2E_BRANCH,
            wait=True,
        )
        assert add.get("committed") or add.get("stored") or "slug" in add, (
            f"BC-G1: wiki_add must succeed, got {add}"
        )
        slug = add.get("slug")
        assert slug, f"BC-G1: wiki_add must return a slug, got {add}"

        # Read under the SAME directory — must resolve to our page.
        same = wiki_read(slug, directory=yadgar_dir, branch_hint=_E2E_BRANCH)
        assert "error" not in same, (
            f"BC-G3: wiki_read(slug, directory=yadgar_dir) MUST resolve the page. Got {same}"
        )
        assert "xg1yad88008" in str(same.get("content", "")), (
            f"BC-G1: resolved page content must match the written page. Got {same}"
        )

        # Read under a DIFFERENT directory — §25 resolution must NOT surface the
        # yadgar-scoped page (no project-canonical/global fallback to it).
        cross = wiki_read(slug, directory=other_dir, branch_hint=_E2E_BRANCH)
        cross_is_other = "error" in cross or "xg1yad88008" not in str(cross.get("content", ""))
        assert cross_is_other, (
            "BC-G3: a page scoped to yadgar_dir MUST NOT resolve under other_dir. "
            f"wiki_read(other_dir) returned the yadgar page: {cross}"
        )


# ===========================================================================
# GREEN — Heat-decay math (entity path = the pure curve)
# BC-HT1: decay = heat * factor^hours, measured from max(last_accessed,
# last_decay_at).
#
# The MEMORY decay path (compute_decay) carries importance/valence/confidence
# modifiers, so it is NOT the pure curve.  The ENTITY decay path
# (heat_decay.py:_decay_entities) is exactly heat * (DECAY_FACTOR ** hours) with
# the same max(last_accessed, last_decay_at) watermark — so that is where the
# pure-formula SHALL is asserted.
# ===========================================================================


class TestBCHT1_HeatDecayCurve:
    """BC-HT1: entity heat decays as heat * factor^hours from the watermark."""

    def test_entity_decay_matches_pure_formula(self, e2e_engines):
        import yadgar.server._state as _st
        from yadgar.config import get_settings

        storage = e2e_engines["storage"]
        settings = get_settings()
        factor = settings.DECAY_FACTOR
        cold = settings.COLD_THRESHOLD

        # Seed an entity accessed exactly 10h ago, heat high enough to stay > cold.
        hours = 10.0
        start_heat = 0.9
        last_accessed = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        eid = storage.insert_entity(
            {
                "name": "bc-ht1-entity-xht1ent99009",
                "type": "concept",
                "heat": start_heat,
                "last_accessed": last_accessed,
            }
        )

        consolidation = _st._consolidation
        assert consolidation is not None
        now = datetime.now(UTC)
        batch = consolidation._decay_entities(now)
        if batch:
            storage.batch_writes(batch)

        ent = storage.get_entity(eid) if hasattr(storage, "get_entity") else None
        if ent is None:
            ent = storage.get_entity_by_name("bc-ht1-entity-xht1ent99009")
        assert ent is not None, "BC-HT1: decayed entity must still exist"

        new_heat = float(ent.get("heat", start_heat))
        elapsed_h = (now - datetime.fromisoformat(last_accessed)).total_seconds() / 3600.0
        expected = start_heat * (factor**elapsed_h)

        if expected < cold:
            # Below cold → archived to 0.0 per _decay_entities.
            assert new_heat == 0.0, (
                f"BC-HT1: entity decaying below cold ({expected:.4f} < {cold}) must go to 0.0, "
                f"got {new_heat}"
            )
        else:
            assert new_heat == pytest.approx(expected, rel=1e-3, abs=1e-4), (
                f"BC-HT1: entity heat MUST follow heat*factor^hours. "
                f"expected={expected:.6f} got={new_heat:.6f} "
                f"(start={start_heat} factor={factor} hours={elapsed_h:.4f})"
            )
        # And decay must have actually lowered heat.
        assert new_heat < start_heat, (
            f"BC-HT1: decay must lower heat from {start_heat}; got {new_heat}"
        )


class TestBCC4_NightlySleepCycleRuns:
    """BC-C4 / BC-SC1a (#37): the nightly consolidation path runs the sleep cycle.

    The sleep/dream cycle was DEAD since v5.7.0 PR-0 dropped the daemon loop —
    ``_maybe_sleep_cycle`` was defined but never called. The nightly cron called
    ``force_consolidate()`` (consolidation only), so dream replay never ran.

    This drives the nightly entrypoint (``run_nightly_consolidation``) against a
    seeded real DB and asserts the dream replay produced an observable artifact:
    a synthetic 'dream' insight memory linking two highly-similar memories. Dream
    replay generates an insight only for pairs with cosine similarity > 0.7, so
    we seed two closely-related (but non-identical, below the 0.95 merge
    threshold) memories.

    Anti-bending: asserts the real observable (a 'dream'-tagged insight memory),
    not that the method was called.
    """

    def _make_scheduler(self, e2e_engines):
        from yadgar.config import Settings
        from yadgar.consolidation import ConsolidationScheduler

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        return ConsolidationScheduler(e2e_engines["storage"], e2e_engines["embeddings"], settings)

    def test_nightly_runs_sleep_cycle_produces_dream_insight(self, e2e_engines):
        """run_nightly_consolidation SHALL run the gated sleep cycle, whose dream
        replay produces a synthetic 'dream' insight memory for a similar pair."""
        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        # Two closely-related prose memories: high embedding similarity (> 0.7 so
        # dream replay generates an insight) but not identical (< 0.95 merge
        # threshold). Pure prose with no shared file/function entities so earlier
        # consolidation phases do not pre-connect them and starve dream replay.
        # Measured pairwise cosine ~0.85 (all-MiniLM-L6-v2): above the 0.7 dream
        # insight threshold, below the 0.95 CURATION_SIMILARITY_THRESHOLD merge
        # threshold — so the pair survives merge_duplicates yet triggers a dream
        # insight.
        _insert_mem(
            e2e_engines,
            "The overnight maintenance job slowly drains the pending request "
            "queue and rebalances load toward the warmer replica nodes. "
            "xc4dream10001",
            yadgar_dir,
            heat=0.9,
        )
        _insert_mem(
            e2e_engines,
            "During the nightly maintenance window the request queue is "
            "gradually drained while traffic is rebalanced across the warm "
            "replicas. xc4dream10002",
            yadgar_dir,
            heat=0.9,
        )

        scheduler = self._make_scheduler(e2e_engines)
        # Fresh scheduler: _last_sleep_cycle is None, so the 6-hour gate opens
        # and the sleep cycle fires on this first nightly run.
        scheduler.run_nightly_consolidation()

        rows = storage.get_memories_by_heat(min_heat=0.0, limit=500)
        dream_memories = [
            r
            for r in rows
            if "dream" in (r.get("tags") or [])
            and str(r.get("content", "")).startswith("Dream connection:")
        ]
        assert dream_memories, (
            "BC-C4/BC-SC1a (#37): the nightly sleep cycle MUST run dream replay and "
            "produce a synthetic 'dream' insight memory for the similar pair. Found "
            "none — the sleep cycle did not run (or dream replay produced nothing). "
            f"Total memories seen: {len(rows)}."
        )


# ===========================================================================
# xfail(strict) — KNOWN-BROKEN subsystems written as failing specs.
# ===========================================================================


class TestBCAC2_AstrocyteDomainConsolidation:
    """BC-AC2 / BC-C5a: AstrocytePool domain consolidation produces a per-domain summary."""

    @pytest.mark.xfail(
        strict=True,
        reason="❌ BC-AC2/BC-C5a #40 — consolidate_domain applies decay + entity "
        "extraction but emits NO domain summary; the SHALL ('assign→consolidate "
        "per domain produces a summary') is unmet. Flips to xpass when #40 adds "
        "domain summarization.",
    )
    def test_consolidate_domain_produces_summary(self, e2e_engines):
        """assign_memory → consolidate_domain SHALL produce a non-empty domain summary."""
        import yadgar.server._state as _st
        from yadgar.astrocyte_pool import AstrocytePool
        from yadgar.config import get_settings

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        yadgar_dir = e2e_engines["yadgar_dir"]
        settings = get_settings()

        # Seed memories with clear domain signal (code-ish content).
        for i in range(3):
            _insert_mem(
                e2e_engines,
                f"BC-AC2 def deploy_service(): kubectl apply container image #{i} xac2dom{i:05d}",
                yadgar_dir,
                heat=0.8,
            )

        pool = AstrocytePool(
            storage=storage,
            embeddings=embeddings,
            knowledge_graph=_st._kg,
            thermodynamics=_st._thermo,
            settings=settings,
        )

        # Route each memory to a domain, then consolidate that domain.
        assigned_domains: set[str] = set()
        for row in storage.get_memories_by_heat(min_heat=0.0, limit=50):
            domains = pool.assign_memory(row)
            assigned_domains.update(domains)

        assert assigned_domains, "test setup: at least one memory must route to a domain"

        produced_summary = False
        for domain in assigned_domains:
            stats = pool.consolidate_domain(domain)
            summary = stats.get("summary") if isinstance(stats, dict) else None
            if summary:
                produced_summary = True

        assert produced_summary, (
            "BC-AC2/C5a: domain consolidation MUST produce a per-domain summary. "
            "consolidate_domain emitted none (#40 — never fires / no summarization)."
        )


class TestBCEN3a_Doc2QueryEnrichment:
    """BC-EN3a: doc2query generates synthetic queries for a stored memory."""

    def test_stored_memory_has_synthetic_queries(self, e2e_engines):
        """A memory written via memorize() SHALL carry enrichment-derived synthetic
        queries (observable as the '[enrichment]' marker the pipeline appends)."""
        yadgar_dir = e2e_engines["yadgar_dir"]
        content = (
            "BC-EN3a enrichment wiring: the connection pool exhausts under burst "
            "load when the reaper interval exceeds the idle timeout xen3a10010"
        )
        row = _memorize_and_find(e2e_engines, content, yadgar_dir, ["e2e", "bc-en3a"])
        assert row is not None, "test setup: memorize() + drain must persist the memory"

        stored = str(row.get("content", "")) + str(row.get("enriched_content", ""))
        assert "[enrichment]" in stored, (
            "BC-EN3a: a stored memory MUST carry doc2query/enrichment synthetic-query "
            "artifacts (#39 — enrichment pipeline is unwired from the write path)."
        )
