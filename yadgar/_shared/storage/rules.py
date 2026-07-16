"""Memory-rule, archive, and transition table CRUD.

_RulesMixin provides:
  - Memory rule insert/query/update/delete
  - Memory archive insert/query
  - Memory transition insert/query/update
  - SR coordinate helpers (on the memory table; grouped here as transition-adjacent)
"""

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span

_log = logging.getLogger(__name__)


class _RulesMixin:
    """Memory rules, archives, and transitions — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Memory Rules

    @trace_span()
    def insert_rule(self, rule: dict) -> int:
        now = self._now_iso()
        rid = self._next_id("memory_rule")
        self._q(
            "CREATE type::record('memory_rule', $id) SET "
            "rule_type = $rule_type, scope = $scope, scope_value = $scope_value, "
            "condition = $condition, action = $action, priority = $priority, "
            "created_at = $created_at, is_active = $is_active",
            {
                "id": rid,
                "rule_type": rule["rule_type"],
                "scope": rule["scope"],
                "scope_value": rule.get("scope_value"),
                "condition": rule["condition"],
                "action": rule["action"],
                "priority": rule.get("priority", 0),
                "created_at": rule.get("created_at", now),
                "is_active": bool(rule.get("is_active", True)),
            },
        )
        return rid

    @trace_span()
    def get_rules_for_scope(self, scope: str, scope_value: str | None = None) -> list[dict]:
        if scope == "global":
            rows = self._q(
                "SELECT * FROM memory_rule WHERE scope = 'global' AND is_active = true "
                "ORDER BY priority DESC",
            )
        else:
            rows = self._q(
                "SELECT * FROM memory_rule WHERE scope = $scope AND scope_value = $sv "
                "AND is_active = true ORDER BY priority DESC",
                {"scope": scope, "sv": scope_value},
            )
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def update_rule(self, rule_id: int, updates: dict):
        allowed = {
            "rule_type",
            "scope",
            "scope_value",
            "condition",
            "action",
            "priority",
            "is_active",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return
        params = {"id": rule_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._q(
            f"UPDATE type::record('memory_rule', $id) SET {', '.join(set_parts)}",
            params,
        )

    def delete_rule(self, rule_id: int):
        self._q(
            "DELETE type::record('memory_rule', $id)",
            {"id": rule_id},
        )

    def get_all_active_rules_by_scope(self, scope: str) -> list[dict]:
        """Return all active rules for a given scope type (no scope_value filtering).

        Used by the rules engine to do its own prefix/glob matching in Python.
        """
        rows = self._q(
            "SELECT * FROM memory_rule WHERE scope = $scope AND is_active = true "
            "ORDER BY priority DESC",
            {"scope": scope},
        )
        return self._rows_to_dicts(rows)

    def get_rule(self, rule_id: int) -> dict | None:
        """Fetch a single rule by ID."""
        rid = int(rule_id)
        rows = self._q(f"SELECT * FROM memory_rule:{rid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_all_active_rules(self) -> list[dict]:
        """Return all active rules, sorted by scope then priority descending."""
        rows = self._q(
            "SELECT * FROM memory_rule WHERE is_active = true ORDER BY scope, priority DESC"
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Memory Archives

    @observe(tier="stage")
    def insert_archive(self, archive: dict) -> int:
        now = self._now_iso()
        aid = self._next_id("memory_archive")
        emb = archive.get("embedding")
        emb_floats = self._bytes_to_floats(emb) if emb else None
        self._q(
            "CREATE type::record('memory_archive', $id) SET "
            "original_memory_id = $orig, content = $content, embedding = $emb, "
            "archived_at = $archived_at, mismatch_score = $mismatch_score, "
            "archive_reason = $archive_reason",
            {
                "id": aid,
                "orig": archive["original_memory_id"],
                "content": archive["content"],
                "emb": emb_floats,
                "archived_at": archive.get("archived_at", now),
                "mismatch_score": archive.get("mismatch_score", 0.0),
                "archive_reason": archive.get("archive_reason", ""),
            },
        )
        return aid

    def get_archives_for_memory(self, memory_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory_archive WHERE original_memory_id = $mid "
            "ORDER BY archived_at DESC",
            {"mid": memory_id},
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Memory Transitions

    @observe(tier="stage")
    def insert_transition(self, transition: dict) -> int:
        from_id = transition["from_memory_id"]
        to_id = transition["to_memory_id"]
        if not isinstance(from_id, int) or not isinstance(to_id, int):
            raise ValueError(
                f"insert_transition: from_memory_id and to_memory_id must be int, "
                f"got from={from_id!r} to={to_id!r}"
            )
        existing = self.get_transition(transition["from_memory_id"], transition["to_memory_id"])
        if existing is not None:
            raise ValueError(
                f"Transition ({transition['from_memory_id']}, {transition['to_memory_id']}) already exists"
            )
        now = self._now_iso()
        tid = self._next_id("memory_transition")
        self._q(
            "CREATE type::record('memory_transition', $id) SET "
            "from_memory_id = $from_id, to_memory_id = $to_id, count = $count, "
            "last_transition = $last_transition, session_id = $session_id",
            {
                "id": tid,
                "from_id": transition["from_memory_id"],
                "to_id": transition["to_memory_id"],
                "count": transition.get("count", 1),
                "last_transition": transition.get("last_transition", now),
                "session_id": transition.get("session_id", ""),
            },
        )
        return tid

    def get_transition(self, from_id: int, to_id: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM memory_transition "
            "WHERE from_memory_id = $from_id AND to_memory_id = $to_id LIMIT 1",
            {"from_id": from_id, "to_id": to_id},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def increment_transition(self, from_id: int, to_id: int):
        now = self._now_iso()
        self._q(
            "UPDATE memory_transition SET count = count + 1, last_transition = $now "
            "WHERE from_memory_id = $from_id AND to_memory_id = $to_id",
            {"now": now, "from_id": from_id, "to_id": to_id},
        )

    def get_transitions_from(self, memory_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory_transition WHERE from_memory_id = $mid ORDER BY count DESC",
            {"mid": memory_id},
        )
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def get_all_transitions(self, limit: int = 0) -> list[dict]:
        """Return all memory_transition rows.

        viz-render-perf (Car A): optional ``limit`` (0/-1 = unlimited). The
        deterministic ``ORDER BY count DESC`` is added ONLY when limiting so a
        capped subset is the strongest edges, not a random slice; the unlimited
        path stays byte-identical for non-viz callers (sr_session/cls/cognitive_map).
        """
        sql = "SELECT from_memory_id, to_memory_id, count FROM memory_transition"
        if limit and limit > 0:
            sql += " ORDER BY count DESC LIMIT $lim"
            rows = self._q(sql, {"lim": limit})
        else:
            rows = self._q(sql)
        # No id to extract; pass through as-is (no embedding fields)
        return [dict(r) for r in rows]
