"""MemoryProvider — wraps the existing Retriever pipeline as a SourceProvider.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
No entry-point code calls this yet (wired in Step 2 behind UNIFIED_RECALL_ENABLED).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yadgar.retrieval.providers.base import Candidate, Scope, SourceProvider

if TYPE_CHECKING:
    from yadgar.retrieval.core import Retriever


class MemoryProvider(SourceProvider):
    """SourceProvider backed by the Retriever pipeline (memory store).

    Calls ``Retriever.recall()`` with the scope's branch/heat context and
    maps the returned memory dicts to normalized Candidate objects.

    The ``raw`` field on each Candidate is the original memory dict, so the
    fan-out orchestrator can return it directly to callers without schema changes.
    """

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    @property
    def type(self) -> str:
        return "memory"

    def candidates(self, query: str, scope: Scope, limit: int) -> list[Candidate]:
        """Call Retriever.recall() and return normalized Candidates.

        Args:
            query: Search query.
            scope: Scope carrying branch context and min_heat.
            limit: Maximum candidates to return (passed as max_results to retriever).

        Returns:
            List of Candidate(type="memory", ...) sorted by native_score descending.
        """
        results = self._retriever.recall(
            query,
            max_results=limit,
            min_heat=scope.min_heat,
            current_branch=scope.branch,
            default_branch=scope.default_branch,
        )

        candidates: list[Candidate] = []
        for m in results:
            mid = m.get("id")
            if mid is None:
                continue
            # native_score: prefer explicit retrieval score, fall back to heat
            native_score = float(m.get("_retrieval_score", m.get("heat", 0.0)))
            candidates.append(
                Candidate(
                    type="memory",
                    id=mid,
                    title=None,  # memories have no title field
                    content=m.get("content", ""),
                    native_score=native_score,
                    directory_context=m.get("directory_context"),
                    branch=m.get("branch"),
                    raw=m,
                )
            )
        return candidates
