"""yadgar.storage — SurrealDB storage engine.

StorageEngine is assembled from topic-specific mixin classes:
  _ClientMixin (client.py)       — transport layer and low-level helpers
  _MigrationsMixin (migrations.py) — schema bootstrap and migrations
  _MemoryMixin (memory.py)       — memory CRUD and primary-table operations
  _EpisodeMixin (episode.py)     — episode table CRUD
  _EntityMixin (entity.py)       — entity + relationship table CRUD
  _VectorMixin (vector.py)       — embedding CRUD and HNSW/MTREE index management
  _ClusterMixin (cluster.py)     — memory_cluster + memory_similarity_link CRUD
  _WikiMixin (wiki.py)           — wiki page CRUD and search
  _BlocksMixin (blocks.py)       — memory_block CRUD (v5.33.0)
  _RuntimeConfigMixin (runtime_config.py) — runtime_config CRUD (ADR-0163)
  _BookmarksMixin (bookmarks.py) — wiki_bookmark CRUD (v5.23.0)
  _QueueMixin (queue.py)         — file hashes and action log
  _DbSizeMixin (dbsize.py)       — database size reporting
  _RulesMixin (rules.py)         — memory_rule, memory_archive, memory_transition CRUD
  _OpsMixin (ops.py)             — consolidation_log, stats, engram_slot, checkpoint, prune
  _NarrativeMixin (narrative.py) — narrative_entry, astrocyte_process, derived_belief, prospective_memory
  _CausalMixin (causal.py)       — causal_dag_edge insert/query/clear (v5.1 C1)
  _UserMixin (user.py)           — user_profile, thermodynamics

Public API: all names importable from ``yadgar.storage`` are re-exported below.
"""

import atexit
import base64
import fcntl
import json
import logging
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.blocks import _BlocksMixin
from yadgar._shared.storage.bookmarks import _BookmarksMixin
from yadgar._shared.storage.causal import _CausalMixin
from yadgar._shared.storage.client import _CAMEL_CASE_RE as _CAMEL_CASE_RE
from yadgar._shared.storage.client import _EMBEDDING_FIELDS as _EMBEDDING_FIELDS
from yadgar._shared.storage.client import _FTS_STOP_WORDS as _FTS_STOP_WORDS
from yadgar._shared.storage.client import _MEMORY_UPDATABLE_FIELDS as _MEMORY_UPDATABLE_FIELDS
from yadgar._shared.storage.client import (
    _RELATIONSHIP_UPDATABLE_FIELDS as _RELATIONSHIP_UPDATABLE_FIELDS,
)
from yadgar._shared.storage.client import _ClientMixin
from yadgar._shared.storage.cluster import _ClusterMixin
from yadgar._shared.storage.dbsize import _DbSizeMixin
from yadgar._shared.storage.entity import RelationshipMeta as RelationshipMeta
from yadgar._shared.storage.entity import _EntityMixin
from yadgar._shared.storage.episode import _EpisodeMixin
from yadgar._shared.storage.memory import _MemoryMixin
from yadgar._shared.storage.migrations import _MIGRATIONS as _MIGRATIONS
from yadgar._shared.storage.migrations import (
    _migration_001_hnsw_indexes as _migration_001_hnsw_indexes,
)
from yadgar._shared.storage.migrations import (
    _migration_002_relationship_indexes as _migration_002_relationship_indexes,
)
from yadgar._shared.storage.migrations import (
    _migration_003_memory_similarity_link_table as _migration_003_memory_similarity_link_table,
)
from yadgar._shared.storage.migrations import (
    _migration_004_branch_field as _migration_004_branch_field,
)
from yadgar._shared.storage.migrations import (
    _migration_006_source_memory_id as _migration_006_source_memory_id,
)
from yadgar._shared.storage.migrations import _MigrationsMixin
from yadgar._shared.storage.narrative import _NarrativeMixin
from yadgar._shared.storage.ops import _OpsMixin
from yadgar._shared.storage.queue import _QueueMixin
from yadgar._shared.storage.rules import _RulesMixin
from yadgar._shared.storage.runtime_config import _RuntimeConfigMixin
from yadgar._shared.storage.user import _UserMixin
from yadgar._shared.storage.vector import _VectorMixin
from yadgar._shared.storage.wiki import _WikiMixin

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level enrichment pipeline singleton (shared across all insert_memory
# calls on any StorageEngine instance in the process).
# ---------------------------------------------------------------------------

_enrichment_pipeline = None
_enrichment_pipeline_lock = threading.Lock()


@observe(tier="hot")
def _get_enrichment_pipeline(settings, embeddings_engine=None):
    global _enrichment_pipeline
    if _enrichment_pipeline is None:
        with _enrichment_pipeline_lock:
            if _enrichment_pipeline is None:
                from yadgar._shared.enrichment import EnrichmentPipeline

                _enrichment_pipeline = EnrichmentPipeline(settings, embeddings_engine)
    return _enrichment_pipeline


# ---------------------------------------------------------------------------
# _chunk_by_bytes — module-level helper used by batch_writes (client.py)
# ---------------------------------------------------------------------------


@observe(
    exempt="sync generator; @observe sync-wraps and would open/close the span at generator creation not exhaustion, so the span never covers the actual chunking work"
)
def _chunk_by_bytes(
    statements: list[tuple[str, dict | None]], max_bytes: int
) -> Iterator[list[tuple[str, dict | None]]]:
    """Yield sub-lists of *statements* whose combined serialised size stays under *max_bytes*.

    Size is estimated as the sum of each statement's SQL text plus the
    JSON-serialised lengths of its parameter values — the dominant cost when
    content fields are large.  The estimate is conservative (ignores framing
    overhead) so actual wire bytes may be slightly higher, but 1 MB default
    leaves ample slack below SurrealDB's compiled-in body limit.

    If a *single* statement's own size exceeds *max_bytes*, it is yielded
    alone with a WARN log so the caller still attempts the request.  We would
    rather receive a clean 413 (and surface it) than silently drop work.
    """
    current_chunk: list[tuple[str, dict | None]] = []
    current_bytes = 0

    for sql, params in statements:
        # Estimate: SQL text + JSON-serialised param values
        stmt_bytes = len(sql.encode())
        if params:
            for v in params.values():
                stmt_bytes += len(json.dumps(v, ensure_ascii=False).encode())

        if current_chunk and current_bytes + stmt_bytes > max_bytes:
            yield current_chunk
            current_chunk = []
            current_bytes = 0

        if stmt_bytes > max_bytes:
            _log.warning(
                "batch_writes: single statement size %d bytes exceeds MAX_BATCH_BYTES=%d; "
                "attempting alone — expect possible 413 if the server rejects it",
                stmt_bytes,
                max_bytes,
            )

        current_chunk.append((sql, params))
        current_bytes += stmt_bytes

    if current_chunk:
        yield current_chunk


# ---------------------------------------------------------------------------
# DB credential resolution
# ---------------------------------------------------------------------------


@observe(tier="hot")
def _resolve_db_credentials() -> tuple[str, str]:
    """Return (user, password) for SurrealDB authentication.

    Fallback chain (v5.49.3: fall back to RW credentials when explicit DB_USER
    not set — ``yadgar setup`` emits RW vars only):

    1. YADGAR_DB_USER / YADGAR_DB_PASS   — explicit DB credential (preferred)
    2. YADGAR_RW_USER / YADGAR_RW_PASS   — RW credentials (fallback; RW has write access)
    3. raise ValueError naming both var names + how to set them
    """
    _user = os.environ.get("YADGAR_DB_USER") or os.environ.get("YADGAR_RW_USER")
    _pass = os.environ.get("YADGAR_DB_PASS") or os.environ.get("YADGAR_RW_PASS")
    if not _user or not _pass:
        raise ValueError(
            "Missing database credentials. Set YADGAR_DB_USER + YADGAR_DB_PASS "
            "(preferred) or YADGAR_RW_USER + YADGAR_RW_PASS (fallback) in "
            "secrets.env or environment. Re-run 'yadgar setup' to regenerate secrets.env."
        )
    return _user, _pass


@observe(tier="hot")
def _resolve_ro_db_credentials() -> tuple[str, str]:
    """Return (user, password) for the READ-ONLY (VIEWER-role) SurrealDB user.

    Separate from ``_resolve_db_credentials`` (which resolves the OWNER/RW user):
    the RO credentials authenticate as the ``yadgar-ro`` VIEWER user provisioned
    by ``entrypoint-backend.sh`` (``DEFINE USER ... ROLES VIEWER``). The DB rejects
    writes over this connection regardless of query text — this is the real safety
    guard behind the ``/read_query`` debug surface (ADR-0078 sanctioned read path).

    Reads ``YADGAR_RO_USER`` / ``YADGAR_RO_PASS`` (no RW fallback — falling back to
    the writer credentials would silently re-grant write access and defeat the
    entire safety claim). Raises ValueError naming both vars when unset.
    """
    _user = os.environ.get("YADGAR_RO_USER")
    _pass = os.environ.get("YADGAR_RO_PASS")
    if not _user or not _pass:
        raise ValueError(
            "Missing read-only database credentials. Set YADGAR_RO_USER + "
            "YADGAR_RO_PASS (the VIEWER-role DB user) in secrets.env or "
            "environment. Re-run 'yadgar setup' to regenerate secrets.env. "
            "There is deliberately NO RW fallback — the RO surface must never "
            "authenticate as the writer."
        )
    return _user, _pass


# StorageEngine
# ---------------------------------------------------------------------------


class StorageEngine(
    _ClientMixin,
    _MigrationsMixin,
    _MemoryMixin,
    _EpisodeMixin,
    _EntityMixin,
    _VectorMixin,
    _ClusterMixin,
    _WikiMixin,
    _BlocksMixin,
    _RuntimeConfigMixin,
    _BookmarksMixin,
    _QueueMixin,
    _DbSizeMixin,
    _RulesMixin,
    _OpsMixin,
    _NarrativeMixin,
    _CausalMixin,
    _UserMixin,
):
    """SurrealDB-backed persistent storage for yadgar.

    Supports two modes:
      - Server mode: YADGAR_DB_URL set → HTTP API against a running SurrealDB.
      - Embedded mode: surrealkv driver, exclusive file lock, rolling backup.
    """

    def __init__(self, db_path: str, embedding_dim: int = 384):
        self._embedding_dim = embedding_dim
        self._db_path = db_path
        self._db_url = os.environ.get("YADGAR_DB_URL")

        if self._db_url:
            # Server mode: talk to the SurrealDB HTTP API with a shared httpx.Client.
            import logging as _logging

            import httpx

            _logging.getLogger("httpx").setLevel(_logging.WARNING)
            _logging.getLogger("httpcore").setLevel(_logging.WARNING)

            _allow_root = os.environ.get("YADGAR_ALLOW_ROOT", "0").lower() in (
                "1",
                "true",
                "yes",
            )
            if _allow_root:
                _user = os.environ.get("YADGAR_DB_USER", "root")
                _pass = os.environ.get("YADGAR_DB_PASS", "root")
                if _user == "root" or _pass == "root":
                    _log.warning(
                        "YADGAR_ALLOW_ROOT=1: using root credentials — "
                        "for production set YADGAR_DB_USER / YADGAR_DB_PASS"
                    )
            else:
                _user, _pass = _resolve_db_credentials()
            _auth = base64.b64encode(f"{_user}:{_pass}".encode()).decode()
            from yadgar._shared.config import get_settings as _get_settings

            _settings = _get_settings()
            _http_timeout_sec = float(_settings.BACKEND_HTTP_TIMEOUT_SEC)
            _mig_timeout_sec = float(_settings.MIGRATION_HTTP_TIMEOUT_SEC)
            self._http = httpx.Client(
                base_url=self._db_url,
                headers={
                    "Authorization": f"Basic {_auth}",
                    "surreal-ns": "yadgar",
                    "surreal-db": "main",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(
                    connect=2.0, read=_mig_timeout_sec, write=_mig_timeout_sec, pool=5.0
                ),
            )
            self._init_schema()
            # Reconfigure to operational timeout post-migration (httpx.Timeout is mutable)
            self._http.timeout = httpx.Timeout(
                connect=2.0, read=_http_timeout_sec, write=_http_timeout_sec, pool=5.0
            )
            # Pool-active / pool-wait metrics.  SurrealDB has no connection pool — the
            # httpx.Client is a singleton.  pool_active=1 for the lifetime of this engine.
            # pool_wait_ms gets one observation of 0.0 (no real acquire latency).
            try:
                from yadgar._shared.observability.metrics import (
                    yadgar_surrealdb_connection_pool_wait_ms,
                    yadgar_surrealdb_pool_active,
                )

                yadgar_surrealdb_pool_active.set(1)
                yadgar_surrealdb_connection_pool_wait_ms.observe(0.0)
            except ImportError:
                pass
            atexit.register(self.close)
            return

        # Embedded mode (existing behavior): single surrealkv connection with file lock.
        from surrealdb import Surreal

        resolved = Path(db_path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._resolved_path = resolved

        # surrealkv embedded mode does not support concurrent connections.
        # Use an exclusive file lock to ensure only one process owns the DB.
        self._lock_path = resolved.parent / "yadgar.lock"
        self._lock_file = open(self._lock_path, "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.write(str(os.getpid()))
            self._lock_file.flush()
        except OSError:
            self._lock_file.close()
            raise RuntimeError(
                f"Another yadgar process holds the DB lock ({self._lock_path}). "
                "surrealkv does not support concurrent access. "
                "Close other Claude sessions or kill stale yadgar processes."
            ) from None

        # Backup DB before opening — defense against crash corruption.
        # Keeps one rolling backup so we can restore if the clog is damaged.
        self._backup_path = resolved.parent / "surreal_db.bak"
        if resolved.exists():
            try:
                if self._backup_path.exists():
                    shutil.rmtree(self._backup_path)
                shutil.copytree(resolved, self._backup_path)
                _log.debug("DB backup created at %s", self._backup_path)
            except (OSError, shutil.Error) as e:
                _log.warning("DB backup failed (non-fatal): %s", e)

        self._embedded_db = Surreal(f"surrealkv://{resolved}")
        self._embedded_db.use("yadgar", "main")
        self._init_schema()

        # Health check: verify we can read field data, not just count records.
        # Detects corruption from prior crashes (records exist but fields are null).
        self._verify_health()

        # Pool-active / pool-wait metrics.  Embedded mode uses a single surrealkv
        # connection (no pool).  pool_active=1 for the lifetime of this engine.
        # pool_wait_ms gets one observation of 0.0 (no real acquire latency).
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_surrealdb_connection_pool_wait_ms,
                yadgar_surrealdb_pool_active,
            )

            yadgar_surrealdb_pool_active.set(1)
            yadgar_surrealdb_connection_pool_wait_ms.observe(0.0)
        except ImportError:
            pass

        # Register atexit handler for clean shutdown even if close() isn't called
        atexit.register(self.close)

    # ------------------------------------------------------------------ Context manager

    @observe(tier="stage")
    def close(self):
        # Unregister atexit to avoid double-close
        try:
            atexit.unregister(self.close)
        except Exception:  # noqa: BLE001 — close() is also the atexit handler; during interpreter shutdown the atexit machinery itself can be half-torn-down, and a raise here would replace whatever really went wrong
            pass
        # Signal connection gone regardless of mode.
        try:
            from yadgar._shared.observability.metrics import yadgar_surrealdb_pool_active

            yadgar_surrealdb_pool_active.set(0)
        except ImportError:
            pass
        if getattr(self, "_db_url", None):
            # Server mode: close the shared httpx client(s) — OWNER + optional RO.
            http = getattr(self, "_http", None)
            if http is not None:
                try:
                    http.close()
                except Exception:  # noqa: BLE001 — teardown: httpx client close reaches sockets and the transport's own cleanup, and the remaining close() steps below must still run
                    pass
            ro_http = getattr(self, "_http_ro", None)
            if ro_http is not None:
                try:
                    ro_http.close()
                except Exception:  # noqa: BLE001 — teardown: same as the OWNER client above, for the optional read-only client
                    pass
            return
        # Embedded mode: close DB and release file lock
        try:
            self._embedded_db.close()
        except Exception:  # noqa: BLE001 — teardown: the embedded SurrealKV SDK raises no common base, and the file-lock release below must still run
            pass
        # Release the file lock
        if hasattr(self, "_lock_file") and self._lock_file and not self._lock_file.closed:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
            except OSError:
                pass
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @observe(tier="stage")
    def _get_ro_http(self):
        """Return a lazily-built httpx.Client authed as the READ-ONLY VIEWER user.

        Server mode only. Mirrors the OWNER ``self._http`` construction but with
        the ``YADGAR_RO_USER``/``YADGAR_RO_PASS`` VIEWER credentials — the DB
        rejects writes over this connection regardless of query text (the real
        guard behind the ``/read_query`` debug surface, ADR-0078). Built on first
        use and reused (single connection, like the OWNER client); closed in
        ``close()``.

        Raises RuntimeError in embedded mode (no HTTP transport; the debug read
        surface is a server-mode-only capability).
        """
        if not getattr(self, "_db_url", None):
            raise RuntimeError(
                "_get_ro_http requires server mode (YADGAR_DB_URL set); the "
                "read-only DB inspection surface is not available in embedded mode."
            )
        existing = getattr(self, "_http_ro", None)
        if existing is not None:
            return existing
        import httpx

        _user, _pass = _resolve_ro_db_credentials()
        _auth = base64.b64encode(f"{_user}:{_pass}".encode()).decode()
        from yadgar._shared.config import get_settings as _get_settings

        _settings = _get_settings()
        _http_timeout_sec = float(_settings.BACKEND_HTTP_TIMEOUT_SEC)
        # Mirror the OWNER client's namespace/db so the RO connection reads the
        # SAME database. The OWNER client's surreal-db header may have been
        # rewritten (e.g. the test harness routes each StorageEngine to its own
        # per-path namespace) — hardcoding "main" would point the RO client at a
        # different, empty db where a SELECT triggers an implicit table-define
        # write ("read only transaction" error). Fall back to the production
        # defaults when the OWNER client is absent.
        _owner = getattr(self, "_http", None)
        _owner_headers = getattr(_owner, "headers", {}) if _owner is not None else {}
        _ns = _owner_headers.get("surreal-ns", "yadgar")
        _db = _owner_headers.get("surreal-db", "main")
        self._http_ro = httpx.Client(
            base_url=self._db_url,
            headers={
                "Authorization": f"Basic {_auth}",
                "surreal-ns": _ns,
                "surreal-db": _db,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                connect=2.0, read=_http_timeout_sec, write=_http_timeout_sec, pool=5.0
            ),
        )
        return self._http_ro
