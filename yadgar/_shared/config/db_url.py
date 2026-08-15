"""Shared DB-URL parsing helpers.

Single source of truth for "is this URL a local-loopback one" — used by CLI
guards that need to decide whether the host-side path is viable or whether
the daemon is in a split-container (URL points at a non-loopback hostname
that is only resolvable from inside the container network).

Sibling to ``config.py`` (pydantic Settings) and ``config_yaml.py``
(FIELD_META schema). The shared ``_EXPORTS`` map in ``config/__init__.py``
re-exports ``_is_db_url_local`` so callers can do
``from yadgar._shared.config import _is_db_url_local``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from yadgar._shared.observability.tracing import trace_span

# Hostnames treated as local-loopback by every caller. ``0.0.0.0`` is included
# because some operators bind the DB to all interfaces and point the CLI at
# that literal address — same effective topology as 127.0.0.1 from the
# host's perspective.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


@trace_span()
def _is_db_url_local(url: str) -> bool:
    """True iff *url*'s hostname is a loopback address.

    An empty/missing URL is NOT a loopback — callers should treat False
    as "consult the env to decide what to do" (the embed-service fall-back
    path, for instance, treats an unset DB URL as the local default).

    Implementation: ``urllib.parse.urlsplit`` -> lowercased hostname,
    checked against the loopback set. Bracketed IPv6 literals
    (``[::1]``) come out of urlsplit as ``::1`` (no brackets), matching
    the bare set entry.
    """
    if not url:
        return False
    host = urlsplit(url).hostname
    if host is None:
        return False
    return host.lower() in _LOOPBACK_HOSTS
