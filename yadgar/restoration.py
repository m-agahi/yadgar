"""Hippocampal Replay — intelligent context restoration after compaction."""

import json
import logging
import re
from dataclasses import dataclass, field

from yadgar.cognitive_map import CognitiveMap
from yadgar.config import Settings
from yadgar.embeddings import EmbeddingEngine
from yadgar.metacognition import MetaCognition
from yadgar.retrieval import Retriever
from yadgar.storage import StorageEngine

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

    def should_auto_checkpoint(self) -> bool:
        """Check if we've hit the auto-checkpoint interval."""
        interval = self._settings.REPLAY_CHECKPOINT_AUTO_INTERVAL
        if interval <= 0:
            return False
        return self._tool_call_count > 0 and self._tool_call_count % interval == 0

    def reset_tool_count(self):
        """Reset after checkpoint."""
        self._tool_call_count = 0

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

    def create_micro_checkpoint(self, directory: str, content: str, reason: str) -> dict | None:
        """Create a lightweight checkpoint triggered by a significant event.

        These are more frequent than manual checkpoints but capture less data.
        They ensure that important state transitions aren't lost between
        full checkpoints.
        """
        summary = content[:150].replace("\n", " ")
        ctx = CheckpointContext(current_task=f"[micro:{reason}] {summary}")
        return self.create_checkpoint(directory, ctx, session_id="micro-auto")

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

        # 1. Get latest checkpoint for this directory
        checkpoint = self._storage.get_active_checkpoint(directory)

        # 2. Get anchored memories (always included, scope-split: global first then project)
        anchored = self._storage.get_anchored_memories_scoped(
            directory=directory, limit=max_memories
        )
        for m in anchored:
            m.pop("embedding", None)

        # 3. Recently stored memories (working memory — what was actively being worked on)
        # These capture incremental progress that may not be "hot" yet but represents
        # the user's active train of thought before compaction
        recent_memories = []
        try:
            recent_memories = self._storage.get_recent_memories(limit=max_memories)
            for m in recent_memories:
                m.pop("embedding", None)

        except Exception:
            logger.debug("Failed to fetch recently stored memories for restore")

        # 4. Hot project memories
        hot_memories = []
        if directory:
            hot_memories = self._storage.get_memories_for_directory(
                directory, min_heat=self._settings.HOT_THRESHOLD
            )
        else:
            hot_memories = self._storage.get_memories_by_heat(self._settings.HOT_THRESHOLD)
        for m in hot_memories:
            m.pop("embedding", None)

        # Exclude anchored and recent IDs from hot to avoid duplicates
        anchor_ids = {m["id"] for m in anchored}
        recent_ids = {m["id"] for m in recent_memories}
        hot_memories = [m for m in hot_memories if m["id"] not in anchor_ids | recent_ids]
        hot_memories = hot_memories[:max_memories]

        # 5. Predictive retrieval via SR cognitive map
        predicted = []
        if self._cognitive_map is not None and self._cognitive_map.has_sufficient_data():
            # Use checkpoint task as query for SR navigation
            query = ""
            if checkpoint:
                query = checkpoint.get("current_task", "")
            if not query and directory:
                query = f"project work in {directory}"
            if query:
                query_emb = self._embeddings.encode(query)
                if query_emb is not None:
                    sr_results = self._cognitive_map.navigate_to(
                        query_emb, self._embeddings, top_k=max_memories // 2
                    )
                    seen_ids = anchor_ids | recent_ids | {m["id"] for m in hot_memories}
                    for mid, proximity in sr_results:
                        if mid not in seen_ids:
                            mem = self._storage.get_memory(mid)
                            if mem:
                                mem.pop("embedding", None)
                                mem["_sr_proximity"] = round(proximity, 4)
                                predicted.append(mem)
                                seen_ids.add(mid)

        # 6. Gap detection
        gaps = []
        if self._metacognition is not None and directory:
            try:
                gaps = self._metacognition.detect_gaps(directory)[:3]
            except Exception:
                logger.debug("Gap detection failed during restore")

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
        # Fetched + rendered via helpers; no new branches in restore() to respect
        # baseline ratchet (cyclo=24 — every additional branch triggers HARD violation).
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

    @staticmethod
    def _parse_list_field(value) -> list:
        """Parse a checkpoint list field — already a list or a JSON string."""
        if isinstance(value, str):
            return json.loads(value)
        return value or []

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

    def _fetch_blocks_safe(self, directory: str) -> list[dict]:
        """Fetch memory blocks, swallowing errors (v5.33.0). Returns [] on failure."""
        try:
            return self._storage.list_blocks(scope=None, directory=directory if directory else None)
        except Exception:
            logger.debug("Failed to fetch memory blocks for restore")
            return []

    def _render_blocks_section(self, blocks: list[dict], directory: str) -> str:
        """Render memory blocks as markdown section for restore() injection (v5.33.0).

        Returns "" when blocks is empty (safe to call unconditionally).
        """
        if not blocks:
            return ""
        lines: list[str] = [
            "## Memory Blocks (always-injected, editable via block_* MCP tools)",
            "",
        ]
        global_blocks = [b for b in blocks if b.get("scope") == "global"]
        project_blocks = [b for b in blocks if b.get("scope") == "project"]
        if global_blocks:
            lines.append("### Global blocks")
            for b in global_blocks:
                content = b.get("content", "")
                name = b.get("name", "")
                lines.append(f"- `{name}`: {content}" if content else f"- `{name}`: *(empty)*")
            lines.append("")
        if project_blocks:
            dir_label = directory or "project"
            lines.append(f"### Project blocks ({dir_label})")
            for b in project_blocks:
                content = b.get("content", "")
                name = b.get("name", "")
                lines.append(f"- `{name}`: {content}" if content else f"- `{name}`: *(empty)*")
            lines.append("")
        return "\n".join(lines)

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
