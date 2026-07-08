"""Shared utility functions used across server submodules.

Imports from _state only — no imports from other yadgar.server.* modules.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

settings = get_settings()


# R3 Car 3d: _q_with_timeout moved to yadgar._shared.server_helpers so the
# backend invariants exec imports only _shared. Re-exported here so existing
# callers (admin_invariants, admin_other, server.__init__, tests) are unchanged.
# Car 3 (folder-split #17): _bounded_set + _is_episodic_query moved to
# yadgar._shared.runtime.recall_utils so the backend recall pipeline
# (_shared.runtime.recall_pipeline) no longer imports yadgar.server._helpers
# (a _shared→server edge). Re-exported here so existing callers (http.py,
# recall.py, server.__init__, tests) are unchanged.
from yadgar._shared.runtime.recall_utils import (  # noqa: E402,F401
    _bounded_set,
    _is_episodic_query,
)
from yadgar._shared.server_helpers import _q_with_timeout  # noqa: E402,F401


@observe(tier="stage")
def _build_dlq_alert_text() -> str:
    """Return a markdown warning string if any items are in the DLQ, else ''."""
    try:
        # Prefer the live FileQueue's dlq_dir when one exists — it is the
        # directory the drainer actually moves failed jobs into. Falling back
        # to the env-derived path keeps the no-queue-yet case cheap (no queue
        # construction just to render an alert). R3: the queue may be built
        # earlier in the process lifetime (backend drainer wiring) under a
        # different YADGAR_DATA_DIR than the current env value — deriving from
        # env alone can point at a directory the queue never writes to.
        import yadgar._shared.runtime.state as _st  # noqa: PLC0415

        fq = _st._file_queue
        if fq is not None:
            dlq_dir = fq.dlq_dir
        else:
            data_dir = Path(os.environ.get("YADGAR_DATA_DIR", settings.DATA_DIR))
            dlq_dir = data_dir / "dlq"
        if not dlq_dir.exists():
            return ""
        alerts = []
        for sidecar in sorted(dlq_dir.glob("*.json.error.json")):
            try:
                meta = json.loads(sidecar.read_text())
                meta["_file"] = sidecar.name[: -len(".error.json")]
                alerts.append(meta)
            except Exception:
                logger.warning("DLQ alert: failed to parse sidecar %s", sidecar, exc_info=True)
        if not alerts:
            return ""
        lines = [f"# Yadgar DLQ Alert — {len(alerts)} item(s) stuck\n"]
        lines.append("These writes failed permanently and will not be retried automatically.")
        lines.append(
            "Run `dlq_inspect()` for details, `dlq_requeue(filename)` after fixing root cause.\n"
        )
        for a in alerts[:5]:
            lines.append(
                f"- {a.get('op_type', '?')}  attempts={a.get('attempts')}  "
                f"moved={a.get('moved_to_dlq_at', '')[:19]}  "
                f"error={str(a.get('last_error', ''))[:80]}"
            )
        if len(alerts) > 5:
            lines.append(f"  ... and {len(alerts) - 5} more")
        return "\n".join(lines)
    except Exception:
        return ""
