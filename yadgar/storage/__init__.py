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
from collections.abc import Iterator
from pathlib import Path

from yadgar.storage.branch import BranchFilter as BranchFilter
from yadgar.storage.branch import _build_branch_clause as _build_branch_clause
from yadgar.storage.causal import _CausalMixin
from yadgar.storage.client import _CAMEL_CASE_RE as _CAMEL_CASE_RE
from yadgar.storage.client import _EMBEDDING_FIELDS as _EMBEDDING_FIELDS
from yadgar.storage.client import _FTS_STOP_WORDS as _FTS_STOP_WORDS
from yadgar.storage.client import _MEMORY_UPDATABLE_FIELDS as _MEMORY_UPDATABLE_FIELDS
from yadgar.storage.client import (
    _RELATIONSHIP_UPDATABLE_FIELDS as _RELATIONSHIP_UPDATABLE_FIELDS,
)
from yadgar.storage.client import _ClientMixin
from yadgar.storage.cluster import _ClusterMixin
from yadgar.storage.dbsize import _DbSizeMixin
from yadgar.storage.entity import RelationshipMeta as RelationshipMeta
from yadgar.storage.entity import _EntityMixin
from yadgar.storage.episode import _EpisodeMixin
from yadgar.storage.memory import _MemoryMixin
from yadgar.storage.migrations import _MIGRATIONS as _MIGRATIONS
from yadgar.storage.migrations import (
    _migration_001_hnsw_indexes as _migration_001_hnsw_indexes,
)
from yadgar.storage.migrations import (
    _migration_002_relationship_indexes as _migration_002_relationship_indexes,
)
from yadgar.storage.migrations import (
    _migration_003_memory_similarity_link_table as _migration_003_memory_similarity_link_table,
)
from yadgar.storage.migrations import (
    _migration_004_branch_field as _migration_004_branch_field,
)
from yadgar.storage.migrations import (
    _migration_006_source_memory_id as _migration_006_source_memory_id,
)
from yadgar.storage.migrations import _MigrationsMixin
from yadgar.storage.narrative import _NarrativeMixin
from yadgar.storage.ops import _OpsMixin
from yadgar.storage.queue import _QueueMixin
from yadgar.storage.rules import _RulesMixin
from yadgar.storage.user import _UserMixin
from yadgar.storage.vector import _VectorMixin
from yadgar.storage.wiki import _WikiMixin

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level enrichment pipeline singleton (shared across all insert_memory
# calls on any StorageEngine instance in the process).
# ---------------------------------------------------------------------------

_enrichment_pipeline = None


def _get_enrichment_pipeline(settings, embeddings_engine=None):
    global _enrichment_pipeline
    if _enrichment_pipeline is None:
        from yadgar.enrichment import EnrichmentPipeline

        _enrichment_pipeline = EnrichmentPipeline(settings, embeddings_engine)
    return _enrichment_pipeline


# ---------------------------------------------------------------------------
# _chunk_by_bytes — module-level helper used by batch_writes (client.py)
# ---------------------------------------------------------------------------


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
                _user = os.environ["YADGAR_DB_USER"]
                _pass = os.environ["YADGAR_DB_PASS"]
            _auth = base64.b64encode(f"{_user}:{_pass}".encode()).decode()
            from yadgar.config import get_settings as _get_settings

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
            except Exception as e:
                _log.warning("DB backup failed (non-fatal): %s", e)

        self._embedded_db = Surreal(f"surrealkv://{resolved}")
        self._embedded_db.use("yadgar", "main")
        self._init_schema()

        # Health check: verify we can read field data, not just count records.
        # Detects corruption from prior crashes (records exist but fields are null).
        self._verify_health()

        # Register atexit handler for clean shutdown even if close() isn't called
        atexit.register(self.close)

    # ------------------------------------------------------------------ Context manager

    def close(self):
        # Unregister atexit to avoid double-close
        try:
            atexit.unregister(self.close)
        except Exception:
            pass
        if getattr(self, "_db_url", None):
            # Server mode: close the shared httpx client.
            http = getattr(self, "_http", None)
            if http is not None:
                try:
                    http.close()
                except Exception:
                    pass
            return
        # Embedded mode: close DB and release file lock
        try:
            self._embedded_db.close()
        except Exception:
            pass
        # Release the file lock
        if hasattr(self, "_lock_file") and self._lock_file and not self._lock_file.closed:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            try:
                self._lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
