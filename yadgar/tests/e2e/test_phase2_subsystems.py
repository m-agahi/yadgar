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

#: The two identities this file needs. C5/ADR-0227 made ``project_id``
#: mandatory at the storage write chokepoint and at every scoped read.
#: The PAIR is load-bearing: BC-G1/BC-G3 proves a page scoped to one project
#: does NOT resolve from another, and Car C7 moved that decision off
#: ``directory_context`` onto ``project_id``.
_TEST_PROJECT = "m-agahi/yadgar"
_OTHER_PROJECT = "m-agahi/aws-work"


# ---------------------------------------------------------------------------
# Helpers (mirror Phase-1 seeding style)
# ---------------------------------------------------------------------------


def _embed(e2e_engines, content: str) -> bytes:
    return e2e_engines["embeddings"].encode(content)


def _drain(e2e_engines) -> None:
    import yadgar._shared.runtime.state as _st

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
    project_id: str = _TEST_PROJECT,
) -> int:
    """Insert a memory with a real embedding; return the row id (seeding only)."""
    storage = e2e_engines["storage"]
    emb = _embed(e2e_engines, content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        "project_id": project_id,
        "heat": heat,
        "tags": tags or [],
        "last_accessed": last_accessed or now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
    }
    return storage.insert_memory(doc)


def _memorize_and_find(
    e2e_engines,
    content: str,
    directory: str,
    tags: list[str],
    project: str = _TEST_PROJECT,
) -> dict | None:
    """Drive the REAL memorize() → drain → lookup path; return the stored row or None.

    C13 (e) — two coupled changes, as in ``test_phase1_db_layer``: ``project`` is
    named (C5/ADR-0227 makes an unnamed memorize fatal), and the read-back
    matches ``directory_context`` against the PROJECT because C10 (f) moved that
    stamp off ``context`` and onto the resolved ``project_id``. Naming the
    project without re-pointing the match would return ``None`` for every
    successful write.
    """
    server = e2e_engines["server"]
    storage = e2e_engines["storage"]

    result = server.memorize(content, directory, tags, project=project)
    if not result.get("queued"):
        return result
    _drain(e2e_engines)

    try:
        rows = storage.search_memories_fts(content[:100], min_heat=0.0, limit=20)
        for row in rows:
            if row.get("content") == content and row.get("directory_context") == project:
                return row
    except Exception:
        pass
    try:
        recent = storage.get_memories_by_heat(min_heat=0.0, limit=100)
        for row in recent:
            if row.get("content") == content and row.get("directory_context") == project:
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
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.config import Settings
        from yadgar.backend.predictive_coding import WriteGate

        # T2 Car E2: the shared root no longer builds the retriever — compose
        # the backend singleton against the live e2e engines first.
        from yadgar.backend.retrieval.compose import ensure_retrieval_engine

        ensure_retrieval_engine()

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
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.http import hook_auto_capture

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
                    # C4/C13 (e): the host-side hook runner stamps this on the
                    # request body. ``hook_auto_capture`` cannot mint one — it
                    # runs in the daemon container (ADR-0227) — so a payload
                    # without it enqueues project_id='' and the drainer DLQs the
                    # batch as ``missing_project_id``. The row never lands and
                    # the assertion below reports an empty action list.
                    "project_id": _TEST_PROJECT,
                }
                resp = await hook_auto_capture(_FakeRequest(payload))
            return resp

        final_resp = asyncio.run(_drive())
        # The 5th call flushes the batch.
        assert final_resp.status_code == 200, (
            f"BC-H1: auto-capture handler must return 200 on flush, got {final_resp.status_code}"
        )

        # T2 Car E1: the flush ENQUEUES an action_log job (queue seam, ADR-0078);
        # drain so the backend replay lands the row before the storage read.
        _drain(e2e_engines)

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

        from yadgar.core.install.install_hooks_lib import install_hooks_impl

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
        from yadgar.core.server.tools.misc import sync_instructions

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
        from yadgar.core.server.tools.wiki import wiki_add, wiki_read

        yadgar_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]

        add = wiki_add(
            title="BC-G1 Yadgar Scoped xg1yad88008 page",
            content="# BC-G1\n\nScoped to the yadgar project xg1yad88008.",
            directory=yadgar_dir,
            category="reference",
            tags=["e2e", "bc-g1"],
            wait=True,
            project=_TEST_PROJECT,
        )
        assert add.get("committed") or add.get("stored") or "slug" in add, (
            f"BC-G1: wiki_add must succeed, got {add}"
        )
        slug = add.get("slug")
        assert slug, f"BC-G1: wiki_add must return a slug, got {add}"

        # Read under the SAME directory — must resolve to our page.
        same = wiki_read(slug, directory=yadgar_dir, project=_TEST_PROJECT)
        assert "error" not in same, (
            f"BC-G3: wiki_read(slug, directory=yadgar_dir) MUST resolve the page. Got {same}"
        )
        assert "xg1yad88008" in str(same.get("content", "")), (
            f"BC-G1: resolved page content must match the written page. Got {same}"
        )

        # Read under a DIFFERENT directory — §25 resolution must NOT surface the
        # yadgar-scoped page (no project-canonical/global fallback to it).
        # C13 (e): the cross read must name the OTHER identity. Post-C7 the §25
        # ladder resolves on project_id, so passing only a different DIRECTORY
        # would no longer describe a different scope and the negative assertion
        # below would be vacuous.
        cross = wiki_read(slug, directory=other_dir, project=_OTHER_PROJECT)
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
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.config import get_settings

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
        from yadgar._shared.config import Settings
        from yadgar.backend.consolidation import ConsolidationScheduler

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        return ConsolidationScheduler(e2e_engines["storage"], e2e_engines["embeddings"], settings)

    def test_nightly_runs_sleep_cycle_produces_dream_insight(self, e2e_engines):
        """run_nightly_consolidation SHALL run the gated sleep cycle, whose dream
        replay produces BOTH artifacts for a similar pair:
          (a) BC-C4/BC-SC1a: a synthetic 'dream' insight memory (tags=['dream', ...],
              content starts with 'Dream connection:')
          (b) BC-SC1a co_occurrence: a co_occurrence relationship in the entity graph
              between entity nodes memory:{id_a} and memory:{id_b}.

        dream_replay() calls _create_dream_connection then _create_dream_insight for
        pairs with cosine > 0.7 (dream.py lines 52-55), so both artifacts are written
        atomically for any qualifying pair.
        """
        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        # Two closely-related prose memories: high embedding similarity (> 0.7 so
        # dream replay generates an insight) but not identical (< 0.95 merge
        # threshold). Pure prose with no shared file/function entities so earlier
        # consolidation phases do not pre-connect them via the entity relationship
        # table and starve dream replay's already-connected guard.
        # Measured pairwise cosine ~0.85 (all-MiniLM-L6-v2): above the 0.7 dream
        # insight threshold, below the 0.95 CURATION_SIMILARITY_THRESHOLD merge
        # threshold — so the pair survives merge_duplicates yet triggers a dream
        # insight and co_occurrence link.
        id_a = _insert_mem(
            e2e_engines,
            "The overnight maintenance job slowly drains the pending request "
            "queue and rebalances load toward the warmer replica nodes. "
            "xc4dream10001",
            yadgar_dir,
            heat=0.9,
        )
        id_b = _insert_mem(
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

        # ── observable (a): dream insight memory ──────────────────────────────
        rows = storage.get_memories_by_heat(min_heat=0.0, limit=500)
        dream_memories = [
            r
            for r in rows
            if "dream" in (r.get("tags") or [])
            and str(r.get("content", "")).startswith("Dream connection:")
        ]
        assert dream_memories, (
            "BC-C4/BC-SC1a (#37): the nightly sleep cycle MUST run dream replay and "
            "produce a synthetic 'dream' insight memory (tags=['dream', ...], "
            "content starts with 'Dream connection:') for the similar pair. Found "
            "none — the sleep cycle did not run (or dream replay produced nothing). "
            f"Total memories seen: {len(rows)}."
        )

        # ── observable (b): co_occurrence relationship in entity graph ─────────
        # dream_replay._create_dream_connection inserts entity nodes named
        # 'memory:{id}' (type='file') and a relationship of type 'co_occurrence'
        # (weight=0.5) between them.  get_entity_by_name is the read-back path.
        ent_a = storage.get_entity_by_name(f"memory:{id_a}")
        ent_b = storage.get_entity_by_name(f"memory:{id_b}")
        assert ent_a is not None, (
            f"BC-SC1a: dream replay MUST create entity node 'memory:{id_a}'. "
            "Node absent — _create_dream_connection did not run for this pair."
        )
        assert ent_b is not None, (
            f"BC-SC1a: dream replay MUST create entity node 'memory:{id_b}'. "
            "Node absent — _create_dream_connection did not run for this pair."
        )
        # get_relationship_between checks both (src→tgt) and (tgt→src) directions
        # so insertion order does not matter.
        rel = storage.get_relationship_between(ent_a["id"], ent_b["id"])
        assert rel is not None, (
            f"BC-SC1a: dream replay MUST insert a 'co_occurrence' relationship "
            f"between entity nodes memory:{id_a} and memory:{id_b}. "
            "No relationship found — _create_dream_connection failed or the "
            "pair was skipped by the already-connected guard."
        )
        assert rel.get("relationship_type") == "co_occurrence", (
            f"BC-SC1a: the entity relationship between memory:{id_a} and "
            f"memory:{id_b} MUST have relationship_type='co_occurrence'. "
            f"Got: {rel.get('relationship_type')!r}"
        )


# ===========================================================================
# GREEN — Stale-embedding re-embedding (BC-SC4)
# SleepComputeEngine.reembed_stale() re-embeds memories whose embedding_model
# differs from the current model.  Observable: after the sleep cycle, a memory
# seeded with embedding_model='stale-model-v0' has embedding_model updated to
# the current model name (all-MiniLM-L6-v2).
#
# Seed with a unique sentinel string so merge_duplicates cannot delete the row.
# Use get_memory(id) after the cycle — it always returns embedding_model (with
# None default) so the field is always present (storage/memory.py:293).
# ===========================================================================


def _insert_mem_stale(
    e2e_engines,
    content: str,
    directory: str,
    stale_model: str,
    *,
    heat: float = 0.8,
) -> int:
    """Insert a memory with an explicit stale embedding_model; return the row id."""
    storage = e2e_engines["storage"]
    emb = _embed(e2e_engines, content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        "project_id": _TEST_PROJECT,
        "heat": heat,
        "tags": [],
        "last_accessed": now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
        "embedding_model": stale_model,
    }
    return storage.insert_memory(doc)


class TestBCSC4_ReembedStale:
    """BC-SC4: reembed_stale updates a memory whose embedding_model is outdated.

    get_memories_needing_reembedding returns memories where
    'embedding_model IS NONE OR embedding_model != current_model'
    (storage/vector.py:113-116).  After run_sleep_cycle(), update_memory_embedding
    sets embedding_model to embeddings.get_model_name() ('all-MiniLM-L6-v2').
    """

    def _make_scheduler(self, e2e_engines):
        from yadgar._shared.config import Settings
        from yadgar.backend.consolidation import ConsolidationScheduler

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        return ConsolidationScheduler(e2e_engines["storage"], e2e_engines["embeddings"], settings)

    def test_reembed_stale_updates_embedding_model(self, e2e_engines):
        """run_nightly_consolidation sleep cycle SHALL re-embed a memory whose
        embedding_model is 'stale-model-v0' (outdated) and update it to
        the current model name 'all-MiniLM-L6-v2'.

        Before: embedding_model='stale-model-v0' (not equal to current model).
        After:  embedding_model='all-MiniLM-L6-v2' (current model, from
                embeddings.get_model_name()).

        The sentinel string 'xsc4reembed' distinguishes this memory from any
        other test data so merge_duplicates cannot accidentally delete it.
        """
        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        stale_model = "stale-model-v0"
        current_model = embeddings.get_model_name()  # "all-MiniLM-L6-v2"

        mem_id = _insert_mem_stale(
            e2e_engines,
            "BC-SC4 reembed: the metrics exporter scrapes Prometheus counters and "
            "pushes them to the time-series store every fifteen seconds xsc4reembed99001",
            yadgar_dir,
            stale_model=stale_model,
            heat=0.8,
        )

        # Pre-condition: the freshly inserted row carries the stale sentinel
        pre_row = storage.get_memory(mem_id)
        assert pre_row is not None, f"BC-SC4 setup: memory {mem_id} must exist before cycle"
        assert pre_row.get("embedding_model") == stale_model, (
            f"BC-SC4 setup: expected embedding_model={stale_model!r} before cycle, "
            f"got {pre_row.get('embedding_model')!r}"
        )

        scheduler = self._make_scheduler(e2e_engines)
        # Fresh scheduler: _last_sleep_cycle is None → 6-hour gate opens → cycle fires.
        scheduler.run_nightly_consolidation()

        # Post-condition: reembed_stale MUST have updated embedding_model to current
        post_row = storage.get_memory(mem_id)
        assert post_row is not None, (
            f"BC-SC4: memory {mem_id} MUST still exist after the sleep cycle "
            "(merge_duplicates should not have removed a unique memory)."
        )
        assert post_row.get("embedding_model") == current_model, (
            f"BC-SC4 (#37): reembed_stale MUST update the stale memory's "
            f"embedding_model from {stale_model!r} to {current_model!r}. "
            f"Got: {post_row.get('embedding_model')!r}. "
            "Either reembed_stale did not run or the update_memory_embedding "
            "call did not persist correctly."
        )


# ===========================================================================
# GREEN — auto_narrate writes a project story (BC-SC6)
# NarrativeEngine.auto_narrate() finds directories with memories heat > 0.3,
# checks for a recent narrative entry, and if none exists generates one via
# generate_narrative() which always inserts a row (storage.insert_narrative_entry).
#
# Observable: after run_nightly_consolidation, get_narratives_for_directory(dir)
# returns ≥1 row, and the row's summary contains the directory string (narrative.py:93
# always emits "In {directory}, during ...: {count} memories recorded.").
#
# We seed ≥1 hot memory so the dir qualifies as active (heat > 0.3), and no
# prior narrative exists (fresh isolated DB per test), so auto_narrate fires.
# ===========================================================================


class TestBCSC6_AutoNarrateWritesProjectStory:
    """BC-SC6: auto_narrate generates a narrative entry for the seeded directory.

    auto_narrate() queries active directories (min_heat=0.3), checks for a
    recent narrative (within NARRATIVE_INTERVAL_HOURS), and calls
    generate_narrative() if none exists.  generate_narrative() always inserts
    a narrative_entry row with a summary that includes the directory path.
    """

    def _make_scheduler(self, e2e_engines):
        from yadgar._shared.config import Settings
        from yadgar.backend.consolidation import ConsolidationScheduler

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        return ConsolidationScheduler(e2e_engines["storage"], e2e_engines["embeddings"], settings)

    def test_auto_narrate_inserts_narrative_for_active_directory(self, e2e_engines):
        """run_nightly_consolidation sleep cycle SHALL call auto_narrate, which
        generates a narrative_entry row for a directory with hot memories.

        Observable: get_narratives_for_directory(yadgar_dir) returns ≥1 row
        AFTER the cycle, and the summary field contains the directory path
        (guaranteed by generate_narrative line: "In {directory}, during ...").

        Seeding three memories (heat=0.9) ensures the directory qualifies as
        active (heat > 0.3 threshold) and provides non-zero memory count in
        the generated summary.
        """
        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        # Pre-condition: no narrative entries yet in the isolated DB
        pre_narratives = storage.get_narratives_for_directory(yadgar_dir, limit=10)
        assert not pre_narratives, (
            "BC-SC6 setup: fresh isolated DB must have no prior narratives for "
            f"{yadgar_dir!r}. Got {len(pre_narratives)} existing entries."
        )

        # Seed hot memories — heat=0.9 keeps them active through one decay pass
        for i in range(3):
            _insert_mem(
                e2e_engines,
                f"BC-SC6 narrate: the load balancer distributes traffic across "
                f"healthy backend replicas using round-robin scheduling xsc6narr{i:05d}",
                yadgar_dir,
                heat=0.9,
            )

        scheduler = self._make_scheduler(e2e_engines)
        # Fresh scheduler: _last_sleep_cycle is None → 6-hour gate opens → cycle fires.
        scheduler.run_nightly_consolidation()

        # Post-condition: at least one narrative_entry row for yadgar_dir
        post_narratives = storage.get_narratives_for_directory(yadgar_dir, limit=10)
        assert post_narratives, (
            f"BC-SC6 (#37): auto_narrate MUST insert a narrative_entry for directory "
            f"{yadgar_dir!r} when hot memories are present (heat=0.9 > 0.3 threshold). "
            "No narrative found — either the sleep cycle did not run, auto_narrate "
            "skipped the directory, or generate_narrative failed silently."
        )

        # The summary MUST include the directory path (narrative.py:93 always emits
        # "In {directory}, during the last N hours: M memories recorded.")
        summary = post_narratives[0].get("summary", "")
        assert yadgar_dir in summary, (
            f"BC-SC6: narrative summary MUST contain the directory path {yadgar_dir!r}. "
            f"Got summary: {summary!r}"
        )


# ===========================================================================
# xfail(strict) — KNOWN-BROKEN subsystems written as failing specs.
# ===========================================================================


class TestBCAC2_AstrocyteDomainConsolidation:
    """BC-AC2 / BC-C5a: AstrocytePool domain consolidation produces a per-domain summary."""

    def test_consolidate_domain_produces_summary(self, e2e_engines):
        """assign_memory → consolidate_domain SHALL produce a non-empty domain summary."""
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.astrocyte_pool import AstrocytePool
        from yadgar._shared.config import get_settings

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
        # Create the 4 domain process records (runs at daemon startup in prod);
        # without it consolidate_domain has no process to consolidate.
        pool.init_processes()

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


def _require_model_cached(model_name: str) -> None:
    """Skip the test unless the seq2seq model is in the local HF cache.

    Host `make e2e` has no COMET/doc2query model → these enrichment e2e SKIP there
    (gate stays green). The model-bundled CI image (yadgar-ci:5.72.0) bakes them →
    the tests actually run + assert in that image. local_files_only=True never
    downloads.
    """
    try:
        from transformers import AutoConfig  # noqa: PLC0415

        AutoConfig.from_pretrained(model_name, local_files_only=True)
    except Exception:
        pytest.skip(
            f"{model_name} not in local HF cache — enrichment e2e runs only in the "
            "model-bundled CI image (yadgar-ci:5.72.0)"
        )


class TestBCEN3a_Doc2QueryEnrichment:
    """BC-EN3a: doc2query/msmarco-t5-small-v1 generates synthetic queries for a stored memory.

    Requires: yadgar-ci:5.72.0 image with doc2query/msmarco-t5-small-v1 baked in.

    Observable: after memorize() + drain, the persisted memory row (re-fetched via
    storage.get_memory(id) → SELECT * FROM memory:N) carries a non-empty
    ``enrichment_queries`` list.  This column is written by
    _enrich_memory_if_enabled (storage/memory.py:227) when DOC2QUERY_ENRICHMENT_ENABLED
    is True (config default) and the model is present.

    We do NOT assert ``[enrichment]`` in ``enriched_content`` — that marker can
    appear from LOGIC_ENRICHMENT alone (no model needed) and would not prove doc2query
    ran.  ``enrichment_queries`` is written ONLY by Doc2QueryExpander.expand().

    Note: search_memories_fts / get_memories_by_heat return SELECT * rows that
    include enrichment columns when stored, but we use storage.get_memory(id) for
    the direct record-lookup path (memory.py:289) to guarantee the full row.
    """

    def test_stored_memory_has_synthetic_queries(self, e2e_engines):
        """A memory written via memorize() SHALL carry a non-empty enrichment_queries
        list written by doc2query/msmarco-t5-small-v1 (BC-EN3a).

        Real observable: ``enrichment_queries`` is a non-empty list in the persisted
        memory row (storage/memory.py:227 — result.queries persisted as enrichment_queries).
        """
        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        from yadgar._shared.config import get_settings  # noqa: PLC0415

        _require_model_cached(get_settings().DOC2QUERY_MODEL)

        # Factual prose — doc2query/msmarco-t5-small-v1 handles any factual
        # document regardless of subject type; no person-subject constraint.
        content = (
            "BC-EN3a enrichment wiring: the connection pool exhausts under burst "
            "load when the reaper interval exceeds the idle timeout xen3a10010"
        )
        row = _memorize_and_find(e2e_engines, content, yadgar_dir, ["e2e", "bc-en3a"])
        assert row is not None, "test setup: memorize() + drain must persist the memory"

        # Re-fetch via direct record lookup to guarantee all enrichment columns
        # are present (SELECT * FROM memory:N — memory.py:289).
        full_row = storage.get_memory(row["id"])
        assert full_row is not None, f"BC-EN3a: get_memory({row['id']}) must return the stored row"

        queries = full_row.get("enrichment_queries")
        assert queries and len(queries) > 0, (
            "BC-EN3a: a stored memory MUST carry a non-empty enrichment_queries list "
            "written by doc2query/msmarco-t5-small-v1 (storage/memory.py:227). "
            f"Got enrichment_queries={queries!r}. "
            "Possible causes: DOC2QUERY_ENRICHMENT_ENABLED=False, model absent from "
            "CI image, or _enrich_memory_if_enabled guard conditions not met "
            "(settings/embedding/content-length checks at memory.py:212-218)."
        )


# ===========================================================================
# GREEN — COMET-BART commonsense inference (BC-EN2a)
# CometInferencer.infer() generates commonsense triples (xAttr/xIntent/xWant)
# for a stored memory.  The pipeline writes them to enrichment_comet
# (storage/memory.py:226).
#
# Content shape matters: _extract_predicates (comet.py:33-46) yields useful
# predicates for sentences with a capitalized named subject or a pronoun subject
# (^[A-Z][a-z]+\s+\w+ OR ^(?:He|She|They|I|We)\s+).  Generic noun-phrase prose
# ("the connection pool...") would fall through to the full-content fallback but
# COMET-BART returns "none" or near-zero-confidence for that shape.
# We use a named-person event to give COMET a clear xAttr/xIntent predicate.
# ===========================================================================


@pytest.mark.xfail(
    reason=(
        "BC-EN2a WON'T-IMPLEMENT — COMET retired to dormant per ADR-0004. The en2a "
        "ablation (benchmarks/reports/en2a_comet_ablation_2026-06-24.md) DECIDED the "
        "open question: un-FPA'd COMET does NOT help recall (multi-session R@5 -4.2pt) "
        "at ~17h/10-core cost → net-negative. COMET_ENRICHMENT_ENABLED now defaults "
        "False; the code is retained dormant. COMET still DOES produce inferences "
        "(verified in-image), but the pipeline FPA filter drops its abstract traits → "
        "enrichment_comet empty. Test kept xfail/skip (not deleted) to guard the "
        "dormant code path. BC-EN2a stays ❌ — intentional, non-blocking."
    ),
    strict=False,
)
class TestBCEN2a_CometEnrichment:
    """BC-EN2a: COMET-BART generates commonsense inferences for a stored memory.

    Requires: yadgar-ci:5.72.0 image with mismayil/comet-bart-ai2 baked in.

    Observable: after memorize() + drain, the persisted memory row carries a
    non-empty ``enrichment_comet`` list written by CometInferencer.infer()
    (storage/memory.py:226 — result.comet_inferences persisted as enrichment_comet).

    Content uses a person-subject event sentence so _extract_predicates yields a
    clean predicate (comet.py:44 — '^[A-Z][a-z]+\\s+\\w+' matches "Alice migrated
    ..."), giving COMET-BART the best chance of returning above-threshold triples.
    """

    def test_stored_memory_has_comet_inferences(self, e2e_engines):
        """A memory written via memorize() with person-subject prose SHALL carry a
        non-empty enrichment_comet list written by mismayil/comet-bart-ai2 (BC-EN2a).

        Real observable: ``enrichment_comet`` is a non-empty list in the persisted
        memory row (storage/memory.py:226 — result.comet_inferences persisted).
        """
        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        from yadgar._shared.config import get_settings  # noqa: PLC0415

        _require_model_cached(get_settings().COMET_MODEL)

        # Person-subject prose — _extract_predicates (comet.py:44) matches
        # '^[A-Z][a-z]+\s+\w+' → "Alice migrated ..." → clean COMET predicate.
        # xAttr/xIntent/xWant relations are configured in COMET_RELATIONS (config.py:244).
        content = (
            "Alice migrated the production database to the new cluster during the "
            "scheduled maintenance window. She wanted to cut query latency for the "
            "downstream services. xen2a20020"
        )
        row = _memorize_and_find(e2e_engines, content, yadgar_dir, ["e2e", "bc-en2a"])
        assert row is not None, "test setup: memorize() + drain must persist the memory"

        # Re-fetch via direct record lookup to guarantee all enrichment columns present.
        full_row = storage.get_memory(row["id"])
        assert full_row is not None, f"BC-EN2a: get_memory({row['id']}) must return the stored row"

        inferences = full_row.get("enrichment_comet")
        assert inferences and len(inferences) > 0, (
            "BC-EN2a: a stored memory MUST carry a non-empty enrichment_comet list "
            "written by mismayil/comet-bart-ai2 (storage/memory.py:226). "
            f"Got enrichment_comet={inferences!r}. "
            "Possible causes: COMET_ENRICHMENT_ENABLED=False, model absent from CI "
            "image, COMET_MIN_CONFIDENCE too high for returned sequences, or "
            "_extract_predicates returned no predicates for the content shape."
        )


# ===========================================================================
# GREEN (network-gated) — ConceptNet HTTP expansion (BC-EN1a)
# ConceptNetExpander(http_enabled=True)._try_http() queries api.conceptnet.io
# and returns related concepts.  The expand() method writes them to
# enrichment_concepts (storage/memory.py:225) when CONCEPTNET_ENRICHMENT_ENABLED
# is True and the pipeline runs.
#
# EN1a is tested by driving ConceptNetExpander directly (not via the full
# memorize() path) because:
#   - The index-time pipeline uses http_enabled=False (default off for perf);
#     flipping it on inside CI would slow every indexed memory by ~5 s/term.
#   - Direct invocation avoids the daemon overhead and lets us inspect the
#     exact HTTP result before any FPA filtering.
#
# NETWORK LIMITATION: _try_http calls https://api.conceptnet.io — requires
# outbound internet from the test runner.  In CI (ci-pr.yaml) e2e tests are
# excluded (-m 'not e2e'); this test is only invoked via `make e2e` in a
# runner with network access.  If the runner has no outbound, the test is
# skipped via a preflight probe (NOT xfailed — skip is cleaner for infra gaps).
#
# We do NOT depend on the ~9 GB conceptnet_lite SQLite DB (_try_lite is absent
# from the CI image); _lite_available will be False immediately.  We also pick
# a term NOT in HARDCODED_EXPANSIONS (conceptnet.py:112-133) — "database" is
# absent from that dict — so any non-empty result from expand() MUST come from
# the HTTP path.
# ===========================================================================


class TestBCEN1a_ConceptNetHTTP:
    """BC-EN1a: ConceptNetExpander(http_enabled=True) fetches related concepts via HTTP.

    NETWORK-GATED: skipped when api.conceptnet.io is unreachable.
    NOT in the CI matrix (-m e2e excluded from ci-pr.yaml); runs via `make e2e` only.
    """

    def test_http_expand_returns_concepts(self, e2e_engines):
        """ConceptNetExpander(http_enabled=True).expand() SHALL return ≥1 concept
        for a term absent from HARDCODED_EXPANSIONS via the api.conceptnet.io HTTP API.

        Real observable: expand() returns a non-empty list for term "database"
        (absent from HARDCODED_EXPANSIONS) when http_enabled=True.  Because
        _try_lite returns [] (conceptnet_lite absent from image) and "database"
        is not in HARDCODED_EXPANSIONS, any non-empty result MUST come from _try_http.

        Network gate: preflight httpx GET to api.conceptnet.io with 5 s timeout;
        pytest.skip on any connection/timeout error so infra gaps don't fail CI.
        """

        import pytest

        # Preflight probe — skip test if outbound network is unavailable.
        try:
            import httpx

            probe_url = "https://api.conceptnet.io/c/en/database?limit=1"
            resp = httpx.get(
                probe_url,
                headers={"Accept": "application/json"},
                timeout=5.0,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(
                f"BC-EN1a: api.conceptnet.io unreachable — network not available "
                f"in this runner ({type(exc).__name__}: {exc}). "
                "This test requires outbound internet; run via `make e2e` on a "
                "network-connected host."
            )

        from yadgar._shared.config import Settings
        from yadgar._shared.enrichment.conceptnet import HARDCODED_EXPANSIONS, ConceptNetExpander

        # Confirm "database" is absent from hardcoded expansions so the result
        # cannot come from _try_hardcoded.
        assert "database" not in HARDCODED_EXPANSIONS, (
            "BC-EN1a test design: 'database' must not be in HARDCODED_EXPANSIONS "
            "so we can prove the result came from the HTTP path. "
            "Choose a different term if 'database' was added to the dict."
        )

        settings = Settings()
        expander = ConceptNetExpander(http_enabled=True)
        # Provide content containing only "database" as a non-stop content term
        # so _extract_terms yields ["database"] and expand() queries that term.
        content = "The database cluster stores persistent records."
        concepts = expander.expand(content, settings)

        assert concepts and len(concepts) > 0, (
            "BC-EN1a: ConceptNetExpander(http_enabled=True).expand() MUST return ≥1 "
            "concept for 'database' (absent from HARDCODED_EXPANSIONS) via the "
            "api.conceptnet.io HTTP API. Got empty list. "
            "Check that _try_http is called (http_enabled=True sets _http_available=True "
            "in __init__) and that the API returned edges above CONCEPTNET_MIN_EDGE_WEIGHT "
            f"({settings.CONCEPTNET_MIN_EDGE_WEIGHT})."
        )


# ===========================================================================
# GREEN — CLS episodic→semantic promotion
# BC-CLS1 (episodic grouped from episodes) + BC-CLS2 (repeated patterns abstracted
# to semantic) + BC-CLS3 (promoted memory derives directory from sources, v5.64).
#
# The qualifying conditions for _qualify_cluster require:
#   - ≥3 cluster members (min_occurrences default)
#   - ≥2 distinct session_ids (via source_episode_id or created_at[:10])
#   - Embedding cosine ≥ CLUSTER_SIMILARITY_THRESHOLD (0.7 default)
#
# We seed 4 closely-related episodic memories split across two calendar dates (to
# get session diversity from created_at[:10]).  Same prose repeated with minor
# variation so their cosine similarity exceeds 0.7.  All stamped with the same
# yadgar_dir so promoted memory inherits that directory via dominant_directory().
# ===========================================================================


def _insert_mem_dated(
    e2e_engines,
    content: str,
    directory: str,
    created_at: str,
    *,
    heat: float = 0.8,
) -> int:
    """Insert a memory with a specific created_at timestamp for session-diversity seeding."""
    storage = e2e_engines["storage"]
    emb = _embed(e2e_engines, content)
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        "project_id": _TEST_PROJECT,
        "heat": heat,
        "tags": [],
        "last_accessed": created_at,
        "created_at": created_at,
        "access_count": 0,
        "is_protected": False,
    }
    return storage.insert_memory(doc)


class TestBCCLS1_2_3_EpisodicToSemantic:
    """BC-CLS1 / BC-CLS2 / BC-CLS3: DualStoreCLS consolidation_cycle promotes
    repeated episodic patterns to a semantic memory stamped with the source directory."""

    def _make_cls(self, e2e_engines):
        from yadgar._shared.config import Settings
        from yadgar.backend.cls_store import DualStoreCLS

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        return DualStoreCLS(
            storage=e2e_engines["storage"],
            embeddings=e2e_engines["embeddings"],
            settings=settings,
        )

    def test_consolidation_cycle_promotes_semantic_and_stamps_directory(self, e2e_engines):
        """consolidation_cycle SHALL:
        1. Group similar episodic memories (BC-CLS1) — find_recurring_patterns returns ≥1.
        2. Abstract them to a new semantic memory (BC-CLS2) — total_semantic goes 0→≥1.
        3. Stamp the promoted memory with the dominant source directory (BC-CLS3).

        Session diversity is provided by two distinct created_at dates (≥2 days apart),
        which _session_proxy uses as a proxy when source_episode_id is absent.
        All source memories are stamped with yadgar_dir, so dominant_directory() → yadgar_dir.
        """
        storage = e2e_engines["storage"]
        yadgar_dir = e2e_engines["yadgar_dir"]

        # Verify precondition: no semantic memories yet
        pre_semantic = storage.count_memories_by_store_type("semantic")

        # Day-1 memories (two on 2024-01-10)
        day1 = "2024-01-10T10:00:00+00:00"
        day2 = "2024-01-11T10:00:00+00:00"

        # Four paraphrases of the same engineering fact — high embedding similarity
        # (cosine ~0.88 with all-MiniLM-L6-v2), well above 0.7 threshold.
        # No negation so check_consistency passes; no _action_stream / auto-abstracted tags.
        prose_variants = [
            "The deployment pipeline publishes container images to ECR and triggers "
            "a rolling restart on the production cluster xcls1seed0001",
            "Publishing Docker images to ECR then issuing a rolling restart on the "
            "prod cluster is the standard deployment pipeline xcls1seed0002",
            "Rolling restarts on the production cluster follow ECR image publication "
            "as part of our established deployment pipeline xcls1seed0003",
            "Standard deployment: push container image to ECR, then perform a rolling "
            "restart across the production cluster xcls1seed0004",
        ]
        # Two on day1, two on day2 → _session_proxy sees ≥2 distinct dates
        for prose in prose_variants[:2]:
            _insert_mem_dated(e2e_engines, prose, yadgar_dir, day1, heat=0.8)
        for prose in prose_variants[2:]:
            _insert_mem_dated(e2e_engines, prose, yadgar_dir, day2, heat=0.8)

        cls = self._make_cls(e2e_engines)

        # BC-CLS1: find_recurring_patterns should find ≥1 qualifying cluster
        patterns = cls.find_recurring_patterns()
        assert patterns, (
            "BC-CLS1: find_recurring_patterns MUST group the similar episodic memories "
            "into ≥1 qualifying cluster. Got 0 patterns — cluster similarity or "
            "session-diversity gate may have rejected all candidates."
        )

        # BC-CLS2: consolidation_cycle promotes to a semantic memory
        stats = cls.consolidation_cycle()
        post_semantic = storage.count_memories_by_store_type("semantic")
        assert stats["promoted"] >= 1, (
            f"BC-CLS2: consolidation_cycle MUST abstract episodic clusters to ≥1 "
            f"semantic memory. promoted={stats['promoted']} (stats={stats})"
        )
        assert post_semantic > pre_semantic, (
            f"BC-CLS2: semantic memory count MUST increase after promotion. "
            f"before={pre_semantic} after={post_semantic}"
        )

        # BC-CLS3: promoted semantic memory derives directory from sources
        semantic_rows = storage.get_memories_by_store_type("semantic", limit=50)
        new_semantics = [r for r in semantic_rows if r.get("heat", 0) > 0]
        assert new_semantics, "BC-CLS3: at least one semantic row must be present after promotion"

        # The promoted memory must carry yadgar_dir as its directory_context —
        # dominant_directory() over 4×yadgar_dir should return yadgar_dir (single real dir).
        promoted_dirs = {r.get("directory_context") for r in new_semantics}
        assert yadgar_dir in promoted_dirs, (
            f"BC-CLS3: promoted semantic memory MUST derive directory from source episodics. "
            f"Expected directory_context={yadgar_dir!r} in promoted rows. "
            f"Got directories: {promoted_dirs}"
        )


# ===========================================================================
# GREEN — MMR diversification (BC-RR7)
# Maximal Marginal Relevance reduces near-duplicate results in the top-k.
#
# We construct a Reranker with real storage+embeddings, seed 3 near-duplicates
# (high mutual cosine) + 1 clearly diverse memory, stamp _retrieval_score
# so mmr_rerank can measure relevance, then assert the diverse memory appears
# in the MMR top-k even though its _retrieval_score is lower than the near-dups.
#
# mmr_rerank fetches the actual stored embedding via storage.get_memory(id), so
# memories must be persisted first.
# ===========================================================================


class TestBCRR7_MMRDiversification:
    """BC-RR7: MMR rerank selects a diverse candidate over a near-duplicate."""

    def _make_reranker(self, e2e_engines):
        from yadgar._shared.config import Settings
        from yadgar.backend.retrieval.reranking import Reranker

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        # Disable ML model — mmr_rerank only uses local embeddings from storage
        return Reranker(settings, e2e_engines["storage"], ml_client=None)

    def test_mmr_selects_diverse_over_near_dup(self, e2e_engines):
        """mmr_rerank SHALL return a diverse candidate in top-k even when near-duplicates
        have higher _retrieval_score, as long as lambda_param < 1 allows diversity.

        BC-RR7 observable: in a set of 3 near-dups + 1 diverse memory, the diverse
        memory appears in the MMR top-3 output even when its raw retrieval score
        is 0.1 lower than the near-dups.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        yadgar_dir = e2e_engines["yadgar_dir"]

        # Near-duplicate trio — same fact stated three ways; expected cosine ~0.88
        near_dup_contents = [
            "The CI pipeline compiles the binary, runs tests, and pushes to ECR on "
            "every merged PR xrr7dup10001",
            "On every merged pull request the CI system compiles, tests, and pushes "
            "the binary image to ECR xrr7dup10002",
            "Merging a PR triggers compilation, test suite execution, and ECR image "
            "push through the CI pipeline xrr7dup10003",
        ]
        diverse_content = (
            "The board game tournament uses a Swiss pairing system for the first "
            "three rounds before a single-elimination bracket xrr7div10004"
        )

        dup_ids = []
        for c in near_dup_contents:
            mid = _insert_mem(e2e_engines, c, yadgar_dir, heat=0.8)
            dup_ids.append(mid)
        diverse_id = _insert_mem(e2e_engines, diverse_content, yadgar_dir, heat=0.8)

        # Assign scores: near-dups score 0.9, diverse scores 0.8 (10% below near-dups)
        # The diverse memory should still win on MMR due to low similarity to selected.
        candidates = []
        for mid in dup_ids:
            mem = storage.get_memory(mid)
            assert mem is not None, f"BC-RR7 setup: memory {mid} must be retrievable"
            mem["_retrieval_score"] = 0.9
            candidates.append(mem)

        diverse_mem = storage.get_memory(diverse_id)
        assert diverse_mem is not None, "BC-RR7 setup: diverse memory must be retrievable"
        diverse_mem["_retrieval_score"] = 0.8
        candidates.append(diverse_mem)

        reranker = self._make_reranker(e2e_engines)
        # Encode the query to get a query embedding for MMR
        query_embedding = embeddings.encode("deployment pipeline CI ECR")

        # lambda=0.5: equal weight to relevance and diversity.
        # At 0.5, the diversity penalty on the 2nd/3rd near-dup should exceed the
        # 0.1 score gap, causing the diverse memory to be selected before the 3rd near-dup.
        result = reranker.mmr_rerank(
            list(candidates),  # copy — mmr mutates order
            query_embedding=query_embedding,
            top_k=3,
            lambda_param=0.5,
        )

        assert len(result) == 3, f"BC-RR7: mmr_rerank must return top_k=3, got {len(result)}"

        result_ids = {r["id"] for r in result}
        assert diverse_id in result_ids, (
            "BC-RR7: MMR MUST select the diverse candidate in top-3 even when its "
            f"_retrieval_score ({0.8}) is lower than near-dups ({0.9}). "
            f"Result ids: {result_ids}, diverse id: {diverse_id}. "
            "MMR diversification is not working — lambda_param may be too high, "
            "or embeddings are not stored with sufficient similarity for the near-dups."
        )


# ===========================================================================
# GREEN — Convex fusion (BC-RR10)
# The fusion default is convex combination of normalized signal scores
# (FUSION_METHOD = "convex" per config default).
#
# We test _convex_fuse directly with two competing signals: in signal A, memory
# M1 scores high; in signal B, M2 scores high.  _convex_fuse with equal weights
# should pick whichever has the better combined score — not the same top-1 as
# either signal alone (proving it fuses, not just delegates to one signal).
# No ML model needed; no storage needed — pure score-dict arithmetic.
# ===========================================================================


class TestBCRR10_ConvexFusion:
    """BC-RR10: _convex_fuse produces a rank-based convex combination, not a passthrough."""

    def test_convex_fuse_combines_signals(self, e2e_engines):
        """_convex_fuse SHALL combine two signals such that a memory dominating
        both signals wins over one that dominates only one signal.

        Observable: M3 (moderate in both vector + fts) ranks higher than M1
        (strong in vector only) or M2 (strong in fts only) after convex fusion
        with equal weights.  This proves the function actually combines signals
        rather than passing through either one.
        """
        from yadgar.backend.retrieval.fusion import _convex_fuse

        # Memory IDs (arbitrary ints for this pure-function test)
        M1, M2, M3 = 1001, 1002, 1003

        signal_scores: dict[str, dict[int, float]] = {
            "vector": {M1: 0.9, M2: 0.1, M3: 0.6},
            "fts": {M1: 0.1, M2: 0.9, M3: 0.6},
        }
        weights = {"vector": 1.0, "fts": 1.0}

        fused = _convex_fuse(signal_scores, weights)
        # Result is [(mid, score), ...] sorted descending
        assert fused, "BC-RR10: _convex_fuse must return a non-empty result"
        top_id = fused[0][0]

        # After min-max normalisation within each signal:
        #   vector: M1=1.0, M2=0.0, M3=0.625  (norm = (0.6-0.1)/(0.9-0.1) = 0.625)
        #   fts:    M2=1.0, M1=0.0, M3=0.625
        # Combined (equal w=0.5 after normalisation):
        #   M1 = 0.5*1.0 + 0.5*0.0 = 0.5
        #   M2 = 0.5*0.0 + 0.5*1.0 = 0.5
        #   M3 = 0.5*0.625 + 0.5*0.625 = 0.625
        # M3 should win — it is consistently moderate across both signals.
        assert top_id == M3, (
            f"BC-RR10: _convex_fuse MUST rank M3 (consistently moderate) above "
            f"M1 (vector-only) and M2 (fts-only) with equal weights. "
            f"top_id={top_id}, expected={M3}. "
            f"Full fused order: {fused}"
        )

        # Sanity: all three IDs in output
        fused_ids = {mid for mid, _ in fused}
        assert {M1, M2, M3} <= fused_ids, (
            f"BC-RR10: all input memory IDs must appear in fused output. fused_ids={fused_ids}"
        )


# ===========================================================================
# GREEN — Confidence gate (BC-RR5)
# The quality floor / confidence gate drops low-confidence results (CE≈0).
#
# compute_signal_confidence returns 0.0 for an empty ranked list (no results
# found by that signal → zero confidence).  For a populated list it returns
# a positive score.  Together these assert the gate's discriminating math.
#
# detect_adversarial returns {"abstain": True} when the single result is
# very low-scored, proving the abstain path for near-zero CE.
# ===========================================================================


class TestBCRR5_ConfidenceGate:
    """BC-RR5: confidence gate / quality floor correctly scores and abstains."""

    def _make_reranker(self, e2e_engines):
        from yadgar._shared.config import Settings
        from yadgar.backend.retrieval.reranking import Reranker

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        return Reranker(settings, e2e_engines["storage"], ml_client=None)

    def test_zero_confidence_for_empty_signal(self, e2e_engines):
        """compute_signal_confidence(vector, []) SHALL return 0.0 — no results → no confidence."""
        reranker = self._make_reranker(e2e_engines)
        conf = reranker.compute_signal_confidence("vector", [])
        assert conf == 0.0, (
            f"BC-RR5: empty ranked list MUST yield 0.0 confidence for 'vector' signal. Got {conf}"
        )

    def test_positive_confidence_for_populated_signal(self, e2e_engines):
        """compute_signal_confidence(vector, non-empty) SHALL return > 0 — results found."""
        reranker = self._make_reranker(e2e_engines)
        # A single result with score 0.7 — confidence = min(1.0, 0.7 * (1 + 0.7)) = 1.0
        conf = reranker.compute_signal_confidence("vector", [(101, 0.7)])
        assert conf > 0.0, (
            f"BC-RR5: non-empty ranked list MUST yield positive confidence. Got {conf}"
        )

    def test_abstain_on_near_zero_score(self, e2e_engines):
        """detect_adversarial SHALL return abstain=True when the single result has
        a near-zero retrieval score (CE≈0 → floor triggers abstain)."""
        reranker = self._make_reranker(e2e_engines)
        low_conf_result = [{"_retrieval_score": 0.01, "id": 202, "content": "x"}]
        analysis = reranker.detect_adversarial(low_conf_result)
        assert analysis.get("abstain") is True, (
            "BC-RR5: detect_adversarial MUST set abstain=True for a single near-zero "
            f"scored result (CE≈0 quality floor). Got: {analysis}"
        )

    def test_no_abstain_on_high_score(self, e2e_engines):
        """detect_adversarial SHALL NOT abstain when top result has a high retrieval score."""
        reranker = self._make_reranker(e2e_engines)
        # Two results: clear winner (0.9) vs runner-up (0.2) → high z-gap → confident
        high_conf_results = [
            {"_retrieval_score": 0.9, "id": 301, "content": "strong match"},
            {"_retrieval_score": 0.2, "id": 302, "content": "weak match"},
        ]
        analysis = reranker.detect_adversarial(high_conf_results)
        assert analysis.get("abstain") is False, (
            "BC-RR5: detect_adversarial MUST NOT abstain when top result is high-scored. "
            f"Got: {analysis}"
        )
