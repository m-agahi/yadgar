"""Hippocampal Replay — intelligent context restoration after compaction."""

import json
import logging
import re
from dataclasses import dataclass, field

from yadgar._shared.blocks_render import render_blocks_section
from yadgar._shared.cognitive_map import CognitiveMap
from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.metacognition import MetaCognition
from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval import Retriever
from yadgar._shared.storage import StorageEngine
from yadgar._shared.tracing import trace_span

logger = logging.getLogger(__name__)

# Patterns that trigger micro-checkpoints
_MICRO_ERROR_RE = re.compile(r"\b(error|exception|traceback|failed|crash|bug)\b", re.IGNORECASE)
_MICRO_DECISION_RE = re.compile(
    r"\b(decided|chose|switched|migrated|will use|going with|opted)\b", re.IGNORECASE
)


@dataclass
class CheckpointContext:
    """Optional context fields for create_checkpoint.

    Bundles the 7 optional checkpoint payload params so the method signature
    stays within the I13 PLR0913 cap (≤8 non-self args).

    resume_hint: if provided, stored verbatim; otherwise derived as
        restore(directory="<directory>").
    """

    current_task: str = ""
    files_being_edited: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    active_errors: list[str] = field(default_factory=list)
    custom_context: str = ""
    resume_hint: str = ""


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

    @trace_span("checkpoint.create")
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
    def anchor_memory(
        self,
        content: str,
        context: str,
        tags: list[str],
        reason: str = "",
        branch: str | None = None,
        tier: str | None = None,
        valid_until: str | None = None,
    ) -> int:
        """Store a memory with maximum protection — survives compaction restoration.

        Anchored memories get heat=1.0, is_protected=True, importance=1.0.
        They are ALWAYS included in restoration regardless of other scoring.

        branch: auto-captured at write time via _detect_branch; None for non-git contexts.
        tier: v5.8.0 — anchor tier string ("semantic_immortal"|"conditional"|"ephemeral").
        valid_until: v5.8.0 — ISO-8601 UTC expiry string; None = no expiry.
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

    @trace_span("checkpoint.micro")
    def create_micro_checkpoint(self, directory: str, content: str, reason: str) -> dict | None:
        """Create a lightweight checkpoint triggered by a significant event.

        These are more frequent than manual checkpoints but capture less data.
        They ensure that important state transitions aren't lost between
        full checkpoints.
        """
        summary = content[:150].replace("\n", " ")
        ctx = CheckpointContext(current_task=f"[micro:{reason}] {summary}")
        return self.create_checkpoint(directory, ctx, session_id="micro-auto")

    @trace_span("checkpoint.pre_compact_drain")
    def pre_compact_drain(self, directory: str) -> dict:
        """Emergency context capture before compaction.

        Called by PreCompact hook. Triggers:
        1. Auto-checkpoint from sensory buffer
        2. Epoch increment (marks compaction boundary)
        3. Emergency consolidation
        """
        new_epoch = self._storage.increment_epoch()

        # Create an auto-checkpoint if no recent one exists (per-directory)
        active = self._storage.get_active_checkpoint(directory)
        auto_created = False
        if active is None or active.get("epoch", 0) < new_epoch - 1:
            self._storage.insert_checkpoint(
                {
                    "session_id": "auto-drain",
                    "directory_context": directory,
                    "current_task": "[auto-captured before compaction]",
                    "epoch": new_epoch,
                }
            )
            auto_created = True
        else:
            # Update existing checkpoint with new epoch
            self._storage.update_checkpoint_epoch(active["id"], new_epoch)

        return {
            "status": "drained",
            "epoch": new_epoch,
            "auto_checkpoint_created": auto_created,
        }

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
    def _build_sr_query(self, checkpoint: dict | None, directory: str) -> str:
        """Derive SR navigation query from checkpoint task or directory (step 5 of restore)."""
        if checkpoint:
            task = checkpoint.get("current_task", "")
            if task:
                return task
        return f"project work in {directory}" if directory else ""

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

    @trace_span("restore.run")
    def restore(self, directory: str = "") -> dict:
        """Intelligent context reconstruction after compaction.

        Combines:
        1. Latest checkpoint (what you were doing)
        2. Anchored memories (critical facts, always included)
        3. Hot project memories (thermodynamic ranking)
        4. Predictive retrieval via SR (what you'll likely need next)
        5. Gap detection (what might have been lost)

        Returns structured data + formatted markdown for injection.
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

    def _prepend_blocks(self, blocks: list[dict], directory: str, markdown: str) -> str:
        """Prepend memory blocks section to markdown if blocks exist (v5.33.0)."""
        section = self._render_blocks_section(blocks, directory)
        return (section + "\n" + markdown) if section else markdown

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
