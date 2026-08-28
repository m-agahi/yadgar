"""Hippocampal Replay — intelligent context restoration after compaction.

T2 Car B (layer-boundary train, census verdict #7): moved from
``yadgar._shared.restoration.checkpoint_restore`` — restore is COMPUTE over DB
data and runs backend-side behind ``POST /restore``. The backend composition
point is ``yadgar.backend.restoration.ensure_restoration_engines`` (called from
the embed-service engine bootstrap + the drainer's ``ensure_write_engines``).
Core reaches restore ONLY over HTTP (``_forward_restore`` /
``_forward_admin("pre_compact_drain", ...)``). The CheckpointContext contract
stays in ``yadgar._shared.restoration.contract``.
"""

import json
import logging
import re

from yadgar._shared.blocks_render import render_blocks_section
from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.metacognition import MetaCognition
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.restoration.contract import CheckpointContext

# _list_worktrees moved to _shared (Car fix-drain-inflight) so the host-side
# drain callers can list worktrees where the git tree is visible. Re-exported
# here to preserve the module-attribute name for the in-container FALLBACK path
# (embedded/dev) and for callers/tests that patch checkpoint_restore._list_worktrees.
from yadgar._shared.restoration.transcript_parse import _list_worktrees
from yadgar._shared.storage import StorageEngine
from yadgar.backend.restoration.cognitive_map import CognitiveMap
from yadgar.backend.retrieval.core import Retriever

logger = logging.getLogger(__name__)

# Patterns that trigger micro-checkpoints
_MICRO_ERROR_RE = re.compile(r"\b(error|exception|traceback|failed|crash|bug)\b", re.IGNORECASE)
_MICRO_DECISION_RE = re.compile(
    r"\b(decided|chose|switched|migrated|will use|going with|opted)\b", re.IGNORECASE
)


class CheckpointRestore:
    """Reconstructs context after Claude Code compaction events.

    Named after the neuroscience phenomenon where the hippocampus
    replays important experiences during sleep to consolidate them.
    Context compaction IS the 'sleep' — we replay what matters when
    Claude 'wakes up'.
    """

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        retriever: Retriever | None = None,
        cognitive_map: CognitiveMap | None = None,
        metacognition: MetaCognition | None = None,
        settings: Settings | None = None,
    ):
        self._storage = storage
        self._embeddings = embeddings
        self._retriever = retriever
        self._cognitive_map = cognitive_map
        self._metacognition = metacognition
        self._settings = settings or Settings()
        self._tool_call_count = 0

    def record_tool_call(self):
        """Track tool calls for auto-checkpoint threshold."""
        self._tool_call_count += 1

    @observe(tier="hot")
    def should_auto_checkpoint(self) -> bool:
        """Check if we've hit the auto-checkpoint interval."""
        interval = self._settings.REPLAY_CHECKPOINT_AUTO_INTERVAL
        if interval <= 0:
            return False
        return self._tool_call_count > 0 and self._tool_call_count % interval == 0

    def reset_tool_count(self):
        """Reset after checkpoint."""
        self._tool_call_count = 0

    @trace_span()
    def create_checkpoint(
        self,
        directory: str,
        ctx: CheckpointContext | None = None,
        session_id: str = "default",
        project_id: str | None = None,
    ) -> dict:
        """Create a working state checkpoint for post-compaction recovery.

        ctx bundles the optional payload fields: current_task,
        files_being_edited, key_decisions, open_questions, next_steps,
        active_errors, custom_context.

        C11 (0047 PR#40 §5): ``project_id`` is the host-minted identity the
        ``checkpoint`` MCP tool already puts on its enqueue payload; migration
        033 gives the table a column to hold it and ``insert_checkpoint`` stamps
        it. ``directory_context`` is still written — it remains the supersede
        key and the transitional read arm, since no backfill covers this table.
        Absent → NONE, never derived (ADR-0227).
        """
        c = ctx or CheckpointContext()
        epoch = self._storage.get_current_epoch()
        checkpoint_id = self._storage.insert_checkpoint(
            {
                "session_id": session_id,
                "directory_context": directory,
                "project_id": project_id,
                "current_task": c.current_task,
                "files_being_edited": c.files_being_edited,
                "key_decisions": c.key_decisions,
                "open_questions": c.open_questions,
                "next_steps": c.next_steps,
                "active_errors": c.active_errors,
                "custom_context": c.custom_context,
                "resume_hint": c.resume_hint,
                "epoch": epoch,
            }
        )
        self.reset_tool_count()
        return {
            "checkpoint_id": checkpoint_id,
            "epoch": epoch,
            "status": "created",
        }

    @observe(tier="boundary")
    def anchor_memory(  # noqa: PLR0913 — mirrors the anchor MCP payload
        self,
        content: str,
        context: str,
        tags: list[str],
        reason: str = "",
        tier: str | None = None,
        valid_until: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Store a memory with maximum protection — survives compaction restoration.

        Anchored memories get heat=1.0, is_protected=True, importance=1.0.
        They are ALWAYS included in restoration regardless of other scoring.

        tier: v5.8.0 — anchor tier string ("semantic_immortal"|"conditional"|"ephemeral").
        valid_until: v5.8.0 — ISO-8601 UTC expiry string; None = no expiry.

        C12 (ADR-0226) — the ``branch`` kwarg is GONE, and this was its ONE live
        non-test caller. It forwarded to ``insert_memory(branch=)``, which appended
        ``branch = $branch`` to the ``memory`` CREATE — re-creating, untyped, the
        column migration 029 dropped, because ``memory`` is SCHEMALESS. Killing the
        writer is the safety property; the schema statement never was.
        project_id: C4b (0047 PR#40 §5) — the enqueue-time stamp threaded from
            the core ``anchor`` tool via ``run_anchor_replay`` (its only
            non-test caller). Reaches ``insert_memory`` as
            ``_resolve_project_id_for_write``'s ``caller_value``, so a stamped
            anchor never touches the classifier this container cannot run
            (ADR-0227 §1.1).

        C10g — ``context`` IS NO LONGER THE SCOPE KEY. ``directory_context`` is
        stamped from ``project_id``, exactly as C10f did for ``memorize``
        (``_memorize_phases/_phase_store.py``). This half is inseparable from
        the read half: ``restore``'s anchor bucket now queries
        ``get_anchored_memories_scoped(project_id=…)``, so a stamp that stayed
        on ``context`` would make every anchor unreachable — which is precisely
        why C10f reverted its attempt rather than shipping one side. ``anchor``
        hardcodes ``file_hash=None``, so ``context`` was PURELY a scope key
        here; with the scope moved it is now only the value the caller typed,
        retained for the payload/log surface.

        ``project_id=None`` (a pre-C4b payload still sitting in the queue)
        stamps NONE rather than falling back to ``context``. A fallback would
        reintroduce the mixed path/identity semantics C10f deleted; an
        unstamped row is the same accepted cost as the un-backfilled corpus
        (plan §8 step 5b), and the C6 backfill is what closes it.

        NOTE — the ``checkpoint`` table was deliberately NOT part of this when
        C10g wrote it, because it had no ``project_id`` column. **C11 added it**
        (migration 033), so ``create_checkpoint`` / ``create_micro_checkpoint`` /
        ``pre_compact_drain`` now take and stamp a ``project_id`` of their own.
        Unlike the memory sinks, the checkpoint READ keeps a transitional legacy
        arm: no backfill covers ``checkpoint``, so a project_id-only predicate
        would make every pre-C11 checkpoint unrestorable.
        """
        embedding = self._embeddings.encode(content)
        memory_payload: dict = {
            "content": content,
            "embedding": embedding,
            "tags": tags + ["_anchor"],
            # C10g: THE STAMP — the resolved project_id, never ``context``.
            # Moves in lockstep with get_anchored_memories_scoped's re-key.
            "directory_context": project_id,
            "heat": self._settings.REPLAY_ANCHOR_HEAT,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": self._embeddings.get_model_name(),
            "project_id": project_id,
        }
        if tier is not None:
            memory_payload["tier"] = tier
        if valid_until is not None:
            memory_payload["valid_until"] = valid_until
        memory_id = self._storage.insert_memory(memory_payload)
        # Set protection and importance flags
        self._storage.protect_memory(
            memory_id,
            is_protected=True,
            importance=1.0,
            contextual_prefix=f"[ANCHOR: {reason}] " if reason else None,
        )
        return memory_id

    @observe(tier="hot")
    def should_micro_checkpoint(
        self, content: str, tags: list[str], surprisal: float = 0.0
    ) -> tuple[bool, str]:
        """Check if content warrants a micro-checkpoint.

        Triggers on significant state changes:
          - Error/exception detected in content
          - Decision made
          - Very high surprise event (surprisal > 0.8)
          - Protected/critical tags

        Returns (should_checkpoint, reason).
        """
        if not self._settings.MICRO_CHECKPOINT_ENABLED:
            return False, ""

        # Cooldown: don't checkpoint too frequently
        if self._tool_call_count < self._settings.MICRO_CHECKPOINT_COOLDOWN:
            return False, ""

        if _MICRO_ERROR_RE.search(content):
            return True, "error_detected"

        if _MICRO_DECISION_RE.search(content):
            return True, "decision_made"

        if surprisal > 0.8:
            return True, "high_surprise_event"

        tag_set = {t.lower() for t in tags}
        if tag_set & {"critical", "important", "architecture", "breaking"}:
            return True, "critical_tag"

        return False, ""

    @trace_span()
    def create_micro_checkpoint(
        self,
        directory: str,
        content: str,
        reason: str,
        project_id: str | None = None,
    ) -> dict | None:
        """Create a lightweight checkpoint triggered by a significant event.

        These are more frequent than manual checkpoints but capture less data.
        They ensure that important state transitions aren't lost between
        full checkpoints.
        """
        summary = content[:150].replace("\n", " ")
        ctx = CheckpointContext(current_task=f"[micro:{reason}] {summary}")
        return self.create_checkpoint(
            directory, ctx, session_id="micro-auto", project_id=project_id
        )

    @trace_span()
    def pre_compact_drain(
        self,
        directory: str,
        transcript_path: str | None = None,
        in_flight: dict | None = None,
        worktree_path: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Emergency context capture before compaction.

        Called by PreCompact hook. Triggers:
        1. Auto-checkpoint from sensory buffer
        2. Epoch increment (marks compaction boundary)
        3. Emergency consolidation

        In-flight capture (Car fix-drain-inflight, v5.135):

        * ``in_flight`` provided → persist it VERBATIM. This is the
          host-captured dict — the host-side drain callers parsed the transcript
          and listed worktrees where ``.claude`` + the git tree are visible (the
          backend container cannot see either). Used as-is; the transcript is NOT
          re-parsed and worktrees are NOT re-listed (an in-container relist would
          clobber the host worktrees with []). Branch keyed on *presence*, not
          truthiness — a host parse that found nothing returns a truthy
          empty-lists dict and it is authoritative.
        * ``in_flight`` absent + ``transcript_path`` given → FALL BACK to the
          in-container parse (embedded/dev deploy where the paths ARE visible).
          Preserves the HOOKS Car 2 behaviour.
        * Both absent → no in_flight written (pre-Car-2 degrade).

        C10 (0047 §5, judgement site (b)) — ``directory`` was doing TWO jobs here:
        the checkpoint **identity** (``get_active_checkpoint`` / the
        ``directory_context`` stamp below) and a **real filesystem path** handed
        to ``git -C`` for the worktree capture. The real-path half is now its own
        parameter, ``worktree_path``; when absent it falls back to ``directory``
        so the in-container fallback parse keeps working exactly as before.

        The identity half is deliberately STILL named ``directory``, and it is
        now JOINED by ``project_id`` rather than replaced by it. C11 added
        ``checkpoint.project_id`` and stamps it here, but the legacy column stays
        the supersede key and the second read arm: no backfill covers this table
        (``project_backfill._TABLES`` is ``("memory", "wiki_page")``; plan §8
        names no step for it), so dropping the path arm would silently strand
        every checkpoint written before this car. Both keys travel; the drop PR
        takes the path one.
        """
        new_epoch = self._storage.increment_epoch()

        if in_flight is None:
            in_flight = self._capture_in_flight(transcript_path, worktree_path or directory)

        # Create an auto-checkpoint if no recent one exists (per-caller).
        # C11: the lookup takes BOTH keys — project_id for rows this car stamps,
        # the legacy path for the historical corpus no backfill reaches.
        active = self._storage.get_active_checkpoint(directory, project_id=project_id or "")
        auto_created = False
        if active is None or active.get("epoch", 0) < new_epoch - 1:
            checkpoint_data: dict = {
                "session_id": "auto-drain",
                "directory_context": directory,
                "project_id": project_id,
                "current_task": "[auto-captured before compaction]",
                "epoch": new_epoch,
            }
            if in_flight is not None:
                checkpoint_data["in_flight"] = in_flight
            self._storage.insert_checkpoint(checkpoint_data)
            auto_created = True
        else:
            # Update existing checkpoint with new epoch (+ in_flight if captured)
            self._storage.update_checkpoint_epoch(active["id"], new_epoch)
            if in_flight is not None:
                self._storage.update_checkpoint_in_flight(active["id"], in_flight)

        return {
            "status": "drained",
            "epoch": new_epoch,
            "auto_checkpoint_created": auto_created,
        }

    @observe(tier="stage")
    def _capture_in_flight(
        self, transcript_path: str | None, worktree_path: str | None
    ) -> dict | None:
        """Parse the transcript for in-flight agents/shells + capture worktrees.

        Returns None when no transcript_path is given (back-compat) or when the
        parse+worktree capture yields nothing actionable. Never raises — the
        drain must not be blocked by a parse failure.

        C10 (b): ``worktree_path`` is a real filesystem path for ``git -C``
        (carve-out 3), never a scoping key. Absent → ``worktrees: []``.
        """
        if not transcript_path:
            return None
        try:
            from yadgar.backend.restoration.transcript_parse import (  # noqa: PLC0415
                parse_in_flight,
            )

            in_flight = parse_in_flight(transcript_path)
            in_flight["worktrees"] = _list_worktrees(worktree_path or "")
            return in_flight
        except Exception:  # noqa: BLE001 — in-flight capture during pre-compact drain: it spans a lazy import, transcript parsing and a git subprocess, which share no common base, and a failed capture must return None rather than block the drain
            logger.debug("pre_compact_drain in-flight capture failed", exc_info=True)
            return None

    @observe(tier="stage")
    def _fetch_recent_memories_safe(self, project_id: str, max_memories: int) -> list[dict]:
        """Fetch this project's recently stored memories (step 3 of restore).

        Returns [] on any storage failure so restore() stays unblocked.

        Car 3 — **this was the sink that leaked on EVERY restore.** It called
        ``get_recent_memories(limit=...)`` with no project against a callee that
        had no project parameter to receive one, and the result is rendered into
        ``## Working Memory (Recently Stored)``. Unlike the other four sinks it
        did not even have a no-scope guard to fall back to, so a caller whose
        identity did not resolve — where every other bucket correctly came back
        empty — still got the corpus's newest rows. Restoring
        ``/home/max/git/nix`` returned quinyx/ai and quinyx/application-gitops
        memories on two shipped versions.

        Same posture as ``_fetch_hot_memories``: NO SCOPE MEANS EMPTY, NOT
        CORPUS-WIDE. Losing an injection is recoverable, leaking one is not.
        """
        if not project_id:
            return []
        try:
            memories = self._storage.get_recent_memories(limit=max_memories, project_id=project_id)
            for m in memories:
                m.pop("embedding", None)
            return memories
        except Exception:  # noqa: BLE001 — per-section degradation in the restore payload: the recent-memories read goes through storage with no common base, and an empty section still lets the other sections restore
            logger.debug("Failed to fetch recently stored memories for restore")
            return []

    @observe(tier="stage")
    def _fetch_hot_memories(
        self,
        project_id: str,
        exclude_ids: set[int],
        max_memories: int,
    ) -> list[dict]:
        """Fetch hot project memories, deduplicated against exclude_ids (step 4 of restore).

        C10g (0047 PR#40 §5): takes the **project_id**, not the caller's path.
        C10f moved ``memorize``'s stamp so a new ``memory`` row carries the
        resolved project_id in ``directory_context``; handing this sink a
        filesystem path matched zero rows and raised nothing.

        NO-SCOPE MEANS EMPTY, NOT CORPUS-WIDE. The deleted ``else`` branch here
        called ``get_memories_by_heat(HOT_THRESHOLD)`` — and ``HOT_THRESHOLD``
        defaults to ``0.0``, i.e. every memory in the DB. That branch was
        near-dead while a path was almost always supplied, but routing this sink
        onto project_id makes ``None`` the COMMON case for the two
        ``_forward_restore`` callers that bypass the MCP tool (the post-compact
        HTTP hook and the CLI, which resolves its project non-fatally), so a
        rare widening branch would have become the default one. ``hook_project_id``
        states the rule this follows: losing an injection is recoverable,
        leaking one is not.
        """
        if not project_id:
            return []
        hot = self._storage.get_memories_for_directory(
            project_id, min_heat=self._settings.HOT_THRESHOLD
        )
        for m in hot:
            m.pop("embedding", None)
        return [m for m in hot if m["id"] not in exclude_ids][:max_memories]

    @observe(tier="hot")
    def _build_sr_query(self, checkpoint: dict | None, project_id: str) -> str:
        """Derive SR navigation query from checkpoint task or project (step 5 of restore).

        C10 (0047 §5, judgement site (b)): this is the ONE site in ``restore``'s
        fan-out whose parameter could be renamed today. It builds **embedding
        query text** — it reads no table, so it cannot produce a silent zero-row
        match the way the four storage-backed sinks would.

        C10g closed the caveat that used to sit here: ``restore`` now threads a
        real project_id, so the string reads ``project work in m-agahi/yadgar``
        rather than ``project work in /home/max/git/yadgar``. When no project is
        named the argument is ``""`` and this returns ``""``, which disables SR
        prediction for that call — the same "no scope means no rows" posture the
        memory-backed sinks take.
        """
        if checkpoint:
            task = checkpoint.get("current_task", "")
            if task:
                return task
        return f"project work in {project_id}" if project_id else ""

    @observe(tier="stage")
    def _predict_memories(
        self,
        checkpoint: dict | None,
        project_id: str,
        seen_ids: set[int],
        max_memories: int,
        directory: str = "",
    ) -> list[dict]:
        """Run SR cognitive-map navigation to predict needed memories (step 5 of restore).

        Returns [] when cognitive map is absent or has insufficient data.

        Car 3 — **the SR path had no project predicate anywhere along it.**
        ``navigate_to`` walks a corpus-wide coordinate dict, the ``search_vectors``
        call that seeds it takes the unscoped HNSW-KNN arm, and ``get_memory(mid)``
        is a bare ``SELECT * FROM memory:{id}``. Every id the map offered was
        hydrated and rendered regardless of owner.

        The filter is applied HERE, at the consumer, rather than pushed into the
        map: the SR matrix is built over the whole corpus and scoping only the
        vector SEED would not scope the WALK that follows it. Re-keying the
        matrix itself is a different, larger change.

        Accepted consequence: other projects' ids still consume the ``top_k``
        budget, so this bucket can come back short or empty even when in-project
        rows exist. That is degradation, not leakage — this module's rule is
        that losing an injection is recoverable and leaking one is not.

        The ``project_id`` guard is load-bearing on its own: ``_build_sr_query``
        returns the checkpoint's ``current_task`` when there is one, so the query
        is non-empty even with no project — an early return keyed only on the
        query string would let the whole path run unscoped for every restore
        that had a checkpoint.
        """
        if self._cognitive_map is None or not self._cognitive_map.has_sufficient_data():
            return []
        if not project_id:
            return []
        query = self._build_sr_query(checkpoint, project_id)
        if not query:
            return []
        query_emb = self._embeddings.encode(query)
        if query_emb is None:
            return []
        sr_results = self._cognitive_map.navigate_to(
            query_emb, self._embeddings, top_k=max_memories // 2
        )
        predicted: list[dict] = []
        local_seen = set(seen_ids)
        for mid, proximity in sr_results:
            if mid in local_seen:
                continue
            mem = self._storage.get_memory(mid)
            if not mem:
                continue
            # The map is corpus-wide; the injection must not be. Same key the
            # other memory-backed sinks use (C10f put the identity here).
            #
            # C308 (#308): the post-C10f corpus stamps ``directory_context`` AND
            # ``project_id`` with the resolved ``owner/repo``, but 2237 of the
            # 2352 rows in the live corpus predate that move and still hold the
            # caller's filesystem path on ``directory_context`` with no
            # ``project_id``. A path-string vs project_id-string compare drops
            # the entire legacy corpus — the SR bucket's 95% invisible. Accept
            # either column, and accept a path-shaped ``directory_context``
            # whose value is the path THIS restore is running against: a
            # foreign project's legacy row holds a different path, the
            # comparison misses, and the leak closes. The caller-supplied path
            # is the only handle we have on "this project" before Task 310's
            # column backfill is universal.
            mem_dir = mem.get("directory_context") or ""
            mem_proj = mem.get("project_id") or ""
            in_project = (
                mem_dir == project_id
                or mem_proj == project_id
                or (mem_dir.startswith("/") and mem_dir == directory)
            )
            if not in_project:
                local_seen.add(mid)
                continue
            mem.pop("embedding", None)
            mem["_sr_proximity"] = round(proximity, 4)
            predicted.append(mem)
            local_seen.add(mid)
        return predicted

    @observe(tier="stage")
    def _detect_gaps_safe(self, project_id: str) -> list[dict]:
        """Detect knowledge gaps, suppressing errors (step 6 of restore).

        Returns at most 3 gaps. Returns [] when metacognition is absent or on error.

        C10g: takes the project_id — ``detect_gaps`` forwards straight into
        ``get_memories_for_directory``, so it inherits that sink's key.
        """
        if self._metacognition is None or not project_id:
            return []
        try:
            return self._metacognition.detect_gaps(project_id)[:3]
        except Exception:  # noqa: BLE001 — per-section degradation: gap detection drives metacognition over storage and the embedding engine, which share no common base
            logger.debug("Gap detection failed during restore")
            return []

    @trace_span()
    def restore(self, directory: str = "", project_id: str | None = None) -> dict:
        """Intelligent context reconstruction after compaction.

        Combines:
        1. Latest checkpoint (what you were doing)
        2. Anchored memories (critical facts, always included)
        3. Hot project memories (thermodynamic ranking)
        4. Predictive retrieval via SR (what you'll likely need next)
        5. Gap detection (what might have been lost)

        Returns structured data + formatted markdown for injection.

        C10g (0047 §5, judgement site (b)) — **THE FAN-OUT IS NOT UNIFORM, AND
        THAT IS THE DESIGN, NOT AN UNFINISHED SWEEP.** ``restore`` takes BOTH
        values and routes each sink to the one its table actually keys on:

          1. ``get_active_checkpoint``        → ``checkpoint.project_id``
             — **project_id, PLUS a legacy path arm.** C10g left this on the
             path because the table had no ``project_id`` column; C11's
             migration 033 added it and ``insert_checkpoint`` stamps it, so the
             sink moved together with its writer. The path arm SURVIVES because
             no backfill covers ``checkpoint`` — see ``get_active_checkpoint``.
          2. ``get_anchored_memories_scoped`` → ``memory.directory_context``
             — **project_id.** C10g moved ``anchor_memory``'s stamp onto the
             project_id in the SAME change; move either alone and every anchor
             becomes unreachable.
          3. ``get_memories_for_directory``   → ``memory.directory_context``
             — **project_id.** C10f moved ``memorize``'s stamp here, so every
             row written after that car carries ``owner/repo`` in this column.
          4. ``list_blocks``                  → ``memory_block.project_id``
             — **project_id, PLUS a legacy path arm.** C10g's rule was "a sink
             moves only when its WRITER has already moved"; C11 moved
             ``create_block``, so this one moved with it. The path arm is what
             keeps blocks written before this car visible — and there is no
             backfill that would ever close that gap. See ``_fetch_blocks_safe``.
          5. ``detect_gaps``                  → forwards into sink 3
             — **project_id**, inherited.
          6. ``get_recent_memories``          → ``memory.directory_context``
             — **project_id (Car 3).** This sink was MISSING FROM THIS LIST, and
             that is how it stayed unscoped through two cars that audited the
             fan-out: the list enumerated the sinks someone had already reasoned
             about, and a reader checking the routing against it saw five of six.
             It had no project parameter on either side of the call, so it read
             the corpus on every restore — including calls where every other
             sink correctly returned empty.
          7. ``get_memory`` via ``navigate_to`` → ``memory.directory_context``
             — **project_id (Car 3), filtered at the CONSUMER.** The SR map is
             built over the whole corpus, so the scope cannot live in the query;
             ``_predict_memories`` drops out-of-project rows after hydration.

        The rule C10g stated for the next reader — a sink moves only when its
        WRITER has already moved — is why sinks 1 and 4 stayed put then and why
        they move now: C11 moved both writers. **C11 adds the second half of the
        rule:** a sink moves onto a NEW key alone only when something makes the
        OLD rows reachable. Nothing does for these two tables, so both keep a
        legacy arm rather than trading a stale read for an empty one.

        Args:
            directory: Host-side project path. Still the key for the checkpoint
                and memory-block sinks, and the value rendered in the footer.
            project_id: Resolved ``owner/repo`` identity. ``None``/empty means
                the caller named no project — the memory-backed sinks then
                return EMPTY rather than widening (see ``_fetch_hot_memories``).
        """
        max_memories = self._settings.REPLAY_MAX_RESTORE_MEMORIES
        scope = project_id or ""

        # 1. Latest checkpoint — C11 moved this sink: project_id FIRST, the
        # legacy path as the transitional second arm.
        checkpoint = self._storage.get_active_checkpoint(directory, project_id=scope)

        # 2. Anchored memories (scope-split: global first then project)
        anchored = self._storage.get_anchored_memories_scoped(project_id=scope, limit=max_memories)
        for m in anchored:
            m.pop("embedding", None)

        # 3. Recently stored memories (working memory) — Car 3 re-keyed this
        # sink onto the project_id; it used to read the corpus unconditionally.
        recent_memories = self._fetch_recent_memories_safe(scope, max_memories)

        # 4. Hot project memories (deduplicated)
        anchor_ids = {m["id"] for m in anchored}
        recent_ids = {m["id"] for m in recent_memories}
        hot_memories = self._fetch_hot_memories(scope, anchor_ids | recent_ids, max_memories)

        # 5. Predictive retrieval via SR cognitive map
        seen_ids = anchor_ids | recent_ids | {m["id"] for m in hot_memories}
        predicted = self._predict_memories(checkpoint, scope, seen_ids, max_memories, directory)

        # 6. Gap detection
        gaps = self._detect_gaps_safe(scope)

        # Build formatted markdown for hook injection
        markdown = self._format_restoration(
            checkpoint,
            anchored,
            recent_memories,
            hot_memories,
            predicted,
            gaps,
            directory,
        )

        # 7. Memory blocks (v5.33.0) — always-injected named text containers.
        blocks = self._fetch_blocks_safe(directory, project_id=scope)
        markdown = self._prepend_blocks(blocks, directory, markdown)

        return {
            "checkpoint": checkpoint,
            "anchored_memories": len(anchored),
            "recent_memories": len(recent_memories),
            "hot_memories": len(hot_memories),
            "predicted_memories": len(predicted),
            "gaps_detected": len(gaps),
            "memory_blocks": len(blocks),
            "epoch": checkpoint.get("epoch", 0) if checkpoint else 0,
            "formatted": markdown,
        }

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """Truncate text to max_len characters, appending '...' if cut."""
        return text if len(text) <= max_len else text[:max_len] + "..."

    @observe(tier="hot")
    @staticmethod
    def _parse_list_field(value) -> list:
        """Parse a checkpoint list field — already a list or a JSON string."""
        if isinstance(value, str):
            return json.loads(value)
        return value or []

    @observe(tier="hot")
    def _format_checkpoint_section(self, checkpoint: dict) -> list[str]:
        """Return markdown lines for the checkpoint block."""
        lines: list[str] = ["## What You Were Doing"]
        if checkpoint.get("current_task"):
            lines.append(f"**Task:** {checkpoint['current_task']}")
        files = self._parse_list_field(checkpoint.get("files_being_edited"))
        if files:
            lines.append(f"**Files:** {', '.join(files)}")
        _list_sections = [
            ("key_decisions", "**Decisions:**"),
            ("open_questions", "**Open questions:**"),
            ("next_steps", "**Next steps:**"),
            ("active_errors", "**Active errors:**"),
        ]
        for fname, header in _list_sections:
            items = self._parse_list_field(checkpoint.get(fname))
            if items:
                lines.append(header)
                lines.extend(f"- {item}" for item in items)
        if checkpoint.get("custom_context"):
            lines.append(f"\n{checkpoint['custom_context']}")
        lines.append("")
        return lines

    @observe(tier="hot")
    def _format_in_flight_section(self, checkpoint: dict) -> list[str]:
        """Return markdown lines for the in-flight orchestration state, if any.

        HOOKS Car 2. Emits nothing unless ``checkpoint['in_flight']`` carries at
        least one in-flight AGENT or bg-SHELL — worktrees alone do NOT trigger the
        block (a repo always has ≥1 worktree, so gating on it would surface an
        empty "none / none" block on every compact). Worktrees appear as a
        sub-line only when there IS in-flight orchestration to contextualize. The
        wording carries the liveness caveat verbatim — dispatched, not confirmed
        still running. Tolerates a JSON-string round-trip from storage.
        """
        raw = checkpoint.get("in_flight")
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:  # JSONDecodeError is a ValueError subclass
                return []
        if not isinstance(raw, dict):
            return []
        agents = raw.get("agents") or []
        shells = raw.get("bg_shells") or []
        worktrees = raw.get("worktrees") or []
        if not (agents or shells):
            return []
        lines = ["## In-Flight At Compaction (verify — not confirmed still running)"]
        lines.append(f"- Agents dispatched, no completion seen: {', '.join(agents) or 'none'}")
        lines.append(f"- Background shells: {', '.join(shells) or 'none'}")
        if worktrees:
            lines.append("- Worktrees:")
            lines.extend(f"  - {w}" for w in worktrees)
        lines.append("")
        return lines

    def _prepend_blocks(self, blocks: list[dict], directory: str, markdown: str) -> str:
        """Prepend memory blocks section to markdown if blocks exist (v5.33.0)."""
        section = self._render_blocks_section(blocks, directory)
        return (section + "\n" + markdown) if section else markdown

    # ── C11 (0047 §5) — the sink that forced C10g's split, now DISCHARGED ──
    #
    # C10g left this on the path because ``memory_block`` had no ``project_id``
    # column and ``block_create(..., directory=...)`` still wrote real paths:
    # flipping restore's value alone would have made every block vanish from
    # every restore — zero rows, no exception.
    #
    # C11 discharged it by moving BOTH sides in one commit: migration 033 adds
    # ``memory_block.project_id``, ``create_block`` stamps it, and ``list_blocks``
    # reads ``(project_id = $pid OR directory = $dir)``.
    #
    # **The legacy arm is not laziness — it is the only thing that keeps the
    # historical corpus visible.** ``project_backfill._TABLES`` is
    # ``("memory", "wiki_page")`` and plan §8 defines no backfill step for
    # ``memory_block``, so a project_id-only predicate here would be permanent
    # silent loss of the user's own curated blocks, not the bounded degraded
    # window §8 5b sanctions for memory/wiki. It dies with the column.
    @observe(tier="stage")
    def _fetch_blocks_safe(self, directory: str, project_id: str = "") -> list[dict]:
        """Fetch memory blocks, swallowing errors (v5.33.0). Returns [] on failure."""
        try:
            return self._storage.list_blocks(
                scope=None,
                directory=directory if directory else None,
                project_id=project_id or None,
            )
        except Exception:  # noqa: BLE001 — per-section degradation for memory blocks; same untypeable storage surface, and the documented contract is 'returns [] on failure'
            logger.debug("Failed to fetch memory blocks for restore")
            return []

    def _render_blocks_section(self, blocks: list[dict], directory: str) -> str:
        """Render memory blocks as markdown section for restore() injection (v5.33.0).

        Delegates to yadgar.blocks_render.render_blocks_section (v5.35.1 DRY extract).
        Returns "" when blocks is empty (safe to call unconditionally).
        """
        return render_blocks_section(blocks, directory)

    @observe(tier="stage")
    def _format_restoration(
        self,
        checkpoint: dict | None,
        anchored: list[dict],
        recent: list[dict],
        hot: list[dict],
        predicted: list[dict],
        gaps: list[dict],
        directory: str,
    ) -> str:
        """Format restoration data as injectable markdown."""
        lines: list[str] = ["# Yadgar Context Restoration (Hippocampal Replay)", ""]

        if checkpoint:
            lines.extend(self._format_checkpoint_section(checkpoint))
            lines.extend(self._format_in_flight_section(checkpoint))

        if anchored:
            lines.append("## Critical Facts (Anchored)")
            lines.extend(f"- {m.get('content', '')}" for m in anchored)
            lines.append("")

        if recent:
            lines.append("## Working Memory (Recently Stored)")
            for m in recent[:6]:
                content = self._truncate(m.get("content", ""), 250)
                created = m.get("created_at", "")[:16]
                lines.append(f"- [{created}] {content}")
            lines.append("")

        if hot:
            lines.append("## Active Project Context")
            for m in hot[:6]:
                content = self._truncate(m.get("content", ""), 200)
                heat = m.get("heat", 0)
                lines.append(f"- [{heat:.1f}] {content}")
            lines.append("")

        if predicted:
            lines.append("## Predicted Context (SR Navigation)")
            for m in predicted[:4]:
                lines.append(f"- {self._truncate(m.get('content', ''), 200)}")
            lines.append("")

        if gaps:
            lines.append("## Knowledge Gaps Detected")
            lines.extend(
                f"- **{g.get('type', 'unknown')}**: {g.get('description', '')}" for g in gaps
            )
            lines.append("")

        if directory:
            lines.append(f"*Restored for directory: {directory}*")

        return "\n".join(lines)
