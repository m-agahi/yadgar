"""Native repo-wiki generation for yadgar (T8 — Option A).

Walk a Python repository, extract module/function signatures and docstrings,
emit wiki pages stamped to the repo's directory_context.

Option B (AST-graph / community detection via leidenalg) is a noted follow-on;
not built here.
"""

from yadgar.core.repo_wiki.generator import generate_module_page, generate_wiki_pages
from yadgar.core.repo_wiki.scanner import scan_python_module, scan_repo

__all__ = [
    "scan_python_module",
    "scan_repo",
    "generate_module_page",
    "generate_wiki_pages",
]
