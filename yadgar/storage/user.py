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

from yadgar.tracing import trace_span

_log = logging.getLogger(__name__)


class _UserMixin:
    """User profiles and thermodynamics — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Thermodynamics

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

    def insert_profile(
        self,
        entity_name: str,
        attribute_type: str,
        attribute_key: str,
        attribute_value: str,
        memory_id: int | None = None,
        confidence: float = 0.5,
        directory_context: str | None = None,
    ) -> int:
        """Insert or supersede a user_profile fact (v5.29.0 bi-temporal).

        Pivots from UPSERT-in-place to close-and-insert when attribute_value
        changes or confidence delta >= PROFILE_BITEMPORAL_VERSION_DELTA (default 0.05).
        Minor confidence drift folds into an in-place update to bound row growth.

        Uniqueness on currently-valid rows is enforced application-side:
        SurrealDB v3 does not support partial indexes (DEFINE INDEX ... WHERE).
        Migration 010 drops the old unconditional UNIQUE index.
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
                from yadgar.storage.bitemporal import invalidate_edge

                invalidate_edge(self, "user_profile", pid)

                new_pid = self._next_id("user_profile")
                self._q(
                    "BEGIN TRANSACTION;\n"
                    "CREATE type::record('user_profile', $id) SET "
                    "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
                    "attribute_value = $av, evidence_memory_ids = $evids, "
                    "confidence = $conf, created_at = $now, updated_at = $now, "
                    "directory_context = $dc, valid_from = $now, valid_until = NONE;\n"
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
        self._q(
            "BEGIN TRANSACTION;\n"
            "CREATE type::record('user_profile', $id) SET "
            "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
            "attribute_value = $av, evidence_memory_ids = $evids, "
            "confidence = $conf, created_at = $now, updated_at = $now, "
            "directory_context = $dc, valid_from = $now, valid_until = NONE;\n"
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
            },
        )
        return pid

    @trace_span("storage.user.search_profiles_fts")
    def search_profiles_fts(
        self, query: str, limit: int = 10, include_invalidated: bool = False
    ) -> list[dict]:
        """FTS over user_profile.

        include_invalidated (v5.29.0): when False (default), excludes
        superseded rows (valid_until IS NOT NONE).
        """
        validity_clause = "" if include_invalidated else " AND valid_until IS NONE"
        rows = self._q(
            f"SELECT * FROM user_profile WHERE (entity_name @@ $q "
            f"OR attribute_type @@ $q OR attribute_key @@ $q OR attribute_value @@ $q)"
            f"{validity_clause} LIMIT $lim",
            {"q": query, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def get_profiles_for_entity(
        self,
        entity_name: str,
        directory_context: str | None = None,
        include_invalidated: bool = False,
    ) -> list[dict]:
        """Return user_profile rows for an entity.

        include_invalidated (v5.29.0): when False (default), returns only
        currently-valid rows (valid_until IS NONE).
        """
        validity_clause = "" if include_invalidated else " AND valid_until IS NONE"
        if directory_context is not None:
            rows = self._q(
                f"SELECT * FROM user_profile WHERE entity_name = $en "
                f"AND directory_context = $dc{validity_clause}",
                {"en": entity_name, "dc": directory_context},
            )
        else:
            rows = self._q(
                f"SELECT * FROM user_profile WHERE entity_name = $en{validity_clause}",
                {"en": entity_name},
            )
        return self._rows_to_dicts(rows)
