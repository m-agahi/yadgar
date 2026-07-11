"""Backend write-execution package (R3 Car 1 write-half).

Owns the synchronous write pipeline the queue drainer replays: the memorize
phase pipeline plus the anchor / checkpoint / wiki_add sync bodies. The core
MCP shells enqueue only; this package executes.

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge.
"""

from __future__ import annotations

import logging
import threading

from yadgar._shared.observability.observe import observe
from yadgar.backend.write_exec.action_log_impl import run_action_log_replay
from yadgar.backend.write_exec.anchor_impl import run_anchor_replay
from yadgar.backend.write_exec.checkpoint_impl import run_checkpoint_replay
from yadgar.backend.write_exec.memorize_impl import run_memorize_replay
from yadgar.backend.write_exec.wiki_add_impl import run_wiki_add_replay

__all__ = [
    "run_memorize_replay",
    "run_anchor_replay",
    "run_checkpoint_replay",
    "run_wiki_add_replay",
    "run_action_log_replay",
    "ensure_write_engines",
]

logger = logging.getLogger(__name__)

_write_engines_lock = threading.Lock()


@observe(tier="stage", metric="write_exec.ensure_write_engines")
def ensure_write_engines() -> None:
    """Build (once) the write-pipeline engines the memorize phases consume.

    R3 Car 1 moved the write pipeline (phase_embed/phase_store/phase_post_write)
    to the backend, but the engines those phases read from ``_st`` —
    ``_write_gate`` (WriteGate), ``_curator`` (MemoryCurator), ``_prospective``
    (ProspectiveMemoryEngine) — were dropped from the core bootstrap without a
    backend construction point. The phases guard on None, so the drainer
    silently ran with write-gating, curation-on-ingest, and prospective
    triggers disabled. This is that missing construction point.

    Called by QueueDrainer before a drain pass. Lazy + idempotent: builds only
    slots that are still None and only when the shared engines they depend on
    (storage/embeddings) are already up. Never raises — a failed engine build
    must not block the drain (the phases tolerate None).
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415
    from yadgar.backend.restoration import ensure_restoration_engines  # noqa: PLC0415

    if _st._storage is None or _st._embeddings is None:
        return  # shared engines not up yet — phases tolerate None
    # T2 Car B: the checkpoint/anchor replay impls + the micro-checkpoint phase
    # read _st._replay, which the shared root no longer builds. Compose the
    # backend restoration engines here so every drain pass has them (idempotent).
    ensure_restoration_engines()
    if _st._write_gate is not None and _st._curator is not None and _st._prospective is not None:
        return
    with _write_engines_lock:
        from yadgar._shared.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        try:
            if _st._write_gate is None and _st._retriever is not None:
                from yadgar.backend.predictive_coding import WriteGate  # noqa: PLC0415

                _st._write_gate = WriteGate(_st._storage, _st._embeddings, _st._retriever, settings)
            if _st._curator is None and _st._thermo is not None:
                from yadgar.backend.curation import MemoryCurator  # noqa: PLC0415

                _st._curator = MemoryCurator(_st._storage, _st._embeddings, _st._thermo, settings)
            if _st._prospective is None:
                from yadgar.backend.prospective import ProspectiveMemoryEngine  # noqa: PLC0415

                _st._prospective = ProspectiveMemoryEngine(_st._storage, settings)
        except Exception:  # noqa: BLE001 — drain must proceed; phases tolerate None
            logger.exception("ensure_write_engines: engine build failed (drain continues)")
