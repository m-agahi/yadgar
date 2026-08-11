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
    ) -> dict:
        """Create a working state checkpoint for post-compaction recovery.

        ctx bundles the optional payload fields: current_task,
        files_being_edited, key_decisions, open_questions, next_steps,
        active_errors, custom_context.
        """
        c = ctx or CheckpointContext()
        epoch = self._storage.get_current_epoch()
        checkpoint_id = self._storage.insert_checkpoint(
            {
                "session_id": session_id,
                "directory_context": directory,
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
        branch: str | None = None,
        tier: str | None = None,
        valid_until: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Store a memory with maximum protection — survives compaction restoration.

        Anchored memories get heat=1.0, is_protected=True, importance=1.0.
        They are ALWAYS included in restoration regardless of other scoring.

        tier: v5.8.0 — anchor tier string ("semantic_immortal"|"conditional"|"ephemeral").
        valid_until: v5.8.0 — ISO-8601 UTC expiry string; None = no expiry.
        project_id: C4b (0047 PR#40 §5) — the enqueue-time stamp threaded from
            the core ``anchor`` tool via ``run_anchor_replay`` (its only
            non-test caller). Reaches ``insert_memory`` as
            ``_resolve_project_id_for_write``'s ``caller_value``, so a stamped
            anchor never touches the classifier this container cannot run
            (ADR-0227 §1.1). Stamped independently of ``context``: ownership
            and reach are different facts (§1.4).

        NOTE — the ``checkpoint`` table is deliberately NOT part of this. It
        has no ``project_id`` column (see ``insert_checkpoint`` in
        ``_shared/storage/ops.py``: the CREATE statement sets none), so
        ``create_checkpoint`` / ``create_micro_checkpoint`` /
        ``pre_compact_drain`` have nothing to stamp. Adding the column is
        C11's per-table work, not this car's.
        """
        embedding = self._embeddings.encode(content)
        memory_payload: dict = {
            "content": content,
            "embedding": embedding,
            "tags": tags + ["_anchor"],
            "directory_context": context,
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
        memory_id = self._storage.insert_memory(memory_payload, branch=branch)
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
    def create_micro_checkpoint(self, directory: str, content: str, reason: str) -> dict | None:
        """Create a lightweight checkpoint triggered by a significant event.

        These are more frequent than manual checkpoints but capture less data.
        They ensure that important state transitions aren't lost between
        full checkpoints.
        """
        summary = content[:150].replace("\n", " ")
        ctx = CheckpointContext(current_task=f"[micro:{reason}] {summary}")
        return self.create_checkpoint(directory, ctx, session_id="micro-auto")

    @trace_span()
    def pre_compact_drain(
        self,
        directory: str,
        transcript_path: str | None = None,
        in_flight: dict | None = None,
        worktree_path: str | None = None,
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

        The identity half is deliberately STILL named ``directory``. Every sink
        it reaches keys on a directory-valued column that has no ``project_id``
        yet — ``checkpoint.directory_context`` (no ``project_id`` column; see the
        note on ``anchor_memory``) and ``memory.directory_context``. Renaming the
        parameter without re-keying those reads would make callers pass
        ``owner/repo`` into ``WHERE directory_context = $dir`` and match zero
        rows without raising. That re-key is C11's per-table work; the seams are
        marked at each call site below.
        """
        new_epoch = self._storage.increment_epoch()

        if in_flight is None:
            in_flight = self._capture_in_flight(transcript_path, worktree_path or directory)

        # Create an auto-checkpoint if no recent one exists (per-directory)
        active = self._storage.get_active_checkpoint(directory)
        auto_created = False
        if active is None or active.get("epoch", 0) < new_epoch - 1:
            checkpoint_data = {
                "session_id": "auto-drain",
                "directory_context": directory,
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
        except Exception:
            logger.debug("pre_compact_drain in-flight capture failed", exc_info=True)
            return None

    @observe(tier="stage")
    def _fetch_recent_memories_safe(self, max_memories: int) -> list[dict]:
        """Fetch recently stored memories, suppressing errors (step 3 of restore).

        Returns [] on any storage failure so restore() stays unblocked.
        """
        try:
            memories = self._storage.get_recent_memories(limit=max_memories)
            for m in memories:
                m.pop("embedding", None)
            return memories
        except Exception:
            logger.debug("Failed to fetch recently stored memories for restore")
            return []

    @observe(tier="stage")
    def _fetch_hot_memories(
        self,
        directory: str,
        exclude_ids: set[int],
        max_memories: int,
    ) -> list[dict]:
        """Fetch hot project memories, deduplicated against exclude_ids (step 4 of restore)."""
        if directory:
            hot = self._storage.get_memories_for_directory(
                directory, min_heat=self._settings.HOT_THRESHOLD
            )
        else:
            hot = self._storage.get_memories_by_heat(self._settings.HOT_THRESHOLD)
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

        Caveat for the next reader: until C11 re-keys ``restore()`` itself, the
        caller still supplies the directory-valued scope key, so the string may
        read ``project work in /home/max/git/yadgar`` rather than
        ``project work in m-agahi/yadgar``. That is a slightly-off embedding
        query, not a wrong answer, and it corrects itself for free when C11
        lands.
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
        directory: str,
        seen_ids: set[int],
        max_memories: int,
    ) -> list[dict]:
        """Run SR cognitive-map navigation to predict needed memories (step 5 of restore).

        Returns [] when cognitive map is absent or has insufficient data.
        """
        if self._cognitive_map is None or not self._cognitive_map.has_sufficient_data():
            return []
        query = self._build_sr_query(checkpoint, directory)
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
            if mem:
                mem.pop("embedding", None)
                mem["_sr_proximity"] = round(proximity, 4)
                predicted.append(mem)
                local_seen.add(mid)
        return predicted

    @observe(tier="stage")
    def _detect_gaps_safe(self, directory: str) -> list[dict]:
        """Detect knowledge gaps, suppressing errors (step 6 of restore).

        Returns at most 3 gaps. Returns [] when metacognition is absent or on error.
        """
        if self._metacognition is None or not directory:
            return []
        try:
            return self._metacognition.detect_gaps(directory)[:3]
        except Exception:
            logger.debug("Gap detection failed during restore")
            return []

    @trace_span()
    def restore(self, directory: str = "") -> dict:
        """Intelligent context reconstruction after compaction.

        Combines:
        1. Latest checkpoint (what you were doing)
        2. Anchored memories (critical facts, always included)
        3. Hot project memories (thermodynamic ranking)
        4. Predictive retrieval via SR (what you'll likely need next)
        5. Gap detection (what might have been lost)

        Returns structured data + formatted markdown for injection.

        C10 (0047 §5, judgement site (b)) — C11 WORKLIST. ``directory`` here is
        the **identity** half of the parameter (b) split; the real-path half is
        gone (``worktree_path``, drain side only). It is deliberately NOT
        renamed to ``project_id``, because it fans out to FIVE sinks and every
        one still keys on a directory-valued column:

          1. ``get_active_checkpoint``      → ``checkpoint.directory_context``
             — table has **no ``project_id`` column** (migration 031 declared it
             on ``wiki_page`` + ``memory`` only). Flips symmetrically with the
             drain writer.
          2. ``get_anchored_memories_scoped`` → ``memory.directory_context``
             — ``memory`` HAS ``project_id``; the READ is not re-keyed yet.
          3. ``get_memories_for_directory``   → ``memory.directory_context``
             — same; ``WHERE directory_context = $dir``.
          4. ``list_blocks``                  → ``memory_block.directory``
             — **no ``project_id`` column, and does NOT flip symmetrically.**
             See the seam note on ``_fetch_blocks_safe``; this is the sink that
             forced the deferral.
          5. ``detect_gaps``                  → metacognition, directory-keyed.

        Renaming the parameter without re-keying these reads would have callers
        pass ``owner/repo`` into ``WHERE directory_context = $dir`` and match
        zero rows while raising nothing. C11 re-keys the columns; sinks 2/3/5
        are also in C9c's path.
        """
        max_memories = self._settings.REPLAY_MAX_RESTORE_MEMORIES

        # 1. Latest checkpoint
        checkpoint = self._storage.get_active_checkpoint(directory)

        # 2. Anchored memories (scope-split: global first then project)
        anchored = self._storage.get_anchored_memories_scoped(
            directory=directory, limit=max_memories
        )
        for m in anchored:
            m.pop("embedding", None)

        # 3. Recently stored memories (working memory)
        recent_memories = self._fetch_recent_memories_safe(max_memories)

        # 4. Hot project memories (deduplicated)
        anchor_ids = {m["id"] for m in anchored}
        recent_ids = {m["id"] for m in recent_memories}
        hot_memories = self._fetch_hot_memories(directory, anchor_ids | recent_ids, max_memories)

        # 5. Predictive retrieval via SR cognitive map
        seen_ids = anchor_ids | recent_ids | {m["id"] for m in hot_memories}
        predicted = self._predict_memories(checkpoint, directory, seen_ids, max_memories)

        # 6. Gap detection
        gaps = self._detect_gaps_safe(directory)

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
        blocks = self._fetch_blocks_safe(directory)
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

    # ── C11 SEAM (0047 §5, judgement site (b)) — the sink that forced the split ──
    #
    # ``list_blocks`` filters ``memory_block`` on ``WHERE directory = $directory``
    # (``_shared/storage/blocks.py``), an EXACT match. ``memory_block`` has **no
    # ``project_id`` column** — migration 031 declared ``project_id`` on
    # ``wiki_page`` and ``memory`` only.
    #
    # This is why ``restore()``'s identity parameter is still named ``directory``
    # and why C10 did NOT rename it. The other storage sinks flip SYMMETRICALLY:
    # the drain writes ``checkpoint.directory_context`` and restore reads it back,
    # so if both move to project_id together they still match. **Blocks do not.**
    # ``memory_block`` rows are written by ``block_create(..., directory=...)``,
    # one of the 18 ``accept_project_param`` sites correctly left C11-blocked
    # because its table has no project_id column. Flip restore's value while the
    # write side stays on real paths and every block silently vanishes from every
    # restore — zero rows, no exception.
    #
    # C11 fixes this by adding the column and re-keying BOTH sides together.
    @observe(tier="stage")
    def _fetch_blocks_safe(self, directory: str) -> list[dict]:
        """Fetch memory blocks, swallowing errors (v5.33.0). Returns [] on failure."""
        try:
            return self._storage.list_blocks(scope=None, directory=directory if directory else None)
        except Exception:
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
