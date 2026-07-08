"""Shared file-queue primitive (enqueue + file-ops). Used by core (enqueue) + backend drainer."""

from yadgar._shared.file_queue.queue import FileQueue

__all__ = ["FileQueue"]
