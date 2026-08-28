"""Host-FS half of staleness detection (T2 Car E1 split).

The ``StalenessDetector`` owns what needs the HOST filesystem: file hashing.
The stateless-over-DB flag compute — "given a changed file, find affected
memories, halve heat, mark stale, upsert the file hash" — was relocated to
the backend (``yadgar.backend.admin_exec.staleness``, census verdict #8,
ADR-0078); this side forwards via ``_forward_admin``.

``validate_memory`` keeps its storage READS core-side (reads are a post-T2
follow-up) plus the host file hash, and forwards only the flag WRITE.

Car K (2026-08-28, ADR-0464): the watchdog arm is GONE. ``start`` /
``stop`` / ``_FileChangeHandler`` / ``scan_directory`` and the
``watch_directory`` parameter that reached them were unreachable — the only
production caller of the chain, ``core/server/_startup.py``, passed a
literal ``None``, so the observer was never scheduled and ``scan_directory``
had no non-test caller. The ``staleness_file_changed`` / ``staleness_scan``
backend ops those arms fed are now unreferenced from core.
"""

import hashlib
import logging

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)


def _forward(op: str, payload: dict) -> dict:
    """Forward one staleness flag-compute op to the backend /admin endpoint.

    Call-time import so the test harness's ``_forward_admin`` bypass patch on
    the ``_forward`` module is picked up.
    """
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

    return _forward_admin(op, payload)


class StalenessDetector:
    def __init__(self, storage: StorageEngine, settings: Settings):
        # Storage is kept for READS only (validate_memory's memory/filepath
        # lookups) — every WRITE forwards to the backend flag-compute ops.
        self._storage = storage
        self._settings = settings

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

    @observe(tier="stage")
    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        try:
            with open(filepath, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except FileNotFoundError:
            return ""
