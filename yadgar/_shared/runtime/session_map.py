"""Hook-authored ``directory -> project_id`` map (the global-config tier).

WHY THIS EXISTS
---------------
Identity reaches the daemon by one of two routes, and which one you get is
decided by how the MCP client is configured:

* **Per-project ``mcpServers`` entry** — the entry carries a static
  ``X-Yadgar-Project-Id`` header, exactly the way it already carries
  ``Authorization``. The middleware stamps it per request and
  ``resolve_effective_project`` tier 2 reads it. Nothing here is involved.

* **One global ``mcpServers`` entry** shared by every project — the common
  setup. Every request then looks identical on the wire, so no header can
  distinguish projects, and the daemon runs ``stateless_http=True`` (see
  ``core/server/_startup.py``) so there is no ``Mcp-Session-Id`` either. The
  ONLY per-call signal that varies is the ``directory`` argument the tool was
  given. This module is what makes that argument usable *without* deriving
  anything from it.

THIS IS A LOOKUP, NOT A DERIVATION — the distinction ADR-0227 turns on
---------------------------------------------------------------------
ADR-0227 deletes ``derive_project_id`` from every path reachable by core or
backend, because those processes run in a container with no git and no project
mount: a derivation there is *guaranteed* wrong and its wrongness is invisible,
since ``local/<basename>`` is a well-formed key.

Nothing here derives. The SessionStart hook — host-side, where the working
tree actually exists — mints the identity through the host-side mint module
and records the pair. The daemon only ever asks "has the hook told me about this
exact directory?". A directory the hook never registered resolves to nothing
and the caller fails loud, which is the same outcome as before this tier
existed. No key is ever manufactured from a path.

WHAT THIS DOES NOT REINTRODUCE
------------------------------
Not sticky session state. There is no "current project" that an instance sets
and then forgets to switch back — the stated reason auto-bind was rejected.
Resolution is per call, from the directory that call names, against a table
only the hook writes. An instance cannot register a directory, and an
unregistered one is not guessed at.

An instance *can* name another registered directory and read that project —
but it can already do that by passing ``project=`` outright, which is a
supported, documented parameter. This tier adds no authority the caller did
not already have.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe

#: Filename under the data dir. The data dir is bind-mounted into both
#: containers (``YADGAR_DATA_DIR=/data``), so a value the host-side hook writes
#: is readable by the daemon without any new mount or socket.
_MAP_FILENAME = "session_projects.json"

#: Cap on retained entries. A developer accumulates directories over months and
#: this file is read on a hot path; the cap keeps it small and bounded. Oldest
#: entries are dropped first (insertion order is preserved by dict).
_MAX_ENTRIES = 512


@observe(tier="hot", span=False, metric="runtime.session_map.path")
def _map_path() -> Path:
    """Absolute path to the map file."""
    from yadgar._shared.paths.paths import _data_dir  # noqa: PLC0415

    return Path(_data_dir()) / _MAP_FILENAME


@observe(tier="stage", metric="runtime.session_map.read")
def _read_map() -> dict[str, str]:
    """Return the whole map, or ``{}`` when it is absent or unreadable.

    Fail-soft on read: a corrupt or half-written file must degrade to "no
    identity registered" (the caller then fails loud with the normal
    unresolved-project error), never raise into a tool call.
    """
    try:
        raw: Any = json.loads(_map_path().read_text())
    except (OSError, ValueError):  # fmt: skip
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}


@observe(tier="hot", span=False, metric="runtime.session_map.lookup")
def lookup_project_for_directory(directory: str | None) -> str | None:
    """Return the project_id the hook registered for *directory*, or None.

    Exact match only. No parent walk, no basename fallback, no normalisation
    beyond stripping a trailing separator — every one of those is a way to
    turn "I don't know" into a plausible wrong answer, which is the failure
    mode ADR-0227 exists to prevent.
    """
    if not directory or not str(directory).strip():
        return None
    key = str(directory).rstrip("/") or "/"
    return _read_map().get(key) or None


@observe(tier="stage", metric="runtime.session_map.register")
def register_session_project(directory: str, project_id: str) -> bool:
    """Record ``directory -> project_id``. Host-side (hook) callers only.

    Written atomically via a temp file + ``os.replace`` so a daemon reading
    concurrently sees either the old map or the new one, never a partial file.
    Returns True when the pair was written.
    """
    if not directory or not str(directory).strip():
        return False
    if not project_id or not str(project_id).strip():
        return False
    # Car 5 (2026-08-20 train): refuse to record a value that names no project.
    # ``_NON_IDENTIFYING_PROJECT_IDS`` is the one authority the create gate and
    # the restamp gates read; a map entry holding ``'global'`` would hand every
    # unqualified call in that directory a manufactured identity ADR-0227
    # deletes. The READER (``resolve_effective_project``) also skips such a
    # value, because entries written before this guard are still on disk — this
    # stops NEW ones, the reader survives the OLD ones.
    from yadgar._shared.storage._project_id_writer import (  # noqa: PLC0415
        _NON_IDENTIFYING_PROJECT_IDS,
    )

    if str(project_id).strip() in _NON_IDENTIFYING_PROJECT_IDS:
        return False
    key = str(directory).rstrip("/") or "/"
    value = str(project_id).strip()

    current = _read_map()
    if current.get(key) == value:
        return True  # already recorded — skip the write entirely
    # Re-insert at the end so the cap drops genuinely stale directories first.
    current.pop(key, None)
    current[key] = value
    while len(current) > _MAX_ENTRIES:
        current.pop(next(iter(current)))

    path = _map_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".session_projects-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(current, fh, indent=2, sort_keys=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True
