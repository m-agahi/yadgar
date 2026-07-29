"""The ONE bearer-token resolver for host-side yadgar HTTP callers (2026-07-29).

Why this module exists
----------------------
``YADGAR_MCP_AUTH_TOKEN`` was being resolved by three separate hand-rolled
copies, and the third one was WRONG:

  1. ``install/clients/mcp_register.resolve_mcp_auth_token`` — env → secrets.env
     (added by the 2026-07-28 fresh-VM QA fix).
  2. ``cli/seed.py::_read_auth_token`` — the same pattern re-typed by hand.
  3. ``core/runtime_config_client`` — ``os.environ`` ONLY.

``/api/`` is auth-gated (``core/auth_middleware/auth_middleware.py``) and NO
installer sources ``secrets.env`` (README tells the user to do that *after*
install), so copy 3 made every host-side runtime-config request unauthenticated.
Writes silently returned ``False`` and reads silently fail-opened to the
caller's default. That is why ``yadgar setup --no-code-graph`` could not
actually persist ``code_graph.enabled=false``: the row never landed.

All three call sites now route through :func:`resolve_auth_token`. A FOURTH copy
is not acceptable — extend this module instead.

Placement: ``core/install/`` rather than ``_shared/`` — the dual-import law
(``_shared/AGENTS.md`` §1) admits a module only when BOTH ``core`` and
``backend`` import it, and every consumer here is core host-side. NOT under
``install/clients/`` so that importing it does not drag the client-registry
package into the hook-imported ``runtime_config_client`` path.

Stdlib + observability only, so ``runtime_config_client`` (which hook scripts
import) keeps its cheap dependency profile.
"""

from __future__ import annotations

import os
from pathlib import Path

from yadgar._shared.observability.observe import observe

#: The env var carrying the yadgar MCP bearer token.
TOKEN_ENV_VAR = "YADGAR_MCP_AUTH_TOKEN"

#: The line prefix used to find the token inside secrets.env (v5.49.3 format).
TOKEN_ENV_LINE_PREFIX = f"{TOKEN_ENV_VAR}="


@observe(tier="stage")
def parse_secrets_env_token(secrets_path: Path) -> str:
    """Best-effort parse of ``YADGAR_MCP_AUTH_TOKEN=`` from *secrets_path*.

    Returns ``""`` when the file is missing, unreadable, or has no matching line
    — never raises. A malformed/legacy secrets file must degrade to "no token",
    never crash an installer.

    This is the SUPERSET of the two parsers it replaces: ``mcp_register``'s did a
    bare ``startswith`` on the raw line; ``seed.py``'s additionally skipped
    comments and stripped surrounding quotes. Quote-stripping is inert for
    ``mcp_register``'s callers (the generated token is urlsafe-base64, which
    never contains a quote), and leading-whitespace tolerance only widens what
    parses — so neither caller loses a token it previously found.
    """
    try:
        text = secrets_path.read_text()
    except OSError:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(TOKEN_ENV_LINE_PREFIX):
            return line[len(TOKEN_ENV_LINE_PREFIX) :].strip().strip('"').strip("'")
    return ""


@observe(tier="stage")
def resolve_auth_token() -> str:
    """Resolve the yadgar MCP bearer token for any host-side HTTP caller.

    Resolution order (never raises; ``""`` means "no token available"):

      1. ``$YADGAR_MCP_AUTH_TOKEN`` (stripped), when non-empty.
      2. Else parse it out of ``paths.SECRETS_ENV_PATH`` (which itself honors
         the ``$YADGAR_SECRETS_ENV_FILE`` override).
      3. Else ``""``.

    An explicitly-exported env var always wins over ``secrets.env`` — deliberate,
    so a user who exports a different token has that override respected.

    ``paths`` is imported lazily: its constants resolve env overrides at ACCESS
    time (PEP-562), so reading the path here rather than at import time is what
    makes ``$YADGAR_SECRETS_ENV_FILE`` work for callers set up mid-process.
    """
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token

    from yadgar._shared import paths as _paths  # noqa: PLC0415 — lazy: see docstring

    return parse_secrets_env_token(_paths.SECRETS_ENV_PATH)
