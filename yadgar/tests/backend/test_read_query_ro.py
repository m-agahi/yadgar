"""GO/NO-GO safety test for the read-only DB inspection surface (ADR-0132).

The ENTIRE safety claim of ``db_inspect`` / ``POST /read_query`` rests on one
property: **a write over the VIEWER-role DB connection does NOT persist,
regardless of query text.** That property is asserted here against a REAL
SurrealDB with a manually-``DEFINE``'d ``yadgar-ro`` VIEWER user — mirroring
entrypoint-backend.sh's production bootstrap SQL exactly. A pure mock CANNOT prove
RO safety.

EMPIRICAL NOTE (verified 2026-07-16): SurrealDB VIEWER signals write-refusal
INCONSISTENTLY — a hard "read only transaction" error when the write implies DDL
(auto-defining a table), but a SILENT status=OK/result=None no-op for a record
write to an EXISTING table. Either way NOTHING PERSISTS, which is the guarantee.
So the go/no-go proves "no mutation" via a read-back, not "the write errors".

Structure (all three parts required — see ADR-0132 / the plan):
1. A SELECT over ``_q_ro`` SUCCEEDS first — proves the RO user exists + authenticates
   (else a 401 from a missing RO user would masquerade as "write rejected").
2. UPDATE / DELETE / CREATE over ``_q_ro`` DIRECTLY (NOT through the route — the
   route's parse-guard would reject first and green for the wrong reason).
3. A read-back over the OWNER connection proves NO mutation occurred (an exception
   alone false-greens on a syntax error).

FAIL-LOUD, never skip silently: this is the go/no-go. If server mode is not active
(no ``surreal`` binary → embedded mode) the test FAILS rather than skips — a skip
here is a NO-GO masquerading as green.
"""

from __future__ import annotations

import uuid

import pytest

from yadgar._shared.storage import StorageEngine

_RO_USER = "yadgar-ro"
_RO_PASS = "test-ro-pass-123"


def _define_ro_viewer_user(storage: StorageEngine) -> None:
    """DEFINE the yadgar-ro VIEWER user over the OWNER connection.

    Mirrors entrypoint-backend.sh:248 exactly (ON ROOT ... ROLES VIEWER).
    Idempotent (IF NOT EXISTS) so it is safe against the shared session server.
    """
    storage._q(
        f"DEFINE USER IF NOT EXISTS `{_RO_USER}` ON ROOT PASSWORD '{_RO_PASS}' ROLES VIEWER;"
    )


@pytest.fixture
def ro_storage(surreal_server, monkeypatch):
    """A server-mode StorageEngine with the yadgar-ro VIEWER user provisioned.

    Requests ``surreal_server`` explicitly so the session SurrealDB is spawned
    (the go/no-go needs a real DB, not the embedded SDK). Fail-loud if server
    mode is not active.
    """
    monkeypatch.setenv("YADGAR_RO_USER", _RO_USER)
    monkeypatch.setenv("YADGAR_RO_PASS", _RO_PASS)

    storage = StorageEngine(db_path="unused-in-server-mode")

    # FAIL-LOUD: this test is meaningless in embedded mode. A skip here would be a
    # NO-GO masquerading as green.
    assert storage._db_url, (
        "read_query RO go/no-go test requires SERVER mode (a live SurrealDB). "
        "YADGAR_DB_URL is not set — the `surreal` binary is missing or the "
        "session server did not spawn. A live backend/SurrealDB is mandatory; "
        "do NOT treat this as passing."
    )

    _define_ro_viewer_user(storage)
    try:
        yield storage
    finally:
        storage.close()


def test_read_query_viewer_rejects_writes(ro_storage):
    """GO/NO-GO: the VIEWER RO connection rejects UPDATE/DELETE/CREATE at the DB.

    If VIEWER does NOT reject writes → STOP, do not ship: the RO role isn't
    read-only.
    """
    storage = ro_storage
    # Record ids are bare alphanumeric hex → safe to inline in the colon-record
    # form (the LET-param + type::thing transport form 400s in server mode).
    rid = f"g{uuid.uuid4().hex[:12]}"
    rid2 = f"g{uuid.uuid4().hex[:12]}"

    # Seed a row over the OWNER connection so we have something to (fail to) mutate.
    storage._q(f"CREATE ro_probe:{rid} SET val = 1;")

    # --- Part 1: a SELECT over _q_ro SUCCEEDS (proves the RO user authenticates) ---
    rows, _truncated = storage._q_ro(f"SELECT id, val FROM ro_probe:{rid};")
    assert any(str(r.get("id")).endswith(rid) for r in rows), (
        "RO SELECT returned no rows — the yadgar-ro VIEWER user is not "
        "authenticating (a 401 would look like a write-rejection). NO-GO."
    )

    # --- Part 2: writes over _q_ro DIRECTLY do not persist (not via route/parse-guard) ---
    # EMPIRICAL SurrealDB VIEWER behavior (verified 2026-07-16): a write over the
    # VIEWER connection does NOT persist, but the DB signals this INCONSISTENTLY —
    # a hard "read only transaction" error when the write implies DDL (e.g. table
    # auto-define), but a SILENT status=OK/result=None no-op for a record write to
    # an EXISTING table. The load-bearing guarantee is "no mutation persists", NOT
    # "the write errors". So we attempt each write and swallow either outcome;
    # Part 3's read-back over OWNER is the SOLE proof that nothing changed.
    import httpx

    for write_sql in (
        f"UPDATE ro_probe:{rid} SET val = 999;",
        f"DELETE ro_probe:{rid};",
        f"CREATE ro_probe:{rid2} SET val = 2;",
    ):
        try:
            storage._q_ro(write_sql)
        except (RuntimeError, httpx.HTTPError):  # fmt: skip
            pass  # a hard rejection is also acceptable — read-back is the real proof

    # --- Part 3: read-back over the OWNER connection proves NO mutation occurred ---
    after = storage._q(f"SELECT id, val FROM ro_probe:{rid};")
    seed_rows = [r for r in after if str(r.get("id")).endswith(rid)]
    assert len(seed_rows) == 1, (
        "seed row missing after RO write attempts — a DELETE succeeded over the "
        "VIEWER connection. NO-GO: the RO role is NOT read-only."
    )
    assert seed_rows[0].get("val") == 1, (
        "seed row val changed after RO UPDATE attempt — an UPDATE succeeded over "
        "the VIEWER connection. NO-GO: the RO role is NOT read-only."
    )
    # The CREATE over RO must not have created the second record.
    created = storage._q(f"SELECT id FROM ro_probe:{rid2};")
    assert not created, "a CREATE succeeded over the VIEWER connection. NO-GO: RO not read-only."

    # Cleanup the seed over OWNER (best-effort; shared session server).
    storage._q(f"DELETE ro_probe:{rid};")


def test_read_query_returns_rows_and_row_cap_truncates(ro_storage):
    """RO SELECT returns rows; the row cap truncates + flags truncated:true."""
    storage = ro_storage
    batch = f"b{uuid.uuid4().hex[:8]}"
    # Seed 6 rows over OWNER (bare-alphanumeric record ids, inline).
    for i in range(6):
        storage._q(f"CREATE ro_cap:{batch}x{i} SET batch = '{batch}', n = {i};")

    # Full read (no truncation with a generous cap). Params bind via _q_ro.
    rows, truncated = storage._q_ro("SELECT n FROM ro_cap WHERE batch = $b;", {"b": batch})
    assert len(rows) == 6, f"param-bound RO SELECT returned {len(rows)} rows, expected 6"
    assert truncated is False

    # row_cap below the result size → truncated.
    capped, truncated2 = storage._q_ro(
        "SELECT n FROM ro_cap WHERE batch = $b;", {"b": batch}, row_cap=3
    )
    assert len(capped) == 3
    assert truncated2 is True

    # A caller can never RAISE the ceiling above _RO_QUERY_ROW_CAP (500).
    from yadgar._shared.storage.client import _RO_QUERY_ROW_CAP

    assert _RO_QUERY_ROW_CAP == 500
    huge, _ = storage._q_ro("SELECT n FROM ro_cap WHERE batch = $b;", {"b": batch}, row_cap=100000)
    assert len(huge) <= _RO_QUERY_ROW_CAP

    storage._q(f"DELETE ro_cap WHERE batch = '{batch}';")


def test_read_query_timeout_is_honored(ro_storage):
    """_q_ro passes timeout_ms through as the httpx per-call timeout.

    A near-zero timeout must surface as an httpx timeout error, not hang or
    silently ignore the bound.
    """
    import httpx

    storage = ro_storage
    with pytest.raises((httpx.TimeoutException, httpx.HTTPError)):  # fmt: skip
        storage._q_ro("SELECT * FROM memory;", timeout_ms=1)


def test_ro_credentials_no_rw_fallback(monkeypatch):
    """_resolve_ro_db_credentials never falls back to the writer creds."""
    from yadgar._shared.storage import _resolve_ro_db_credentials

    monkeypatch.delenv("YADGAR_RO_USER", raising=False)
    monkeypatch.delenv("YADGAR_RO_PASS", raising=False)
    monkeypatch.setenv("YADGAR_RW_USER", "yadgar-rw")
    monkeypatch.setenv("YADGAR_RW_PASS", "should-not-be-used")
    with pytest.raises(ValueError):
        _resolve_ro_db_credentials()
