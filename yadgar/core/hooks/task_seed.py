"""Mechanical harness task-list seeder — SessionStart (Car C, ADR-0137 Option A).

Writes the Claude Code harness's own on-disk task records
(``<config_dir>/tasks/<session_id>/<N>.json``) so the ledger's open tasks are
present in the harness task list the moment a session opens, at zero model
cost.

Why this exists: the SessionStart nudge (``http.py:_format_task_list_nudge_rows``)
ordered the model to re-create every open task by hand via ``TaskCreate`` —
measured at 16,705 output tokens for ~81 tasks. ADR-0137 held this mechanical
writer as the fallback "to build ONLY if B measurably still leaves TaskList
empty"; B was in fact failing by exception for its whole life (the ROW-constructor
bug fixed in Car B), so that condition is met. The requirement is unchanged: the
harness list MUST be populated. Only its cost changes.

Id scheme: the ledger id IS the harness id. ``41.json`` holds ``"id": "41"``, so
nothing has to be invented, prefixed or reconciled. Measured 2026-08-16:
allocation does not reuse gaps (created 80, deleted the file, created again and
got 81), and behaves as ``max(file ids, .highwatermark) + 1`` — so a dir seeded
with ledger ids up to 92 hands the next ``TaskCreate`` 93.

The store is UNDOCUMENTED and internal, so it is version-fragile. Every guard
below fails the whole seed rather than writing a record into a format that has
moved; the caller then falls back to the (lean) nudge. A wrong write is worse
than no write.

Format facts, measured read-only against the live store 2026-08-16 — 46 session
dirs, 400 task files:

* ``.lock`` is a 0-byte regular file present in EVERY dir, including 10 that
  hold no task files at all. It marks the dir, it is not a busy signal: a
  seeder that skips when it sees one never fires. Its content changing is,
  however, a strong signal the semantics moved — hence the size guard.
* Keys: ``{id, subject, description, status, blocks, blockedBy}`` always;
  ``activeForm`` in 243/400 files and absent from 156; ``metadata`` in 1. The
  reader is sparse-tolerant, so the seeder writes the minimum and omits
  ``activeForm`` and ``metadata`` entirely rather than guessing their contract.
* ``id`` is a JSON string equal to the file stem.
* ``status`` is only ever ``pending`` / ``in_progress`` / ``completed``.
* ``.highwatermark`` holds a bare integer.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["seed_harness_task_list", "tasks_root"]

# A session id is a bare uuid-ish token. Anything with a separator in it would
# let a malformed payload address ANOTHER live session's task store.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_KNOWN_STATUSES = frozenset({"pending", "in_progress", "completed"})
_OPEN_STATUSES = frozenset({"pending", "in_progress"})


def tasks_root() -> Path:
    """Return the harness task-store root (``<config dir>/tasks``).

    ``CLAUDE_CONFIG_DIR`` is the harness's own override and is what every test
    pins — the real ``~/.claude/tasks`` holds the live state of running
    sessions and must never be written by a test.
    """
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    base = Path(cfg) if cfg else Path.home() / ".claude"
    return base / "tasks"


def _normalise_rows(rows: Any) -> tuple[list[dict[str, Any]], int]:
    """Return (writable records, skipped count) for a ledger row list.

    A row that cannot be turned into a record is dropped, not guessed at: no
    id, a non-positive id, no title, or a status outside the open set (the
    ledger is asked for open tasks only — a closed row here means the caller
    sent something else).
    """
    out: list[dict[str, Any]] = []
    skipped = 0
    seen: set[int] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        raw_id = row.get("id")
        title = row.get("title") or row.get("subject")
        status = row.get("status")
        try:
            task_id = int(str(raw_id).strip())
        except (TypeError, ValueError):  # fmt: skip
            skipped += 1
            continue
        if task_id <= 0 or not isinstance(title, str) or not title.strip():
            skipped += 1
            continue
        if status not in _OPEN_STATUSES or task_id in seen:
            skipped += 1
            continue
        seen.add(task_id)
        out.append(
            {
                "id": str(task_id),
                "subject": f"{task_id}: {title.strip()}",
                "description": "",
                "status": status,
                "blocks": _edge_ids(row.get("blocks")),
                "blockedBy": _edge_ids(row.get("blocked_by")),
            }
        )
    return _prune_dangling_edges(out, seen), skipped


def _edge_ids(raw: Any) -> list[str]:
    """Normalise one ledger edge list to harness ids (strings), or ``[]``.

    Harness ids are JSON STRINGS — ``id`` is ``"41"``, not ``41``, measured
    across 400 live files. The edge arrays were empty in every one of those
    files, so their element type is INFERRED from ``id``'s, not measured. That
    inference is why everything unrecognisable degrades to ``[]`` here rather
    than being passed through: this store's doctrine is that a wrong write is
    worse than no write.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):  # fmt: skip
            continue
        if value > 0 and str(value) not in out:
            out.append(str(value))
    return out


def _prune_dangling_edges(records: list[dict[str, Any]], seen: set[int]) -> list[dict[str, Any]]:
    """Drop edge targets that are not themselves in the seeded set.

    The ledger's open tasks can depend on CLOSED ones, and a closed task is
    not written to the harness store. An edge pointing at an id the harness
    does not hold is an untested path through an undocumented reader — the
    seeder's whole posture is to write only shapes it has seen, so a target
    outside the set is dropped rather than offered up.
    """
    known = {str(i) for i in seen}
    for rec in records:
        for key in ("blocks", "blockedBy"):
            rec[key] = [t for t in rec[key] if t in known and t != rec["id"]]
    return records


def _inspect_dir(session_dir: Path) -> tuple[set[int], int, str]:
    """Return (existing ids, highwatermark, "") — or ("", "", reason) on a trip.

    Anything the survey did not observe is treated as "the format moved".
    """
    existing: set[int] = set()
    hwm = 0
    if not session_dir.is_dir():
        return existing, hwm, ""

    lock = session_dir / ".lock"
    if lock.exists() and (not lock.is_file() or lock.stat().st_size != 0):
        return set(), 0, "unexpected_lock"

    mark = session_dir / ".highwatermark"
    if mark.is_file():
        try:
            hwm = int(mark.read_text().strip())
        except (OSError, ValueError):  # fmt: skip
            return set(), 0, "unexpected_schema"

    for entry in session_dir.glob("*.json"):
        if not entry.stem.isdigit():
            return set(), 0, "unexpected_schema"
        try:
            record = json.loads(entry.read_text())
        except (OSError, ValueError):  # fmt: skip
            return set(), 0, "unexpected_schema"
        if not isinstance(record, dict):
            return set(), 0, "unexpected_schema"
        if record.get("id") != entry.stem or not isinstance(record.get("subject"), str):
            return set(), 0, "unexpected_schema"
        if record.get("status") not in _KNOWN_STATUSES:
            return set(), 0, "unexpected_schema"
        existing.add(int(entry.stem))
    return existing, hwm, ""


def _write_atomic(path: Path, payload: str) -> None:
    """temp+rename inside the target dir so a reader never sees a partial file."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".yadgar-seed-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def seed_harness_task_list(
    session_id: Any,
    rows: Any,
    root: Path | None = None,
) -> dict[str, Any]:
    """Seed the harness task store for ``session_id`` from ledger ``rows``.

    Returns ``{"ok", "seeded", "already_present", "skipped", "reason", "dir"}``.
    ``ok`` is the caller's signal to SUPPRESS the fallback nudge: a partial
    seed still suppresses it, because a short list beats a list the model
    duplicates by hand.

    Never raises — a hook must not be able to brick SessionStart.
    """
    result: dict[str, Any] = {
        "ok": False,
        "seeded": 0,
        "already_present": 0,
        "skipped": 0,
        "reason": "",
        "dir": "",
    }
    try:
        if os.environ.get("YADGAR_HARNESS_SEED_DISABLED", "").strip() not in ("", "0"):
            result["reason"] = "disabled"
            return result
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            result["reason"] = "bad_session_id"
            return result

        base = root if root is not None else tasks_root()
        # The config dir is created by the harness. Its absence means this is
        # not a Claude Code host and there is no store to seed.
        if not base.parent.is_dir():
            result["reason"] = "no_config_dir"
            return result

        records, skipped = _normalise_rows(rows)
        result["skipped"] = skipped
        if not records:
            # Nothing to seed is a success only when nothing was offered;
            # rows that all failed validation must let the nudge through.
            result["ok"] = skipped == 0
            result["reason"] = "no_rows" if skipped == 0 else "no_valid_rows"
            return result

        session_dir = base / session_id
        result["dir"] = str(session_dir)
        existing, hwm, trip = _inspect_dir(session_dir)
        if trip:
            result["reason"] = trip
            return result

        session_dir.mkdir(parents=True, exist_ok=True)
        seeded = 0
        for record in records:
            task_id = int(record["id"])
            if task_id in existing:
                result["already_present"] += 1
                continue
            _write_atomic(session_dir / f"{task_id}.json", json.dumps(record, indent=2))
            seeded += 1

        result["seeded"] = seeded
        if seeded:
            # Belt-and-braces: allocation is max(file ids, .highwatermark) + 1,
            # and the harness only writes this file on DELETE. Stamping it here
            # keeps a seeded id from being handed out again even if every
            # seeded file is later deleted.
            top = max([hwm, *(int(r["id"]) for r in records), *existing])
            _write_atomic(session_dir / ".highwatermark", str(top))
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001 — never brick SessionStart
        result["ok"] = False
        result["reason"] = f"error: {type(exc).__name__}"
        return result
