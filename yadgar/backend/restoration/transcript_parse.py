"""Back-compat re-export shim — the parser moved to ``_shared`` (v5.135).

Car fix-drain-inflight moved the pure transcript in-flight parser from here to
``yadgar._shared.restoration.transcript_parse`` so it can be imported HOST-SIDE
(from ``yadgar.core.cli._shared`` — core cannot import backend). ``_shared`` is
importable by both core and backend. The backend keeps importing it from here as
a fallback for the embedded/dev deploy; this shim preserves the old import path
(``yadgar.backend.restoration.transcript_parse.parse_in_flight``) for existing
callers/tests. See the shared module docstring for the full rationale.
"""

from __future__ import annotations

from yadgar._shared.restoration.transcript_parse import (
    capture_in_flight,
    parse_in_flight,
)

__all__ = ["capture_in_flight", "parse_in_flight"]
