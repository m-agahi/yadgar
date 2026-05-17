"""Shared thread-local state for file_queue subpackage."""

from __future__ import annotations

import threading

# Thread-local flag: True while QueueDrainer._apply() is executing.
# Write tools check this to skip re-enqueueing during crash-recovery replay.
_drain_local = threading.local()
