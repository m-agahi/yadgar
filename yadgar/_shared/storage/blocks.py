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

Configurable caps (v5.35.1 — I25 env+yaml+registry, config.settings.*):
  MEMORY_BLOCK_MAX_PER_SCOPE        = 10    — max blocks per (scope, directory) tuple
  MEMORY_BLOCK_DEFAULT_CHAR_LIMIT   = 2000  — default char_limit when not specified
  MEMORY_BLOCK_HARD_CHAR_LIMIT      = 8000  — absolute max char_limit
  MEMORY_BLOCK_TOTAL_BUDGET_CHARS   = 12000 — total budget across all blocks at restore-time
"""

from __future__ import annotations

import logging
import re

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.storage._project_id_writer import project_id_set_fragment

_log = logging.getLogger(__name__)


def _settings():
    """Lazy import to avoid circular refs at module load time."""
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    return get_settings()


def _max_per_scope() -> int:
    return _settings().MEMORY_BLOCK_MAX_PER_SCOPE


def _default_char_limit() -> int:
    return _settings().MEMORY_BLOCK_DEFAULT_CHAR_LIMIT


def _hard_char_limit() -> int:
    return _settings().MEMORY_BLOCK_HARD_CHAR_LIMIT


# Block name: lowercase letters/digits/underscores, start with letter
_BLOCK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@observe(tier="hot")
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


@observe(tier="hot")
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

    @observe(tier="hot")
    def _block_dir_clause(self, scope: str, directory: str | None) -> tuple[str, dict]:
        """Return WHERE clause fragment + params for (scope, directory) lookup."""
        if scope == "global":
            return "scope = $scope AND directory IS NONE", {"scope": "global"}
        return "scope = $scope AND directory = $directory", {
            "scope": "project",
            "directory": directory,
        }

    @observe(tier="stage")
    def _count_blocks_in_scope(
        self, scope: str, directory: str | None, project_id: str | None = None
    ) -> int:
        """Count existing blocks for this scope, using the READ key.

        C11: counted through ``_block_project_clause`` for the same reason the
        uniqueness check is — the cap must be measured over the set the reader
        will actually return, or one project checked out twice gets
        ``MEMORY_BLOCK_MAX_PER_SCOPE`` blocks per checkout and the reader sees
        the sum.
        """
        if scope == "global":
            rows = self._q(
                "SELECT count() AS cnt FROM memory_block WHERE scope = 'global' AND directory IS NONE GROUP ALL"
            )
        else:
            proj_sql, params = self._block_project_clause(directory, project_id)
            rows = self._q(
                f"SELECT count() AS cnt FROM memory_block WHERE scope = $scope AND {proj_sql} "
                "GROUP ALL",
                {"scope": scope, **params},
            )
        return int(rows[0]["cnt"]) if rows else 0

    @trace_span()
    def create_block(
        self,
        name: str,
        content: str,
        scope: str = "project",
        directory: str | None = None,
        char_limit: int | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Create a new memory block.

        Returns {id, name, scope, content, char_limit, created_at, updated_at} on success.
        Returns {ok: False, error: "..."} on validation failure or duplicate.

        C11 (0047 PR#40 §5): **DUAL-WRITE.** Migration 033 declares
        ``memory_block.project_id``, and this writer stamps the caller's value
        into the CREATE — but it keeps writing ``directory`` too. ADR-0225 keeps
        the legacy column *because the backfill derives from it*, so a row with
        a ``project_id`` and no ``directory`` would be unattributable in both
        directions. The legacy write dies with the column, in the drop PR.

        ``project_id`` is NEVER derived and never substituted: a caller that
        names none writes NONE (ADR-0227). A ``scope='global'`` block belongs to
        no project by construction, so it is stored unstamped — the same reason
        ``_canonical_dir`` returns ``None`` for it.
        """
        if char_limit is None:
            char_limit = _default_char_limit()

        # Validate name
        try:
            name = _validate_block_name(name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Validate char_limit
        hard_cap = _hard_char_limit()
        if char_limit > hard_cap:
            return {
                "ok": False,
                "error": f"char_limit {char_limit} exceeds hard cap of {hard_cap}",
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

        # A global block names no project — never stamp one on it (ADR-0227).
        canonical_pid = project_id if canonical_dir is not None else None

        # Check per-scope cap
        max_per_scope = _max_per_scope()
        count = self._count_blocks_in_scope(scope, canonical_dir, canonical_pid)
        if count >= max_per_scope:
            return {
                "ok": False,
                "error": (
                    f"scope cap reached: already {count} blocks in scope={scope!r} "
                    f"directory={canonical_dir!r} (max={max_per_scope}); "
                    "delete unused blocks first"
                ),
            }

        # Check uniqueness on the READ key, not the storage path — see get_block.
        existing = self.get_block(
            name, scope=scope, directory=canonical_dir, project_id=canonical_pid
        )
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

        pid_sql, pid_params = project_id_set_fragment(canonical_pid)

        if canonical_dir is None:
            self._q(
                "CREATE type::record('memory_block', $id) SET "
                "name = $name, scope = $scope, directory = NONE, "
                f"{pid_sql}, "
                "content = $content, char_limit = $char_limit, "
                "created_at = $ts, updated_at = $ts",
                {
                    "id": bid,
                    "name": name,
                    "scope": scope,
                    "content": content,
                    "char_limit": char_limit,
                    "ts": now,
                    **pid_params,
                },
            )
        else:
            self._q(
                "CREATE type::record('memory_block', $id) SET "
                "name = $name, scope = $scope, directory = $directory, "
                f"{pid_sql}, "
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
                    **pid_params,
                },
            )

        return {
            "id": bid,
            "name": name,
            "scope": scope,
            "directory": canonical_dir,
            "project_id": canonical_pid,
            "content": content,
            "char_limit": char_limit,
            "created_at": now,
            "updated_at": now,
        }

    @trace_span()
    def get_block(
        self,
        name: str,
        scope: str = "project",
        directory: str | None = None,
        project_id: str | None = None,
    ) -> dict | None:
        """Fetch a single block by name within the caller's scope, or None.

        C11 — **the lookup key must be at least as WIDE as the read key.**
        ``list_blocks`` selects on ``(project_id = $pid OR directory = $dir)``;
        if this stayed path-only, ``create_block``'s uniqueness check would miss
        a sibling row stored under a different path in the SAME project and
        create a second one. That is reachable today: ``block_create`` does not
        normalize worktree paths (only ``misc.py::checkpoint`` does), so a
        worktree and its main clone are distinct ``directory`` values under one
        resolved project — and ``restore()`` would then render the same block
        twice. Measured before the fix: two ``create_block`` calls, same name,
        same project, different directories → ``list_blocks`` returned 2 rows.

        Both keys go through ``_block_project_clause``, so the write's duplicate
        check and the read's selection cannot diverge again.
        """
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
            proj_sql, params = self._block_project_clause(canonical_dir, project_id)
            rows = self._q(
                f"SELECT * FROM memory_block WHERE name = $name AND scope = $scope AND {proj_sql} "
                "LIMIT 1",
                {"name": name, "scope": scope, **params},
            )

        if not rows:
            return None
        return self._row_to_dict(rows[0])

    @trace_span()
    def update_block(
        self,
        name: str,
        content: str,
        scope: str = "project",
        directory: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Replace block content. Validates against char_limit.

        Returns updated block dict on success.
        Returns {ok: False, error: "..."} on failure.

        C11: the lookup AND the UPDATE predicate both go through
        ``_block_project_clause``. Re-keying only the lookup would be worse than
        leaving both alone — ``get_block`` would find a row stored under another
        path in the same project and the ``UPDATE`` would then match nothing,
        returning success while writing no row.
        """
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        existing = self.get_block(name, scope=scope, directory=canonical_dir, project_id=project_id)
        if existing is None:
            return {
                "ok": False,
                "error": f"block {name!r} not found in scope={scope!r} directory={canonical_dir!r}",
            }

        char_limit = int(existing.get("char_limit") or _default_char_limit())
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
            proj_sql, proj_params = self._block_project_clause(canonical_dir, project_id)
            self._q(
                "UPDATE memory_block SET content = $content, updated_at = $ts "
                f"WHERE name = $name AND scope = $scope AND {proj_sql}",
                {
                    "name": name,
                    "scope": scope,
                    "content": content,
                    "ts": now,
                    **proj_params,
                },
            )

        return {
            **existing,
            "content": content,
            "updated_at": now,
        }

    @trace_span()
    def delete_block(
        self,
        name: str,
        scope: str = "project",
        directory: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Delete a block within the caller's scope. Idempotent — no error if missing.

        C11: keyed like the read. A delete narrower than ``list_blocks``' select
        would report success and leave the block still rendering into restore.
        """
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
            proj_sql, proj_params = self._block_project_clause(canonical_dir, project_id)
            self._q(
                f"DELETE memory_block WHERE name = $name AND scope = $scope AND {proj_sql}",
                {"name": name, "scope": scope, **proj_params},
            )

    @observe(tier="hot")
    def _block_project_clause(
        self, directory: str | None, project_id: str | None
    ) -> tuple[str, dict]:
        """Return the project-scope arm for ``memory_block`` + its params.

        C11 (0047 PR#40 §5) — **TWO ARMS, and the second one is transitional.**

        ``(project_id = $pid OR directory = $dir)``. The first arm is the one
        this car adds; the second is what keeps the HISTORICAL corpus readable.
        No backfill covers ``memory_block`` — ``project_backfill._TABLES`` is
        ``("memory", "wiki_page")`` and plan §8 names no step for this table —
        so a ``project_id``-only predicate would not be the degraded window
        §8 5b sanctions but permanent silent loss of the user's own curated
        blocks from every ``restore()``. The legacy arm dies with the column, in
        the drop PR.

        **This is NOT the ``project_id IS NONE`` sentinel
        ``build_project_scope_clause`` refuses.** That would admit EVERY
        unstamped row in the corpus into every project. This matches one
        specific legacy key the caller actually holds.

        **Spelled out here rather than delegated to
        ``build_project_scope_clause``** for the reason C13f recorded:
        that helper emits ``project_id = $p OR 'global' IN tags``, and
        ``memory_block`` has no ``tags`` column — importing it would attach an
        arm this table cannot answer.

        A caller with no ``project_id`` gets the path arm ALONE, never a
        widening one: C10g's ``_fetch_hot_memories`` leak is the generalised
        lesson that an ``else`` branch becomes the default when a key changes.
        """
        if project_id and directory:
            return "(project_id = $project_id OR directory = $directory)", {
                "project_id": project_id,
                "directory": directory,
            }
        if project_id:
            return "project_id = $project_id", {"project_id": project_id}
        return "directory = $directory", {"directory": directory}

    @trace_span()
    def list_blocks(
        self,
        scope: str | None = None,
        directory: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        """Return blocks filtered by scope and project (with a legacy path arm).

        scope=None: return both global and project blocks for the caller.
        scope='global': return only global blocks.
        scope='project': return only project blocks for the caller.

        C11: the project arm is ``_block_project_clause`` — ``project_id``
        first, the legacy ``directory`` second. Global blocks are unaffected:
        they carry neither key by construction.
        """
        if scope == "global":
            rows = self._q(
                "SELECT * FROM memory_block WHERE scope = 'global' AND directory IS NONE "
                "ORDER BY name ASC"
            )
        elif scope == "project":
            if not (directory or project_id):
                return []
            proj_sql, proj_params = self._block_project_clause(directory, project_id)
            rows = self._q(
                f"SELECT * FROM memory_block WHERE scope = 'project' AND {proj_sql} "
                "ORDER BY name ASC",
                proj_params,
            )
        else:
            # scope=None: return global + project for this caller
            if directory or project_id:
                proj_sql, proj_params = self._block_project_clause(directory, project_id)
                rows = self._q(
                    "SELECT * FROM memory_block WHERE "
                    "(scope = 'global' AND directory IS NONE) "
                    f"OR (scope = 'project' AND {proj_sql}) "
                    "ORDER BY scope ASC, name ASC",
                    proj_params,
                )
            else:
                rows = self._q(
                    "SELECT * FROM memory_block WHERE scope = 'global' AND directory IS NONE "
                    "ORDER BY name ASC"
                )

        return self._rows_to_dicts(rows)

    @trace_span()
    def replace_block(
        self,
        name: str,
        old_text: str,
        new_text: str,
        scope: str = "project",
        directory: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """String-replace old_text with new_text in block content.

        Errors if old_text is not found OR found more than once (force disambiguation).
        Returns updated block dict on success.
        Returns {ok: False, error: "..."} on failure.
        """
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        existing = self.get_block(name, scope=scope, directory=canonical_dir, project_id=project_id)
        if existing is None:
            return {
                "ok": False,
                "error": f"block {name!r} not found in scope={scope!r} directory={canonical_dir!r}",
            }

        content = existing.get("content", "")
        count = content.count(old_text)
        if count == 0:
            return {
                "ok": False,
                "error": (f"old_text {old_text!r} not found in block {name!r}; nothing replaced"),
            }
        if count > 1:
            return {
                "ok": False,
                "error": (
                    f"old_text {old_text!r} found {count} times in block {name!r} — "
                    "ambiguous; use a more specific old_text for disambiguation"
                ),
            }

        new_content = content.replace(old_text, new_text, 1)
        char_limit = int(existing.get("char_limit") or _default_char_limit())
        if len(new_content) > char_limit:
            return {
                "ok": False,
                "error": (
                    f"replacement content length {len(new_content)} exceeds char_limit {char_limit}; "
                    "shorten new_text"
                ),
            }

        return self.update_block(
            name, new_content, scope=scope, directory=canonical_dir, project_id=project_id
        )

    @trace_span()
    def append_block(
        self,
        name: str,
        text: str,
        scope: str = "project",
        directory: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Append text to block content with a newline separator.

        Respects the block's char_limit. Returns updated block dict on success.
        Returns {ok: False, error: "..."} on failure.
        """
        try:
            canonical_dir = _canonical_dir(scope, directory)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        existing = self.get_block(name, scope=scope, directory=canonical_dir, project_id=project_id)
        if existing is None:
            return {
                "ok": False,
                "error": f"block {name!r} not found in scope={scope!r} directory={canonical_dir!r}",
            }

        current = existing.get("content", "")
        new_content = (current + "\n" + text) if current else text
        char_limit = int(existing.get("char_limit") or _default_char_limit())
        if len(new_content) > char_limit:
            return {
                "ok": False,
                "error": (
                    f"appended content length {len(new_content)} exceeds char_limit {char_limit}; "
                    "shorten text or raise char_limit"
                ),
            }

        return self.update_block(
            name, new_content, scope=scope, directory=canonical_dir, project_id=project_id
        )
