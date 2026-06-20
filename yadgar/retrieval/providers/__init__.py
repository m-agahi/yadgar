"""SourceProvider abstraction for unified scoped recall (v6 T6).

Public API:
  Candidate    — normalized result from any source
  SourceProvider — ABC for memory, wiki, and future sources
  Scope        — query scope (directory, branch, min_heat)

Providers live under this package:
  memory.py    — MemoryProvider (wraps Retriever)
  wiki.py      — WikiProvider (wraps WikiStore)

Usage::

    from yadgar.retrieval.providers import Candidate, Scope
    from yadgar.retrieval.providers.memory import MemoryProvider
    from yadgar.retrieval.providers.wiki import WikiProvider
"""

from yadgar.retrieval.providers.base import Candidate, Scope, SourceProvider

__all__ = ["Candidate", "Scope", "SourceProvider"]
