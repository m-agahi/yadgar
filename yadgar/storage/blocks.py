"""Memory block CRUD — stored in `memory_block` SurrealDB table.

_BlocksMixin provides:
  - create_block(name, content, scope, directory, char_limit) -> dict
  - get_block(name, scope, directory) -> dict | None
  - update_block(name, content, scope, directory) -> dict
  - delete_block(name, scope, directory) -> None
  - list_blocks(scope, directory) -> list[dict]

Schema (memory_block table, migration 012):
  id            int             — auto-increment
  name          string          — block name (snake_case, [a-z][a-z0-9_]*)
  scope         string          — 'global' or 'project'
  directory     string | None   — abs path for project scope; None for global
  content       string          — block text, enforced ≤ char_limit
  char_limit    int             — per-block char cap (default 2000, hard max 8000)
  created_at    string          — ISO timestamp
  updated_at    string          — ISO timestamp (updated on every write)

Uniqueness invariant (application-enforced, not DB constraint):
  (name, scope, directory) must be unique.

Hard caps (hardcoded constants — env knobs promoted in v5.33.x):
  _MAX_PER_SCOPE        = 10    — max blocks per (scope, directory) tuple
  _DEFAULT_CHAR_LIMIT   = 2000  — default char_limit when not specified
  _HARD_CHAR_LIMIT      = 8000  — absolute max char_limit
"""

from __future__ import annotations

import logging
import re

from yadgar.tracing import trace_span

_log = logging.getLogger(__name__)

# Hard caps — promote to env knobs in v5.33.x
_MAX_PER_SCOPE = 10
_DEFAULT_CHAR_LIMIT = 2000
_HARD_CHAR_LIMIT = 8000

# Block name validation: lowercase letters/digits/underscores, start with letter
_BLOCK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_block_name(name: str) -> str:
    """Validate block name: [a-z][a-z0-9_]*, ≤64 chars. Returns name if valid."""
    if not name or not isinstance(name, str):
        raise ValueError(f"block name must be a non-empty string, got {name!r}")
    if len(name) > 64:
        raise ValueError(f"block name {name!r} exceeds 64 chars")
    if not _BLOCK_NAME_RE.match(name):
        raise ValueError(
            f"block name {name!r} invalid: must match [a-z][a-z0-9_]* (no spaces, "
            "no uppercase, start with letter)"
        )
    return name


def _canonical_dir(scope: str, directory: str | None) -> str | None:
    """Return canonical directory value for storage: None for global, abs path for project."""
    if scope == "global":
        return None
    if scope == "project":
        if not directory:
            raise ValueError("directory is required for scope='project'")
        return str(directory)
    raise ValueError(f"scope must be 'global' or 'project', got {scope!r}")


class _BlocksMixin:
    """Memory block CRUD — mixed into StorageEngine."""

    def _block_dir_clause(self, scope: str, directory: str | None) -> tuple[str, dict]:
        """Return WHERE clause fragment + params for (scope, directory) lookup."""
        if scope == "global":
            return "scope = $scope AND directory IS NONE", {"scope": "global"}
        return "scope = $scope AND directory = $directory", {
            "scope": "project",
            "directory": directory,
        }

    def _count_blocks_in_scope(self, scope: str, directory: str | None) -> int:
        """Count existing blocks for a (scope, directory) tuple."""
        if scope == "global":
            rows = self._q(
                "SELECT count() AS cnt FROM memory_block WHERE scope = 'global' AND directory IS NONE GROUP ALL"
            )
        else:
            rows = self._q(
                "SELECT count() AS cnt FROM memory_block WHERE scope = $scope AND directory = $directory GROUP ALL",
                {"scope": scope, "directory": directory},
            )
        return int(rows[0]["cnt"]) if rows else 0

    @trace_span("storage.blocks.create_block")
    def create_block(
        self,
        name: str,
        content: str,
        scope: str = "project",
        directory: str | None = None,
        char_limit: int = _DEFAULT_CHAR_LIMIT,
    ) -> dict:
        """Create a new memory block.

        Returns {id, name, scope, content, char_limit, created_at, updated_at} on success.
        Returns {ok: False, error: "..."} on validation failure or duplicate.
        """
        # Validate name
        try:
            name = _validate_block_name(name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Validate char_limit
        if char_limit > _HARD_CHAR_LIMIT:
            return {
                "ok": False,
                "error": f"char_limit {char_limit} exceeds hard cap of {_HARD_CHAR_LIMIT}",
            }
        if char_limit <= 0:
            return {"ok": False, "error": f"char_limit must be > 0, got {char_limit}"}

        # Validate content length
        if len(content) > char_limit:
            return {
                "ok": False,
                "error": (
                    f"content length {len(content)} exceeds char_limit {char_limit}; "
                    "shorten or raise char_limit"
                ),
            }

        # Validate scope + directory
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Check per-scope cap
        count = self._count_blocks_in_scope(scope, canonical_dir)
        if count >= _MAX_PER_SCOPE:
            return {
                "ok": False,
                "error": (
                    f"scope cap reached: already {count} blocks in scope={scope!r} "
                    f"directory={canonical_dir!r} (max={_MAX_PER_SCOPE}); "
                    "delete unused blocks first"
                ),
            }

        # Check uniqueness: (name, scope, directory)
        existing = self.get_block(name, scope=scope, directory=canonical_dir)
        if existing is not None:
            return {
                "ok": False,
                "error": (
                    f"block {name!r} already exists in scope={scope!r} "
                    f"directory={canonical_dir!r}; use block_update to modify"
                ),
            }

        now = self._now_iso()
        bid = self._next_id("memory_block")

        if canonical_dir is None:
            self._q(
                "CREATE type::record('memory_block', $id) SET "
                "name = $name, scope = $scope, directory = NONE, "
                "content = $content, char_limit = $char_limit, "
                "created_at = $ts, updated_at = $ts",
                {
                    "id": bid,
                    "name": name,
                    "scope": scope,
                    "content": content,
                    "char_limit": char_limit,
                    "ts": now,
                },
            )
        else:
            self._q(
                "CREATE type::record('memory_block', $id) SET "
                "name = $name, scope = $scope, directory = $directory, "
                "content = $content, char_limit = $char_limit, "
                "created_at = $ts, updated_at = $ts",
                {
                    "id": bid,
                    "name": name,
                    "scope": scope,
                    "directory": canonical_dir,
                    "content": content,
                    "char_limit": char_limit,
                    "ts": now,
                },
            )

        return {
            "id": bid,
            "name": name,
            "scope": scope,
            "directory": canonical_dir,
            "content": content,
            "char_limit": char_limit,
            "created_at": now,
            "updated_at": now,
        }

    @trace_span("storage.blocks.get_block")
    def get_block(
        self, name: str, scope: str = "project", directory: str | None = None
    ) -> dict | None:
        """Fetch a single block by (name, scope, directory). Returns None if not found."""
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError:
            return None

        if canonical_dir is None:
            rows = self._q(
                "SELECT * FROM memory_block WHERE name = $name AND scope = $scope AND directory IS NONE LIMIT 1",
                {"name": name, "scope": scope},
            )
        else:
            rows = self._q(
                "SELECT * FROM memory_block WHERE name = $name AND scope = $scope AND directory = $directory LIMIT 1",
                {"name": name, "scope": scope, "directory": canonical_dir},
            )

        if not rows:
            return None
        return self._row_to_dict(rows[0])

    @trace_span("storage.blocks.update_block")
    def update_block(
        self,
        name: str,
        content: str,
        scope: str = "project",
        directory: str | None = None,
    ) -> dict:
        """Replace block content. Validates against char_limit.

        Returns updated block dict on success.
        Returns {ok: False, error: "..."} on failure.
        """
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        existing = self.get_block(name, scope=scope, directory=canonical_dir)
        if existing is None:
            return {
                "ok": False,
                "error": f"block {name!r} not found in scope={scope!r} directory={canonical_dir!r}",
            }

        char_limit = int(existing.get("char_limit") or _DEFAULT_CHAR_LIMIT)
        if len(content) > char_limit:
            return {
                "ok": False,
                "error": (
                    f"content length {len(content)} exceeds char_limit {char_limit}; "
                    "shorten or raise char_limit"
                ),
            }

        now = self._now_iso()
        if canonical_dir is None:
            self._q(
                "UPDATE memory_block SET content = $content, updated_at = $ts "
                "WHERE name = $name AND scope = $scope AND directory IS NONE",
                {"name": name, "scope": scope, "content": content, "ts": now},
            )
        else:
            self._q(
                "UPDATE memory_block SET content = $content, updated_at = $ts "
                "WHERE name = $name AND scope = $scope AND directory = $directory",
                {
                    "name": name,
                    "scope": scope,
                    "directory": canonical_dir,
                    "content": content,
                    "ts": now,
                },
            )

        return {
            **existing,
            "content": content,
            "updated_at": now,
        }

    @trace_span("storage.blocks.delete_block")
    def delete_block(self, name: str, scope: str = "project", directory: str | None = None) -> None:
        """Delete block by (name, scope, directory). Idempotent — no error if missing."""
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError:
            return

        if canonical_dir is None:
            self._q(
                "DELETE memory_block WHERE name = $name AND scope = $scope AND directory IS NONE",
                {"name": name, "scope": scope},
            )
        else:
            self._q(
                "DELETE memory_block WHERE name = $name AND scope = $scope AND directory = $directory",
                {"name": name, "scope": scope, "directory": canonical_dir},
            )

    @trace_span("storage.blocks.list_blocks")
    def list_blocks(self, scope: str | None = None, directory: str | None = None) -> list[dict]:
        """Return blocks filtered by scope and directory.

        scope=None: return both global and project blocks for the given directory.
        scope='global': return only global blocks.
        scope='project': return only project blocks for the given directory.
        """
        if scope == "global":
            rows = self._q(
                "SELECT * FROM memory_block WHERE scope = 'global' AND directory IS NONE "
                "ORDER BY name ASC"
            )
        elif scope == "project":
            if not directory:
                return []
            rows = self._q(
                "SELECT * FROM memory_block WHERE scope = 'project' AND directory = $directory "
                "ORDER BY name ASC",
                {"directory": directory},
            )
        else:
            # scope=None: return global + project for this directory
            if directory:
                rows = self._q(
                    "SELECT * FROM memory_block WHERE "
                    "(scope = 'global' AND directory IS NONE) "
                    "OR (scope = 'project' AND directory = $directory) "
                    "ORDER BY scope ASC, name ASC",
                    {"directory": directory},
                )
            else:
                rows = self._q(
                    "SELECT * FROM memory_block WHERE scope = 'global' AND directory IS NONE "
                    "ORDER BY name ASC"
                )

        return self._rows_to_dicts(rows)
