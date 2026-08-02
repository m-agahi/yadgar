# SPDX-License-Identifier: Apache-2.0
"""LedgerCache — spine Car B read cache.

Fronts hot lookups (task_list, adr_list, agent_prompt_list) and invalidates
on write. Cache key is (kind, project_id) so different projects don't
collide. Invalidation is whole-flush per project on any write — fine at
single-digit writes per day per project.

D20: this cache fronts reads; writes still go through _LedgerMixin.
"""

from __future__ import annotations

import time
from typing import Any

from yadgar._shared.observability.observe import observe

_KEY_SEP = "\x00"


class LedgerCache:
    """Per-project, per-kind TTL cache for ledger reads."""

    def __init__(self, ttl_seconds: int = 60) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def _key(project_id: str, kind: str) -> str:
        return f"{kind}{_KEY_SEP}{project_id}"

    @observe(tier="stage")
    def _get(self, project_id: str, kind: str) -> Any:
        key = self._key(project_id, kind)
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    @observe(tier="stage")
    def _set(self, project_id: str, kind: str, value: Any, ttl_seconds: int | None = None) -> None:
        key = self._key(project_id, kind)
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl
        self._store[key] = (time.monotonic() + ttl, value)

    def get_task_list(self, project_id: str) -> Any:
        return self._get(project_id, "task_list")

    def set_task_list(self, project_id: str, value: Any, ttl_seconds: int | None = None) -> None:
        self._set(project_id, "task_list", value, ttl_seconds)

    def get_adr_list(self, project_id: str) -> Any:
        return self._get(project_id, "adr_list")

    def set_adr_list(self, project_id: str, value: Any, ttl_seconds: int | None = None) -> None:
        self._set(project_id, "adr_list", value, ttl_seconds)

    def get_agent_prompt_list(self, project_id: str) -> Any:
        return self._get(project_id, "agent_prompt_list")

    def set_agent_prompt_list(
        self, project_id: str, value: Any, ttl_seconds: int | None = None
    ) -> None:
        self._set(project_id, "agent_prompt_list", value, ttl_seconds)

    def invalidate(self, project_id: str) -> None:
        """Drop all cached entries for `project_id`."""
        prefix = f"{_KEY_SEP}{project_id}"
        self._store = {k: v for k, v in self._store.items() if not k.endswith(prefix)}
