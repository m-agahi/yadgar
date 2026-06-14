import hashlib
import logging
import os
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from yadgar.config import Settings
from yadgar.storage import StorageEngine

logger = logging.getLogger(__name__)

IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
IGNORE_EXTENSIONS = {".pyc", ".pyo", ".so", ".o", ".dylib"}


class _FileChangeHandler(FileSystemEventHandler):
    def __init__(self, detector: StalenessDetector):
        super().__init__()
        self._detector = detector

    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        for part in parts:
            if part in IGNORE_DIRS:
                return True
        if any(path.endswith(ext) for ext in IGNORE_EXTENSIONS):
            return True
        return False

    def _handle_event(self, event) -> None:
        """Single handler for both on_modified and on_created (T-0017-staleness)."""
        if event.is_directory or self._should_ignore(event.src_path):
            return
        self._detector._handle_file_change(event.src_path)

    def on_modified(self, event):
        self._handle_event(event)

    def on_created(self, event):
        self._handle_event(event)

    def on_deleted(self, event):
        if event.is_directory or self._should_ignore(event.src_path):
            return
        self._detector._handle_file_change(event.src_path)


class StalenessDetector:
    def __init__(self, storage: StorageEngine, settings: Settings):
        self._storage = storage
        self._settings = settings
        self._observer: Observer | None = None
        self._watched_dirs: set[str] = set()
        self.is_running: bool = False

    def start(self, directory: str):
        if self._observer is None:
            self._observer = Observer()

        abs_dir = str(Path(directory).resolve())
        if abs_dir not in self._watched_dirs:
            handler = _FileChangeHandler(self)
            self._observer.schedule(handler, abs_dir, recursive=True)
            self._watched_dirs.add(abs_dir)

        if not self.is_running:
            self._observer.start()
            self.is_running = True

    def stop(self):
        if self._observer is not None and self.is_running:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            self.is_running = False
            self._watched_dirs.clear()

    def _handle_file_change(self, filepath: str):
        new_hash = self._compute_file_hash(filepath)
        old_hash = self._storage.get_file_hash(filepath)

        if old_hash is not None and old_hash != new_hash:
            self._flag_memories_for_file(filepath, old_hash)

        self._storage.upsert_file_hash(filepath, new_hash)

    def _flag_memories_for_file(self, filepath: str, old_hash: str):
        memories = self._storage.get_memories_by_file_hash(old_hash)

        parent_dir = str(Path(filepath).parent)
        dir_memories = self._storage.get_memories_for_directory(parent_dir, min_heat=0.0)

        seen_ids: set[int] = set()
        all_memories = []
        for m in memories + dir_memories:
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                all_memories.append(m)

        for memory in all_memories:
            new_heat = memory["heat"] / 2.0
            self._storage.update_memory_heat(memory["id"], new_heat)
            self._storage.update_memory_staleness(memory["id"], True)

    def validate_memory(self, memory_id: int) -> dict:
        memory = self._storage.get_memory(memory_id)
        if memory is None:
            return {"valid": False, "reason": "memory not found"}

        file_hash = memory.get("file_hash")
        if not file_hash:
            return {"valid": True, "reason": "no file reference"}

        filepath = self._storage.get_filepath_by_hash(file_hash)
        if filepath is None:
            return {"valid": True, "reason": "no file reference"}

        current_hash = self._compute_file_hash(filepath)
        if current_hash != file_hash:
            new_heat = memory["heat"] / 2.0
            self._storage.update_memory_heat(memory_id, new_heat)
            self._storage.update_memory_staleness(memory_id, True)
            return {"valid": False, "reason": "file changed"}

        return {"valid": True, "reason": "file unchanged"}

    def _flag_scan_memories(
        self, filepath: str, old_hash: str, flagged_memory_ids: set[int]
    ) -> None:
        """Flag memories for a changed file during a directory scan.

        Deduplication is global across the full scan walk via ``flagged_memory_ids``,
        which the caller owns and passes by reference.  This differs from
        ``_flag_memories_for_file``, whose dedup scope is per-call only.
        """
        memories = self._storage.get_memories_by_file_hash(old_hash)
        parent_dir = str(Path(filepath).parent)
        dir_memories = self._storage.get_memories_for_directory(parent_dir, min_heat=0.0)

        for m in memories + dir_memories:
            if m["id"] not in flagged_memory_ids:
                flagged_memory_ids.add(m["id"])
                self._storage.update_memory_heat(m["id"], m["heat"] / 2.0)
                self._storage.update_memory_staleness(m["id"], True)

    def scan_directory(self, directory: str) -> dict:
        files_scanned = 0
        files_changed = 0
        flagged_memory_ids: set[int] = set()

        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]

            for filename in files:
                if any(filename.endswith(ext) for ext in IGNORE_EXTENSIONS):
                    continue

                filepath = os.path.join(root, filename)

                if self._is_binary(filepath):
                    continue

                files_scanned += 1
                new_hash = self._compute_file_hash(filepath)
                old_hash = self._storage.get_file_hash(filepath)

                if old_hash is not None and old_hash != new_hash:
                    files_changed += 1
                    self._flag_scan_memories(filepath, old_hash, flagged_memory_ids)

                self._storage.upsert_file_hash(filepath, new_hash)

        return {
            "files_scanned": files_scanned,
            "files_changed": files_changed,
            "memories_flagged": len(flagged_memory_ids),
        }

    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        try:
            with open(filepath, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except FileNotFoundError:
            return ""

    @staticmethod
    def _is_binary(filepath: str) -> bool:
        try:
            with open(filepath, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except OSError:
            return True
