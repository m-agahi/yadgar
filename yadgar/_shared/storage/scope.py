"""ScopeFilter — directory predicate bundle for fan-out recall (v6 T6 Step 3).

Replaces the separate directory_filter param in storage methods for the
fan-out path.  Only the fan-out path (MemoryProvider, WikiProvider) uses
ScopeFilter.

ADR-0215: the branch half of this bundle is gone.  ScopeFilter used to compose
a branch predicate with a directory predicate; branch scoping is removed from
the system entirely, so the bundle now carries the directory predicate alone.
The wrapper is kept (rather than callers reaching for DirectoryFilter directly)
because the composition seam and its ``build_clause`` contract are what the
storage methods are typed against.

Design (per plan unified-scoped-recall-v2-steps3-5.md §2):
  - ScopeFilter(directory) carries the directory scoping predicate.
  - build_clause() delegates to _build_directory_clause.
  - Empty when directory is None → ('', {}) — exact legacy no-op.
  - ScopeFilter.from_scope(scope) factory builds from a Scope dataclass.

I30 note: bundling deletes the need for a separate directory_filter param on
storage methods, keeping per-method param counts within allowlist limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.directory import DirectoryFilter, _build_directory_clause

if TYPE_CHECKING:
    from yadgar._shared.retrieval.providers.base import Scope


@dataclass(frozen=True)
class ScopeFilter:
    """Bundle of scoping predicates for DB-level filtering.

    Attributes:
        directory: DirectoryFilter for directory-aware WHERE predicate, or None (no filter).

    Usage:
        sf = ScopeFilter(directory=DirectoryFilter(...))
        sql_fragment, params = sf.build_clause()
        # sql_fragment is '' when directory is None (exact legacy no-op)
    """

    directory: DirectoryFilter | None = None

    @observe(tier="hot")
    def build_clause(self) -> tuple[str, dict]:
        """Return (sql_fragment, params) for the directory predicate.

        The fragment is suitable for injection into a WHERE clause as:
          WHERE <existing_conditions> AND <sql_fragment>

        When the filter is None (or produces an empty clause), returns ('', {}).
        The caller must check for '' and omit the AND.

        Returns:
            Tuple of (sql_fragment, params_dict). sql_fragment is '' when no
            filtering is needed.
        """
        dir_clause, dir_params = _build_directory_clause(self.directory)
        if not dir_clause:
            return "", {}
        return dir_clause, dict(dir_params)

    @classmethod
    @observe(tier="hot")
    def from_scope(cls, scope: Scope) -> ScopeFilter:
        """Build a ScopeFilter from a provider Scope.

        Args:
            scope: Scope carrying the caller directory.

        Returns:
            ScopeFilter with the directory predicate populated (or None when absent).
        """
        df: DirectoryFilter | None = None
        if scope.directory:
            df = DirectoryFilter(caller_dir=scope.directory)

        return cls(directory=df)
