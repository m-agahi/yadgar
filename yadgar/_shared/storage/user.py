"""User profile and thermodynamics table CRUD.

_UserMixin provides:
  - User profile insert/search/query (bi-temporal close-and-insert from v5.29.0)
  - Memory thermodynamics helpers (update_memory_scores, update_memory_metamemory,
    get_memories_in_time_window)
"""

import json
import logging
import os
from datetime import datetime

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.storage._project_id_writer import project_id_set_fragment

_log = logging.getLogger(__name__)


# ── C11 (0047 PR#40 §5): the profile supersession key stays on directory_context
#
# C13f left this call open and C11 closed it as a DEFERRAL, with the reason
# recorded here rather than in ``insert_profile``'s docstring (the I13 HARD
# fn_loc cap).
#
# ``insert_profile``'s currently-valid lookup keys on
# ``(entity_name, attribute_type, attribute_key, directory_context)``, so two
# projects writing the same profile key under ONE directory (a worktree, a
# second clone) supersede each other. Migration 033 declared
# ``user_profile.project_id``, so condition 1 of the two-condition rename rule
# is now met — but this predicate is not a filter, it is the SUPERSESSION key,
# and changing it changes which row gets INVALIDATED. Both candidate shapes are
# wrong today, in opposite directions:
#
#   * ``AND project_id = $pid`` hides every pre-C13f row (which carries none)
#     from the check, so the next write creates a SECOND currently-valid row for
#     the same key and breaks the application-enforced uniqueness invariant this
#     query exists to hold.
#   * The transitional two-arm form C11 uses elsewhere
#     (``project_id = $pid OR <legacy> = $dc``) would match — and then
#     invalidate — another project's row in the same directory: the very
#     collision the change was meant to prevent.
#
# So this needs the backfill, not a predicate edit. It is resolved in the drop
# PR, where ``directory_context`` leaves the key entirely.


class _UserMixin:
    """User profiles and thermodynamics — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Thermodynamics

    @observe(tier="stage")
    def update_memory_scores(
        self,
        memory_id: int,
        surprise_score: float | None = None,
        importance: float | None = None,
        emotional_valence: float | None = None,
    ):
        fields = {}
        if surprise_score is not None:
            fields["surprise_score"] = surprise_score
        if importance is not None:
            fields["importance"] = importance
        if emotional_valence is not None:
            fields["emotional_valence"] = emotional_valence
        if not fields:
            return
        params = {"id": memory_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._q(
            f"UPDATE type::record('memory', $id) SET {', '.join(set_parts)}",
            params,
        )

    @observe(tier="stage")
    def update_memory_metamemory(
        self,
        memory_id: int,
        access_count: int,
        useful_count: int,
        confidence: float,
        access_count_since_decay: int | None = None,
    ):
        params: dict = {
            "id": memory_id,
            "ac": access_count,
            "uc": useful_count,
            "conf": confidence,
        }
        set_clause = "access_count = $ac, useful_count = $uc, confidence = $conf"
        if access_count_since_decay is not None:
            set_clause += ", access_count_since_decay = $acd"
            params["acd"] = access_count_since_decay
        self._q(
            f"UPDATE type::record('memory', $id) SET {set_clause}",
            params,
        )

    @observe(tier="stage")
    def get_memories_in_time_window(self, center_time: str, window_minutes: int) -> list[dict]:
        """Return memories created within window_minutes of center_time."""
        # Parse center_time and compute window bounds in Python (no julianday in SurrealDB)
        try:
            center_dt = datetime.fromisoformat(center_time)
        except ValueError:
            return []
        from datetime import timedelta

        delta = timedelta(minutes=window_minutes)
        start = (center_dt - delta).isoformat()
        end = (center_dt + delta).isoformat()
        rows = self._q(
            "SELECT * FROM memory WHERE heat > 0 AND created_at >= $start AND created_at <= $end",
            {"start": start, "end": end},
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ User Profiles

    @observe(tier="stage")
    def insert_profile(
        self,
        entity_name: str,
        attribute_type: str,
        attribute_key: str,
        attribute_value: str,
        memory_id: int | None = None,
        confidence: float = 0.5,
        directory_context: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Insert or supersede a user_profile fact (v5.29.0 bi-temporal).

        Pivots from UPSERT-in-place to close-and-insert when attribute_value
        changes or confidence delta >= PROFILE_BITEMPORAL_VERSION_DELTA (default 0.05).
        Minor confidence drift folds into an in-place update to bound row growth.

        Uniqueness on currently-valid rows is enforced application-side:
        SurrealDB v3 does not support partial indexes (DEFINE INDEX ... WHERE).
        Migration 010 drops the old unconditional UNIQUE index.

        Car C13f (0047 §5): ``project_id`` is STAMPED HERE so the read path can
        scope by it. ``user_profile`` is SCHEMALESS — no ``DEFINE FIELD`` covers
        it (the plan's §C11 table names it among exactly those) — so the column
        needs no migration to exist; writing it is enough. That is deliberately
        NOT a migration: §C11 owns declaring the type + index for the remaining
        directory-bearing tables, and a schema statement here would collide with
        that car. Rows written before this parameter existed carry no
        ``project_id`` and are correctly invisible to a scoped read, which is the
        same degraded-window trade C7 already made for ``memory``/``wiki_page``
        (ADR-0227: zero rows beats a guessed identity).

        C11 (0047 PR#40 §5) deliberately did NOT add ``project_id`` to the
        supersession key — see ``_C11_PROFILE_KEY_DEFERRAL`` above this class
        for why both candidate shapes are unsafe until the rows carry one.
        """
        now = self._now_iso()
        delta = float(os.environ.get("PROFILE_BITEMPORAL_VERSION_DELTA", "0.05"))

        # Find currently-valid row for this key (application-side unique check)
        existing = self._q(
            "SELECT id, attribute_value, confidence, evidence_memory_ids FROM user_profile "
            "WHERE entity_name = $en AND attribute_type = $at AND attribute_key = $ak "
            "AND directory_context = $dc AND valid_until IS NONE LIMIT 1",
            {
                "en": entity_name,
                "at": attribute_type,
                "ak": attribute_key,
                "dc": directory_context,
            },
        )

        if existing:
            row = existing[0]
            pid = self._extract_id(row["id"])
            old_value = row.get("attribute_value", "")
            old_conf = float(row.get("confidence", 0.5))
            evidence = row.get("evidence_memory_ids", [])
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            if memory_id is not None and memory_id not in evidence:
                evidence.append(memory_id)

            value_changed = old_value != attribute_value
            conf_delta = abs(confidence - old_conf)
            needs_supersession = value_changed or conf_delta >= delta

            if needs_supersession:
                from yadgar._shared.storage.bitemporal import invalidate_edge

                invalidate_edge(self, "user_profile", pid)

                new_pid = self._next_id("user_profile")
                pid_sql, pid_params = project_id_set_fragment(project_id)
                self._q(
                    "BEGIN TRANSACTION;\n"
                    "CREATE type::record('user_profile', $id) SET "
                    "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
                    "attribute_value = $av, evidence_memory_ids = $evids, "
                    "confidence = $conf, created_at = $now, updated_at = $now, "
                    f"directory_context = $dc, {pid_sql}, "
                    "valid_from = $now, valid_until = NONE;\n"
                    "COMMIT TRANSACTION",
                    {
                        "id": new_pid,
                        "en": entity_name,
                        "at": attribute_type,
                        "ak": attribute_key,
                        "av": attribute_value,
                        "evids": evidence,
                        "conf": confidence,
                        "now": now,
                        "dc": directory_context,
                        **pid_params,
                    },
                )
                return new_pid

            # Below threshold: in-place update only (merge evidence, bump updated_at)
            self._q(
                "BEGIN TRANSACTION;\n"
                "UPDATE type::record('user_profile', $id) SET "
                "evidence_memory_ids = $evids, updated_at = $now;\n"
                "COMMIT TRANSACTION",
                {
                    "id": pid,
                    "evids": evidence,
                    "now": now,
                },
            )
            return pid

        # No existing currently-valid row — fresh insert
        evidence = [memory_id] if memory_id is not None else []
        pid = self._next_id("user_profile")
        pid_sql, pid_params = project_id_set_fragment(project_id)
        self._q(
            "BEGIN TRANSACTION;\n"
            "CREATE type::record('user_profile', $id) SET "
            "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
            "attribute_value = $av, evidence_memory_ids = $evids, "
            "confidence = $conf, created_at = $now, updated_at = $now, "
            f"directory_context = $dc, {pid_sql}, "
            "valid_from = $now, valid_until = NONE;\n"
            "COMMIT TRANSACTION",
            {
                "id": pid,
                "en": entity_name,
                "at": attribute_type,
                "ak": attribute_key,
                "av": attribute_value,
                "evids": evidence,
                "conf": confidence,
                "now": now,
                "dc": directory_context,
                **pid_params,
            },
        )
        return pid

    @trace_span()
    def search_profiles_fts(
        self,
        query: str,
        limit: int = 10,
        include_invalidated: bool = False,
        project_id: str | None = None,
    ) -> list[dict]:
        """FTS over user_profile, scoped to *project_id* when one is supplied.

        include_invalidated (v5.29.0): when False (default), excludes
        superseded rows (valid_until IS NOT NONE).

        Car C13f (0047 §5): ``project_id`` is the SCOPE, and it is enforced HERE
        rather than after the rows come back. Before this, the caller's project
        reached ``_search_profiles_and_beliefs`` and was never read, so profiles
        were searched CORPUS-WIDE and every project's structured knowledge was a
        candidate for every other project's recall.

        ONE ARM, NOT TWO. ``build_project_scope_clause`` emits
        ``project_id = $p OR 'global' IN tags``; ``user_profile`` HAS NO ``tags``
        COLUMN (see the CREATE in ``insert_profile``), so the reach arm would
        test a field that does not exist. The predicate is therefore the project
        arm alone — the honest predicate for a table with no cross-project reach
        concept — and it is spelled out here instead of imported so it cannot
        silently acquire an arm this table cannot answer.

        A ``None``/empty *project_id* means NO filtering, matching
        ``build_project_scope_clause``'s empty-fragment case for daemon-internal
        and legacy callers. Unstamped rows (``project_id`` absent) match no
        project, which is the same treatment memories and wiki pages get.
        """
        validity_clause = "" if include_invalidated else " AND valid_until IS NONE"
        params: dict = {"q": query, "lim": limit}
        scope_clause = ""
        if project_id:
            scope_clause = " AND project_id = $pid"
            params["pid"] = project_id
        rows = self._q(
            f"SELECT * FROM user_profile WHERE (entity_name @@ $q "
            f"OR attribute_type @@ $q OR attribute_key @@ $q OR attribute_value @@ $q)"
            f"{validity_clause}{scope_clause} LIMIT $lim",
            params,
        )
        return self._rows_to_dicts(rows)
