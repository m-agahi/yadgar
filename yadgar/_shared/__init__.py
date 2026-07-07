"""yadgar._shared — modules imported by BOTH the core and backend subpackages.

Populated by the core/backend folder split (#17). Contains the shared leaf
libraries (config, observability, metrics, tracing, paths, models, embeddings,
…). No cross-imports into ``yadgar.core`` or ``yadgar.backend`` are permitted
from this subpackage (enforced by import-linter).
"""
