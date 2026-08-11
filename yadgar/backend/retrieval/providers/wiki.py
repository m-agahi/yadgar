"""WikiProvider — wraps WikiStore.query as a SourceProvider.

Part of v6 T6 (unified-scoped-recall), Step 1 — pure extraction.
Car C7 (0047 §5 C7): scoping moved INTO the query. ``WikiStore.query()`` now
takes ``project_id`` + ``opt_in_tags`` and pushes both into the stage-1 ``WHERE``
clause. The Python post-filters this provider used to apply
(``is_directory_eligible`` on ``directory_context``, and the disposition arm of
``is_recall_visible``) are subsumed: the rows they would have dropped are no
longer fetched, so they can no longer consume a pool slot.

``is_recall_visible`` is still called. That is deliberate, not leftover: it is
idempotent with the WHERE (both read ``recall_disposition`` + ``opt_in_tag``
from the same policy), and it remains the row-level guard for any page that
reaches this provider by a path the clause did not cover.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.directory import RecallScope
from yadgar._shared.wiki.policy import is_recall_visible
from yadgar.backend.retrieval.providers.base import Candidate, Scope, SourceProvider

if TYPE_CHECKING:
    from yadgar._shared.wiki.store import WikiStore


class WikiProvider(SourceProvider):
    """SourceProvider backed by WikiStore (wiki knowledge base).

    Calls ``WikiStore.query()`` and maps returned wiki page dicts to normalized
    Candidate objects with ``type="wiki"``.

    Car C7: scoping happens in SQL (``project_id`` + the ``global`` reach tag
    + the policy-derived ``page_type`` exclusion), not in a post-filter.

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
            scope: Scope carrying the caller's ``project_id`` and ``opt_in_tags``,
                both pushed into the stage-1 WHERE clause.
            limit: Maximum candidates to return.

        Returns:
            List of Candidate(type="wiki", ...) sorted by native_score descending,
            already scoped to ``scope.project_id`` by the query itself.
        """
        include_tag = self._tags[0] if self._tags else None
        # Car C7: the opt-in tags are the caller's requested tags. ``scope`` may
        # carry them explicitly; fall back to this provider's own ``tags`` so a
        # caller that only set ``tags=`` keeps reaching the agent-prompt library.
        opt_in = scope.opt_in_tags if scope.opt_in_tags is not None else self._tags
        results = self._wiki.query(
            query,
            max_results=limit,
            include_tag=include_tag,
            exclude_tags=self._exclude_tags,
            scope=RecallScope(project_id=scope.project_id or None, opt_in_tags=opt_in),
        )

        candidates: list[Candidate] = []
        for page in results:
            pid = page.get("id")
            if pid is None:
                continue
            # Car C (#83): policy-driven recall exclusion, shared with
            # wiki_query since task 0134 (both search paths, one rule — see
            # is_recall_visible). Keeps agent-library pages (and formerly
            # repo_wiki's structural code inventory, decommissioned
            # #33/ADR-0162) out of everyday recall. Exact-key reads
            # (wiki_read / wiki_get / wiki_list) never apply it.
            # The tag opt-in is PER PAGE: passing tags is consent to see the
            # pages carrying them, not a blanket kill-switch for the filter.
            # Disposition is switchable: one-field flip in policy.py — and
            # C7's anti-drift test proves that flip also moves the emitted SQL.
            if not is_recall_visible(page, opt_in):
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
                    project_id=page.get("project_id"),
                    raw=raw,
                )
            )
        return candidates
