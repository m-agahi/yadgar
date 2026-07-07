"""Database size reporting.

_DbSizeMixin provides get_db_size(), which delegates to the backend
/admin/dbsize endpoint in server mode or walks the local filesystem in
embedded mode.

v5.1 A1: bearer-token fix — YADGAR_MCP_AUTH_TOKEN is passed as
Authorization: Bearer <token> to the /admin/dbsize endpoint.
"""

import logging
import os

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)

_ZERO_SIZE: dict = {
    "db_size_bytes": 0,
    "vlog_size_bytes": 0,
    "sstables_size_bytes": 0,
    "wal_size_bytes": 0,
    "other_size_bytes": 0,
    "vlog_pct_of_total": 0,
    "size_warning": False,
}


def _zero_size_dict() -> dict:
    """Return a zeroed-out db-size breakdown (error/unavailable sentinel)."""
    return dict(_ZERO_SIZE)


@observe(tier="hot")
def _resolve_embed_url(db_url: str) -> str | None:
    """Derive the embed-service URL from *db_url*, or return None.

    Checks YADGAR_BACKEND_EMBED_URL first; if not set, tries to replace
    :8000 with :8001.  Returns None when no URL can be derived.
    """
    explicit = os.environ.get("YADGAR_BACKEND_EMBED_URL")
    if explicit:
        return explicit
    db_url = db_url.rstrip("/")
    if ":8000" in db_url:
        return db_url.replace(":8000", ":8001")
    return None


@observe(tier="stage")
def _fetch_remote_db_size(embed_url: str, threshold: int, timeout_sec: float) -> dict | None:
    """GET /admin/dbsize from *embed_url*.

    Returns the parsed response dict (with size_warning injected) on success,
    or a zeroed dict on HTTP / timeout failure.  Returns None only when the
    caller should fall through to the local filesystem walk (currently never,
    but kept for future use).
    """
    import httpx as _httpx

    _auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    _headers = {"Authorization": f"Bearer {_auth_token}"} if _auth_token else {}
    _timeout = _httpx.Timeout(connect=2.0, read=timeout_sec, write=timeout_sec, pool=5.0)

    try:
        resp = _httpx.get(f"{embed_url}/admin/dbsize", headers=_headers, timeout=_timeout)
        resp.raise_for_status()
        data = resp.json()
        total = data.get("db_size_bytes", 0)
        data["size_warning"] = total > threshold
        return data
    except _httpx.HTTPStatusError as exc:
        _log.warning(
            "get_db_size: backend /admin/dbsize request failed: %s (status %s)",
            exc,
            exc.response.status_code,
        )
        return _zero_size_dict()
    except _httpx.TimeoutException as exc:
        _log.warning("backend timeout: get_db_size /admin/dbsize timed out: %s", exc)
        return _zero_size_dict()
    except Exception as exc:
        _log.warning("get_db_size: backend /admin/dbsize request failed: %s", exc)
        return _zero_size_dict()


@observe(tier="hot")
def _stat_size(path: str) -> int:
    """Return file size via stat(), or 0 on OSError."""
    import os as _os

    try:
        return _os.stat(path).st_size
    except OSError:
        return 0


@observe(tier="stage")
def _walk_local_db_size(db_path: os.PathLike | None, threshold: int) -> dict:
    """Walk *db_path* on the local filesystem and return a size breakdown dict."""
    import os as _os

    known_subdirs = {"vlog", "sstables", "wal"}
    size_by_dir: dict[str, int] = {k: 0 for k in known_subdirs}
    other_size = 0

    if db_path is None or not db_path.exists():
        total = 0
    else:
        for dirpath, _dirs, filenames in _os.walk(db_path):
            rel = _os.path.relpath(dirpath, db_path)
            top = rel.split(_os.sep)[0] if rel != "." else ""
            for fname in filenames:
                fsize = _stat_size(_os.path.join(dirpath, fname))
                if fsize == 0:
                    continue
                if top in known_subdirs:
                    size_by_dir[top] += fsize
                else:
                    other_size += fsize

        total = sum(size_by_dir.values()) + other_size

    vlog = size_by_dir["vlog"]
    vlog_pct = int(vlog * 100 / total) if total > 0 else 0

    return {
        "db_size_bytes": total,
        "vlog_size_bytes": vlog,
        "sstables_size_bytes": size_by_dir["sstables"],
        "wal_size_bytes": size_by_dir["wal"],
        "other_size_bytes": other_size,
        "vlog_pct_of_total": vlog_pct,
        "size_warning": total > threshold,
    }


class _DbSizeMixin:
    """DB size reporting — mixed into StorageEngine."""

    @observe(tier="stage")
    def get_db_size(self) -> dict:
        """Return a breakdown of the SurrealDB directory size in bytes.

        In server mode (YADGAR_DB_URL set), delegates to the backend's
        GET /admin/dbsize endpoint — the local db_path_resolved doesn't exist
        on the core container side.

        In embedded mode, walks DB_PATH using os.walk() + stat() — no subprocess.
        Subdirs of interest: vlog/, sstables/, wal/.  Anything else (LOCK,
        manifest, etc.) goes into other_size_bytes.
        """
        from yadgar._shared.config import get_settings as _get_settings

        settings = _get_settings()
        threshold = settings.DB_SIZE_WARNING_BYTES

        if self._db_url is not None:
            # Server mode: ask the backend container for the filesystem walk.
            embed_url = _resolve_embed_url(self._db_url)
            if embed_url is not None:
                timeout_sec = float(settings.BACKEND_HTTP_TIMEOUT_SEC)
                return _fetch_remote_db_size(embed_url, threshold, timeout_sec)
            # embed URL not derivable — fall through to local filesystem walk

        return _walk_local_db_size(settings.db_path_resolved, threshold)
