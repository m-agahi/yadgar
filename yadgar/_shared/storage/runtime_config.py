"""Runtime config CRUD — stored in `runtime_config` SurrealDB table (ADR-0163).

_RuntimeConfigMixin provides a DB-backed, directory-scoped, typed key/value
config store — the storage half of the runtime config store (Car G1). It mirrors
_BlocksMixin's directory-scoping exactly (`_canonical_config_dir`, the
`directory IS NONE` vs `= $directory` WHERE-clause split), differing only in that
there is no `scope` field: a row's `directory` alone carries the scope
(None = global, an abs path = project).

  - set_config_row(key, value, *, directory) -> dict          (upsert)
  - get_config_row(key, *, directory) -> dict | None          (exact row, no fallback)
  - list_config_rows(directory=<sentinel>) -> list[dict]       (all / scoped)
  - delete_config_row(key, *, directory) -> None              (idempotent)

Schema (runtime_config table, migration 027):
  id            int             — auto-increment
  key           string          — config key (arbitrary string)
  directory     string | None   — abs path for project scope; None for global
  value         string          — JSON-encoded value (bool/int/str/list/dict)
  created_at    string          — ISO timestamp
  updated_at    string          — ISO timestamp (bumped on every write)

Values round-trip typed: the mixin JSON-encodes `value` on write and JSON-decodes
it on read (the shared `_row_to_dict` only decodes a fixed field-name allowlist,
which does NOT include `value`, so decode is done here). A stored ``True`` reads
back ``True``; a stored ``[1, 2]`` reads back a list.

Uniqueness invariant (application-enforced, not a DB constraint — mirroring
blocks; a UNIQUE index over a nullable `directory` is deliberately avoided):
  (key, directory) must be unique.

Resolution / fallback (per-dir → global → default) is Car G2's getter, NOT here —
these methods return the RAW (key, directory) row only.
"""

from __future__ import annotations

import json
import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span

_log = logging.getLogger(__name__)

# Sentinel for list_config_rows: distinguishes "no directory filter → ALL rows
# (global + every dir, for warmup)" from directory=None ("global-only"). None is
# a meaningful value here (global scope), so it cannot double as the unset marker.
_UNSET = object()

# Hoisted exception tuple (portability: no inline ``except (A, B):``). Raised when
# a stored `value` is present but not JSON-decodable (legacy/garbage row).
_JSON_DECODE_ERRORS = (ValueError, TypeError)


@observe(tier="hot")
def _canonical_config_dir(directory: str | None) -> str | None:
    """Return canonical directory value for storage: None for global, abs path str otherwise."""
    if directory is None:
        return None
    return str(directory)


class _RuntimeConfigMixin:
    """Runtime config CRUD — mixed into StorageEngine."""

    @observe(tier="hot")
    def _config_dir_clause(self, directory: str | None) -> tuple[str, dict]:
        """Return WHERE clause fragment + params for a (key, directory) lookup."""
        if directory is None:
            return "key = $key AND directory IS NONE", {}
        return "key = $key AND directory = $directory", {"directory": directory}

    @observe(tier="hot")
    def _config_row_to_dict(self, record: dict | None) -> dict | None:
        """Convert a raw runtime_config row to a dict with `value` JSON-decoded."""
        d = self._row_to_dict(record)
        if d is None:
            return None
        # A NONE `directory` field is omitted from the SurrealDB row — normalize
        # the missing key back to an explicit None (global scope).
        d.setdefault("directory", None)
        raw = d.get("value")
        if isinstance(raw, str):
            try:
                d["value"] = json.loads(raw)
            except _JSON_DECODE_ERRORS:
                # Non-JSON legacy/garbage value — leave as-is rather than crash.
                _log.warning("runtime_config: value for key=%s not JSON-decodable", d.get("key"))
        return d

    @trace_span()
    def set_config_row(self, key: str, value, *, directory: str | None) -> dict:
        """Upsert (key, directory) → value. Returns the stored row dict.

        `value` is JSON-encoded for storage (supports bool/int/str/list/dict) and
        decoded back on the returned dict. `updated_at` is bumped on every write;
        `created_at` is preserved across updates.
        """
        canonical_dir = _canonical_config_dir(directory)
        encoded = json.dumps(value, ensure_ascii=False)
        now = self._now_iso()

        existing = self.get_config_row(key, directory=canonical_dir)
        if existing is not None:
            if canonical_dir is None:
                self._q(
                    "UPDATE runtime_config SET value = $value, updated_at = $ts "
                    "WHERE key = $key AND directory IS NONE",
                    {"key": key, "value": encoded, "ts": now},
                )
            else:
                self._q(
                    "UPDATE runtime_config SET value = $value, updated_at = $ts "
                    "WHERE key = $key AND directory = $directory",
                    {"key": key, "value": encoded, "directory": canonical_dir, "ts": now},
                )
            return {
                **existing,
                "value": value,
                "updated_at": now,
            }

        cid = self._next_id("runtime_config")
        if canonical_dir is None:
            self._q(
                "CREATE type::record('runtime_config', $id) SET "
                "key = $key, directory = NONE, value = $value, "
                "created_at = $ts, updated_at = $ts",
                {"id": cid, "key": key, "value": encoded, "ts": now},
            )
        else:
            self._q(
                "CREATE type::record('runtime_config', $id) SET "
                "key = $key, directory = $directory, value = $value, "
                "created_at = $ts, updated_at = $ts",
                {"id": cid, "key": key, "directory": canonical_dir, "value": encoded, "ts": now},
            )

        return {
            "id": cid,
            "key": key,
            "directory": canonical_dir,
            "value": value,
            "created_at": now,
            "updated_at": now,
        }

    @trace_span()
    def get_config_row(self, key: str, *, directory: str | None) -> dict | None:
        """Fetch the exact (key, directory) row. Returns None if not found.

        NO fallback — the raw row only. Resolution/fallback is Car G2's getter.
        """
        canonical_dir = _canonical_config_dir(directory)
        if canonical_dir is None:
            rows = self._q(
                "SELECT * FROM runtime_config WHERE key = $key AND directory IS NONE LIMIT 1",
                {"key": key},
            )
        else:
            rows = self._q(
                "SELECT * FROM runtime_config WHERE key = $key AND directory = $directory LIMIT 1",
                {"key": key, "directory": canonical_dir},
            )
        if not rows:
            return None
        return self._config_row_to_dict(rows[0])

    @trace_span()
    def list_config_rows(self, directory: str | None = _UNSET) -> list[dict]:  # type: ignore[assignment]
        """Return runtime_config rows.

        directory=<sentinel> (default): ALL rows — global + every directory. This
            is the bulk read Car G2's warmup uses.
        directory=None: global rows only.
        directory=<abs path>: rows for that directory only.
        """
        if directory is _UNSET:
            rows = self._q("SELECT * FROM runtime_config ORDER BY key ASC")
        elif directory is None:
            rows = self._q("SELECT * FROM runtime_config WHERE directory IS NONE ORDER BY key ASC")
        else:
            rows = self._q(
                "SELECT * FROM runtime_config WHERE directory = $directory ORDER BY key ASC",
                {"directory": directory},
            )
        return [d for d in (self._config_row_to_dict(r) for r in rows) if d is not None]

    @trace_span()
    def delete_config_row(self, key: str, *, directory: str | None) -> None:
        """Delete the (key, directory) row. Idempotent — no error if missing."""
        canonical_dir = _canonical_config_dir(directory)
        if canonical_dir is None:
            self._q(
                "DELETE runtime_config WHERE key = $key AND directory IS NONE",
                {"key": key},
            )
        else:
            self._q(
                "DELETE runtime_config WHERE key = $key AND directory = $directory",
                {"key": key, "directory": canonical_dir},
            )
