"""DuckDB analytics exporter for Yadgar memory corpus.

Exports SurrealDB tables → typed DuckDB tables + 10 analytics views.
Lazy-imports duckdb: module-level import is intentionally absent so that
the CLI parser can be registered without duckdb installed.

§secret_gate: v5.10.2 secret-gate operates at write-time. No secret_flag
column exists on memory rows. --include-secrets is a forward-compat no-op
reserved for future row-level tagging schemas. The flag is accepted and
a banner printed when enabled, but no filter is applied today.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from yadgar.export.schema import (
    EXPORT_TABLE_NAMES,
    TABLE_COLUMNS,
    Column,
    build_create_table_ddl,
    build_junction_table_ddl,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Duration parsing helper
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([dhm])$", re.IGNORECASE)


def _parse_duration(spec: str) -> timedelta | None:
    """Parse '30d', '12h', '60m' → timedelta.  'all' → None (no filter)."""
    if spec.lower() == "all":
        return None
    m = _DURATION_RE.match(spec)
    if not m:
        raise ValueError(f"Invalid duration {spec!r}. Use Nd / Nh / Nm (e.g. 30d) or 'all'.")
    amount = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


# ---------------------------------------------------------------------------
# SurrealDB record-ID helpers
# ---------------------------------------------------------------------------


def _stringify_id(raw_id: Any) -> str:
    """Stringify a SurrealDB record ID (dict or string)."""
    if raw_id is None:
        return ""
    if isinstance(raw_id, dict):
        tbl = raw_id.get("tb", "")
        pk = raw_id.get("id", "")
        if isinstance(pk, dict):
            pk = json.dumps(pk)
        return f"{tbl}:{pk}"
    return str(raw_id)


def _split_record_id(raw_id: Any) -> tuple[str, str, str]:
    """Return (stringified_id, table, pk) from a SurrealDB record ID."""
    full = _stringify_id(raw_id)
    if ":" in full:
        tbl, pk = full.split(":", 1)
        return full, tbl, pk
    return full, "", full


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def _parse_ts(val: Any) -> str | None:
    """Convert SurrealDB timestamp to ISO string DuckDB can parse, or None."""
    if val is None:
        return None
    if isinstance(val, dict) and "secs_since_epoch" in val:
        # SurrealDB internal datetime struct
        secs = val["secs_since_epoch"]
        nanos = val.get("nanos_since_epoch", 0)
        ms = secs * 1_000 + nanos // 1_000_000
        from datetime import UTC, datetime

        dt = datetime.fromtimestamp(ms / 1_000, tz=UTC)
        return dt.isoformat()
    if isinstance(val, str):
        return val.replace("Z", "+00:00") if val else None
    return str(val)


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _parse_embedding(val: Any, dim: int) -> list[float] | None:
    """Parse embedding from SurrealDB (bytes or list) → float list of length dim."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        floats = [float(x) for x in val]
        if len(floats) == dim:
            return floats
        if len(floats) > dim:
            return floats[:dim]
        # Pad with zeros if shorter
        return floats + [0.0] * (dim - len(floats))
    if isinstance(val, (bytes, bytearray)):
        import struct

        n_floats = len(val) // 4
        floats = list(struct.unpack(f"{n_floats}f", val[: n_floats * 4]))
        return _parse_embedding(floats, dim)
    return None


# ---------------------------------------------------------------------------
# Lazy duckdb import
# ---------------------------------------------------------------------------


def _import_duckdb() -> Any:
    """Lazy-import duckdb; raise ImportError if not available."""
    return importlib.import_module("duckdb")


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _coerce_scalar(raw: Any, col_type: str) -> Any:
    """Coerce a scalar field value to the target DuckDB type."""
    if "TIMESTAMP" in col_type:
        return _parse_ts(raw)
    if col_type.startswith("FLOAT["):
        # Embedding — caller passes dim separately; return raw for now.
        # Caller uses _parse_embedding directly.
        return raw
    if col_type == "JSON":
        if raw is None:
            return None
        return raw if isinstance(raw, str) else json.dumps(raw)
    if col_type in ("DOUBLE", "FLOAT"):
        return float(raw) if raw is not None else None
    if col_type in ("BIGINT", "INTEGER"):
        return int(raw) if raw is not None else None
    if col_type == "BOOLEAN":
        return bool(raw) if raw is not None else None
    return str(raw) if raw is not None else None


def _resolve_id_column(duckdb_col: str, raw_id: Any) -> Any:
    """Resolve derived id / id_table / id_pk virtual columns."""
    full, tbl, pk = _split_record_id(raw_id)
    if duckdb_col == "id":
        return full
    if duckdb_col == "id_table":
        return tbl
    return pk  # id_pk


def _extract_row(
    row: dict,
    columns: list[Column],
    embedding_dim: int,
) -> dict:
    """Extract typed columns from a SurrealDB row dict.

    Returns a dict mapping duckdb_col → typed value, plus extra_fields JSON
    for any keys in the row that are not in the column spec.
    """
    result: dict[str, Any] = {}
    known_surreal_fields: set[str] = set()
    seen_duckdb_cols: set[str] = set()

    for col in columns:
        known_surreal_fields.add(col.surreal_field)
        if col.duckdb_col in seen_duckdb_cols:
            continue
        seen_duckdb_cols.add(col.duckdb_col)

        # Derived ID columns
        if col.duckdb_col in ("id", "id_table", "id_pk"):
            result[col.duckdb_col] = _resolve_id_column(col.duckdb_col, row.get("id"))
            continue

        raw = row.get(col.surreal_field)
        if col.duckdb_type.startswith("FLOAT["):
            result[col.duckdb_col] = _parse_embedding(raw, embedding_dim)
        else:
            result[col.duckdb_col] = _coerce_scalar(raw, col.duckdb_type)

    # extra_fields: keys in the row not in column spec
    extras = {
        k: v for k, v in row.items() if k not in known_surreal_fields and not k.startswith("_")
    }
    result["extra_fields"] = json.dumps(extras) if extras else None
    return result


# ---------------------------------------------------------------------------
# Main exporter class
# ---------------------------------------------------------------------------


class ExportConfig:
    """Configuration for a DuckDB export run.

    Extracted so DuckDBExporter.__init__ stays under the 8-arg lint cap.
    """

    def __init__(
        self,
        include_secrets: bool = False,
        action_log_since: str = "30d",
        action_log_limit: int = 100_000,
        create_views: bool = True,
        tables: list[str] | None = None,
        embedding_dim: int = 384,
        force: bool = False,
    ) -> None:
        self.include_secrets = include_secrets
        self.action_log_since = action_log_since
        self.action_log_limit = action_log_limit
        self.create_views = create_views
        self.tables = tables or EXPORT_TABLE_NAMES
        self.embedding_dim = embedding_dim
        self.force = force


class DuckDBExporter:
    """Export SurrealDB tables to a DuckDB analytics file.

    Pass db_path, output_path, and an ExportConfig.
    Convenience classmethod from_kwargs() available for callers that prefer
    flat keyword arguments (mirrors the old 9-arg signature).
    """

    def __init__(self, db_path: str, output_path: str, config: ExportConfig) -> None:
        self._db_path = db_path
        self._output_path = output_path
        self._cfg = config

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the export pipeline. Exits 2 if duckdb is not installed."""
        try:
            duckdb = _import_duckdb()
        except ImportError:
            print(
                "duckdb is not installed. Install the analytics extra:\n"
                "    pip install yadgar[analytics]",
                file=sys.stderr,
            )
            sys.exit(2)

        out = Path(self._output_path)
        if out.exists() and not self._cfg.force:
            print(
                f"Output file already exists: {out}\nUse --force to overwrite.",
                file=sys.stderr,
            )
            raise FileExistsError(f"Output file already exists: {out}")

        if out.exists() and self._cfg.force:
            out.unlink()

        if self._cfg.include_secrets:
            print(
                "WARNING: --include-secrets enabled. Export may contain sensitive data.",
                file=sys.stderr,
            )

        surreal_rows = self._read_from_surreal()
        self._write_to_duckdb(duckdb, surreal_rows)

    # ------------------------------------------------------------------
    # SurrealDB read path
    # ------------------------------------------------------------------

    def _read_from_surreal(self) -> dict[str, list[dict]]:
        """Read all table rows from SurrealDB. Returns table_name → rows."""
        from yadgar.storage import StorageEngine

        storage = StorageEngine(self._db_path, embedding_dim=self._cfg.embedding_dim)
        try:
            return self._fetch_all_tables(storage)
        finally:
            storage.close()

    def _fetch_all_tables(self, storage: Any) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        duration = _parse_duration(self._cfg.action_log_since)

        for table_name in self._cfg.tables:
            rows = self._fetch_table(storage, table_name, duration)
            result[table_name] = rows

        return result

    def _fetch_table(
        self,
        storage: Any,
        table_name: str,
        action_log_duration: timedelta | None,
    ) -> list[dict]:
        """Fetch rows for one table, with special handling for action_log."""
        try:
            if table_name == "action_log":
                return self._fetch_action_log(storage, action_log_duration)
            raw = storage._q(f"SELECT * FROM {table_name}")  # noqa: S608
            if raw and isinstance(raw[0], list):
                return raw[0]
            return raw or []
        except Exception as exc:
            _log.warning("Table %r not found or query failed: %s", table_name, exc)
            return []

    def _fetch_action_log(
        self,
        storage: Any,
        duration: timedelta | None,
    ) -> list[dict]:
        """Fetch action_log with optional time window and hard row cap."""
        if duration is not None:
            from datetime import UTC, datetime

            cutoff = (datetime.now(UTC) - duration).isoformat()
            try:
                raw = storage._q(
                    "SELECT * FROM action_log WHERE ts >= type::datetime($cutoff) ORDER BY ts DESC LIMIT $lim",
                    {"cutoff": cutoff, "lim": self._cfg.action_log_limit},
                )
            except Exception:
                raw = storage._q(
                    f"SELECT * FROM action_log "  # noqa: S608
                    f"ORDER BY id DESC LIMIT {self._cfg.action_log_limit}"
                )
        else:
            raw = storage._q(
                f"SELECT * FROM action_log ORDER BY id DESC LIMIT {self._cfg.action_log_limit}"  # noqa: S608
            )
        if raw and isinstance(raw[0], list):
            return raw[0]
        return raw or []

    # ------------------------------------------------------------------
    # DuckDB write path
    # ------------------------------------------------------------------

    def _write_to_duckdb(
        self,
        duckdb: Any,
        surreal_rows: dict[str, list[dict]],
    ) -> None:
        con = duckdb.connect(self._output_path)
        try:
            self._create_schema(con)
            self._insert_all_rows(con, surreal_rows)
            if self._cfg.create_views:
                self._execute_views(con)
        finally:
            con.close()

    def _create_schema(self, con: Any) -> None:
        """Create all DuckDB tables (DDL)."""
        for table_name in self._cfg.tables:
            cols = TABLE_COLUMNS.get(table_name, [])
            ddl = build_create_table_ddl(table_name, cols, self._cfg.embedding_dim)
            con.execute(ddl)
        # junction table for tag analytics
        con.execute(build_junction_table_ddl())

    def _insert_all_rows(
        self,
        con: Any,
        surreal_rows: dict[str, list[dict]],
    ) -> None:
        for table_name, rows in surreal_rows.items():
            cols = TABLE_COLUMNS.get(table_name, [])
            self._insert_table_rows(con, table_name, cols, rows)
        # populate memory_tag junction
        self._populate_memory_tag(con, surreal_rows.get("memory", []))

    def _insert_table_rows(
        self,
        con: Any,
        table_name: str,
        columns: list[Column],
        rows: list[dict],
    ) -> None:
        """Insert rows for one table via parameter binding."""
        if not rows:
            return

        import duckdb as _duckdb

        # Build column name list (deduplicated, in order)
        col_names: list[str] = []
        seen: set[str] = set()
        for col in columns:
            if col.duckdb_col not in seen:
                col_names.append(col.duckdb_col)
                seen.add(col.duckdb_col)
        col_names.append("extra_fields")

        placeholders = ", ".join(["?"] * len(col_names))
        col_list = ", ".join(col_names)
        insert_sql = f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"  # noqa: S608

        batch: list[tuple] = []
        for row in rows:
            mapped = _extract_row(row, columns, self._cfg.embedding_dim)
            values = tuple(mapped.get(c) for c in col_names)
            batch.append(values)

        try:
            con.executemany(insert_sql, batch)
        except _duckdb.Error as exc:
            _log.warning("Insert failed for %r: %s", table_name, exc)

    def _populate_memory_tag(
        self,
        con: Any,
        memory_rows: list[dict],
    ) -> None:
        """Populate memory_tag junction table from memory.tags."""
        batch: list[tuple[str, str]] = []
        for row in memory_rows:
            full_id, _, _ = _split_record_id(row.get("id"))
            tags = row.get("tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            for tag in tags:
                if tag and isinstance(tag, str):
                    batch.append((full_id, tag))
        if batch:
            con.executemany("INSERT INTO memory_tag (memory_id, tag) VALUES (?, ?)", batch)

    def _execute_views(self, con: Any) -> None:
        """Execute views.sql to create all analytics views."""
        try:
            views_path = Path(__file__).parent / "views.sql"
            sql = views_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            _log.warning("views.sql not found; skipping analytics views")
            return

        # Execute each statement (split on semicolons, skip empty/comment-only blocks)
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            # Strip leading comment lines to check if real SQL remains
            lines = stmt.splitlines()
            non_comment_lines = [ln for ln in lines if not ln.strip().startswith("--")]
            real_sql = "\n".join(non_comment_lines).strip()
            if not real_sql:
                continue
            try:
                con.execute(stmt)
            except Exception as exc:
                _log.warning("View creation failed: %s | stmt: %.80s", exc, stmt)
