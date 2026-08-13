"""Pydantic request/response models for the embed_service backend routes.

Split out of ``embed_service.py`` (C1, module-standardization train #18). Pure
schema definitions — no app, no singletons, no I/O. Re-exported by
``embed_service.py`` so ``embed_service.embed_service.<Model>`` keeps resolving
for every importer and test.

The ``@field_validator`` methods are pydantic-framework-instrumented (no
per-call ``@observe`` span) and stay I33-exempt via ``.observe-allowlist.json``;
their keys move with the split from ``embed_service:<qual>`` to
``embed_service_models:<qual>``.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class EmbedRequest(BaseModel):
    texts: list[str]
    mode: str = "document"  # "document" | "query" | "raw"

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, v: list[str]) -> list[str]:
        if len(v) > 128:
            raise ValueError("Maximum 128 texts per request")
        for text in v:
            if len(text) > 32768:
                raise ValueError("Text exceeds maximum length of 32768 characters")
        return v


class EmbedResponse(BaseModel):
    embeddings: list[list[float] | None]
    model: str
    dim: int


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    mode: str = "ce"  # "ce" | "nli" | "pair"

    model_config = {"extra": "forbid"}

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("ce", "nli", "pair"):
            raise ValueError(f"mode must be 'ce', 'nli', or 'pair'; got {v!r}")
        return v


class RerankResponse(BaseModel):
    scores: list[float]
    mode: str


class RecallRequest(BaseModel):
    """Request body for POST /recall."""

    query: str
    # C7 (0047 §5 C7): ``project_id`` is now THE read-path scope key and is
    # REQUIRED. C3 added it as optional while the pipeline still scoped on
    # ``directory``; the WHERE clause is now keyed on it, so an absent value
    # would mean an unscoped corpus-wide read — the exact silent fallback
    # ADR-0227 exists to delete.
    #
    # ``extra="forbid"`` means an old client sending ``directory`` breaks
    # LOUDLY (HTTP 422) rather than silently reading the whole corpus. That is
    # correct under ADR-0227 — and it means THE CORE AND BACKEND IMAGES MUST
    # DEPLOY TOGETHER. The runbook records it.
    project_id: str
    # Car H1 (§1.3): the DELIBERATE whole-corpus read (BC-VZ2 viz search, the
    # "instructions-loaded" hook) must spell its intent on the wire rather than
    # being INFERRED from ``project_id`` being empty. Falsy ``project_id``
    # without this flag still raises at the clause builder — the guarantee
    # Car H1 exists to enforce. Only the handful of hook call sites that KNOW
    # they have no project (``hook_project_id`` returning ``""``) may set it.
    unscoped: bool = False
    # Retained as OPTIONAL, and deliberately NOT deleted: the directory is still
    # the caller's physical working path, which several response-side consumers
    # and the action-log correlation still key on. It no longer scopes anything.
    directory: str | None = None
    max_results: int = 5
    min_heat: float = 0.0
    type: str = "all"  # noqa: A003 — matches MCP schema convention
    profile: str | None = None
    mode: str | None = None
    stage_overrides: dict | None = None
    tags: list[str] | None = None
    knobs: dict = {}  # noqa: B006 — Pydantic default_factory not needed here
    # ADR-0077: client compute budget in ms. When set, the route converts it to
    # a monotonic deadline and the pipeline aborts remaining stages once it is
    # exceeded (partial results) — a hook client that already timed out at 2.0s
    # must not keep the backend computing. None = no deadline (MCP recall path).
    deadline_ms: int | None = None

    model_config = {"extra": "forbid"}

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        valid = {"all", "memory", "wiki"}
        if v not in valid:
            raise ValueError(f"type must be one of {sorted(valid)}; got {v!r}")
        return v


class RecallResponse(BaseModel):
    """Response body for POST /recall."""

    results: list[dict]


class RestoreRequest(BaseModel):
    """Request body for POST /restore.

    C10g (0047 PR#40 §5): carries BOTH scope values, because ``restore``'s
    sinks key on different columns — ``directory`` still keys the checkpoint
    and memory-block sinks (neither table has a ``project_id`` column yet),
    ``project_id`` keys the memory-backed ones. ``extra="forbid"`` is safe for
    the added optional field: core and backend images deploy together.
    """

    directory: str = ""
    project_id: str | None = None

    model_config = {"extra": "forbid"}


class RestoreResponse(BaseModel):
    """Response body for POST /restore.

    ``result`` is the exact payload CheckpointRestore.restore returns (the dict
    the core restore tool returned pre-Car-B): checkpoint, anchored_memories,
    recent_memories, hot_memories, predicted_memories, gaps_detected,
    memory_blocks, epoch, formatted.
    """

    result: dict


class ConsolidateRequest(BaseModel):
    """Request body for POST /consolidate."""

    mode: str = "light"

    model_config = {"extra": "forbid"}

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        valid = {"light", "full", "nightly"}
        if v not in valid:
            raise ValueError(f"mode must be one of {sorted(valid)}; got {v!r}")
        return v


class ConsolidateResponse(BaseModel):
    """Response body for POST /consolidate."""

    stats: dict


class AdminRequest(BaseModel):
    """Request body for POST /admin."""

    op: str
    payload: dict = {}  # noqa: RUF012 — Pydantic model field default, not a mutable class attr

    model_config = {"extra": "forbid"}


class AdminResponse(BaseModel):
    """Response body for POST /admin.

    ``scope_versions`` (Car B, §15.2 envelope choice): the current
    ``(scope_kind, scope_id) -> int`` map for the kinds Cars D/F/I care
    about (``config``, ``ledger``). Core compares this against its own
    ``scope_versions`` snapshot; a bumped version means its PTC entries
    for that scope are unreachable — zero extra round-trips in steady
    state. Defaults to an empty dict so existing direct construction
    (no ``scope_versions`` kwarg) still works.
    """

    result: dict
    scope_versions: dict = {}  # noqa: RUF012 — Pydantic model field default


class VizRequest(BaseModel):
    """Request body for POST /viz."""

    op: str
    payload: dict = {}  # noqa: RUF012 — Pydantic model field default, not a mutable class attr

    model_config = {"extra": "forbid"}


class VizResponse(BaseModel):
    """Response body for POST /viz."""

    result: dict


class ReadQueryRequest(BaseModel):
    """Request body for POST /read_query (sanctioned read-only DB inspection).

    The query runs on the VIEWER-role RO DB connection (writes rejected at the
    DB regardless of query text — ADR-0078). ``timeout_ms`` bounds the per-call
    DB timeout.
    """

    query: str
    params: dict = {}  # noqa: RUF012 — Pydantic model field default, not a mutable class attr
    timeout_ms: int = 5000

    model_config = {"extra": "forbid"}


class ReadQueryResponse(BaseModel):
    """Response body for POST /read_query."""

    rows: list[dict]
    row_count: int
    truncated: bool
