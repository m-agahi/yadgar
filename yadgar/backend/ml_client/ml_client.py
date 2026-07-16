"""ML scoring client — local (sentence_transformers) or remote (HTTP).

LocalMLClient: loads models directly (used in stdio/daemon mode).
RemoteMLClient: delegates to backend /rerank HTTP endpoint (used in Docker core container).

No sentence_transformers import at module level — all heavy imports are lazy
inside LocalMLClient methods, so importing this module is safe in core container.
"""

from __future__ import annotations

import logging
import os  # noqa: F401 — re-exported for __init__.py _EXPORTS["os"]
import time  # noqa: F401 — tests do patch.object(ml_mod.time, "monotonic")

from yadgar._shared.config import resolve_knob  # noqa: F401 — re-exported for _EXPORTS
from yadgar._shared.contracts.protocols import MLClientProtocol as MLClient  # noqa: E402
from yadgar._shared.observability.observe import observe  # noqa: F401 — re-exported for _EXPORTS

# _CircuitBreaker + the _STATE_* constants moved to ``circuit_breaker.py`` (task
# #18 C2 internal split). Imported here for RemoteMLClient; re-exported via the
# package ``__init__`` for back-compat importers.
from yadgar.backend.ml_client._telemetry import (
    _emit_unload_telemetry,  # noqa: F401 — re-exported for _EXPORTS
    _idle_eviction_seconds,  # noqa: F401 — re-exported for _EXPORTS
    _record_model_load,  # noqa: F401 — re-exported for _EXPORTS
    _rpc_span,  # noqa: F401 — re-exported for _EXPORTS
)
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_CLOSED as _STATE_CLOSED,  # noqa: PLC0414 — intentional re-export
)
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_HALF_OPEN as _STATE_HALF_OPEN,  # noqa: PLC0414
)
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_OPEN as _STATE_OPEN,  # noqa: PLC0414
)
from yadgar.backend.ml_client.circuit_breaker import (
    _CircuitBreaker,  # noqa: F401 — re-exported for _EXPORTS
)
from yadgar.backend.ml_client.local_ml_client import LocalMLClient
from yadgar.backend.ml_client.remote_ml_client import RemoteMLClient

logger = logging.getLogger(__name__)

__all__ = ["LocalMLClient", "MLClient", "RemoteMLClient"]
