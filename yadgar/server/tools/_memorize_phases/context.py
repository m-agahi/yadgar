"""MemorizeContext dataclass — shared state threaded through memorize phases."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemorizeContext:
    """Mutable state container threaded through all memorize() phases.

    Inputs are set at construction and frozen after _phase_validate().
    Derived fields are populated by subsequent phases.
    """

    # --- Inputs (set at construction, frozen after validate) ---
    content: str
    context: str
    tags: list[str]
    is_protected: bool
    provenance_agent: str | None
    tier: str | None
    valid_until: str | None
    ttl_days: int | None
    reason: str
    branch_hint: str | None

    # --- Derived (set during phases) ---
    computed_valid_until: str | None = None
    resolved_branch: str | None = None
    embedding: list[float] | None = None
    provenance_agent_resolved: str = "default"

    # Thermo scores
    surprise: float = 0.0
    importance: float = 0.5
    valence: float = 0.0
    initial_heat: float = 1.0

    # Gate result (from write gate phase)
    gate_result: dict | None = None

    # Contextual prefix for richer embedding semantics
    contextual_prefix: str | None = None

    # Storage result
    memory_id: int | None = None
    curation_action: str = "created"

    # Post-write results (populated by _phase_post_write)
    triggered_memories: list[dict] = field(default_factory=list)
    engram_result: dict | None = None
    auto_protected: bool = False
    related_context: list[dict] = field(default_factory=list)
