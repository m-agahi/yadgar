"""Backend retriever composition (T2 Car E2 — Car B ensure_restoration_engines precedent).

The shared composition root (``_shared.runtime.lifecycle``) no longer builds
the ``Retriever`` — the retrieval implementation is backend-only after the
sink, and a static ``lifecycle → backend.retrieval`` import would add a new
composition-root waiver (forbidden by the train's exit criteria). Every
backend entry that needs the retriever calls this instead:

  * embed-service ``_ensure_recall_engines`` (POST /recall, /restore, /admin, /viz)
  * ``restoration.ensure_restoration_engines`` (CheckpointRestore wants it)
  * the test harness ``patch_recall_bypass`` (in-process ``_fanout_recall``)

Lazy + idempotent; returns silently when the shared engines are not up yet.
"""

from __future__ import annotations

import logging
import threading

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

_retriever_lock = threading.Lock()


@observe(tier="stage", metric="backend.retrieval.ensure_retrieval_engine")
def ensure_retrieval_engine() -> None:
    """Build (once) the backend Retriever singleton onto ``_st._retriever``.

    Wires the cross-engine dependencies the shared root used to set
    (engram / rules-engine / metacognition) when those engines exist.
    """
    if _st._retriever is not None:
        return
    if _st._storage is None or _st._embeddings is None:
        return  # shared engines not up yet — nothing to compose against
    with _retriever_lock:
        if _st._retriever is not None:
            return
        from yadgar._shared.config import get_settings  # noqa: PLC0415
        from yadgar.backend.cache import get_ce_cache  # noqa: PLC0415
        from yadgar.backend.retrieval.core import Retriever  # noqa: PLC0415

        retriever = Retriever(
            _st._storage,
            _st._embeddings,
            _st._kg,
            get_settings(),
            ml_client=_st._ml_client,
            ce_cache=get_ce_cache(),
        )
        if _st._engram is not None:
            retriever.set_engram(_st._engram)
        if _st._rules_engine is not None:
            retriever.set_rules_engine(_st._rules_engine)
        if _st._metacognition is not None:
            retriever.set_metacognition(_st._metacognition)
        _st._retriever = retriever
