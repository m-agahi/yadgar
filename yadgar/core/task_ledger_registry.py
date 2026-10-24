"""The yaadgaar project-registry answer for the external task ledger gate (ledger 1803).

yadgarhq/docs ADR-1000: whether a project's tasks live in yaadgaar is decided by
the yaadgaar gateway's project registry, not by a project_id prefix. yadgar
cannot ask the gateway itself: the credential belongs to the yaadgaar client,
and the daemon runs in a container that sees neither the client's files nor the
network path to the gateway. So the answer travels in two hops, both files:

1. **The record (written by the yaadgaar client).** Its SessionStart hook
   already asks the gateway about the session's project. It records the answer
   at ``record_path(project)``:

       $YAADGAAR_STATE_DIR/registry/<key>.json            (override)
       ${XDG_STATE_HOME:-~/.local/state}/yaadgaar/registry/<key>.json

   where ``<key>`` is ``urllib.parse.quote(project.casefold(), safe="")``
   (``acme/widget`` -> ``acme%2Fwidget.json``), and the body is

       {"project": "<the key the client sent>", "registered": true | false,
        "checked_at": "<ISO-8601 UTC>", "client_version": "<x.y.z>"}

   ``registered: false`` ONLY on an explicit "the gateway registers no project"
   answer. On a network, auth or gateway fault the client writes nothing and
   leaves any earlier record alone. The write is atomic (temp file + rename).
   ``project`` is minted by the same rule yadgar uses (``.yadgar/project-id``,
   else the ``origin`` remote, insteadOf resolved, host and ``.git`` stripped).

2. **The mirror (written by yadgar's host SessionStart hook).** The hook reads
   the record after minting the project_id and copies the answer into
   ``mirror_path()`` in the yadgar data dir, which the daemon container mounts
   as ``/data``. No record -> the mirror entry is removed, so an uninstalled
   client cannot leave a project frozen forever. Every gate caller (daemon
   SessionStart, ``task_write``, the host stop hook) reads the mirror, so one
   session sees one decision.

Every read is tri-state: ``True`` (registered), ``False`` (explicitly not
registered) or ``None`` (no answer: absent, unreadable or malformed). ``None``
is what sends the gate to its prefix fallback.
"""

import json
import logging
import os
import tempfile
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

#: Override for the yaadgaar client's state dir (tests, non-XDG layouts).
YAADGAAR_STATE_DIR_ENV = "YAADGAAR_STATE_DIR"

#: The mirror file in the yadgar data dir.
MIRROR_FILENAME = "external_ledger_registry.json"


@observe(tier="hot", span=False, metric="core.task_ledger_registry.record_path")
def record_path(project_id: str) -> Path:
    """Where the yaadgaar client records the registry answer for ``project_id``."""
    override = os.environ.get(YAADGAAR_STATE_DIR_ENV, "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        from yadgar._shared.paths.paths import _xdg_state_home

        base = _xdg_state_home() / "yaadgaar"
    return base / "registry" / f"{urllib.parse.quote(project_id.casefold(), safe='')}.json"


@observe(tier="hot", span=False, metric="core.task_ledger_registry.mirror_path")
def mirror_path() -> Path:
    """The mirror file, in the yadgar data dir (``/data`` inside the daemon)."""
    from yadgar._shared.paths.paths import _data_dir

    return Path(_data_dir()) / MIRROR_FILENAME


@observe(tier="stage", metric="core.task_ledger_registry.read_record")
def read_yaadgaar_record(project_id: str) -> bool | None:
    """The yaadgaar client's answer for ``project_id``; None when it gave none.

    A record that is not a JSON object, lacks a boolean ``registered``, or
    names a different project is no answer: guessing from a bad record would
    put a project's tasks in the wrong ledger.
    """
    path = record_path(project_id)
    try:
        raw: Any = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # fmt: skip
        logger.warning("yaadgaar registry record %s is unreadable; no registry answer", path)
        return None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("registered"), bool)
        or not isinstance(raw.get("project"), str)
        or raw["project"].casefold() != project_id.casefold()
    ):
        logger.warning("yaadgaar registry record %s is malformed; no registry answer", path)
        return None
    return raw["registered"]


@observe(tier="stage", metric="core.task_ledger_registry.read_mirror_map")
def _read_mirror_map() -> dict[str, dict]:
    """The whole mirror, or ``{}`` when it is absent, unreadable or not an object."""
    try:
        raw: Any = json.loads(mirror_path().read_text())
    except (OSError, ValueError):  # fmt: skip
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


@observe(tier="stage", metric="core.task_ledger_registry.read_mirror")
def read_mirror(project_id: str) -> bool | None:
    """The mirrored registry answer for ``project_id``; None when there is none."""
    entry = _read_mirror_map().get(project_id.casefold())
    if entry is None:
        return None
    registered = entry.get("registered")
    return registered if isinstance(registered, bool) else None


@observe(tier="stage", metric="core.task_ledger_registry.write_mirror_map")
def _write_mirror_map(current: dict[str, dict]) -> None:
    """Replace the mirror atomically, so a concurrent daemon read never sees half a file."""
    path = mirror_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".external_ledger_registry-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@observe(tier="stage", metric="core.task_ledger_registry.mirror")
def mirror_registry_answer(project_id: str) -> bool | None:
    """Host-side: copy the yaadgaar record for ``project_id`` into the mirror.

    Returns the answer mirrored (None = no record, entry removed). Skips the
    write when the mirror already says the same thing. A failed write leaves
    the old mirror in place and is logged; it never raises into SessionStart.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    answer = read_yaadgaar_record(project_id)
    key = project_id.casefold()
    try:
        current = _read_mirror_map()
        existing = current.get(key, {}).get("registered")
        if answer is None:
            if key not in current:
                return None
            current.pop(key)
        elif existing is answer:
            return answer
        else:
            current[key] = {
                "registered": answer,
                "mirrored_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        _write_mirror_map(current)
    except OSError:
        logger.warning("could not update %s; the daemon keeps its last answer", MIRROR_FILENAME)
    return answer
