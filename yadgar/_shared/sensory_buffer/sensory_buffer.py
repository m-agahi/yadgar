import uuid
from collections import deque
from datetime import UTC, datetime

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine


class ActionLogger:
    def __init__(self, storage: StorageEngine, settings: Settings):
        self._storage = storage
        self._settings = settings
        self._max_chars = settings.MAX_EPISODE_TOKENS * 4  # token ≈ 4 chars
        self._overlap_chars = settings.OVERLAP_TOKENS * 4
        self.session_id: str | None = None
        self.current_episode: dict | None = None
        # Action stream: lightweight log of all tool invocations for pattern extraction
        self._action_stream: deque[dict] = deque(maxlen=200)

    def start_session(self) -> str:
        self.session_id = uuid.uuid4().hex
        self.current_episode = {
            "session_id": self.session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "directory": "",
            "project_id": None,
            "raw_content": "",
            "overlap_start": None,
            "overlap_end": None,
        }
        return self.session_id

    @observe(tier="stage")
    def capture(self, content: str, directory: str, project_id: str | None = None) -> None:
        """Append *content* to the open episode, tagging it with the caller's scope.

        C11 (0047 PR#40 §5): the buffer carries ``project_id`` alongside
        ``directory`` because migration 033 gave the ``episode`` table a column
        for it. Both travel: ``causal_discovery/pc.py`` and
        ``consolidation/cls.py`` still read the ``directory`` column, and the
        legacy value is what a later backfill would derive from.

        ``project_id`` is only OVERWRITTEN by a caller that names one — a
        subsequent unstamped ``capture`` into the same open episode must not
        erase an identity an earlier one supplied, or the episode's scope would
        depend on which write happened to be last.
        """
        episode = self._ensure_episode()
        episode["directory"] = directory
        if project_id:
            episode["project_id"] = project_id
        episode["raw_content"] += content
        if len(episode["raw_content"]) > self._max_chars:
            self._rotate_episode()

    @observe(tier="hot")
    def _ensure_episode(self) -> dict:
        """Return the open episode, starting a session if there is none.

        ``current_episode`` is ``dict | None``, so every indexed access through
        the attribute is an unnarrowable ``Optional`` for the type checker.
        Returning a non-optional local is the fix rather than repeating the
        guard at each subscript — same shape as ``_rotate_episode``'s §13 check,
        which raises rather than proceeding on a state that cannot occur.
        """
        if self.current_episode is None:
            self.start_session()
        episode = self.current_episode
        if episode is None:
            raise RuntimeError("ActionLogger: start_session did not open an episode")
        return episode

    @observe(tier="stage")
    def flush(self) -> int | None:
        if self.current_episode is None or not self.current_episode["raw_content"]:
            return None
        ep_id = self._storage.insert_episode(self.current_episode)
        self.current_episode = {
            "session_id": self.session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "directory": self.current_episode["directory"],
            # C11: the scope carries across the rotation, exactly as the
            # directory always has — a flushed episode does not end the session.
            "project_id": self.current_episode.get("project_id"),
            "raw_content": "",
            "overlap_start": None,
            "overlap_end": None,
        }
        return ep_id

    def get_current_episode(self) -> dict | None:
        return self.current_episode

    def get_session_episodes(self, session_id: str) -> list[dict]:
        return self._storage.get_session_episodes(session_id)

    @observe(tier="stage")
    def capture_action(
        self,
        tool: str,
        directory: str,
        summary: str,
        result_type: str,
        project_id: str | None = None,
    ) -> None:
        """Record a tool invocation in the action stream.

        Action stream entries are lightweight structured records that capture
        what happened during the session. They feed into the sensory buffer
        as formatted text and can be used by consolidation to extract patterns
        like 'user tends to recall X before editing Y'.
        """
        if not self._settings.ACTION_STREAM_ENABLED:
            return

        action = {
            "tool": tool,
            "directory": directory,
            "project_id": project_id,
            "summary": summary[:200],
            "result_type": result_type,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._action_stream.append(action)

        # Also inject into the sensory buffer as structured text
        action_text = f"[ACTION:{tool}] {result_type}: {summary[:150]}"
        self.capture(action_text, directory, project_id=project_id)

    def get_recent_actions(self, n: int = 20) -> list[dict]:
        """Return the last N action stream entries."""
        return list(self._action_stream)[-n:]

    @observe(tier="hot")
    def get_action_summary(self) -> str:
        """Generate a summary of recent actions for checkpoint context."""
        if not self._action_stream:
            return ""

        recent = list(self._action_stream)[-10:]
        lines = []
        for a in recent:
            lines.append(f"- {a['tool']}: {a['summary'][:80]}")
        return "Recent actions:\n" + "\n".join(lines)

    @observe(tier="stage")
    def _rotate_episode(self) -> None:
        # §13: current_episode must be set before _rotate_episode is called
        if self.current_episode is None:
            raise RuntimeError("ActionLogger: _rotate_episode called with current_episode=None")
        old_content = self.current_episode["raw_content"]
        old_directory = self.current_episode["directory"]
        old_project_id = self.current_episode.get("project_id")

        # Save old episode
        self._storage.insert_episode(self.current_episode)

        # Extract overlap from the end of old content
        overlap = old_content[-self._overlap_chars :]
        overlap_start = len(old_content) - len(overlap)
        overlap_end = len(old_content)

        # Start new episode with overlap as seed
        self.current_episode = {
            "session_id": self.session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "directory": old_directory,
            "project_id": old_project_id,
            "raw_content": overlap,
            "overlap_start": overlap_start,
            "overlap_end": overlap_end,
        }
