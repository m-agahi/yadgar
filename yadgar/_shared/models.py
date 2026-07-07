from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from yadgar._shared.observability.observe import observe


class Episode(BaseModel):
    id: int | None = None
    session_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    directory: str
    raw_content: str
    overlap_start: int | None = None
    overlap_end: int | None = None


class Entity(BaseModel):
    id: int | None = None
    name: str
    type: Literal["file", "function", "variable", "dependency", "decision", "error", "solution"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_accessed: datetime = Field(default_factory=lambda: datetime.now(UTC))
    heat: float = 1.0
    archived: bool = False
    # Watermark for the last heat-decay pass; decay spans now - max(last_accessed,
    # last_decay_at) so repeated cycles don't compound over-decay. None = never decayed.
    last_decay_at: datetime | None = None
    # v2 fields
    causal_weight: float = 0.0
    domain: str | None = None


class Relationship(BaseModel):
    id: int | None = None
    source_entity_id: int
    target_entity_id: int
    relationship_type: str  # co_occurrence, imports, calls, debugged_with, decided_to_use, caused_by, resolved_by, preceded_by, derived_from
    weight: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_reinforced: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # v2 fields
    event_time: datetime | None = None
    record_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_causal: bool = False
    confidence: float = 1.0


class Memory(BaseModel):
    id: int | None = None
    content: str
    embedding: bytes | None = None
    tags: list[str] = Field(default_factory=list)
    source_episode_id: int | None = None
    directory_context: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_accessed: datetime = Field(default_factory=lambda: datetime.now(UTC))
    heat: float = 1.0
    # Watermark for the last heat-decay pass; decay spans now - max(last_accessed,
    # last_decay_at) so repeated cycles don't compound over-decay. None = never decayed.
    last_decay_at: datetime | None = None
    is_stale: bool = False
    file_hash: str | None = None
    # v2 fields
    surprise_score: float = 0.0
    importance: float = 0.5
    emotional_valence: float = 0.0
    confidence: float = 1.0
    access_count: int = 0
    useful_count: int = 0
    embedding_model: str | None = None
    contextual_prefix: str | None = None
    cluster_id: int | None = None
    is_prospective: bool = False
    trigger_condition: str | None = None
    narrative_weight: float = 0.0
    compressed: bool = False
    # v3 frontier fields
    plasticity: float = 1.0  # Spikes on access, decays with ~6h half-life (reconsolidation)
    stability: float = 0.0  # Increases with successful retrievals (reconsolidation)
    excitability: float = 1.0  # Competitive allocation score (engram)
    last_excitability_update: datetime | None = None  # For excitability decay calc
    store_type: str = "episodic"  # "episodic" or "semantic" (CLS dual-store)
    compression_level: int = 0  # 0=full, 1=gist, 2=tag (rate-distortion)
    original_content: str | None = None  # Full content preserved before compression
    sr_x: float = 0.0  # Successor representation x coordinate (cognitive map)
    sr_y: float = 0.0  # Successor representation y coordinate (cognitive map)
    reconsolidation_count: int = 0  # Number of times reconsolidated
    last_reconsolidated: datetime | None = None  # Timestamp of last reconsolidation
    provenance_agent: str = "default"  # Which agent created this memory (CRDT)
    vector_clock: str = "{}"  # JSON vector clock for CRDT conflict resolution
    is_protected: bool = False  # Protected from modification and compression


class ConsolidationLog(BaseModel):
    id: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    memories_added: int = 0
    memories_updated: int = 0
    memories_archived: int = 0
    memories_deleted: int = 0
    duration_ms: int = 0


class FileHash(BaseModel):
    id: int | None = None
    filepath: str
    hash: str
    last_checked: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryStats(BaseModel):
    total_memories: int
    active_count: int
    archived_count: int
    stale_count: int
    avg_heat: float
    last_consolidation: datetime | None = None


# -- v2 models --


class MemoryCluster(BaseModel):
    id: int | None = None
    name: str
    level: int = 0  # 0=leaf, 1=intermediate, 2=root
    parent_cluster_id: int | None = None
    summary: str = ""
    centroid_embedding: bytes | None = None
    member_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    heat: float = 1.0


class ProspectiveMemory(BaseModel):
    id: int | None = None
    content: str
    trigger_condition: str
    trigger_type: Literal["directory_match", "keyword_match", "entity_match", "time_based"]
    target_directory: str | None = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    triggered_at: datetime | None = None
    triggered_count: int = 0


class NarrativeEntry(BaseModel):
    id: int | None = None
    directory_context: str
    summary: str
    period_start: datetime
    period_end: datetime
    key_decisions: list[str] = Field(default_factory=list)
    key_events: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    heat: float = 1.0


class AstrocyteProcess(BaseModel):
    id: int | None = None
    name: str
    domain: str
    specialization: str = ""
    memory_ids: list[int] = Field(default_factory=list)
    entity_ids: list[int] = Field(default_factory=list)
    heat: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active: datetime = Field(default_factory=lambda: datetime.now(UTC))


# -- v3 frontier models --


class MemoryRule(BaseModel):
    id: int | None = None
    rule_type: Literal[
        "hard", "soft", "write_block", "write_redact"
    ]  # hard/soft = read-path; write_block/write_redact = write-path
    scope: Literal["global", "directory", "file"]  # where rule applies
    scope_value: str | None = None  # directory path or file pattern for scoped rules
    condition: str  # e.g. "language == typescript", "tag contains architecture"
    action: str  # e.g. "filter", "boost:0.3", "penalty:0.2"
    priority: int = 0  # higher = applied first
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True


class MemoryArchive(BaseModel):
    id: int | None = None
    original_memory_id: int
    content: str
    embedding: bytes | None = None
    archived_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mismatch_score: float = 0.0
    archive_reason: str = ""  # "reconsolidation", "compression", "extinction"


class MemoryTransition(BaseModel):
    id: int | None = None
    from_memory_id: int
    to_memory_id: int
    count: int = 1
    last_transition: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_id: str = ""


class CausalDAGEdge(BaseModel):
    id: int | None = None
    source_entity_id: int
    target_entity_id: int
    algorithm: str = "pc"  # "pc", "ges", "heuristic"
    confidence: float = 1.0
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_validated: bool = False


# -- v5 models --

# Valid ADR status values — canonical list; adr_add's _VALID_STATUSES is built from this.
_ADR_VALID_STATUSES: frozenset[str] = frozenset(
    {"open", "accepted", "superseded", "rejected", "deprecated"}
)


@observe(tier="hot")
def _indent_continuation(value: str) -> str:
    """Indent every line after the first by two spaces (markdown list continuation).

    Keeps the first line flush so the ``- {key}: {first line}`` bullet shape is
    preserved, but pushes continuation lines off column 0.  This neutralises any
    embedded ``## heading`` or ```` ``` ```` fence inside a multi-line ADR field
    value, which would otherwise be parsed as a real markdown structure by the
    ADR id scan (``^## ADR-NNNN``) and ``wiki_append_section`` heading detection.
    Blank lines are left empty (no trailing indent whitespace).
    """
    lines = value.split("\n")
    if len(lines) == 1:
        return value
    head, *rest = lines
    indented_rest = [f"  {ln}" if ln else ln for ln in rest]
    return "\n".join([head, *indented_rest])


class ADR(BaseModel):
    """Typed record shape for an Architecture Decision Record entry.

    Represents the 10 content fields stored in the ADR log wiki page.
    ``directory`` is a routing argument in ``adr_add``, not part of the record.
    ``adr_id`` is assigned sequentially by ``adr_add`` and is optional here
    (None until assigned).

    FastMCP derives the JSON Schema for ``adr_add`` from flat keyword args —
    the model is used as shape/post-validation inside the tool body, not as
    the tool signature. Validation (empty-field check, status enum) stays in
    ``adr_add`` to preserve exact error messages expected by the test suite.
    """

    adr_id: str | None = None
    title: str
    status: str  # One of _ADR_VALID_STATUSES; enforced by adr_add, not pydantic
    date: str
    context: str
    decision: str
    rationale: str
    alternatives: str
    consequences: str
    revisit_trigger: str
    supersedes: str

    def to_body_dict(self) -> dict[str, str]:
        """Return ordered dict of the 9 body fields (title excluded — used as heading).

        Field order matches the flat-bullet rendering in ``_build_adr_body``.
        """
        return {
            "status": self.status,
            "date": self.date,
            "context": self.context,
            "decision": self.decision,
            "rationale": self.rationale,
            "alternatives": self.alternatives,
            "consequences": self.consequences,
            "revisit_trigger": self.revisit_trigger,
            "supersedes": self.supersedes,
        }

    def to_markdown_body(self) -> str:
        """Return the flat-bullet markdown body for this ADR, matching adr_add output.

        Each field renders as ``- {key}: {first line}`` followed by any
        continuation lines indented two spaces (standard markdown list
        continuation).  Indentation is mandatory for correctness, not cosmetics:
        a multi-line value flushed to column 0 would let an embedded ``## ...``
        line be parsed as a real heading — poisoning ``_next_adr_id``'s
        ``^## ADR-NNNN`` scan (sequential IDs jump to e.g. ADR-10000) and
        ``wiki_append_section``'s section detection.  Indenting pushes such
        lines (and stray ```` ``` ```` fences) off column 0 so neither parser
        misfires, while keeping the ``- {key}: `` flat-bullet shape intact.
        """
        return "".join(
            f"- {k}: {_indent_continuation(str(v))}\n" for k, v in self.to_body_dict().items()
        )


class AgentPrompt(BaseModel):
    """Typed record shape for an agent-prompt library entry.

    Represents the content fields stored as a wiki page with slug
    ``agent-prompt-<pattern>``. ``directory`` is a routing arg in
    ``agent_prompt_save``, not part of the record.

    FastMCP derives the JSON Schema for ``agent_prompt_save`` from flat
    keyword args — the model is used as shape/post-validation inside the
    tool body, not as the tool signature.
    """

    pattern: str  # slug stem: agent-prompt-<pattern>
    purpose: str  # one-line description; feeds the TOC
    content: str  # the dispatch-prompt body


# -- v4 models --


class Checkpoint(BaseModel):
    id: int | None = None
    session_id: str = "default"
    directory_context: str
    current_task: str = ""
    files_being_edited: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    active_errors: list[str] = Field(default_factory=list)
    custom_context: str = ""
    epoch: int = 0  # compaction epoch counter
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True  # only latest checkpoint is active
