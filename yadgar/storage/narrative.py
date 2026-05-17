"""Narrative, astrocyte, prospective-memory, and derived-belief table CRUD.

_NarrativeMixin provides:
  - Prospective memory insert/query/trigger/deactivate
  - Narrative entry insert/query
  - Astrocyte process insert/query/update
  - Derived belief insert/query
"""

import logging

_log = logging.getLogger(__name__)


class _NarrativeMixin:
    """Narrative and belief tables — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Prospective Memories

    def insert_prospective_memory(self, pm: dict) -> int:
        now = self._now_iso()
        pid = self._next_id("prospective_memory")
        self._q(
            "CREATE type::record('prospective_memory', $id) SET "
            "content = $content, trigger_condition = $trigger_condition, "
            "trigger_type = $trigger_type, target_directory = $target_directory, "
            "is_active = $is_active, created_at = $created_at, "
            "triggered_at = $triggered_at, triggered_count = $triggered_count",
            {
                "id": pid,
                "content": pm["content"],
                "trigger_condition": pm["trigger_condition"],
                "trigger_type": pm["trigger_type"],
                "target_directory": pm.get("target_directory"),
                "is_active": bool(pm.get("is_active", True)),
                "created_at": pm.get("created_at", now),
                "triggered_at": pm.get("triggered_at"),
                "triggered_count": pm.get("triggered_count", 0),
            },
        )
        return pid

    def get_active_prospective_memories(self) -> list[dict]:
        rows = self._q("SELECT * FROM prospective_memory WHERE is_active = true")
        return self._rows_to_dicts(rows)

    def trigger_prospective_memory(self, pm_id: int):
        now = self._now_iso()
        self._q(
            "UPDATE type::record('prospective_memory', $id) SET "
            "triggered_at = $now, triggered_count = triggered_count + 1",
            {"id": pm_id, "now": now},
        )

    def deactivate_prospective_memory(self, pm_id: int) -> None:
        self._q(
            "UPDATE type::record('prospective_memory', $id) SET is_active = false",
            {"id": pm_id},
        )

    # ------------------------------------------------------------------ Narrative Entries

    def insert_narrative_entry(self, entry: dict) -> int:
        now = self._now_iso()
        nid = self._next_id("narrative_entry")
        self._q(
            "CREATE type::record('narrative_entry', $id) SET "
            "directory_context = $dir, summary = $summary, "
            "period_start = $period_start, period_end = $period_end, "
            "key_decisions = $key_decisions, key_events = $key_events, "
            "created_at = $created_at, heat = $heat",
            {
                "id": nid,
                "dir": entry["directory_context"],
                "summary": entry["summary"],
                "period_start": entry["period_start"],
                "period_end": entry["period_end"],
                "key_decisions": entry.get("key_decisions", []),
                "key_events": entry.get("key_events", []),
                "created_at": entry.get("created_at", now),
                "heat": entry.get("heat", 1.0),
            },
        )
        return nid

    def get_narratives_for_directory(self, directory: str, limit: int = 10) -> list[dict]:
        rows = self._q(
            "SELECT * FROM narrative_entry WHERE directory_context = $dir "
            "ORDER BY period_end DESC LIMIT $lim",
            {"dir": directory, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Astrocyte Processes

    def insert_astrocyte_process(self, proc: dict) -> int:
        now = self._now_iso()
        aid = self._next_id("astrocyte_process")
        self._q(
            "CREATE type::record('astrocyte_process', $id) SET "
            "name = $name, domain = $domain, specialization = $specialization, "
            "memory_ids = $memory_ids, entity_ids = $entity_ids, "
            "heat = $heat, created_at = $created_at, last_active = $last_active",
            {
                "id": aid,
                "name": proc["name"],
                "domain": proc["domain"],
                "specialization": proc.get("specialization", ""),
                "memory_ids": proc.get("memory_ids", []),
                "entity_ids": proc.get("entity_ids", []),
                "heat": proc.get("heat", 1.0),
                "created_at": proc.get("created_at", now),
                "last_active": proc.get("last_active", now),
            },
        )
        return aid

    def get_astrocyte_processes(self) -> list[dict]:
        rows = self._q("SELECT * FROM astrocyte_process ORDER BY heat DESC")
        return self._rows_to_dicts(rows)

    def update_astrocyte_process(self, proc_id: int, updates: dict):
        allowed = {
            "name",
            "domain",
            "specialization",
            "memory_ids",
            "entity_ids",
            "heat",
            "last_active",
        }
        fields = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            fields[k] = v
        if not fields:
            return
        if "last_active" not in fields:
            fields["last_active"] = self._now_iso()
        params = {"id": proc_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._q(
            f"UPDATE type::record('astrocyte_process', $id) SET {', '.join(set_parts)}",
            params,
        )

    # ------------------------------------------------------------------ Derived Beliefs

    def insert_belief(
        self,
        belief_type: str,
        subject: str,
        content: str,
        evidence_memory_ids: list[int] | None = None,
        confidence: float = 0.5,
        embedding: bytes | None = None,
        embedding_model: str | None = None,
        directory_context: str | None = None,
    ) -> int:
        now = self._now_iso()
        evidence = evidence_memory_ids or []
        emb_floats = self._bytes_to_floats(embedding) if embedding else None
        bid = self._next_id("derived_belief")
        self._q(
            "CREATE type::record('derived_belief', $id) SET "
            "belief_type = $bt, subject = $subject, content = $content, "
            "evidence_memory_ids = $evids, confidence = $conf, "
            "embedding = $emb, embedding_model = $em, "
            "created_at = $now, updated_at = $now, directory_context = $dc",
            {
                "id": bid,
                "bt": belief_type,
                "subject": subject,
                "content": content,
                "evids": evidence,
                "conf": confidence,
                "emb": emb_floats,
                "em": embedding_model,
                "now": now,
                "dc": directory_context,
            },
        )
        return bid

    def search_beliefs_fts(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._q(
            "SELECT * FROM derived_belief WHERE subject @@ $q "
            "OR belief_type @@ $q OR content @@ $q LIMIT $lim",
            {"q": query, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def get_beliefs_for_subject(
        self, subject: str, directory_context: str | None = None
    ) -> list[dict]:
        if directory_context is not None:
            rows = self._q(
                "SELECT * FROM derived_belief WHERE subject = $subj AND directory_context = $dc",
                {"subj": subject, "dc": directory_context},
            )
        else:
            rows = self._q(
                "SELECT * FROM derived_belief WHERE subject = $subj",
                {"subj": subject},
            )
        return self._rows_to_dicts(rows)
