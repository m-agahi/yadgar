"""DLQ (dead-letter queue) MCP tools: dlq_inspect and dlq_requeue."""

from __future__ import annotations

import logging

import yadgar.server._state as _st
from yadgar.server._app import _tool
from yadgar.server.lifecycle import _get_file_queue

logger = logging.getLogger(__name__)


@_tool()
def dlq_inspect() -> list[dict]:
    """List items stuck in the dead-letter queue (failed writes that exhausted retries).

    Returns entries with op_type, attempts, classification, last_error, moved_to_dlq_at,
    and file_size. Each entry has a filename you can pass to dlq_requeue().

    These operations will NOT be retried automatically. Fix the root cause first, then
    call dlq_requeue(filename) to send them back through the queue.
    """
    import json as _json

    fq = _get_file_queue()
    if not fq.dlq_dir.exists():
        return []
    results = []
    for sidecar in sorted(fq.dlq_dir.glob("*.json.error.json")):
        try:
            meta = _json.loads(sidecar.read_text())
        except Exception:
            meta = {}
        fname = sidecar.name[: -len(".error.json")]
        main_file = fq.dlq_dir / fname
        try:
            file_size = main_file.stat().st_size if main_file.exists() else None
        except OSError:
            file_size = None
        results.append(
            {
                "file": fname,
                "op_type": meta.get("op_type", "unknown"),
                "attempts": meta.get("attempts"),
                "classification": meta.get("classification"),
                "last_error": (meta.get("last_error") or "")[:200],
                "first_failed_at": meta.get("first_failed_at"),
                "moved_to_dlq_at": meta.get("moved_to_dlq_at"),
                "file_size": file_size,
            }
        )
    return results


@_tool(power=True)
def dlq_requeue(filename: str) -> dict:
    """Move a DLQ item back to the queue so it will be retried on the next drain pass.

    Call after fixing the root cause of the failure. The item's retry counter is reset.

    filename: exact filename from dlq_inspect() (e.g. "0001778139482800_<uuid>.json")
    """
    # §4: Reject null bytes, Unicode separators, path traversal chars.
    # U+202F NARROW NO-BREAK SPACE, U+200B ZERO WIDTH SPACE,
    # U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR
    _FORBIDDEN_IN_FILENAME = ("/", "\\", "\x00", " ", " ", "​", " ", " ")
    if filename.startswith(".") or any(c in filename for c in _FORBIDDEN_IN_FILENAME):
        return {"requeued": False, "error": "Invalid filename — must be a plain filename"}
    fq = _get_file_queue()
    src = fq.dlq_dir / filename
    if not src.exists():
        return {"requeued": False, "error": f"Not found in DLQ: {filename}"}
    dest = fq.queue_dir / filename
    if dest.exists():
        return {"requeued": False, "error": f"Already exists in queue: {filename}"}
    try:
        src.rename(dest)
    except OSError as exc:
        return {"requeued": False, "error": str(exc)}
    # Remove sidecar
    (fq.dlq_dir / (filename + ".error.json")).unlink(missing_ok=True)
    # Reset in-memory retry tracker
    if _st._queue_drainer is not None:
        _st._queue_drainer.reset_attempt(filename)
    return {
        "requeued": True,
        "file": filename,
        "message": "Item will be retried on next drain pass",
    }
