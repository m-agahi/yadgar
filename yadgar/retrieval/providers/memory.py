"""MemoryProvider — wraps the existing Retriever pipeline as a SourceProvider.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
Step 3 (v6 T6): adds directory + branch scoping via ScopeFilter.from_scope().

The MemoryProvider applies a Python-side directory post-filter on top of the
retriever pipeline results. This mirrors the legacy recall path's
is_directory_eligible() post-filter (recall.py:362-365) but runs at the
provider layer so the fan-out orchestrator always gets scoped candidates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yadgar.observability.observe import observe
from yadgar.retrieval.providers.base import Candidate, Scope, SourceProvider
from yadgar.storage.directory import is_directory_eligible

if TYPE_CHECKING:
    from yadgar.retrieval.core import Retriever


class MemoryProvider(SourceProvider):
    """SourceProvider backed by the Retriever pipeline (memory store).

    Calls ``Retriever.recall()`` with the scope's branch/heat context and
    maps the returned memory dicts to normalized Candidate objects.

    Step 3: applies is_directory_eligible() post-filter at the provider level
    so the fan-out path enforces the same directory scoping as the legacy path.
    The DB-level ScopeFilter clause is available for future threading into
    Retriever.recall(); the Python post-filter is the active enforcement point.

    The ``raw`` field on each Candidate is the original memory dict, so the
    fan-out orchestrator can return it directly to callers without schema changes.
    """

    def __init__(self, retriever: Retriever, profile: str | None = None) -> None:
        self._retriever = retriever
        self._profile = profile

    @property
    def type(self) -> str:
        return "memory"

    @observe(tier="stage", name="retrieval.provider.memory_candidates")
    def candidates(self, query: str, scope: Scope, limit: int) -> list[Candidate]:
        """Call Retriever.recall() and return normalized, directory-scoped Candidates.

        Args:
            query: Search query.
            scope: Scope carrying directory, branch context, and min_heat.
            limit: Maximum candidates to return before directory filtering.

        Returns:
            List of Candidate(type="memory", ...) in the retriever's NATIVE order
            (Retriever.recall owns ranking: WRRF + rerank pipeline + optional
            rule/metacognition reordering — NOT necessarily native_score-descending),
            filtered to scope.directory (same eligible set as is_directory_eligible).
            Order is preserved verbatim so callers must not assume a score sort.
        """
        results = self._retriever.recall(
            query,
            max_results=limit,
            min_heat=scope.min_heat,
            current_branch=scope.branch,
            default_branch=scope.default_branch,
            profile=self._profile,
        )

        # Step 3: Python-side directory post-filter (same semantics as legacy path).
        # Eligible: {scope.directory, 'global', '', None}. 'system' excluded (v5.65).
        caller_dir = scope.directory if scope.directory else None
        candidates: list[Candidate] = []
        for m in results:
            mid = m.get("id")
            if mid is None:
                continue
            dc = m.get("directory_context")
            if not is_directory_eligible(dc, caller_dir):
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
                    directory_context=dc,
                    branch=m.get("branch"),
                    raw=m,
                )
            )
        return candidates
