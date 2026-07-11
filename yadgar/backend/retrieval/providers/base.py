"""SourceProvider ABC + Candidate/Scope dataclasses.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
No entry-point code calls these yet (wired in Step 2 behind UNIFIED_RECALL_ENABLED).

Design notes:
  - Candidate carries all fields needed for downstream fusion/reranking steps
    (Steps 3–4) without surfacing them before those steps ship.
  - scope.directory and scope.branch are carried so Step 3 (DB-level
    DirectoryFilter) can be wired without a signature change.
  - ``raw`` holds the provider's native dict for lossless pass-through; the
    fan-out orchestrator (Step 2) returns raw dicts, not Candidates, so
    existing recall callers see no schema change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Scope:
    """Query scope passed to every SourceProvider.

    Attributes:
        directory: Caller's working directory (required — mirrors v5.65 Fix D).
            Used in Step 3 for DB-level DirectoryFilter; carried here so the
            provider signature is stable.
        branch: Active git branch, or None when not in a git repo.
            Used for branch-aware filtering (Step 3).
        default_branch: Repository default branch (e.g. "master"), or None.
        min_heat: Minimum heat threshold forwarded to the memory provider.
            Wiki candidates have no heat; this field is ignored by WikiProvider.
    """

    directory: str
    branch: str | None = None
    default_branch: str | None = None
    min_heat: float = 0.0


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
        directory_context: Directory the item was stored under, or None.
        branch: Branch the item was stored on, or None.
        raw: Original provider dict, preserved for lossless pass-through.
            Step 2 returns raw dicts (not Candidates) to avoid breaking the
            existing recall return type.
    """

    type: str  # "memory" | "wiki"
    id: Any  # int for memory, str slug for wiki
    title: str | None
    content: str
    native_score: float
    directory_context: str | None
    branch: str | None
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
            scope: Directory/branch/heat scope for this call.
            limit: Maximum number of candidates to return.  Providers may
                return fewer; the orchestrator requests limit > max_results
                to give the fusion step a candidate pool to work with.

        Returns:
            List of Candidate objects sorted by native_score descending.
        """
