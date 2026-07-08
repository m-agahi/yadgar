"""Shared write-exec pieces (_shared-clean): MemorizeContext + phase_validate.

Moved here in R3 Car 1 so core and backend both import from _shared,
not across the core<->backend boundary.
"""

from __future__ import annotations

from .context import MemorizeContext
from .validate import phase_validate

__all__ = ["MemorizeContext", "phase_validate"]
