"""WikiProvider — wraps WikiStore.query as a SourceProvider.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
No entry-point code calls this yet (wired in Step 2 behind UNIFIED_RECALL_ENABLED).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yadgar.retrieval.providers.base import Candidate, Scope, SourceProvider

if TYPE_CHECKING:
    from yadgar.wiki import WikiStore


class WikiProvider(SourceProvider):
    """SourceProvider backed by WikiStore (wiki knowledge base).

    Calls ``WikiStore.query()`` and maps returned wiki page dicts to normalized
    Candidate objects with ``type="wiki"``.

    The ``raw`` field on each Candidate carries the original wiki page dict
    (including ``_retrieval_score`` from the wiki hybrid search) so the
    fan-out orchestrator can pass it through to callers with ``_source="wiki"``
    set — matching the existing wiki-blend schema in recall.py.
    """

    def __init__(self, wiki: WikiStore) -> None:
        self._wiki = wiki

    @property
    def type(self) -> str:
        return "wiki"

    def candidates(self, query: str, scope: Scope, limit: int) -> list[Candidate]:
        """Call WikiStore.query() and return normalized Candidates.

        Args:
            query: Search query.
            scope: Scope (directory/branch carried for Step 3; not applied here).
            limit: Maximum candidates to return.

        Returns:
            List of Candidate(type="wiki", ...) sorted by native_score descending.
        """
        results = self._wiki.query(query, max_results=limit)

        candidates: list[Candidate] = []
        for page in results:
            pid = page.get("id")
            if pid is None:
                continue
            native_score = float(page.get("_retrieval_score", 0.0))
            # Tag raw dict so orchestrator can set _source="wiki" downstream
            raw = dict(page)
            raw["_source"] = "wiki"
            candidates.append(
                Candidate(
                    type="wiki",
                    id=page.get("slug") or pid,
                    title=page.get("title"),
                    content=page.get("content", ""),
                    native_score=native_score,
                    directory_context=page.get("directory_context"),
                    branch=page.get("branch"),
                    raw=raw,
                )
            )
        return candidates
