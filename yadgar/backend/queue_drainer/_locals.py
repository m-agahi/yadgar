"""Thread-local flag used by the queue drainer's replay path.

# Why this exists
``_apply()`` (yadgar/backend/queue_drainer/apply.py) sets this to True while
replaying a queued write. The original design (v5.10.x) called for write
tools to consult the flag and skip re-enqueueing — preventing exponential
queue growth if the replay pipeline ever triggers another write that
would normally enqueue.

# Current status (ledger task #342)
No production write tool inside the replay pipeline (``memorize_impl.py``,
``wiki_add_impl.py``, etc.) re-enqueues today — the pipeline runs
``validate → embed → contradiction → store → post_write`` end-to-end
without touching the file queue. The flag is therefore DEFENSIVE: a
no-op when no reader exists, but already wired correctly so any future
write path added to the replay pipeline only needs a single
``if not is_draining(): _get_file_queue().enqueue(...)`` guard.

# Reader contract
If you add a new write path inside ``QueueDrainer._apply_inner`` or any
helper it transitively calls, consult ``is_draining()`` before
enqueueing. ``apply.py`` resets the flag in a ``finally`` block, so a
crash mid-replay also leaves the flag correctly cleared.
"""

from __future__ import annotations

import threading

_drain_local = threading.local()
