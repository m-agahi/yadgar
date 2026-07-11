"""Host-FS half of staleness detection (T2 Car E1 split).

The ``StalenessDetector`` owns everything that needs the HOST filesystem:
watchdog event wiring, file hashing, and directory walks. The
stateless-over-DB flag compute — "given a changed file, find affected
memories, halve heat, mark stale, upsert the file hash" — was relocated to
the backend (``yadgar.backend.admin_exec.staleness``, census verdict #8,
ADR-0078); this side forwards via ``_forward_admin``.

``validate_memory`` keeps its storage READS core-side (reads are a post-T2
follow-up) plus the host file hash, and forwards only the flag WRITE.
"""

import hashlib
import logging
import os
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)

IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
IGNORE_EXTENSIONS = {".pyc", ".pyo", ".so", ".o", ".dylib"}


def _forward(op: str, payload: dict) -> dict:
    """Forward one staleness flag-compute op to the backend /admin endpoint.

    Call-time import so the test harness's ``_forward_admin`` bypass patch on
    the ``_forward`` module is picked up.
    """
    from yadgar.core.server.tools._forward import _forward_admin  # noqa: PLC0415

    return _forward_admin(op, payload)


class _FileChangeHandler(FileSystemEventHandler):
    def __init__(self, detector: StalenessDetector):
        super().__init__()
        self._detector = detector

    @observe(tier="hot")
    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        for part in parts:
            if part in IGNORE_DIRS:
                return True
        if any(path.endswith(ext) for ext in IGNORE_EXTENSIONS):
            return True
        return False

    @observe(tier="hot")
    def _handle_event(self, event) -> None:
        """Single handler for both on_modified and on_created (T-0017-staleness)."""
        if event.is_directory or self._should_ignore(event.src_path):
            return
        self._detector._handle_file_change(event.src_path)

    def on_modified(self, event):
        self._handle_event(event)

    def on_created(self, event):
        self._handle_event(event)

    @observe(tier="hot")
    def on_deleted(self, event):
        if event.is_directory or self._should_ignore(event.src_path):
            return
        self._detector._handle_file_change(event.src_path)


class StalenessDetector:
    def __init__(self, storage: StorageEngine, settings: Settings):
        # Storage is kept for READS only (validate_memory's memory/filepath
        # lookups) — every WRITE forwards to the backend flag-compute ops.
        self._storage = storage
        self._settings = settings
        self._observer: Observer | None = None
        self._watched_dirs: set[str] = set()
        self.is_running: bool = False

    @observe(tier="boundary")
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

    @observe(tier="boundary")
    def stop(self):
        if self._observer is not None and self.is_running:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            self.is_running = False
            self._watched_dirs.clear()

    @observe(tier="stage")
    def _handle_file_change(self, filepath: str):
        """Hash the file host-side, forward the flag compute to the backend."""
        new_hash = self._compute_file_hash(filepath)
        try:
            _forward("staleness_file_changed", {"filepath": filepath, "new_hash": new_hash})
        except Exception as e:  # noqa: BLE001 — watchdog thread must never die on a flake
            logger.warning("staleness forward failed for %s: %s", filepath, e)

    @observe(tier="stage")
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
            _forward("staleness_flag_memory", {"memory_id": memory_id})
            return {"valid": False, "reason": "file changed"}

        return {"valid": True, "reason": "file unchanged"}

    @observe(tier="boundary")
    def scan_directory(self, directory: str) -> dict:
        """Walk + hash host-side, forward ONE batched flag-compute op."""
        files: list[dict] = []

        for root, dirs, filenames in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]

            for filename in filenames:
                if any(filename.endswith(ext) for ext in IGNORE_EXTENSIONS):
                    continue

                filepath = os.path.join(root, filename)

                if self._is_binary(filepath):
                    continue

                files.append({"path": filepath, "hash": self._compute_file_hash(filepath)})

        result = _forward("staleness_scan", {"files": files})
        return {
            "files_scanned": len(files),
            "files_changed": int(result.get("files_changed", 0)),
            "memories_flagged": int(result.get("memories_flagged", 0)),
        }

    @observe(tier="stage")
    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        try:
            with open(filepath, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except FileNotFoundError:
            return ""

    @observe(tier="hot")
    @staticmethod
    def _is_binary(filepath: str) -> bool:
        try:
            with open(filepath, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except OSError:
            return True
