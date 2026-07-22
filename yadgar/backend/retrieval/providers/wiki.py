"""WikiProvider — wraps WikiStore.query as a SourceProvider.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
Step 3 (v6 T6): adds directory scoping via is_directory_eligible post-filter.

WikiStore.query() does not currently accept a directory parameter. Step 3
applies a Python-side directory post-filter matching the is_directory_eligible
eligible set, consistent with the legacy recall + wiki_query paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.directory import is_directory_eligible
from yadgar._shared.wiki.policy import get_policy
from yadgar.backend.retrieval.providers.base import Candidate, Scope, SourceProvider

if TYPE_CHECKING:
    from yadgar._shared.wiki.store import WikiStore


class WikiProvider(SourceProvider):
    """SourceProvider backed by WikiStore (wiki knowledge base).

    Calls ``WikiStore.query()`` and maps returned wiki page dicts to normalized
    Candidate objects with ``type="wiki"``.

    Step 3: applies is_directory_eligible() post-filter so wiki results are
    scoped to the caller directory, matching the legacy wiki_query path.

    The ``raw`` field on each Candidate carries the original wiki page dict
    (including ``_retrieval_score`` from the wiki hybrid search) so the
    fan-out orchestrator can pass it through to callers with ``_source="wiki"``
    set — matching the existing wiki-blend schema in recall.py.

    S3 (tag-aware recall):
      ``tags``: when set, passes ``include_tag=tags[0]`` to WikiStore.query() — triggers
      SQL pre-filter path (no HNSW dilution). Single-tag assumption is safe; agent-prompt
      is the only real caller.
      ``exclude_tags``: when set, passes to WikiStore.query() — post-rank exclusion.
    """

    def __init__(
        self,
        wiki: WikiStore,
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
    ) -> None:
        self._wiki = wiki
        self._tags = tags
        self._exclude_tags = exclude_tags

    @property
    def type(self) -> str:
        return "wiki"

    @observe(tier="stage", metric="retrieval.provider.wiki_candidates")
    def candidates(self, query: str, scope: Scope, limit: int) -> list[Candidate]:
        """Call WikiStore.query() and return normalized, directory-scoped Candidates.

        Args:
            query: Search query.
            scope: Scope carrying directory for the Python-side directory post-filter.
            limit: Maximum candidates to return from WikiStore before filtering.

        Returns:
            List of Candidate(type="wiki", ...) sorted by native_score descending,
            filtered to scope.directory (same eligible set as is_directory_eligible).
        """
        include_tag = self._tags[0] if self._tags else None
        results = self._wiki.query(
            query,
            max_results=limit,
            include_tag=include_tag,
            exclude_tags=self._exclude_tags,
        )

        # Step 3: Python-side directory post-filter.
        # Eligible: {scope.directory, 'global', '', None}. 'system' excluded (v5.65).
        caller_dir = scope.directory if scope.directory else None
        candidates: list[Candidate] = []
        for page in results:
            pid = page.get("id")
            if pid is None:
                continue
            dc = page.get("directory_context")
            if not is_directory_eligible(dc, caller_dir):
                continue
            # Car C (#83): policy-driven recall exclusion.
            # Pages whose page_type resolves to recall_disposition="exclude" are
            # dropped from fanout recall. This keeps repo_wiki (structural code
            # inventory) and agent_prompt pages out of everyday recall while
            # wiki_query/wiki_read/wiki_list (which call WikiStore.query directly,
            # not via this provider) remain fully unaffected.
            # OVERRIDE: when the caller passed an explicit include_tag (self._tags),
            # they opted in to this page type — skip the exclusion so
            # recall(tags=["agent-prompt"]) can still reach agent_prompt pages.
            # Disposition is switchable: one-field flip in policy.py.
            if not self._tags and get_policy(page.get("page_type")).recall_disposition == "exclude":
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
                    directory_context=dc,
                    branch=page.get("branch"),
                    raw=raw,
                )
            )
        return candidates
