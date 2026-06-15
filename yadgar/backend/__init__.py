"""Backend-only subpackage: SurrealDB/embedding service runtime.

Modules here ship in the yadgar-backend image only (embed_service uvicorn app,
ml_client scoring, LRU cache, prometheus metrics). Kept separate from core so the
core/backend boundary is explicit and CI can detect which image changed.
"""
