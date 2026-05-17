"""Database size reporting.

_DbSizeMixin provides get_db_size(), which delegates to the backend
/admin/dbsize endpoint in server mode or walks the local filesystem in
embedded mode.

v5.1 A1: bearer-token fix — YADGAR_MCP_AUTH_TOKEN is passed as
Authorization: Bearer <token> to the /admin/dbsize endpoint.
"""

import logging
import os

_log = logging.getLogger(__name__)


class _DbSizeMixin:
    """DB size reporting — mixed into StorageEngine."""

    def get_db_size(self) -> dict:
        """Return a breakdown of the SurrealDB directory size in bytes.

        In server mode (YADGAR_DB_URL set), delegates to the backend's
        GET /admin/dbsize endpoint — the local db_path_resolved doesn't exist
        on the core container side.

        In embedded mode, walks DB_PATH using os.walk() + stat() — no subprocess.
        Subdirs of interest: vlog/, sstables/, wal/.  Anything else (LOCK,
        manifest, etc.) goes into other_size_bytes.
        """
        from yadgar.config import get_settings as _get_settings

        settings = _get_settings()
        threshold = settings.DB_SIZE_WARNING_BYTES

        if self._db_url is not None:
            # Server mode: ask the backend container for the filesystem walk.
            # The embed service (FastAPI) shares the same container as SurrealDB
            # but listens on port 8001.  /admin/dbsize is served by that app.
            # Derive the embed-service URL from YADGAR_DB_URL (port 8000 → 8001).
            # If we cannot derive the embed URL (no :8000 and no explicit override),
            # fall through to the local filesystem walk below.
            import httpx as _httpx

            db_url = self._db_url.rstrip("/")
            explicit_embed_url = os.environ.get("YADGAR_BACKEND_EMBED_URL")
            if explicit_embed_url:
                embed_url: str | None = explicit_embed_url
            elif ":8000" in db_url:
                embed_url = db_url.replace(":8000", ":8001")
            else:
                embed_url = None

            if embed_url is not None:
                try:
                    _auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
                    _headers = {"Authorization": f"Bearer {_auth_token}"} if _auth_token else {}
                    resp = _httpx.get(f"{embed_url}/admin/dbsize", headers=_headers, timeout=5.0)
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
                    return {
                        "db_size_bytes": 0,
                        "vlog_size_bytes": 0,
                        "sstables_size_bytes": 0,
                        "wal_size_bytes": 0,
                        "other_size_bytes": 0,
                        "vlog_pct_of_total": 0,
                        "size_warning": False,
                    }
                except Exception as exc:
                    _log.warning("get_db_size: backend /admin/dbsize request failed: %s", exc)
                    return {
                        "db_size_bytes": 0,
                        "vlog_size_bytes": 0,
                        "sstables_size_bytes": 0,
                        "wal_size_bytes": 0,
                        "other_size_bytes": 0,
                        "vlog_pct_of_total": 0,
                        "size_warning": False,
                    }
            # embed URL not derivable — fall through to local filesystem walk

        import os as _os

        db_path = settings.db_path_resolved

        known_subdirs = {"vlog", "sstables", "wal"}
        size_by_dir: dict[str, int] = {k: 0 for k in known_subdirs}
        other_size = 0

        if not db_path.exists():
            total = 0
        else:
            for dirpath, _dirs, filenames in _os.walk(db_path):
                rel = _os.path.relpath(dirpath, db_path)
                # Determine which bucket this path belongs to.
                top = rel.split(_os.sep)[0] if rel != "." else ""
                for fname in filenames:
                    try:
                        fsize = _os.stat(_os.path.join(dirpath, fname)).st_size
                    except OSError:
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
