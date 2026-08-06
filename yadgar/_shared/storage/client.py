"""SurrealDB transport layer and low-level helper methods.

_ClientMixin provides:
  - _db property (embedded mode accessor)
  - _verify_health / _restore_from_backup
  - Byte/float conversion helpers (_bytes_to_floats, _floats_to_bytes)
  - Row normalisation helpers (_extract_id, _row_to_dict, _rows_to_dicts)
  - Counter helpers (_next_id, _reserve_ids, _now_iso)
  - Query execution (_q, _q_timeout)
  - Batch write machinery (_build_chunk_body, _send_chunk, batch_writes)
  - FTS content preprocessing (_enrich_content_for_fts, _preprocess_fts_query)

# Module size justified: single-responsibility transport layer. Every method is
# either a transport primitive (_q, batch_writes, _send_chunk), a helper that
# supports query execution (row normalisation, byte/float conversion, FTS
# preprocessing), or health/backup logic. The FTS helpers are inseparable from
# _preprocess_fts_query which is called directly by _q callers. Splitting would
# create circular dependencies since all other storage mixins depend on _ClientMixin.
"""

import json
import logging
import re
import struct
import time

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span

_log = logging.getLogger(__name__)

_CAMEL_CASE_RE = re.compile(r"([a-z])([A-Z])")

# Embedded SurrealDB (Python SDK v2) rejects an INTEGER second arg to the
# `type::record('table', $id)` builtin — "the second argument must be a table
# name or a string". Server mode (HTTP) accepts it. To keep ONE statement form
# across both modes, the embedded transport inlines `type::record('t', $p)` ->
# t:{int} when the param is an integer (the canonical record-id type), dropping
# the inlined param.
#
# Keep the SurrealQL call backticked and never let it START a comment line: a
# comment opening `# type:` is parsed by mypy as a PEP 484 type comment and
# rejected as invalid syntax, which aborts the whole mypy run.
_TYPE_RECORD_RE = re.compile(r"type::record\(\s*'(\w+)'\s*,\s*\$(\w+)\s*\)")

# Hard row-cap for the read-only DB inspection surface (_q_ro / POST /read_query /
# db_inspect). A module constant, NOT a knob (avoid I25 config-surface churn): the
# cap bounds how many rows the debug read can pull into an LLM's context. Callers
# may clamp LOWER but must never raise it.
_RO_QUERY_ROW_CAP: int = 500


@observe(tier="hot")
def _inline_int_record_ids(surql: str, params: dict | None) -> tuple[str, dict | None]:
    """Rewrite type::record('t', $p) -> t:{int} for integer params (embedded only).

    Returns (surql, params) with integer-id params inlined + removed. A param is
    only dropped if it no longer appears as $p anywhere in the rewritten SQL
    (guards against a param reused outside the type::record call).
    """
    if not params or "type::record" not in surql:
        return surql, params
    inlined: set[str] = set()

    def _sub(m: re.Match) -> str:
        table, pname = m.group(1), m.group(2)
        val = params.get(pname)
        if isinstance(val, int) and not isinstance(val, bool):
            inlined.add(pname)
            return f"{table}:{val}"
        return m.group(0)

    new_surql = _TYPE_RECORD_RE.sub(_sub, surql)
    if not inlined:
        return surql, params
    new_params = {k: v for k, v in params.items() if k not in inlined or f"${k}" in new_surql}
    return new_surql, new_params


_FTS_STOP_WORDS = frozenset(
    {
        # Standard English stop words
        "a",
        "an",
        "the",
        "is",
        "it",
        "in",
        "on",
        "at",
        "to",
        "of",
        "for",
        "and",
        "or",
        "but",
        "not",
        "with",
        "by",
        "from",
        "as",
        "be",
        "was",
        "were",
        "been",
        "are",
        "am",
        "do",
        "did",
        "does",
        "has",
        "had",
        "have",
        "will",
        "would",
        "could",
        "should",
        "may",
        "can",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "if",
        "then",
        "so",
        "no",
        "yes",
        "all",
        "any",
        "some",
        "my",
        "your",
        "its",
        "our",
        "their",
        "we",
        "he",
        "she",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        # Coding/conversation domain stop words
        "use",
        "using",
        "used",
        "like",
        "just",
        "get",
        "got",
        "set",
        "make",
        "made",
        "let",
        "try",
        "need",
        "want",
        "know",
        "think",
        "code",
        "file",
        "thing",
        "stuff",
    }
)

# Embedding fields that hold float arrays in SurrealDB and must be converted to bytes on read
_EMBEDDING_FIELDS = ("embedding", "centroid_embedding", "implicit_embedding")

_MEMORY_UPDATABLE_FIELDS = frozenset(
    {
        "content",
        "tags",
        "embedding",
        "embedding_model",
        "contextual_prefix",
        "heat",
        "importance",
        "surprise_score",
        "emotional_valence",
        "is_protected",
        "is_stale",
        "is_prospective",
        "compressed",
        "store_type",
        "cluster_id",
        "wiki_refs",
        "compression_level",
        "file_hash",
        "provenance_agent",
        "vector_clock",
        "branch",
        # v5.8.0 anchor hygiene fields
        "tier",
        "valid_until",
        "migration_grace",
        # v5.17.0 contradiction confidence decay
        "confidence",
        # v5.35.1 — were missing since initial implementation (same class as v5.17.0 confidence fix)
        "last_accessed",
        "access_count",
        # v5.54.1 — precomputed graph prior (consolidation phase, additive boost in fusion)
        "graph_prior",
        # v5.54.2 — precomputed co-recall (transition-edge) prior (consolidation phase, additive boost in fusion)
        "cofire_prior",
        # v5.73.0 — shadow gate fields (surprise_score = gate surprisal; would_reject = shadow decision)
        # surprise_score was already present; would_reject is new.
        "would_reject",
    }
)

_RELATIONSHIP_UPDATABLE_FIELDS = frozenset(
    {
        "is_causal",
        "weight",
        "confidence",
        "relationship_type",
        "event_time",
        "record_time",
    }
)


@observe(tier="hot")
def _normalize_rows(raw) -> list:
    """Normalise a raw SurrealDB response to a flat list of dicts.

    Handles None, dict, flat list, and list-of-lists (embedded SDK wraps
    results in an outer list on some code paths).
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        if not raw:
            return []
        first = raw[0]
        if isinstance(first, list):
            return first
        return raw
    return []


# A valid SurrealQL bind-parameter name. The HTTP `/sql` query paths interpolate
# the param NAME directly into the query text (`LET $<name> = <json-value>`)
# because SurrealDB's `/sql` endpoint exposes no JSON bind-var facility — only
# string-typed URL query params (see `_q_server`). Param VALUES are JSON-escaped
# and cannot break out of their literal, but an unvalidated NAME would be a
# SurrealQL-injection vector. Any legitimate bind name already matches this shape.
_PARAM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_param_keys(params: dict | None) -> None:
    """Reject param names that are not valid SurrealQL identifiers.

    Called on every param dict before it is interpolated into a `LET $name = …`
    statement. This makes name-injection structurally impossible rather than
    relying on every current and future caller to pass only internal, trusted
    keys. A rejected name raises ``ValueError`` (a caller/programming error, or —
    on the `db_inspect` read path — hostile input, which now fails cleanly
    instead of producing a confusing DB error).
    """
    if not params:
        return
    for k in params:
        if not isinstance(k, str) or not _PARAM_KEY_RE.match(k):
            raise ValueError(f"Invalid SurrealQL parameter name: {k!r}")


@observe(tier="hot")
def _prefix_param_tokens(sql: str, params: dict, i: int) -> str:
    """Rewrite ``$k`` parameter tokens in *sql* to ``$p{i}_{k}``.

    Uses a character-level state machine to skip over single- and double-quoted
    string literals so that ``$k`` inside a quoted string is never rewritten.
    Only standalone parameter tokens (preceded by ``$``, followed by a
    non-identifier character or end-of-string) are replaced.
    """
    new_sql_parts: list[str] = []
    in_quote = False
    quote_char = ""
    current: list[str] = []
    pos = 0
    while pos < len(sql):
        ch = sql[pos]
        if not in_quote and ch in ("'", '"'):
            # Flush pending non-quoted segment and rewrite params in it.
            segment = "".join(current)
            for k in params:
                segment = re.sub(rf"\${re.escape(k)}(?=[^A-Za-z0-9_]|$)", f"$p{i}_{k}", segment)
            new_sql_parts.append(segment)
            current = [ch]
            in_quote = True
            quote_char = ch
        elif in_quote and ch == quote_char and (pos == 0 or sql[pos - 1] != "\\"):
            current.append(ch)
            new_sql_parts.append("".join(current))
            current = []
            in_quote = False
            quote_char = ""
        else:
            current.append(ch)
        pos += 1
    # Flush remainder — rewrite only if we ended outside a quoted string.
    segment = "".join(current)
    if not in_quote:
        for k in params:
            segment = re.sub(rf"\${re.escape(k)}(?=[^A-Za-z0-9_]|$)", f"$p{i}_{k}", segment)
    new_sql_parts.append(segment)
    return "".join(new_sql_parts)


def _sql_op(surql: str) -> str:
    """Extract the first SQL keyword from a SurrealQL statement for the op label.

    Returns the uppercased first token (e.g. "SELECT", "CREATE", "UPSERT", "DELETE",
    "UPDATE", "INFO", "BEGIN", "COMMIT") or "OTHER" if the statement is empty.
    """
    first = surql.lstrip().split(None, 1)[0].upper() if surql.strip() else "OTHER"
    return first


@observe(tier="hot")
def _observe_query_metrics(surql: str, elapsed_s: float) -> None:
    """Observe DB-layer query histogram for a single query execution.

    - yadgar_surrealdb_query_duration_ms: labelled op=<first keyword>, milliseconds.
    """
    try:
        from yadgar._shared.observability.metrics import (
            yadgar_surrealdb_query_duration_ms,
        )

        op = _sql_op(surql)
        yadgar_surrealdb_query_duration_ms.labels(op=op).observe(elapsed_s * 1000.0)
    except Exception:
        # Never let metrics errors crash a query.
        pass


class _ClientMixin:
    """SurrealDB transport + low-level helpers — mixed into StorageEngine."""

    @property
    def _db(self):
        """Embedded mode only. Raises if called in server mode — use _q() instead."""
        if self._db_url:
            raise RuntimeError("_db accessed in server mode — use _q() instead")
        return self._embedded_db

    @observe(tier="stage")
    def _verify_health(self):
        """Post-startup health check — detect corrupted DB state."""
        try:
            count_rows = self._q("SELECT count() AS c FROM memory GROUP ALL")
            total = int(count_rows[0]["c"]) if count_rows else 0
            if total == 0:
                return  # Empty DB, nothing to check

            heat_rows = self._q("SELECT math::mean(heat) AS avg FROM memory GROUP ALL")
            avg_heat = (
                float(heat_rows[0]["avg"])
                if heat_rows and heat_rows[0].get("avg") is not None
                else 0.0
            )

            if total > 0 and avg_heat == 0.0:
                _log.warning(
                    "DB health check: %d memories but avg_heat=0.0 — possible corruption. "
                    "Attempting restore from backup.",
                    total,
                )
                if self._backup_path.exists():
                    self._restore_from_backup()
                else:
                    _log.error("No backup available to restore from.")
        except Exception as e:
            _log.warning("DB health check failed: %s", e)

    @observe(tier="stage")
    def _restore_from_backup(self):
        """Restore DB from the rolling backup after detecting corruption."""
        try:
            self._embedded_db.close()
        except Exception:
            _log.warning("Failed to close embedded DB before backup restore", exc_info=True)

        from surrealdb import Surreal

        resolved = self._resolved_path
        import shutil

        _log.warning("Restoring DB from backup %s", self._backup_path)
        try:
            shutil.rmtree(resolved)
            shutil.copytree(self._backup_path, resolved)
            self._embedded_db = Surreal(f"surrealkv://{resolved}")
            self._embedded_db.use("yadgar", "main")
            self._init_schema()
            _log.warning("DB restored from backup successfully.")
        except Exception as e:
            _log.error("DB restore failed: %s", e)

    # ------------------------------------------------------------------ helpers

    @observe(tier="hot")
    def _bytes_to_floats(self, data: bytes, expected_dim: int | None = None) -> list[float]:
        # Q20: validate alignment and optional dimension match
        if len(data) % 4 != 0:
            raise ValueError(f"_bytes_to_floats: data length {len(data)} is not divisible by 4")
        n = len(data) // 4
        if expected_dim is not None and n != expected_dim:
            raise ValueError(f"_bytes_to_floats: got {n} floats but expected_dim={expected_dim}")
        return list(struct.unpack(f"<{n}f", data))

    def _floats_to_bytes(self, floats: list[float]) -> bytes:
        return struct.pack(f"<{len(floats)}f", *floats)

    @observe(tier="hot", span=False)
    def _extract_id(self, record_id) -> int | None:
        if record_id is None:
            return None
        if hasattr(record_id, "id") and hasattr(record_id, "table_name"):
            return int(record_id.id)
        if isinstance(record_id, str) and ":" in record_id:
            return int(record_id.split(":")[1])
        return int(record_id)

    @observe(tier="hot")
    def _next_id(self, table: str) -> int:
        rows = self._q(f"UPSERT counter:{table} SET val = (val ?? 0) + 1")
        if rows:
            return int(rows[0].get("val", 1))
        return 1

    @observe(tier="stage")
    def _reserve_ids(self, table: str, n: int) -> list[int]:
        """Reserve n consecutive IDs in one HTTP request.

        Returns [max-n+1, ..., max] so they can be assigned to n records.
        """
        rows = self._q(f"UPSERT counter:{table} SET val = (val ?? 0) + {n}")
        top = int(rows[0].get("val", n)) if rows else n
        return list(range(top - n + 1, top + 1))

    @observe(tier="hot", span=False)
    def _row_to_dict(self, record: dict | None) -> dict | None:
        if record is None:
            return None
        d = dict(record)
        # Convert RecordID id to int
        if "id" in d:
            d["id"] = self._extract_id(d["id"])
        # Convert embedding float arrays -> bytes
        for emb_field in _EMBEDDING_FIELDS:
            if emb_field in d and isinstance(d[emb_field], list):
                d[emb_field] = self._floats_to_bytes(d[emb_field])
        # JSON fields — SurrealDB stores them as native lists; ensure they are lists
        for json_field in (
            "tags",
            "key_decisions",
            "key_events",
            "memory_ids",
            "entity_ids",
            "evidence_memory_ids",
            "files_being_edited",
            "open_questions",
            "next_steps",
            "active_errors",
        ):
            if json_field in d and isinstance(d[json_field], str):
                d[json_field] = json.loads(d[json_field])
        # Booleans
        for bool_field in (
            "archived",
            "is_stale",
            "is_prospective",
            "is_causal",
            "is_active",
            "compressed",
            "is_protected",
            "is_validated",
        ):
            if bool_field in d:
                d[bool_field] = bool(d[bool_field])
        return d

    def _rows_to_dicts(self, rows: list[dict]) -> list[dict]:
        return [self._row_to_dict(r) for r in rows if r is not None]

    def _now_iso(self) -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()

    @observe(tier="stage")
    def _q_timeout(self, surql: str, params: dict | None = None, timeout: float = 30.0) -> list:
        """Like _q but with a per-request timeout (seconds).

        Uses httpx's per-request timeout in server mode.  In embedded mode falls
        back to _q (the SDK doesn't support per-call timeouts).
        """
        import json as _json

        _t0 = time.perf_counter()
        if self._db_url:
            if params:
                lets = [
                    f"LET ${k} = {_json.dumps(v, ensure_ascii=False)};" for k, v in params.items()
                ]
                body = "\n".join(lets) + "\n" + surql
            else:
                body = surql
            resp = self._http.post(
                "/sql",
                content=body.encode(),
                headers={"Content-Type": "text/plain"},
                timeout=timeout,
            )
            resp.raise_for_status()
            results = resp.json()
            for entry in results:
                if entry.get("status") == "ERR":
                    raise RuntimeError(
                        f"SurrealDB error: {entry.get('detail') or entry.get('result') or entry}"
                    )
            raw = results[-1].get("result") if results else None
            _observe_query_metrics(surql, time.perf_counter() - _t0)
        else:
            # Embedded mode: delegate to _q (no per-call timeout in the SDK).
            # Metrics are observed inside _q.
            return self._q(surql, params)

        # Normalise to flat list of dicts (same as _q).
        return _normalize_rows(raw)

    @observe(tier="stage")
    def _q_ro(
        self,
        surql: str,
        params: dict | None = None,
        *,
        timeout_ms: int = 5000,
        row_cap: int = _RO_QUERY_ROW_CAP,
    ) -> tuple[list, bool]:
        """Run *surql* on the READ-ONLY (VIEWER-role) DB connection.

        This is the safety-critical read path behind the ``/read_query`` debug
        surface (ADR-0078). It runs on ``_get_ro_http()`` (the ``yadgar-ro``
        VIEWER-authed httpx client) — a write over this connection does NOT
        persist regardless of the query text. This is the REAL guard; the
        parse-guard in the route is defense-in-depth only.

        NOTE (empirical, 2026-07-16): SurrealDB VIEWER signals write-refusal
        INCONSISTENTLY — a hard "read only transaction" RuntimeError when the
        write implies DDL (auto-defining a table), but a SILENT status=OK /
        result=None no-op for a record write to an existing table. The guarantee
        is "no mutation persists", not "the write errors" — callers must NOT rely
        on _q_ro raising on a write attempt.

        Applies a hard row cap post-fetch (``row_cap``, ceiling ``_RO_QUERY_ROW_CAP``
        = 500) and the per-call httpx timeout from ``timeout_ms``.

        Returns ``(rows, truncated)`` where ``truncated`` is True iff the result
        was capped (more rows existed than were returned).

        Server mode only — raises RuntimeError in embedded mode (via ``_get_ro_http``).
        """
        import json as _json

        # Never let a caller raise the ceiling; clamp to the module hard cap.
        effective_cap = min(int(row_cap), _RO_QUERY_ROW_CAP)

        client = self._get_ro_http()
        if params:
            _validate_param_keys(params)
            lets = [f"LET ${k} = {_json.dumps(v, ensure_ascii=False)};" for k, v in params.items()]
            body = "\n".join(lets) + "\n" + surql
        else:
            body = surql

        _t0 = time.perf_counter()
        resp = client.post(
            "/sql",
            content=body.encode(),
            headers={"Content-Type": "text/plain"},
            timeout=max(0.001, timeout_ms / 1000.0),
        )
        resp.raise_for_status()
        results = resp.json()
        # Raise on any SurrealDB-level error (HTTP is always 200). A write over the
        # VIEWER connection MAY surface here as an ERR entry (when it implies DDL,
        # e.g. auto-defining a table → "read only transaction"), but may also
        # SILENTLY no-op with status=OK — either way it does not persist. This
        # error-raise is for genuine query errors; write-safety is proven by the
        # VIEWER role not persisting, asserted via read-back in the go/no-go test.
        for entry in results:
            if entry.get("status") == "ERR":
                raise RuntimeError(
                    f"SurrealDB error: {entry.get('detail') or entry.get('result') or entry}"
                )
        raw = results[-1].get("result") if results else None
        _observe_query_metrics(surql, time.perf_counter() - _t0)

        rows = _normalize_rows(raw)
        truncated = len(rows) > effective_cap
        if truncated:
            rows = rows[:effective_cap]
        return rows, truncated

    @observe(tier="stage")
    def _q_server(self, surql: str, params: dict | None) -> object:
        """Execute *surql* via HTTP POST and return the raw result object.

        Server mode only.  Raises RuntimeError on any SurrealDB-level error.

        ensure_ascii=False so emoji and other non-ASCII pass as UTF-8; SurrealDB v3
        rejects \\uD800–\\uDFFF surrogate pairs that json.dumps emits with ensure_ascii=True.

        Params are bound via ``LET $k = <json-value>`` prepended to the query.
        SurrealDB's HTTP ``/sql`` endpoint has NO JSON bind-var body (only
        string-typed URL query params — https://surrealdb.com/docs/surrealdb/integration/http),
        so this LET form is the only way to bind typed/complex values over HTTP.
        Values are JSON-escaped (breakout-safe); names are validated as
        identifiers by ``_validate_param_keys`` so the name cannot inject either.
        A true native-bind migration would mean moving off ``/sql`` to the RPC
        ``query(sql, vars)`` endpoint — a larger change tracked separately.
        """
        import json as _json

        if params:
            _validate_param_keys(params)
            lets = [f"LET ${k} = {_json.dumps(v, ensure_ascii=False)};" for k, v in params.items()]
            body = "\n".join(lets) + "\n" + surql
        else:
            body = surql
        resp = self._http.post(
            "/sql", content=body.encode(), headers={"Content-Type": "text/plain"}
        )
        resp.raise_for_status()
        results = resp.json()
        # Raise on any SurrealDB-level error (HTTP is always 200).
        for entry in results:
            if entry.get("status") == "ERR":
                raise RuntimeError(
                    f"SurrealDB error: {entry.get('detail') or entry.get('result') or entry}"
                )
        # Last entry is the actual query result (LET entries precede it).
        return results[-1].get("result") if results else None

    @observe(tier="stage")
    def _q_embedded(self, surql: str, params: dict | None) -> object:
        """Execute *surql* via the embedded SurrealKV SDK and return the raw result.

        Embedded mode only.  Read-only statements (SELECT / INFO / SHOW) are
        retried once on failure to handle transient SDK errors; write statements
        are never retried to prevent double-writes (§5 Q3).
        """
        # Embedded SDK rejects integer type::record($id) — inline to t:{int}.
        surql, params = _inline_int_record_ids(surql, params)
        _surql_upper = surql.lstrip().upper()
        _is_readonly = any(
            _surql_upper.startswith(kw) for kw in ("SELECT", "INFO FOR", "INFO", "SHOW")
        )
        # Non-readonly: attempt once; guard clause avoids retry overhead.
        if not _is_readonly:
            return self._embedded_db.query(surql, params or {})
        try:
            return self._embedded_db.query(surql, params or {})
        except Exception as exc:
            _log.debug("Embedded DB error (%s), retrying…", exc)
            return self._embedded_db.query(surql, params or {})

    @observe(tier="stage")
    def _q(self, surql: str, params: dict | None = None) -> list:
        """Run a parameterised query via HTTP (server mode) or embedded SDK.

        Returns rows as a flat list of dicts.
        """
        _t0 = time.perf_counter()
        if self._db_url:
            raw = self._q_server(surql, params)
        else:
            raw = self._q_embedded(surql, params)
        _observe_query_metrics(surql, time.perf_counter() - _t0)
        return _normalize_rows(raw)

    @observe(tier="stage")
    def _q_multi(self, statements: list[tuple[str, dict | None]]) -> list[list]:
        """Run N read statements in ONE round-trip; return one row-list per statement.

        Unlike ``_q`` (which flattens the whole response to a single list via
        ``_normalize_rows`` and, in server mode, returns only the LAST statement's
        result), ``_q_multi`` preserves PER-STATEMENT attribution: the return is a
        list positionally aligned with ``statements``, where element *i* is the
        normalised rows of statement *i*.

        This is the read-side counterpart of ``batch_writes``: the SurrealDB
        multi-statement HTTP response contains one entry per statement (proven by
        ``_send_chunk`` iterating ``results`` one-per-statement), and the embedded
        SDK's ``query`` returns one result set per statement. Both are unwrapped
        positionally here.

        READ-ONLY: intended for SELECT statements only (no BEGIN/COMMIT framing, so
        it is not atomic — callers must not mix writes). Per-statement params are
        prefixed (``$p{i}_{k}``) so identically-named params in different statements
        never collide (same tokeniser-safe rewrite ``batch_writes`` uses).

        Empty input → ``[]``. Raises RuntimeError on any SurrealDB-level error
        (mirrors ``_q_server``); callers that need graceful degradation catch it and
        replay per-statement via ``_q``.
        """
        if not statements:
            return []
        _t0 = time.perf_counter()
        if self._db_url:
            result = self._q_multi_server(statements)
        else:
            result = self._q_multi_embedded(statements)
        _observe_query_metrics("_q_multi", time.perf_counter() - _t0)
        return result

    @observe(tier="stage")
    def _q_multi_server(self, statements: list[tuple[str, dict | None]]) -> list[list]:
        """Server-mode multi-statement read — one HTTP POST, per-statement results."""
        import json as _json

        body_parts: list[str] = []
        # A statement with N params emits N LET entries BEFORE its SELECT entry.
        # Track, per statement, how many response entries precede its own result so
        # we can pick out the right one positionally.
        let_counts: list[int] = []
        for i, (sql, params) in enumerate(statements):
            n_lets = 0
            if params:
                _validate_param_keys(params)
                for k, v in params.items():
                    body_parts.append(f"LET $p{i}_{k} = {_json.dumps(v, ensure_ascii=False)};")
                    n_lets += 1
                sql = _prefix_param_tokens(sql, params, i)
            body_parts.append(sql.rstrip(";") + ";")
            let_counts.append(n_lets)

        body = "\n".join(body_parts)
        resp = self._http.post(
            "/sql", content=body.encode(), headers={"Content-Type": "text/plain"}
        )
        resp.raise_for_status()
        entries = resp.json()
        for entry in entries:
            if entry.get("status") == "ERR":
                raise RuntimeError(
                    f"SurrealDB error: {entry.get('detail') or entry.get('result') or entry}"
                )
        # Walk the flat entry list, consuming n_lets LET-entries then one SELECT-entry
        # per statement, in order.
        out: list[list] = []
        idx = 0
        for n_lets in let_counts:
            idx += n_lets
            raw = entries[idx].get("result") if idx < len(entries) else None
            out.append(_normalize_rows(raw))
            idx += 1
        return out

    @observe(tier="stage")
    def _q_multi_embedded(self, statements: list[tuple[str, dict | None]]) -> list[list]:
        """Embedded-mode multi-statement read — one SDK query, per-statement results."""
        import json as _json

        body_parts: list[str] = []
        for i, (sql, params) in enumerate(statements):
            if params:
                _validate_param_keys(params)
                for k, v in params.items():
                    body_parts.append(f"LET $p{i}_{k} = {_json.dumps(v, ensure_ascii=False)};")
                sql = _prefix_param_tokens(sql, params, i)
            body_parts.append(sql.rstrip(";") + ";")
        combined = "\n".join(body_parts)
        raw = self._embedded_db.query(combined, {})
        # The embedded SDK returns a list with one element PER statement (LET returns
        # None, SELECT returns its rows). Filter to the SELECT results positionally by
        # replaying the same LET/SELECT structure.
        return self._split_embedded_multi(statements, raw)

    @staticmethod
    @observe(tier="hot")
    def _split_embedded_multi(statements: list[tuple[str, dict | None]], raw: object) -> list[list]:
        """Map an embedded multi-statement raw response to per-SELECT row-lists.

        The embedded SDK yields one result element per executed statement in order
        (LET statements included). We know how many LETs precede each SELECT, so we
        walk the flat list the same way ``_q_multi_server`` does.
        """
        entries = raw if isinstance(raw, list) else [raw]
        out: list[list] = []
        idx = 0
        for _sql, params in statements:
            idx += len(params) if params else 0
            elem = entries[idx] if idx < len(entries) else None
            out.append(_normalize_rows(elem))
            idx += 1
        return out

    @staticmethod
    @observe(tier="hot")
    def _build_chunk_body(chunk: list[tuple[str, dict | None]], json_mod: object) -> bytes:
        """Build the actual HTTP body for a single BEGIN…COMMIT transaction chunk.

        Returns the UTF-8 encoded body exactly as it would be sent to SurrealDB,
        so callers can measure its real size before POSTing.

        §5 Q7: param names are prefixed per-statement using a tokeniser-safe
        word-boundary replacement so '$id' inside a SQL string literal is never
        accidentally rewritten.  Each $k is replaced only when it appears as a
        standalone token (word boundary on both sides, not inside quotes).
        The rewrite is delegated to the module-level ``_prefix_param_tokens``.
        """
        parts = ["BEGIN TRANSACTION"]
        for i, (sql, params) in enumerate(chunk):
            if params:
                _validate_param_keys(params)
                for k, v in params.items():
                    parts.append(f"LET $p{i}_{k} = {json_mod.dumps(v, ensure_ascii=False)}")
                sql = _prefix_param_tokens(sql, params, i)
            parts.append(sql.rstrip(";"))
        parts.append("COMMIT TRANSACTION")
        return (";\n".join(parts) + ";").encode()

    @observe(tier="stage")
    def _send_chunk(
        self,
        chunk: list[tuple[str, dict | None]],
        max_bytes: int,
        json_mod: object,
    ) -> None:
        """Build the real HTTP body for *chunk* and POST it.

        If the real body exceeds *max_bytes* and the chunk has more than one
        statement, split it in half and recurse.  A single-statement chunk is
        always attempted (with a WARN) so we never silently drop work.
        """
        body = self._build_chunk_body(chunk, json_mod)
        if len(body) > max_bytes:
            if len(chunk) == 1:
                _log.warning(
                    "batch_writes: single statement real body %d bytes exceeds "
                    "MAX_BATCH_BYTES=%d; attempting alone — expect possible 413",
                    len(body),
                    max_bytes,
                )
                # Fall through and attempt the request anyway.
            else:
                mid = len(chunk) // 2
                self._send_chunk(chunk[:mid], max_bytes, json_mod)
                self._send_chunk(chunk[mid:], max_bytes, json_mod)
                return

        resp = self._http.post("/sql", content=body, headers={"Content-Type": "text/plain"})
        resp.raise_for_status()
        results = resp.json()
        for entry in results:
            if entry.get("status") == "ERR":
                raise RuntimeError(
                    f"SurrealDB batch error: {entry.get('detail') or entry.get('result') or entry}"
                )

    @trace_span()
    def batch_writes(self, statements: list[tuple[str, dict | None]]) -> None:
        """Execute multiple write statements against SurrealDB.

        Statements are split into chunks limited by *both* MAX_BATCH_STATEMENTS
        (default 500 rows) *and* MAX_BATCH_BYTES (default 1 MB of serialised
        body).  Whichever limit fires first starts a new chunk.  Each chunk
        becomes its own BEGIN…COMMIT transaction so we never build a single
        unbounded SQL string that can crash SurrealDB's recursive serialiser or
        exceed the HTTP body limit (HTTP 413 Payload Too Large).

        The byte cap is enforced by measuring the *real* HTTP body string after
        all parameter substitution and framing — not an estimate.  If a chunk's
        real body exceeds MAX_BATCH_BYTES it is split in half recursively until
        every piece fits (or until a single statement remains, which is attempted
        alone with a WARN).

        Each chunk is atomic in itself; a failure in one chunk does NOT roll back
        earlier chunks — callers that require strict all-or-nothing must keep
        batches small enough to fit in a single chunk.

        Empty list is a no-op (no transaction sent).

        Server mode chunks into BEGIN…COMMIT HTTP transactions. Embedded mode
        (no YADGAR_DB_URL — e.g. the nightly consolidation cycle) has no HTTP
        transaction; statements run per-statement via _q (which applies the
        embedded type::record inline rewrite). Per-statement embedded execution
        is not cross-statement atomic — acceptable, since server mode is also only
        per-chunk atomic, and the nightly cycle is single-writer.
        """
        if not statements:
            return
        if self._db_url is None:
            for sql, params in statements:
                self._q(sql, params)
            return

        from yadgar._shared.config import get_settings

        settings = get_settings()
        chunk_size = settings.MAX_BATCH_STATEMENTS
        max_bytes = settings.MAX_BATCH_BYTES

        import json as _json

        # First pass: split by statement count.  _chunk_by_bytes provides a
        # cheap pre-split so we don't build enormous bodies before measuring;
        # _send_chunk then measures the real body and recursively halves if
        # needed.
        from yadgar._shared.storage import _chunk_by_bytes

        for count_chunk_start in range(0, len(statements), chunk_size):
            count_chunk = statements[count_chunk_start : count_chunk_start + chunk_size]
            for chunk in _chunk_by_bytes(count_chunk, max_bytes):
                self._send_chunk(chunk, max_bytes, _json)

    @observe(tier="hot")
    def _enrich_content_for_fts(self, content: str) -> str:
        """Enrich content with split identifier tokens for better FTS matching."""
        tokens = content.split()
        extra_tokens = []
        for token in tokens:
            split = _CAMEL_CASE_RE.sub(r"\1 \2", token)
            split = split.replace("_", " ")
            sub_tokens = split.split()
            if len(sub_tokens) > 1:
                extra_tokens.extend(t for t in sub_tokens if t != token)
        if extra_tokens:
            return content + " " + " ".join(extra_tokens)
        return content

    @observe(tier="hot")
    def _preprocess_fts_query(self, query: str) -> str:
        """Preprocess query for SurrealDB full-text search.

        SurrealDB's analyzer handles tokenization, lowercasing, and stemming.
        We just strip punctuation, split identifiers, and remove stop words.
        No FTS5 OR syntax — return plain space-separated terms.
        """
        parts = []
        raw_tokens = query.split()

        for token in raw_tokens:
            token = token.strip('?!,;:()[]{}"\'"')  # noqa: B005
            if not token:
                continue

            split_term = _CAMEL_CASE_RE.sub(r"\1 \2", token)
            split_term = split_term.replace("_", " ").replace(".", " ")
            sub_tokens = split_term.split()

            filtered = [t for t in sub_tokens if t.lower() not in _FTS_STOP_WORDS and len(t) >= 2]

            parts.extend(filtered)

        return " ".join(parts) if parts else query
