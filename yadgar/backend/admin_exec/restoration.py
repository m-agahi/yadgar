"""Backend admin-op bodies for the restoration family (T2 Car B).

``pre_compact_drain`` is storage-write-only (epoch increment + auto-checkpoint
upsert, no compute) so it rides the generic POST /admin seam instead of the
POST /restore compute route. Callers: the core /hooks/pre-compact HTTP hook and
the ``yadgar drain`` CLI subcommand — both thin ``_forward_admin`` shells after
Car B (CheckpointRestore no longer exists in the core process).
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_replay
from yadgar.backend.restoration import ensure_restoration_engines


@observe(tier="boundary", metric="backend.admin.pre_compact_drain")
def pre_compact_drain(payload: dict) -> dict:
    """Emergency context capture before compaction. Storage-write half.

    payload: {directory, transcript_path?}
    HOOKS Car 2: optional transcript_path is parsed for in-flight orchestration
    state and stored on the checkpoint. Absent/None degrades to pre-Car-2.
    Returns the CheckpointRestore.pre_compact_drain result dict:
    {status, epoch, auto_checkpoint_created}.
    """
    ensure_restoration_engines()
    replay = _get_replay()
    return replay.pre_compact_drain(
        payload.get("directory", ""),
        transcript_path=payload.get("transcript_path"),
    )
