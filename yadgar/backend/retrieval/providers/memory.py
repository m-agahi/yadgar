"""MemoryProvider — wraps the existing Retriever pipeline as a SourceProvider.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
Car C7 (0047 §5 C7): scoping moved into the query. ``Retriever.recall`` now
takes ``project_id`` and pushes ``project_id = $p OR 'global' IN tags`` into the
FTS and vector arms, so those signals spend their candidate budget on in-scope
rows instead of being post-filtered afterwards.

THE RESIDUAL GUARD BELOW IS NOT LEFTOVER. The clause covers the two SQL-driven
signals; the PPR graph walk and spreading activation traverse EDGES and hand
back memory ids the clause never saw. ``is_project_eligible`` catches exactly
those. It differs from the retired ``is_directory_eligible`` in the way that
matters: it reads ``project_id`` + the ``global`` reach tag — the same two arms
the SQL uses, with the same treatment of unstamped rows — instead of a wider
``{'global', '', None}`` sentinel set that admitted every unattributed row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.directory import is_project_eligible
from yadgar.backend.retrieval.providers.base import Candidate, Scope, SourceProvider

if TYPE_CHECKING:
    from yadgar.backend.retrieval.core import Retriever


class MemoryProvider(SourceProvider):
    """SourceProvider backed by the Retriever pipeline (memory store).

    Calls ``Retriever.recall()`` with the scope's heat context and
    maps the returned memory dicts to normalized Candidate objects.

    Car C7: the DB-level predicate is the active enforcement point; the
    row guard here only catches graph-walk candidates (see module docstring).

    The ``raw`` field on each Candidate is the original memory dict, so the
    fan-out orchestrator can return it directly to callers without schema changes.
    """

    def __init__(
        self,
        retriever: Retriever,
        profile: str | None = None,
        deadline: float | None = None,
    ) -> None:
        self._retriever = retriever
        self._profile = profile
        # ADR-0077: monotonic deadline threaded into Retriever.recall so the
        # signal-collection stages can abort once the client budget is exceeded.
        self._deadline = deadline

    @property
    def type(self) -> str:
        return "memory"

    @observe(tier="stage", metric="retrieval.provider.memory_candidates")
    def candidates(self, query: str, scope: Scope, limit: int) -> list[Candidate]:
        """Call Retriever.recall() and return normalized, directory-scoped Candidates.

        Args:
            query: Search query.
            scope: Scope carrying project_id and min_heat.
            limit: Maximum candidates to return.

        Returns:
            List of Candidate(type="memory", ...) in the retriever's NATIVE order
            (Retriever.recall owns ranking: WRRF + rerank pipeline + optional
            rule/metacognition reordering — NOT necessarily native_score-descending),
            scoped to scope.project_id.
            Order is preserved verbatim so callers must not assume a score sort.
        """
        caller_project = scope.project_id or None
        results = self._retriever.recall(
            query,
            max_results=limit,
            min_heat=scope.min_heat,
            profile=self._profile,
            deadline=self._deadline,
            project_id=caller_project,
            unscoped=scope.unscoped,
        )

        # Car C7: residual guard for graph-walk candidates only — the SQL arms
        # are already scoped. Same two arms as the clause, so this cannot
        # disagree with it.
        #
        # Car H1 (§1.3): ``is_project_eligible`` raises on a falsy caller
        # project — it has no ``unscoped`` opt-in of its own (see its
        # docstring). A deliberate whole-corpus scope (BC-VZ2, the
        # "instructions-loaded" hook) skips the guard here instead, the same
        # shape as ``http.py::_filter_prompt_recall_results``: no project to
        # filter BY means every candidate is admitted, not that filtering is
        # attempted against nothing.
        candidates: list[Candidate] = []
        for m in results:
            mid = m.get("id")
            if mid is None:
                continue
            if not scope.unscoped and not is_project_eligible(
                m.get("project_id"), m.get("tags"), caller_project
            ):
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
                    project_id=m.get("project_id"),
                    raw=m,
                )
            )
        return candidates
