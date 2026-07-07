"""Small recall-path utilities shared between core and the backend recall pipeline.

Car 3 (folder-split #17): ``_bounded_set`` and ``_is_episodic_query`` were used by
``_shared.runtime.recall_pipeline`` via a func-local ``from yadgar.server._helpers
import ...`` — a ``_shared → server`` edge. Both are self-contained (they touch
only ``_st._DICT_MAX_SIZE`` and ``settings``), so they move here into ``_shared``.
``yadgar.server._helpers`` re-exports them for back-compat (existing callers in
http.py / recall.py / server.__init__ / tests are unchanged).
"""

from __future__ import annotations

from collections import OrderedDict

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe

settings = get_settings()


@observe(tier="stage")
def _bounded_set(d: OrderedDict, key, value, max_size: int = _st._DICT_MAX_SIZE) -> None:
    """Insert key→value, evicting oldest entry if dict exceeds max_size."""
    d[key] = value
    if len(d) > max_size:
        d.popitem(last=False)  # remove LRU (first inserted)


@observe(tier="stage")
def _is_episodic_query(query: str) -> bool:
    """Return True if the query is temporal/episodic — wiki blending is skipped."""
    q = query.lower()
    for kw in settings.TEMPORAL_KEYWORDS.split(","):
        kw = kw.strip()
        if kw and kw in q:
            return True
    return False
