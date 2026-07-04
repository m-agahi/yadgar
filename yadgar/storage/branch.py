"""Branch-aware predicate helpers.

Used by memory/wiki query methods to restrict results to the current or
default branch.  Kept separate so retrieval.core can import BranchFilter
without pulling in the full StorageEngine.
"""

from yadgar.observability.observe import observe


class BranchFilter:
    """Carry branch context for SurrealQL predicate injection.

    Attributes:
        current_branch: The active git branch, or None for non-git contexts.
        default_branch: The repo default branch (e.g. 'master', 'main').

    Predicate generated:
        (branch IS NONE OR branch = $bf_default
         [OR branch = $bf_current when current_branch is not None])
    """

    __slots__ = ("current_branch", "default_branch")

    def __init__(self, current_branch: str | None, default_branch: str) -> None:
        self.current_branch = current_branch
        self.default_branch = default_branch

    def __repr__(self) -> str:
        return f"BranchFilter(current={self.current_branch!r}, default={self.default_branch!r})"


@observe(tier="hot")
def _build_branch_clause(
    branch_filter: BranchFilter | None,
) -> tuple[str, dict]:
    """Return (sql_fragment, params_dict) for a branch WHERE predicate.

    When branch_filter is None: returns ('', {}) — no filtering.
    When current_branch is set: allows NULL, default_branch, and current_branch.
    When current_branch is None: allows NULL and default_branch only.
    """
    if branch_filter is None:
        return "", {}

    params: dict = {"bf_default": branch_filter.default_branch}
    if branch_filter.current_branch is not None:
        params["bf_current"] = branch_filter.current_branch
        clause = "(branch IS NONE OR branch = $bf_default OR branch = $bf_current)"
    else:
        clause = "(branch IS NONE OR branch = $bf_default)"
    return clause, params
