"""Backend restoration package — restore/checkpoint COMPUTE (T2 Car B).

Census verdict #7 (layer-boundary train): ``CheckpointRestore`` +
``CognitiveMap`` are restore compute over DB data — BACKEND territory (7 CPUs,
next to the DB; the live core process was exceeding the 95s offload ceiling
running restore() on 1 CPU). Core reaches them ONLY over HTTP:

  * ``POST /restore``                      → ``run_restore`` (this package)
  * ``POST /admin`` op ``pre_compact_drain`` → ``admin_exec.restoration``

The session-side half stays out of this package by design:
  * SR transition RECORDING — ``yadgar._shared.runtime.sr_session``
    (``SRTransitionRecorder``, the base class of ``CognitiveMap``); the core
    recall path records transitions per census verdict #5.
  * checkpoint/anchor ENQUEUE — core MCP shells → file queue → the backend
    drainer replays via ``write_exec`` using the singletons built here.

``ensure_restoration_engines`` is the backend composition point: the shared
composition root (``_shared/runtime/lifecycle.py``) no longer constructs these
two engines (that construction was the reason CheckpointRestore could not live
backend-side — a ``_shared → backend`` edge outside the ADR-0056 waivers).
"""

from __future__ import annotations

import logging
import threading

from yadgar._shared.observability.observe import observe
from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore
from yadgar.backend.restoration.cognitive_map import CognitiveMap

__all__ = [
    "CheckpointRestore",
    "CognitiveMap",
    "ensure_restoration_engines",
    "run_restore",
]

logger = logging.getLogger(__name__)

_restoration_engines_lock = threading.Lock()


@observe(tier="stage", metric="restoration.ensure_restoration_engines")
def ensure_restoration_engines() -> None:
    """Build (once) the backend CognitiveMap + CheckpointRestore singletons.

    T2 Car B: the shared composition root builds only the session-side
    ``SRTransitionRecorder`` into ``_st._cognitive_map`` and leaves
    ``_st._replay`` None. Every backend entry that needs the full engines calls
    this first:

      * embed-service ``_ensure_recall_engines`` (POST /restore, /recall, /admin)
      * ``write_exec.ensure_write_engines`` (drainer replay: checkpoint/anchor/
        micro-checkpoint)
      * ``admin_exec.run_admin_op`` (ops that anchor, e.g. agent_prompt_save)

    Lazy + idempotent: upgrades ``_st._cognitive_map`` to the full CognitiveMap
    (subclass of the recorder — slot type unchanged) and builds ``_st._replay``
    against whatever shared engines are up. Requires storage + embeddings;
    returns silently when they are not up yet (callers build them first).
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    if _st._storage is None or _st._embeddings is None:
        return  # shared engines not up yet — nothing to compose against
    # T2 Car E2: CheckpointRestore wants the backend retriever — compose it
    # first (idempotent; no-op when already built or engines missing).
    from yadgar.backend.retrieval.compose import ensure_retrieval_engine  # noqa: PLC0415

    ensure_retrieval_engine()
    if isinstance(_st._cognitive_map, CognitiveMap) and _st._replay is not None:
        return
    with _restoration_engines_lock:
        if isinstance(_st._cognitive_map, CognitiveMap) and _st._replay is not None:
            return
        from yadgar._shared.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        if not isinstance(_st._cognitive_map, CognitiveMap):
            _st._cognitive_map = CognitiveMap(_st._storage, settings)
        if _st._replay is None:
            _st._replay = CheckpointRestore(
                storage=_st._storage,
                embeddings=_st._embeddings,
                retriever=_st._retriever,
                cognitive_map=_st._cognitive_map,
                metacognition=_st._metacognition,
                settings=settings,
            )


@observe(tier="boundary", metric="restoration.run_restore")
def run_restore(directory: str = "") -> dict:
    """Run the restore compute backend-side (the POST /restore body).

    Invalidates the SR matrix first: transitions are recorded in the CORE
    process (DB writes via the recall session seam), so the backend-resident
    matrix cannot rely on the in-process ``_dirty`` flag — pre-Car-B the flag
    and the matrix lived in the same process. Rebuilding per restore preserves
    the old "fresh matrix when transitions changed" behavior; restore is a
    post-compaction (rare) path, and the rebuild is exactly the compute this
    car moved onto the backend's CPUs.
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    ensure_restoration_engines()
    if _st._replay is None:
        raise RuntimeError(
            "CheckpointRestore not initialized — backend engines are not up "
            "(storage/embeddings missing)"
        )
    if _st._cognitive_map is not None:
        _st._cognitive_map.invalidate()
    return _st._replay.restore(directory=directory)
