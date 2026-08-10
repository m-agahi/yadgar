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

    # C4b (0047 PR#40 §5): the enqueue-time project_id, resolved by the core
    # ``memorize`` tool — the only participant that can see the session — and
    # carried through the drainer replay to ``insert_memory``'s
    # ``caller_value``. Stamped INDEPENDENTLY of ``context``: a memory whose
    # directory is collapsed for reach still keeps its real owner (§1.4,
    # ownership ≠ reach; same treatment C3 gave ``WikiAddOptions``). ``None``
    # only for a legacy payload enqueued before this car; C5 makes that raise.
    project_id: str | None = None

    # --- Derived (set during phases) ---
    computed_valid_until: str | None = None
    embedding: list[float] | None = None
    provenance_agent_resolved: str = "default"

    # Thermo scores (thermo.compute_surprise — HEAT BOOST only, NOT the gate's surprisal)
    surprise: float = 0.0
    importance: float = 0.5
    valence: float = 0.0
    initial_heat: float = 1.0

    # Gate result (from write gate phase)
    gate_result: dict | None = None

    # Shadow gate fields (v5.73.0).
    # gate_surprisal: the surprisal returned by _write_gate.should_store() — distinct from
    #   ctx.surprise (thermo score for heat boost). None when gate is disabled.
    # would_reject: True if gate WOULD reject at WRITE_GATE_SHADOW_THRESHOLD.
    #   Nothing is actually dropped — WRITE_GATE_THRESHOLD stays 0.0.
    gate_surprisal: float | None = None
    would_reject: bool | None = None

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

    # Car 2 (Part B) — memorize soft-gate (non-blocking near-duplicate surface).
    # Populated by phase_soft_gate for DURABLE writes only; attached to the
    # response by _phase_post_write. Empty list = no near-duplicates / gate off.
    near_duplicates: list[dict] = field(default_factory=list)
