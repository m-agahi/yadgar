"""SourceProvider ABC + Candidate/Scope dataclasses.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
No entry-point code calls these yet (wired in Step 2 behind UNIFIED_RECALL_ENABLED).

Design notes:
  - Candidate carries all fields needed for downstream fusion/reranking steps
    (Steps 3–4) without surfacing them before those steps ship.
  - Car C7 (0047 §5 C7) re-keyed ``Scope`` and ``Candidate`` off
    ``directory``/``directory_context`` and onto ``project_id``. The scope is no
    longer a hint the provider post-filters with — it is pushed into the
    stage-1 SQL ``WHERE``.
  - ``raw`` holds the provider's native dict for lossless pass-through; the
    fan-out orchestrator (Step 2) returns raw dicts, not Candidates, so
    existing recall callers see no schema change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from yadgar._shared.storage.directory import RecallScope


@dataclass
class Scope:
    """Query scope passed to every SourceProvider.

    Attributes:
        project_id: Caller's resolved project id (required). Car C7 re-keyed
            this from ``directory``: it is pushed into the stage-1 ``WHERE``
            clause (``project_id = $p OR 'global' IN tags``) rather than being
            applied as a Python post-filter after the query spent its LIMIT.
        opt_in_tags: Tags the caller explicitly requested. Two consumers, both
            required. Wiki: feeds the ``POLICY_BY_TYPE``-derived ``page_type``
            exclusion so ``recall(type="wiki", tags=["agent-prompt"])`` still
            reaches the agent-prompt library (ADR-0007). Memory (ledger task
            82): ``MemoryProvider`` drops rows that do not carry every listed
            tag — the field was populated here from the start but the memory
            provider never read it, so a tag-filtered memory recall returned
            whatever the query's text scorers matched.
        min_heat: Minimum heat threshold forwarded to the memory provider.
            Wiki candidates have no heat; this field is ignored by WikiProvider.
        excluded_slugs: Car C8 (0047 §5 C8) — wiki slugs this read must not
            surface, today the SUPERSEDED ADR set read from the SQL ledger. It
            arrives already-resolved from the async route (upstream of the
            ``to_thread`` boundary) because ``asyncmy`` is async-only and a
            lookup down here would need a private event loop per recall. The
            memory table has no ``slug`` column, so MemoryProvider ignores it.
        unscoped: Car H1 (§1.3) — the DELIBERATE whole-corpus read (e.g. the
            BC-VZ2 viz god's-eye search, and the "instructions-loaded" hook,
            both of which forward with an empty project on purpose). A falsy
            ``project_id`` without this raises at the clause builder rather
            than silently returning the whole corpus.
    """

    project_id: str
    min_heat: float = 0.0
    opt_in_tags: list[str] | None = None
    excluded_slugs: tuple[str, ...] | None = None
    unscoped: bool = False

    def to_recall_scope(self, opt_in: list[str] | None) -> RecallScope:
        """Convert to the storage-layer ``RecallScope`` pushed into the WHERE.

        Car C8 EXTRACTED THIS FROM ``WikiProvider.candidates`` ON PURPOSE. The
        conversion is a field-by-field re-construction, and a field added to
        both dataclasses but forgotten HERE is dropped SILENTLY: recall keeps
        returning results, just without the exclusion. One shared conversion is
        what lets the C8 nightly invariant traverse the SAME hop production
        traverses — a check that re-built the scope itself would agree with
        itself while this hop quietly dropped the field, which is precisely the
        vacuous pass the cross-engine arm exists to eliminate.

        Args:
            opt_in: The caller's requested tags, already resolved by the
                provider (its own ``tags`` when the scope carries none).

        Returns:
            The equivalent ``RecallScope``.
        """
        return RecallScope(
            project_id=self.project_id or None,
            opt_in_tags=opt_in,
            excluded_slugs=self.excluded_slugs,
            unscoped=self.unscoped,
        )


@dataclass
class Candidate:
    """Normalized result from a SourceProvider.

    All fields are provider-agnostic so they can be pooled and (later) fused
    across types without the orchestrator knowing the source internals.

    Attributes:
        type: Source type tag — "memory" or "wiki".
        id: Unique identifier within the source (memory int id or wiki slug).
        title: Human-readable title (wiki page title or None for memories).
        content: Full text content used for reranking.
        native_score: Provider's own relevance signal (retrieval score,
            heat-weighted WRRF, BM25+vector blend). Used as a prior in
            Step 4 fusion; NOT the final ranking key.
        project_id: Project the item is owned by, or None when unstamped
            (C6 made the column ``option<string>``; an un-backfilled row reads
            as ``None``, NOT as ``"global"``).
        raw: Original provider dict, preserved for lossless pass-through.
            Step 2 returns raw dicts (not Candidates) to avoid breaking the
            existing recall return type.
    """

    type: str  # "memory" | "wiki"
    id: Any  # int for memory, str slug for wiki
    title: str | None
    content: str
    native_score: float
    project_id: str | None
    raw: dict = field(default_factory=dict)


class SourceProvider(ABC):
    """Abstract base class for recall source providers.

    Each provider fans out a query to one knowledge source and returns a list
    of normalized Candidate objects.  The fan-out orchestrator (Step 2) calls
    ``candidates()`` on every registered provider, pools the results, and
    applies dedup + (later) cross-encoder fusion.

    Subclasses:
      MemoryProvider  — wraps the existing Retriever pipeline
      WikiProvider    — wraps WikiStore.query
    """

    @property
    @abstractmethod
    def type(self) -> str:
        """Source type identifier, e.g. "memory" or "wiki"."""

    @abstractmethod
    def candidates(self, query: str, scope: Scope, limit: int) -> list[Candidate]:
        """Run provider-native retrieval and return normalized Candidates.

        Args:
            query: Free-text search query.
            scope: Directory/heat scope for this call.
            limit: Maximum number of candidates to return.  Providers may
                return fewer; the orchestrator requests limit > max_results
                to give the fusion step a candidate pool to work with.

        Returns:
            List of Candidate objects sorted by native_score descending.
        """
