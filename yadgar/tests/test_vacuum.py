"""Tests for cmd_vacuum (v4.8 rewrite).

TDD: characterization test written first, expected to FAIL against the
old no-op implementation, then pass once the new implementation is in place.
"""

from __future__ import annotations

import contextlib
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: build a minimal argparse namespace for cmd_vacuum
# ---------------------------------------------------------------------------


def _vacuum_args(
    backend_url: str = "http://127.0.0.1:8080",
    service_mode: str = "manual",
    db_path: str | None = None,
    yes: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        backend_url=backend_url,
        service_mode=service_mode,
        db_path=db_path,
        yes=yes,
    )


# ---------------------------------------------------------------------------
# P2 side-build seams (v5.69): the new vacuum flow builds the compacted DB on a
# side path via a throwaway surreal subprocess.  These mock-based unit tests have
# no live backend, so the two surreal-touching seams are patched while the rest
# of the orchestration (export, strip, _atomic_swap [pure os.rename], finalize,
# check_invariants, log-row) runs for REAL.  The live side-build is covered by
# the e2e suite (BC-E1 / BC-E2c) against a real embedded surreal.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_side_build_success():
    """Patch the P2 side-build to SUCCEED hermetically (no surreal subprocess).

    _capture_table_counts → a fixed dict; _build_and_verify_side_db → creates the
    side path (so the real _atomic_swap can rename it in) and returns True.
    """

    def _make_side(backend_url, filtered_path, side_path, source_counts):
        side_path.mkdir(parents=True, exist_ok=True)
        (side_path / "compacted.marker").write_bytes(b"compacted")
        return True

    with (
        patch("yadgar.vacuum._capture_table_counts", return_value={"memory": 1}),
        patch("yadgar.vacuum._build_and_verify_side_db", side_effect=_make_side),
    ):
        yield


@contextlib.contextmanager
def _patch_side_build_abort():
    """Patch the P2 side-build to ABORT (import/verify failure) hermetically.

    _capture_table_counts → a fixed dict (so the flow proceeds past count
    capture); _build_and_verify_side_db → False (the abort path: canonical must
    be left untouched, no `.old-*` ever created).
    """
    with (
        patch("yadgar.vacuum._capture_table_counts", return_value={"memory": 1}),
        patch("yadgar.vacuum._build_and_verify_side_db", return_value=False),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixture: fake ~/.yadgar layout
# ---------------------------------------------------------------------------


@pytest.fixture
def yadgar_home(tmp_path, monkeypatch):
    """Fake ~/.yadgar directory with a surreal_db containing known subdirs."""
    home = tmp_path / ".yadgar"
    db = home / "surreal_db"
    for subdir in ("vlog", "sstables", "wal"):
        (db / subdir).mkdir(parents=True)
    # Put some bytes so before_bytes > 0
    (db / "vlog" / "00001.vlog").write_bytes(b"x" * 10_000)
    (db / "wal" / "00001.log").write_bytes(b"y" * 1_000)
    monkeypatch.setenv("YADGAR_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Fixture: fake cleanup-backups.sh script
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup_script(tmp_path, monkeypatch):
    script = tmp_path / "cleanup-backups.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))
    return script


# ---------------------------------------------------------------------------
# strip_action_log tests  (independent of HTTP / daemons)
# ---------------------------------------------------------------------------


class TestStripActionLog:
    """Unit tests for strip_action_log() — pure text transformation."""

    def _import_strip(self):
        from yadgar.vacuum import strip_action_log

        return strip_action_log


# ---------------------------------------------------------------------------
# Tests for strip_export_for_vacuum (user/access stripping)
# ---------------------------------------------------------------------------


class TestStripExportForVacuum:
    """Unit tests for the extended strip_export_for_vacuum() function.

    Covers stripping of DEFINE USER / DEFINE ACCESS / REMOVE USER statements
    that represent infrastructure state owned by the backend entrypoint, not
    user data.  These must not survive into the /import payload or they will
    overwrite the freshly-bootstrapped users with stale password hashes.

    Also inherits the original TestStripActionLog test methods via _import_strip,
    which now routes to strip_action_log (an alias for strip_export_for_vacuum).
    """

    def _strip(self):
        from yadgar.vacuum.strip import strip_export_for_vacuum

        return strip_export_for_vacuum

    def _import_strip(self):
        """Back-compat alias — same function now, used by the old test methods below."""
        from yadgar.vacuum import strip_action_log

        return strip_action_log

    def test_strip_removes_define_user_on_root(self):
        """DEFINE USER ... ON ROOT ... stripped from export."""
        strip = self._strip()
        surql = (
            "DEFINE TABLE memory TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "DEFINE USER yadgar-rw ON ROOT PASSWORD '$argon2id$v=19$m=4096$...' ROLES OWNER;\n"
            "DEFINE USER yadgar-ro ON ROOT PASSWORD '$argon2id$v=19$m=4096$...' ROLES VIEWER;\n"
            "INSERT INTO memory { content: 'hi', heat: 1.0 };\n"
        )
        out = strip(surql)
        assert "DEFINE USER yadgar-rw ON ROOT" not in out
        assert "DEFINE USER yadgar-ro ON ROOT" not in out
        assert "DEFINE TABLE memory" in out
        assert "INSERT INTO memory" in out

    def test_strip_removes_define_user_on_namespace(self):
        """DEFINE USER ... ON NAMESPACE ... (and NS alias) stripped."""
        strip = self._strip()
        surql = (
            "DEFINE USER ns_user ON NAMESPACE yadgar PASSWORD 'x' ROLES OWNER;\n"
            "DEFINE USER ns_user2 ON NS yadgar PASSWORD 'y' ROLES VIEWER;\n"
            "INSERT INTO memory { content: 'safe' };\n"
        )
        out = strip(surql)
        assert "DEFINE USER ns_user ON NAMESPACE" not in out
        assert "DEFINE USER ns_user2 ON NS" not in out
        assert "INSERT INTO memory" in out

    def test_strip_removes_define_access(self):
        """DEFINE ACCESS ... stripped (SurrealDB v2+ user/token syntax)."""
        strip = self._strip()
        surql = (
            "DEFINE TABLE wiki_page TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "DEFINE ACCESS yadgar_api ON DATABASE TYPE JWT ALGORITHM HS256 KEY 'secret';\n"
            "INSERT INTO wiki_page { title: 'Home' };\n"
        )
        out = strip(surql)
        assert "DEFINE ACCESS yadgar_api" not in out
        assert "DEFINE TABLE wiki_page" in out
        assert "INSERT INTO wiki_page" in out

    def test_strip_removes_remove_user(self):
        """REMOVE USER ... stripped (defensive, also infra state)."""
        strip = self._strip()
        surql = "REMOVE USER old_user ON ROOT;\nINSERT INTO memory { content: 'keep me' };\n"
        out = strip(surql)
        assert "REMOVE USER old_user" not in out
        assert "INSERT INTO memory" in out

    def test_strip_preserves_table_definitions(self):
        """DEFINE TABLE, DEFINE INDEX, DEFINE FIELD survive user stripping."""
        strip = self._strip()
        surql = (
            "DEFINE TABLE memory TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "DEFINE INDEX memory_heat ON TABLE memory COLUMNS heat;\n"
            "DEFINE FIELD content ON TABLE memory TYPE string;\n"
            "DEFINE USER yadgar-rw ON ROOT PASSWORD 'hash' ROLES OWNER;\n"
        )
        out = strip(surql)
        assert "DEFINE TABLE memory" in out
        assert "DEFINE INDEX memory_heat" in out
        assert "DEFINE FIELD content" in out
        assert "DEFINE USER yadgar-rw ON ROOT" not in out

    def test_strip_preserves_memory_data(self):
        """INSERT memory rows with arbitrary content are not touched."""
        strip = self._strip()
        surql = (
            "DEFINE USER yadgar-rw ON ROOT PASSWORD '$argon2id$...' ROLES OWNER;\n"
            "INSERT INTO memory {\n"
            "  content: 'You can DEFINE USER ON ROOT to create a root-level user.',\n"
            "  heat: 0.8\n"
            "};\n"
        )
        out = strip(surql)
        # The memory content mentions DEFINE USER but must NOT be stripped.
        assert "You can DEFINE USER ON ROOT" in out
        # The actual DEFINE USER statement must be stripped.
        assert "DEFINE USER yadgar-rw ON ROOT PASSWORD" not in out

    def test_strip_no_false_positive_on_mid_line_define_user(self):
        """DEFINE USER mid-line (inside a string value) is not stripped.

        The ^ anchor in MULTILINE mode ensures only start-of-line statements
        are matched.  Content inside INSERT rows that mentions DEFINE USER
        must survive.
        """
        strip = self._strip()
        surql = "INSERT INTO memory { content: 'DEFINE USER foo ON ROOT ...' };\n"
        out = strip(surql)
        # Must be preserved — it's data, not a SQL statement
        assert "DEFINE USER foo ON ROOT" in out

    def test_strip_preserves_existing_action_log_strip(self):
        """Original action_log stripping still works after function rename."""
        strip = self._strip()
        surql = (
            "DEFINE TABLE action_log SCHEMAFULL;\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:1 CONTENT {cmd: 'bad'};\n"
            "\n"
            "INSERT INTO memory { content: 'safe' };\n"
        )
        out = strip(surql)
        assert "action_log:1" not in out
        assert "DEFINE TABLE action_log" not in out
        assert "INSERT INTO memory" in out

    def test_strip_action_log_alias_still_works(self):
        """strip_action_log remains a back-compat alias for strip_export_for_vacuum."""
        from yadgar.vacuum.strip import strip_action_log, strip_export_for_vacuum

        surql = (
            "DEFINE USER yadgar-rw ON ROOT PASSWORD 'hash' ROLES OWNER;\n"
            "INSERT INTO memory { content: 'data' };\n"
        )
        # Both functions must produce identical output
        assert strip_action_log(surql) == strip_export_for_vacuum(surql)

    def test_removes_data_block(self):
        strip = self._import_strip()
        surql = (
            "-- TABLE DATA: memory ----\n"
            "UPSERT memory:1 CONTENT {text: 'hi'};\n"
            "\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:1 CONTENT {cmd: 'rm -rf /'};\n"
            "UPSERT action_log:2 CONTENT {cmd: 'echo hi'};\n"
            "\n"
            "-- TABLE DATA: wiki_page ----\n"
            "UPSERT wiki_page:1 CONTENT {title: 'Home'};\n"
        )
        out = strip(surql)
        assert "action_log:1" not in out
        assert "action_log:2" not in out
        # Surrounding tables preserved verbatim
        assert "memory:1" in out
        assert "wiki_page:1" in out

    def test_removes_define_table_action_log(self):
        strip = self._import_strip()
        surql = (
            "DEFINE TABLE action_log SCHEMAFULL;\n"
            "DEFINE TABLE memory SCHEMAFULL;\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:x CONTENT {};\n"
            "\n"
            "-- TABLE DATA: memory ----\n"
        )
        out = strip(surql)
        assert "DEFINE TABLE action_log" not in out
        assert "DEFINE TABLE memory" in out

    def test_no_action_log_passthrough(self):
        strip = self._import_strip()
        surql = "UPSERT memory:1 CONTENT {text: 'safe'};\n"
        assert strip(surql) == surql

    def test_preserves_tables_after_action_log(self):
        """Tables that appear AFTER action_log in the export must survive."""
        strip = self._import_strip()
        surql = (
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:1 CONTENT {cmd: 'bad'};\n"
            "\n"
            "-- TABLE DATA: wiki_crossref ----\n"
            "RELATE wiki_page:1->wiki_crossref:1->wiki_page:2;\n"
        )
        out = strip(surql)
        assert "wiki_crossref" in out
        assert "action_log" not in out

    def test_real_surrealdb_format(self):
        """Strip handles the actual SurrealDB v3.0.5 dashes-block export format."""
        strip = self._import_strip()
        # Mimics the actual /export output structure
        surql = (
            "-- ------------------------------\n"
            "-- TABLE: memory\n"
            "-- ------------------------------\n"
            "\n"
            "DEFINE TABLE memory TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "\n"
            "\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE: action_log\n"
            "-- ------------------------------\n"
            "\n"
            "DEFINE TABLE action_log TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "\n"
            "\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE: wiki_page\n"
            "-- ------------------------------\n"
            "\n"
            "DEFINE TABLE wiki_page TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "\n"
            "\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE DATA: memory\n"
            "-- ------------------------------\n"
            "\n"
            "INSERT [ { id: memory:aaa, content: 'hello' } ];\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE DATA: action_log\n"
            "-- ------------------------------\n"
            "\n"
            "INSERT [ { id: action_log:z, cmd: 'rm -rf /' } ];\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE DATA: wiki_page\n"
            "-- ------------------------------\n"
            "\n"
            "INSERT [ { id: wiki_page:bbb, title: 'Home' } ];\n"
        )
        out = strip(surql)

        # action_log schema and data stripped
        assert "DEFINE TABLE action_log" not in out
        assert "action_log:z" not in out

        # Surrounding tables preserved
        assert "DEFINE TABLE memory" in out
        assert "memory:aaa" in out
        assert "DEFINE TABLE wiki_page" in out
        assert "wiki_page:bbb" in out


# ---------------------------------------------------------------------------
# Service-mode detection tests
# ---------------------------------------------------------------------------


class TestServiceModeDetection:
    def test_systemd_from_invocation_id(self, monkeypatch):
        from yadgar.ops import detect_service_mode

        monkeypatch.setenv("INVOCATION_ID", "abc123")
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        assert detect_service_mode() == "systemd"

    def test_docker_from_dockerenv(self, monkeypatch, tmp_path):
        from yadgar.ops import detect_service_mode

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        fake_dockerenv = tmp_path / ".dockerenv"
        fake_dockerenv.touch()
        with patch("yadgar.ops.Path") as MockPath:
            # Make Path("/.dockerenv").exists() return True
            mock_p = MagicMock()
            mock_p.exists.return_value = True
            MockPath.return_value = mock_p
            result = detect_service_mode()
        assert result == "docker"

    def test_manual_fallback(self, monkeypatch):
        from yadgar.ops import detect_service_mode

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        with patch("yadgar.ops.Path") as MockPath:
            mock_p = MagicMock()
            mock_p.exists.return_value = False
            MockPath.return_value = mock_p
            result = detect_service_mode()
        assert result == "manual"


# ---------------------------------------------------------------------------
# Characterization test — MUST FAIL on the old cmd_vacuum (no-op)
# ---------------------------------------------------------------------------


class TestCharacterization:
    """Full-flow characterization test.

    Proves that cmd_vacuum does the export→swap→reimport dance and:
    - Produces a .filtered.surql file on disk.
    - Inserts a consolidation_log row with kind="vacuum".
    - Does NOT lose rows from preserved tables.
    - action_log data is stripped from the export.

    This test FAILS on the old cmd_vacuum (no-op: exits early with
    "No clog directory found"). That's intentional — TDD.
    """

    def _mock_backend(self, tmp_path, monkeypatch, import_status: int = 200):
        """Set up mocks so the HTTP calls succeed without a real daemon."""
        # Fake export surql
        fake_surql = (
            "-- TABLE DATA: memory ----\n"
            "UPSERT memory:aaa CONTENT {content: 'test', heat: 1.0};\n"
            "\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:z CONTENT {cmd: 'bad stuff'};\n"
            "\n"
            "-- TABLE DATA: wiki_page ----\n"
            "UPSERT wiki_page:bbb CONTENT {title: 'Test Page'};\n"
        )

        mock_export_resp = MagicMock()
        mock_export_resp.status_code = 200
        mock_export_resp.text = fake_surql

        mock_import_resp = MagicMock()
        mock_import_resp.status_code = import_status
        mock_import_resp.text = "OK" if import_status == 200 else "Internal Server Error"

        mock_health_resp = MagicMock()
        mock_health_resp.status_code = 200

        mock_invariants_resp = MagicMock()
        mock_invariants_resp.status_code = 200
        mock_invariants_resp.json.return_value = {"ok": True}

        mock_consolidation_resp = MagicMock()
        mock_consolidation_resp.status_code = 200
        mock_consolidation_resp.json.return_value = [{}]

        import httpx

        def mock_get(url, **kwargs):
            if "/health" in url or "/healthz" in url:
                return mock_health_resp
            if "/export" in url:
                return mock_export_resp
            return MagicMock(status_code=200, text="")

        def mock_post(url, **kwargs):
            if "/import" in url:
                return mock_import_resp
            if "/api/check_invariants" in url:
                return mock_invariants_resp
            if "/sql" in url:
                return mock_consolidation_resp
            return MagicMock(status_code=200, text="")

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        return fake_surql

    def test_characterization_produces_filtered_surql_and_log_row(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """Discriminating check: new cmd_vacuum produces a .filtered.surql file.

        The old no-op returns early because clog/ doesn't exist — it never
        creates any file. This assertion FAILS on the old code.
        """
        from yadgar.vacuum import cmd_vacuum_impl

        self._mock_backend(tmp_path, monkeypatch)

        log_rows = []

        def fake_log_row(row):
            log_rows.append(row)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with _patch_side_build_success():
            with patch("yadgar.vacuum._log_consolidation_row", fake_log_row):
                with patch("yadgar.vacuum.ServiceController") as MockSVC:
                    with patch("yadgar.vacuum._wait_for_health", return_value=True):
                        with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                            with patch("yadgar.vacuum._redefine_users_post_import"):
                                mock_svc = MagicMock()
                                MockSVC.return_value = mock_svc

                                result = cmd_vacuum_impl(args)

        # Must succeed
        assert result == 0, "cmd_vacuum_impl should return 0 on success"

        # Must produce a filtered file
        filtered_files = list(yadgar_home.glob("vacuum_export_*.filtered.surql"))
        assert filtered_files, "Expected a .filtered.surql file in ~/.yadgar/"

        # action_log stripped
        content = filtered_files[0].read_text()
        assert "action_log:z" not in content

        # Surrounding tables preserved
        assert "memory:aaa" in content
        assert "wiki_page:bbb" in content

    def test_cmd_vacuum_delegates_to_impl(self, tmp_path, monkeypatch, yadgar_home, cleanup_script):
        """Regression: cmd_vacuum() in __main__ must delegate to cmd_vacuum_impl.

        The old cmd_vacuum was a no-op (checked for clog/ which doesn't exist
        in surrealkv >= v2). The new wrapper must call cmd_vacuum_impl and exit
        non-zero on failure, zero on success.
        """
        import httpx

        from yadgar import __main__ as main_mod

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        monkeypatch.setattr(
            httpx,
            "get",
            lambda url, **kw: MagicMock(
                status_code=200, text=fake_surql if "/export" in url else ""
            ),
        )
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kw: MagicMock(
                status_code=200, text="OK", json=MagicMock(return_value={"ok": True})
            ),
        )

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with _patch_side_build_success():
            with patch("yadgar.vacuum._log_consolidation_row"):
                with patch("yadgar.vacuum.ServiceController"):
                    with patch("yadgar.vacuum._wait_for_health", return_value=True):
                        with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                            with patch("yadgar.vacuum._redefine_users_post_import"):
                                # Should not raise — exit_code 0 means no sys.exit call
                                try:
                                    main_mod.cmd_vacuum(args)
                                except SystemExit as e:
                                    pytest.fail(
                                        f"cmd_vacuum raised SystemExit({e.code}) unexpectedly"
                                    )


# ---------------------------------------------------------------------------
# RELATE preservation test
# ---------------------------------------------------------------------------


class TestRelatePreservation:
    """RELATE edges (wiki_crossref) must survive the vacuum cycle."""

    def test_wiki_crossref_preserved_in_filtered_surql(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """After strip_action_log, RELATE / wiki_crossref rows must survive."""
        import httpx

        from yadgar.vacuum import cmd_vacuum_impl

        fake_surql = (
            "-- TABLE DATA: memory ----\n"
            "UPSERT memory:1 CONTENT {content: 'alpha'};\n"
            "UPSERT memory:2 CONTENT {content: 'beta'};\n"
            "UPSERT memory:3 CONTENT {content: 'gamma'};\n"
            "\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:x CONTENT {cmd: 'bad'};\n"
            "\n"
            "-- TABLE DATA: wiki_crossref ----\n"
            "RELATE wiki_page:A->wiki_crossref:1->wiki_page:B;\n"
            "RELATE wiki_page:B->wiki_crossref:2->wiki_page:C;\n"
        )

        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_import = MagicMock(status_code=200, text="OK")
        mock_health = MagicMock(status_code=200)
        mock_invariants = MagicMock(status_code=200, json=MagicMock(return_value={"ok": True}))

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            if "/import" in url:
                return mock_import
            return mock_invariants

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with _patch_side_build_success():
            with patch("yadgar.vacuum._log_consolidation_row"):
                with patch("yadgar.vacuum.ServiceController"):
                    with patch("yadgar.vacuum._wait_for_health", return_value=True):
                        with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                            with patch("yadgar.vacuum._redefine_users_post_import"):
                                cmd_vacuum_impl(args)

        filtered = list(yadgar_home.glob("vacuum_export_*.filtered.surql"))
        assert filtered
        content = filtered[0].read_text()

        # Both RELATE edges must survive
        assert "wiki_crossref:1" in content
        assert "wiki_crossref:2" in content
        # action_log stripped
        assert "action_log:x" not in content


# ---------------------------------------------------------------------------
# Failure injection test — /import returns HTTP 500
# ---------------------------------------------------------------------------


class TestFailureInjection:
    """V2 safe-swap semantics: /import failure must restore original DB."""

    def test_import_500_restores_original_db(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """NEW SAFE BEHAVIOR: /import 500 → surreal_db restored, no .bloated-* leftover."""
        import httpx

        from yadgar.vacuum import cmd_vacuum_impl

        # Write a sentinel file so we can confirm original contents after restore
        sentinel = yadgar_home / "surreal_db" / "sentinel.txt"
        sentinel.write_bytes(b"original")

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_import = MagicMock(status_code=500, text="Internal Server Error")
        mock_health = MagicMock(status_code=200)

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            return mock_import

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        started_services = []
        stopped_services = []

        # P2: drive the abort path for real — side-build/verify FAILS, so the
        # canonical must be left untouched (NEVER renamed → no `.old-*`).
        with _patch_side_build_abort():
            with patch("yadgar.vacuum._log_consolidation_row"):
                with patch("yadgar.vacuum.ServiceController") as MockSVC:
                    with patch("yadgar.vacuum._wait_for_health", return_value=True):
                        mock_svc = MagicMock()
                        mock_svc.start_backend.side_effect = lambda: started_services.append(
                            "start_backend"
                        )
                        mock_svc.start_yadgar.side_effect = lambda: started_services.append(
                            "start_yadgar"
                        )
                        mock_svc.stop_backend.side_effect = lambda: stopped_services.append(
                            "stop_backend"
                        )
                        MockSVC.return_value = mock_svc

                        exit_code = cmd_vacuum_impl(args)

        # Non-zero exit on import failure
        assert exit_code != 0, "Expected non-zero exit on /import HTTP 500"

        # surreal_db must exist with original contents (sentinel preserved) —
        # the canonical was NEVER renamed on the abort path.
        db_path = yadgar_home / "surreal_db"
        assert db_path.exists(), "surreal_db must be untouched on /import failure"
        assert (db_path / "sentinel.txt").read_bytes() == b"original", (
            "surreal_db must still contain original data after abort"
        )

        # ABORT-UNTOUCHED proof (P2): no swap-staging siblings created/left behind.
        assert not list(yadgar_home.glob("surreal_db.old-*")), (
            "canonical was renamed on an abort path (.old-* present) — swap must "
            "begin only AFTER side-build+verify"
        )
        assert not list(yadgar_home.glob("surreal_db.new-*")), (
            "a .new-* side dir leaked on the abort path"
        )

        # yadgar (MCP layer) must NOT be started after import failure
        assert "start_yadgar" not in started_services, (
            "yadgar service must NOT be started after import failure"
        )

    def test_import_403_restores_original_db(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """HTTP 403 (original root-creds bug) also triggers DB restore."""
        import httpx

        from yadgar.vacuum import cmd_vacuum_impl

        sentinel = yadgar_home / "surreal_db" / "sentinel.txt"
        sentinel.write_bytes(b"original")

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_import = MagicMock(status_code=403, text="Not enough permissions")
        mock_health = MagicMock(status_code=200)

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            return mock_import

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with _patch_side_build_abort():
            with patch("yadgar.vacuum._log_consolidation_row"):
                with patch("yadgar.vacuum.ServiceController") as MockSVC:
                    with patch("yadgar.vacuum._wait_for_health", return_value=True):
                        MockSVC.return_value = MagicMock()
                        exit_code = cmd_vacuum_impl(args)

        assert exit_code != 0
        db_path = yadgar_home / "surreal_db"
        assert db_path.exists(), "surreal_db must be untouched on 403 abort"
        assert (db_path / "sentinel.txt").read_bytes() == b"original"
        assert not list(yadgar_home.glob("surreal_db.old-*")), (
            "canonical was renamed on the 403 abort path (.old-* present)"
        )

    def test_real_side_import_500_aborts_inside_build_canonical_untouched(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script, capsys
    ):
        """H1 (CI-runnable): drive the REAL abort branch INSIDE _build_and_verify_side_db.

        The 500/403 tests above stub `_build_and_verify_side_db → False`, so they
        only exercise cmd_vacuum_impl's handling of a False return — NOT the real
        internal abort.  A regression that made the abort path rename/empty the
        canonical would pass those green.  This test instead runs the REAL side
        build (no surreal binary: spawn/teardown/health/namespace are the only
        host/binary seams stubbed) and injects an HTTP 500 at the side `/import`
        POST — the actual orchestrator↔backend fault boundary.  It asserts:
          (1) _build_and_verify_side_db returns False (spy-wrapped, not stubbed),
          (2) the real `/import returned HTTP 500` abort message was emitted,
          (3) NO `.old-*` sibling was ever created (canonical never renamed),
          (4) NO `.new-*` / `.building-*` staging dir leaked,
          (5) the canonical still holds its original data (sentinel preserved),
          (6) yadgar (MCP layer) was never started.
        Runs under the default CI selection (`-m 'not integration and not e2e'`):
        unmarked, no real `surreal` process.
        """
        import httpx

        import yadgar.vacuum as _vac
        from yadgar.vacuum import cmd_vacuum_impl

        sentinel = yadgar_home / "surreal_db" / "sentinel.txt"
        sentinel.write_bytes(b"original")

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_health = MagicMock(status_code=200)

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            # The side build's /import POST is the injected fault boundary.
            if str(url).endswith("/import"):
                req = httpx.Request("POST", url)
                return httpx.Response(500, text="simulated side /import failure", request=req)
            return MagicMock(status_code=200, text="")

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        # Host/binary seams ONLY (no real surreal): a fake throwaway proc, a
        # no-op spawn/teardown, forced-healthy wait, and a no-op namespace
        # bootstrap (its real client would POST to a dead port and raise BEFORE
        # the /import branch, masking the abort under test).
        fake_proc = MagicMock()
        monkeypatch.setattr("yadgar._surreal_runner.spawn_surreal", lambda *a, **kw: fake_proc)
        monkeypatch.setattr("yadgar._surreal_runner.teardown_surreal_proc", lambda *a, **kw: None)
        monkeypatch.setattr("yadgar.vacuum._bootstrap_namespace", lambda *a, **kw: None)

        # Spy-wrap the REAL function (NOT a stub) so its body runs end-to-end and
        # we can assert it actually returned False from the real abort branch.
        _real_build = _vac._build_and_verify_side_db
        recorded: dict[str, object] = {}

        def _spy_build(*a, **kw):
            rv = _real_build(*a, **kw)
            recorded["rv"] = rv
            return rv

        started_services: list[str] = []

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with patch("yadgar.vacuum._build_and_verify_side_db", side_effect=_spy_build):
            with patch("yadgar.vacuum._capture_table_counts", return_value={"memory": 1}):
                with patch("yadgar.vacuum._log_consolidation_row"):
                    with patch("yadgar.vacuum.ServiceController") as MockSVC:
                        with patch("yadgar.vacuum._wait_for_health", return_value=True):
                            mock_svc = MagicMock()
                            mock_svc.start_yadgar.side_effect = lambda: started_services.append(
                                "start_yadgar"
                            )
                            MockSVC.return_value = mock_svc
                            exit_code = cmd_vacuum_impl(args)

        # (1) the REAL side build returned False from its internal abort branch.
        assert recorded.get("rv") is False, (
            "the real _build_and_verify_side_db must return False on a side /import "
            f"HTTP 500, got {recorded.get('rv')!r}"
        )
        assert exit_code != 0, "Expected non-zero exit when the side /import fails"

        # (2) prove the abort reason was the /import 500 — not some earlier path.
        err = capsys.readouterr().err
        assert "side /import returned HTTP 500" in err, (
            "the abort must originate at the real /import branch; stderr was:\n" + err
        )

        # (3) canonical was NEVER renamed → no `.old-*` ever created.
        assert not list(yadgar_home.glob("surreal_db.old-*")), (
            "canonical was renamed on the real abort path (.old-* present) — the "
            "swap must begin only AFTER side-build+verify"
        )
        # (4) no staging dir leaked.
        assert not list(yadgar_home.glob("surreal_db.new-*")), "a `.new-*` side dir leaked"
        assert not list(yadgar_home.glob("surreal_db.building-*")), (
            "a `.building-*` UNVERIFIED side dir leaked on the abort path"
        )

        # (5) canonical still holds its original data.
        db_path = yadgar_home / "surreal_db"
        assert db_path.exists(), "surreal_db must be untouched on side /import failure"
        assert (db_path / "sentinel.txt").read_bytes() == b"original", (
            "surreal_db must still contain original data after the real abort"
        )

        # (6) yadgar (MCP layer) must NOT be started after the abort.
        assert "start_yadgar" not in started_services, (
            "yadgar service must NOT be started after a side-build abort"
        )

    def test_import_success_leaves_no_surreal_db_at_bloated_path(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """SUCCESS path (P2): compacted DB swapped in at canonical, no staging left."""
        import httpx

        from yadgar.vacuum import cmd_vacuum_impl

        sentinel = yadgar_home / "surreal_db" / "sentinel.txt"
        sentinel.write_bytes(b"original")

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_import = MagicMock(status_code=200, text="OK")
        mock_health = MagicMock(status_code=200)
        mock_invariants = MagicMock(status_code=200, json=MagicMock(return_value={"ok": True}))

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            if "/import" in url:
                return mock_import
            if "/api/check_invariants" in url:
                return mock_invariants
            return MagicMock(status_code=200, text="")

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with _patch_side_build_success():
            with patch("yadgar.vacuum._log_consolidation_row"):
                with patch("yadgar.vacuum.ServiceController") as MockSVC:
                    with patch("yadgar.vacuum._wait_for_health", return_value=True):
                        with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                            with patch("yadgar.vacuum._redefine_users_post_import"):
                                MockSVC.return_value = MagicMock()
                                exit_code = cmd_vacuum_impl(args)

        assert exit_code == 0, f"Expected 0 on success, got {exit_code}"

        # New-contract (P2): no side-build staging dirs survive a clean vacuum.
        new_dirs = list(yadgar_home.glob("surreal_db.new-*"))
        assert not new_dirs, f"No .new-* side dirs should remain after success: {new_dirs}"
        # The compacted DB is swapped in at the canonical path.
        assert (yadgar_home / "surreal_db").exists(), "canonical must hold the swapped-in DB"


# ---------------------------------------------------------------------------
# V1 — Admin creds precedence: SURREAL_USER/PASS > YADGAR_DB_USER/PASS > root/root
# ---------------------------------------------------------------------------


class TestAdminCreds:
    """_build_http_client and _surreal_headers use SURREAL_USER/PASS first."""

    def _decode_auth(self, headers: dict) -> tuple[str, str]:
        import base64

        raw = headers.get("Authorization", "")
        assert raw.startswith("Basic "), f"Expected Basic auth, got: {raw!r}"
        decoded = base64.b64decode(raw[len("Basic ") :]).decode()
        user, _, password = decoded.partition(":")
        return user, password

    # _build_http_client tests -------------------------------------------

    def test_build_http_client_surreal_wins_over_yadgar(self, monkeypatch):
        """SURREAL_USER set, YADGAR_DB_USER set → SURREAL_USER wins."""
        monkeypatch.setenv("SURREAL_USER", "surreal_admin")
        monkeypatch.setenv("SURREAL_PASS", "surreal_secret")
        monkeypatch.setenv("YADGAR_DB_USER", "yadgar_rw")
        monkeypatch.setenv("YADGAR_DB_PASS", "yadgar_secret")

        from yadgar.vacuum import _build_http_client

        client = _build_http_client("http://127.0.0.1:8080")
        auth_header = dict(client.headers).get("authorization", "")
        import base64

        decoded = base64.b64decode(auth_header[len("Basic ") :]).decode()
        user, _, _ = decoded.partition(":")
        assert user == "surreal_admin", f"Expected surreal_admin, got {user!r}"
        client.close()

    def test_build_http_client_falls_back_to_yadgar(self, monkeypatch):
        """SURREAL_USER unset, YADGAR_DB_USER set → YADGAR_DB_USER used."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.setenv("YADGAR_DB_USER", "yadgar_rw")
        monkeypatch.setenv("YADGAR_DB_PASS", "yadgar_secret")

        from yadgar.vacuum import _build_http_client

        client = _build_http_client("http://127.0.0.1:8080")
        auth_header = dict(client.headers).get("authorization", "")
        import base64

        decoded = base64.b64decode(auth_header[len("Basic ") :]).decode()
        user, _, _ = decoded.partition(":")
        assert user == "yadgar_rw", f"Expected yadgar_rw, got {user!r}"
        client.close()

    def test_build_http_client_defaults_root(self, monkeypatch):
        """Both unset → root/root default."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_PASS", raising=False)

        from yadgar.vacuum import _build_http_client

        client = _build_http_client("http://127.0.0.1:8080")
        auth_header = dict(client.headers).get("authorization", "")
        import base64

        decoded = base64.b64decode(auth_header[len("Basic ") :]).decode()
        user, _, password = decoded.partition(":")
        assert user == "root", f"Expected root, got {user!r}"
        assert password == "root", f"Expected root password, got {password!r}"
        client.close()

    # _surreal_headers tests ---------------------------------------------

    def test_surreal_headers_surreal_wins_over_yadgar(self, monkeypatch):
        """SURREAL_USER set → _surreal_headers uses SURREAL_USER."""
        monkeypatch.setenv("SURREAL_USER", "surreal_admin")
        monkeypatch.setenv("SURREAL_PASS", "surreal_secret")
        monkeypatch.setenv("YADGAR_DB_USER", "yadgar_rw")
        monkeypatch.setenv("YADGAR_DB_PASS", "yadgar_secret")

        from yadgar.vacuum.phases import _surreal_headers

        headers = _surreal_headers()
        user, _ = self._decode_auth(headers)
        assert user == "surreal_admin", f"Expected surreal_admin, got {user!r}"

    def test_surreal_headers_falls_back_to_yadgar(self, monkeypatch):
        """SURREAL_USER unset → _surreal_headers falls back to YADGAR_DB_USER."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.setenv("YADGAR_DB_USER", "yadgar_rw")
        monkeypatch.setenv("YADGAR_DB_PASS", "yadgar_secret")

        from yadgar.vacuum.phases import _surreal_headers

        headers = _surreal_headers()
        user, _ = self._decode_auth(headers)
        assert user == "yadgar_rw", f"Expected yadgar_rw, got {user!r}"

    def test_surreal_headers_defaults_root(self, monkeypatch):
        """Both unset → root/root default in _surreal_headers."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_PASS", raising=False)

        from yadgar.vacuum.phases import _surreal_headers

        headers = _surreal_headers()
        user, password = self._decode_auth(headers)
        assert user == "root", f"Expected root, got {user!r}"
        assert password == "root", f"Expected root password, got {password!r}"


# ---------------------------------------------------------------------------
# Regression: _wait_for_yadgar_health must poll /health, not /healthz
# ---------------------------------------------------------------------------


class TestWaitForYadgarHealth:
    """`_wait_for_yadgar_health` must request /health (not /healthz).

    v5.1.2 deployed with a typo: the URL was constructed as `<url>/healthz`.
    yadgar exposes `/health` (no z).  This caused the 60 s poll to never
    receive a 200, so vacuum exited with status 2 even on fully successful
    runs.
    """

    def test_polls_health_not_healthz(self, monkeypatch):
        """_wait_for_yadgar_health must call <url>/health, not <url>/healthz."""
        import httpx

        polled_urls: list[str] = []

        def fake_get(url, **kwargs):
            polled_urls.append(url)
            m = MagicMock()
            m.status_code = 200
            return m

        monkeypatch.setattr(httpx, "get", fake_get)

        from yadgar.vacuum import _wait_for_yadgar_health

        result = _wait_for_yadgar_health("http://127.0.0.1:8765", timeout_s=5.0)

        assert result is True
        assert polled_urls, "httpx.get was never called"
        for url in polled_urls:
            assert url.endswith("/health"), (
                f"_wait_for_yadgar_health polled {url!r} — must end in /health not /healthz"
            )
            assert not url.endswith("/healthz"), (
                f"_wait_for_yadgar_health polled {url!r} — must NOT end in /healthz"
            )


# ---------------------------------------------------------------------------
# v5.1.6 Bug 2 — _wait_for_yadgar_health timeout must be 180s, not 60s
# ---------------------------------------------------------------------------


class TestWaitForYadgarHealthTimeout:
    """v5.1.6 B2: yadgar cold-start (embedding model load) takes ~70-90s.

    The old 60s default caused vacuum to abort with exit-code 2 even on
    successful runs.  The timeout must be at least 180s (3x empirical max).

    Two checks:
    1. The default parameter value of _wait_for_yadgar_health is 180s.
    2. The call site in cmd_vacuum passes timeout_s=180.0 (not 60.0).
    """

    def test_default_timeout_is_180s(self) -> None:
        """_wait_for_yadgar_health default timeout_s must be 180.0, not 60.0."""
        import inspect

        from yadgar.vacuum import _wait_for_yadgar_health

        sig = inspect.signature(_wait_for_yadgar_health)
        default = sig.parameters["timeout_s"].default
        assert default == 180.0, (
            f"_wait_for_yadgar_health default timeout_s is {default!r}; "
            "expected 180.0 (yadgar cold-start can take 70-90s on real hardware)"
        )

    def test_callsite_passes_180s(self) -> None:
        """The call in _vacuum_finalize must pass timeout_s=180.0, not 60.0."""
        import ast
        import inspect

        from yadgar import vacuum

        source = inspect.getsource(vacuum)
        tree = ast.parse(source)

        # Find all Call nodes for _wait_for_yadgar_health
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "_wait_for_yadgar_health":
                continue
            for kw in node.keywords:
                if kw.arg == "timeout_s":
                    val = kw.value
                    if isinstance(val, ast.Constant):
                        assert val.value == 180.0, (
                            f"_wait_for_yadgar_health called with timeout_s={val.value!r}; "
                            "expected 180.0"
                        )
                    return  # found the call

        pytest.fail(
            "_wait_for_yadgar_health call with explicit timeout_s not found in vacuum source"
        )


# ---------------------------------------------------------------------------
# B1 — _redefine_users_post_import
# ---------------------------------------------------------------------------


class TestRedefineUsersPostImport:
    """Unit tests for _redefine_users_post_import (v5.1.4 B1).

    SurrealDB /import wipes non-root user definitions.  This helper re-creates
    yadgar-rw and yadgar-ro via DEFINE USER after every successful /import.
    """

    def test_issues_define_user_sql_for_rw_and_ro(self, monkeypatch):
        """Helper posts raw SurrealQL DEFINE USER for both rw and ro users.

        v5.1.5 fix: SurrealDB v3 /sql silently no-ops when the body is JSON.
        Only Content-Type: text/plain is parsed as SurrealQL.  This test
        verifies the fix — raw SQL body, correct roles, backtick-quoted names.
        """
        monkeypatch.setenv("YADGAR_RW_USER", "yadgar-rw")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw-secret")
        monkeypatch.setenv("YADGAR_RO_USER", "yadgar-ro")
        monkeypatch.setenv("YADGAR_RO_PASS", "ro-secret")

        posted_calls: list[dict] = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        with patch("yadgar.vacuum._build_http_client", return_value=mock_client):

            def _capture_post(path, content=None, headers=None, **kwargs):
                posted_calls.append(
                    {
                        "path": path,
                        "content": content.decode() if isinstance(content, bytes) else content,
                        "headers": headers or {},
                    }
                )
                return mock_resp

            mock_client.post.side_effect = _capture_post

            from yadgar.vacuum import _redefine_users_post_import

            _redefine_users_post_import("http://127.0.0.1:8080")

        assert posted_calls, "No POST was issued"
        call = posted_calls[0]
        sql = call["content"]

        # Must be plain-text, not JSON
        assert call["headers"].get("Content-Type") == "text/plain", (
            "Must use Content-Type: text/plain — JSON body is a silent no-op in SurrealDB v3"
        )

        # SQL must contain DEFINE USER for both users with correct roles
        assert "DEFINE USER" in sql
        assert "ROLES OWNER" in sql
        assert "ROLES VIEWER" in sql

        # Usernames must be backtick-quoted identifiers in the SQL body
        assert "`yadgar-rw`" in sql
        assert "`yadgar-ro`" in sql

        # Passwords must be embedded as SurrealQL string literals
        assert "'rw-secret'" in sql
        assert "'ro-secret'" in sql

    def test_escapes_single_quotes_in_passwords(self, monkeypatch):
        """Password containing single-quote is SQL-escaped by doubling ('' rule).

        v5.1.5 raw-SQL approach: passwords are embedded in SurrealQL single-quoted
        string literals.  A literal ' in the password becomes '' (SQL standard).
        """
        monkeypatch.setenv("YADGAR_RW_USER", "yadgar-rw")
        monkeypatch.setenv("YADGAR_RW_PASS", "abc'def")
        monkeypatch.setenv("YADGAR_RO_USER", "yadgar-ro")
        monkeypatch.setenv("YADGAR_RO_PASS", "ro-secret")

        posted_calls: list[dict] = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        def _capture_post(path, content=None, headers=None, **kwargs):
            posted_calls.append(
                {"content": content.decode() if isinstance(content, bytes) else content}
            )
            return mock_resp

        mock_client.post.side_effect = _capture_post

        with patch("yadgar.vacuum._build_http_client", return_value=mock_client):
            from yadgar.vacuum import _redefine_users_post_import

            _redefine_users_post_import("http://127.0.0.1:8080")

        assert posted_calls
        sql = posted_calls[0]["content"]
        # Single-quote in password must be doubled (SQL-standard escaping)
        assert "abc''def" in sql, f"Expected doubled quote in SQL, got: {sql!r}"
        # The unescaped form must NOT appear outside the doubled form
        assert "abc'def" not in sql.replace("abc''def", "")

    def test_raises_if_passwords_missing(self, monkeypatch):
        """RuntimeError raised when YADGAR_RW_PASS or YADGAR_RO_PASS unset."""
        monkeypatch.delenv("YADGAR_RW_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RO_PASS", raising=False)
        monkeypatch.setenv("YADGAR_RW_USER", "yadgar-rw")
        monkeypatch.setenv("YADGAR_RO_USER", "yadgar-ro")

        from yadgar.vacuum import _redefine_users_post_import

        with pytest.raises(RuntimeError, match="YADGAR_RW_PASS"):
            _redefine_users_post_import("http://127.0.0.1:8080")

    def test_raises_on_http_500(self, monkeypatch):
        """RuntimeError raised when the /sql call returns a non-200 status."""
        monkeypatch.setenv("YADGAR_RW_USER", "yadgar-rw")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw-secret")
        monkeypatch.setenv("YADGAR_RO_USER", "yadgar-ro")
        monkeypatch.setenv("YADGAR_RO_PASS", "ro-secret")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        with patch("yadgar.vacuum._build_http_client", return_value=mock_client):
            from yadgar.vacuum import _redefine_users_post_import

            with pytest.raises(RuntimeError, match="HTTP 500"):
                _redefine_users_post_import("http://127.0.0.1:8080")


# ---------------------------------------------------------------------------
# v5.1.7 F1 — check_invariants POST must include bearer token
# ---------------------------------------------------------------------------


class TestCheckInvariantsBearer:
    """v5.1.7: /api/check_invariants POST must include Authorization: Bearer header.

    Root cause: vacuum/__init__.py:398 called httpx.post without a bearer token.
    v5.0.0 added bearer-auth on /api/* — so the call returned 401 Unauthorized,
    exit code 2 false-failure, bloated dir retained.
    """

    def test_check_invariants_passes_bearer(self, monkeypatch):
        """When YADGAR_MCP_AUTH_TOKEN is set, POST /api/check_invariants includes bearer."""
        import httpx

        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token-abc123")

        captured_calls: list[dict] = []

        def fake_post(url, **kwargs):
            captured_calls.append({"url": url, "headers": kwargs.get("headers", {})})
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"ok": True}
            return m

        monkeypatch.setattr(httpx, "post", fake_post)

        # Drive cmd_vacuum_impl through phase 4 (check_invariants)
        import types as _types

        from yadgar.vacuum import cmd_vacuum_impl

        monkeypatch._pytest_tmpdir if hasattr(monkeypatch, "_pytest_tmpdir") else None
        # Use pytest tmp_path indirectly via the yadgar_home fixture approach —
        # we set YADGAR_HOME to a valid dir so preflight passes.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path

            p = Path(td)
            db = p / "surreal_db"
            for sub in ("vlog", "sstables", "wal"):
                (db / sub).mkdir(parents=True)
            (db / "vlog" / "00001.vlog").write_bytes(b"x" * 1000)

            monkeypatch.setenv("YADGAR_HOME", td)

            # Stub cleanup script
            script = p / "cleanup-backups.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

            args = _types.SimpleNamespace(
                backend_url="http://127.0.0.1:8080",
                service_mode="manual",
                db_path=str(db),
                yes=True,
            )

            fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"

            def fake_get(url, **kwargs):
                m = MagicMock()
                m.status_code = 200
                m.text = fake_surql if "/export" in url else ""
                return m

            monkeypatch.setattr(httpx, "get", fake_get)

            with _patch_side_build_success():
                with patch("yadgar.vacuum._log_consolidation_row"):
                    with patch("yadgar.vacuum.ServiceController"):
                        with patch("yadgar.vacuum._wait_for_health", return_value=True):
                            with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                                with patch("yadgar.vacuum._redefine_users_post_import"):
                                    cmd_vacuum_impl(args)

        # Find the check_invariants call
        ci_calls = [c for c in captured_calls if "/api/check_invariants" in c["url"]]
        assert ci_calls, "check_invariants was never called"

        headers = ci_calls[0]["headers"]
        assert "Authorization" in headers, (
            f"check_invariants POST missing Authorization header; got headers={headers!r}"
        )
        assert headers["Authorization"] == "Bearer test-token-abc123", (
            f"Wrong Authorization value: {headers['Authorization']!r}"
        )

    def test_check_invariants_env_missing_warns(self, monkeypatch, capsys):
        """When YADGAR_MCP_AUTH_TOKEN is unset, check_invariants POST has no bearer header
        and a WARNING is printed to stderr."""
        import types as _types

        import httpx

        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)

        captured_calls: list[dict] = []

        def fake_post(url, **kwargs):
            captured_calls.append({"url": url, "headers": kwargs.get("headers", {})})
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"ok": True}
            return m

        monkeypatch.setattr(httpx, "post", fake_post)

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path

            p = Path(td)
            db = p / "surreal_db"
            for sub in ("vlog", "sstables", "wal"):
                (db / sub).mkdir(parents=True)
            (db / "vlog" / "00001.vlog").write_bytes(b"x" * 1000)

            monkeypatch.setenv("YADGAR_HOME", td)

            script = p / "cleanup-backups.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

            fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"

            def fake_get(url, **kwargs):
                m = MagicMock()
                m.status_code = 200
                m.text = fake_surql if "/export" in url else ""
                return m

            monkeypatch.setattr(httpx, "get", fake_get)

            args = _types.SimpleNamespace(
                backend_url="http://127.0.0.1:8080",
                service_mode="manual",
                db_path=str(db),
                yes=True,
            )

            from yadgar.vacuum import cmd_vacuum_impl

            with _patch_side_build_success():
                with patch("yadgar.vacuum._log_consolidation_row"):
                    with patch("yadgar.vacuum.ServiceController"):
                        with patch("yadgar.vacuum._wait_for_health", return_value=True):
                            with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                                with patch("yadgar.vacuum._redefine_users_post_import"):
                                    cmd_vacuum_impl(args)

        ci_calls = [c for c in captured_calls if "/api/check_invariants" in c["url"]]
        assert ci_calls, "check_invariants was never called"

        headers = ci_calls[0]["headers"]
        # No Authorization header when token missing
        assert "Authorization" not in headers, (
            f"Expected no Authorization header when token unset; got headers={headers!r}"
        )

        # Warning printed to stderr
        captured = capsys.readouterr()
        assert "WARNING" in captured.err and "YADGAR_MCP_AUTH_TOKEN" in captured.err, (
            f"Expected WARNING about YADGAR_MCP_AUTH_TOKEN in stderr; got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# v5.10.2 — _log_consolidation_row must use YADGAR_DB_URL not hard-coded :8080
# ---------------------------------------------------------------------------


class TestLogConsolidationRowURL:
    """v5.10.2 Bug 2: _log_consolidation_row used http://127.0.0.1:8080 literal.
    It must use YADGAR_DB_URL env var (with :8080 as fallback only if env unset).
    """

    def test_uses_yadgar_db_url_when_set(self, monkeypatch):
        """When YADGAR_DB_URL is set, _log_consolidation_row must use it."""
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("YADGAR_DB_URL", "http://db.example.com:9000")

        urls_used = []

        def _fake_build_http_client(url):
            urls_used.append(url)
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: s
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = MagicMock(status_code=200)
            mock_client.post.return_value.__enter__ = lambda s: s
            mock_client.post.return_value.__exit__ = MagicMock(return_value=False)
            return mock_client

        from yadgar.vacuum import _log_consolidation_row

        row = {
            "kind": "vacuum",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "duration_seconds": 60,
            "before_bytes": 1000,
            "after_bytes": 800,
            "saved_bytes": 200,
            "saved_pct": 20.0,
        }

        with patch("yadgar.vacuum._build_http_client", _fake_build_http_client):
            _log_consolidation_row(row)

        assert urls_used, "_build_http_client must be called"
        assert "9000" in urls_used[0], (
            f"Expected db.example.com:9000 URL but got {urls_used[0]!r}. "
            "Fix: use os.environ.get('YADGAR_DB_URL', 'http://127.0.0.1:8080') as fallback."
        )

    def test_falls_back_to_8080_when_env_unset(self, monkeypatch):
        """When YADGAR_DB_URL is not set, fallback to http://127.0.0.1:8080."""
        from unittest.mock import MagicMock, patch

        monkeypatch.delenv("YADGAR_DB_URL", raising=False)

        urls_used = []

        def _fake_build_http_client(url):
            urls_used.append(url)
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: s
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = MagicMock(status_code=200)
            mock_client.post.return_value.__enter__ = lambda s: s
            mock_client.post.return_value.__exit__ = MagicMock(return_value=False)
            return mock_client

        from yadgar.vacuum import _log_consolidation_row

        row = {
            "kind": "vacuum",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "duration_seconds": 60,
            "before_bytes": 1000,
            "after_bytes": 800,
            "saved_bytes": 200,
            "saved_pct": 20.0,
        }

        with patch("yadgar.vacuum._build_http_client", _fake_build_http_client):
            _log_consolidation_row(row)

        assert urls_used, "_build_http_client must be called"
        assert "8080" in urls_used[0], f"Expected :8080 fallback URL but got {urls_used[0]!r}"


# ---------------------------------------------------------------------------
# #43 — spawn_surreal cred kwargs + _build_and_verify_side_db cred resolution
#
# BUG: spawn_surreal hardcoded `--user root --pass root`.  The vacuum HTTP
# client resolves creds from env (SURREAL_USER/PASS → YADGAR_RW_USER/PASS →
# YADGAR_DB_USER/PASS → root/root).  When the nightly env has e.g.
# YADGAR_DB_USER set, the client sends non-root creds to a side backend that
# only knows root/root → HTTP 401 on _bootstrap_namespace → exit 40.
#
# FIX: spawn_surreal accepts surreal_user/surreal_pass kwargs; the side-build
# resolves env creds with the SAME precedence as _build_http_client and passes
# them through.  The shared _resolve_db_creds() helper in vacuum/__init__.py
# encapsulates the four-tier precedence so both callers stay in sync.
# ---------------------------------------------------------------------------


class TestSpawnSurrealCredKwargs:
    """spawn_surreal passes surreal_user/surreal_pass into the argv.

    FAILS before the fix: the function ignores those kwargs and always passes
    `--user root --pass root` regardless of arguments.
    """

    def _mock_proc(self):
        """Return a MagicMock Popen with a pid attribute (int — atexit iterates it)."""
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        return mock_proc

    def test_spawn_surreal_passes_default_root_creds(self):
        """No kwargs → argv still contains --user root --pass root."""
        mock_proc = self._mock_proc()

        with patch("yadgar._surreal_runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
            from yadgar._surreal_runner import spawn_surreal

            spawn_surreal(port=9999, data_dir="/tmp/test")

        argv = mock_popen.call_args[0][0]
        assert "--user" in argv
        assert "--pass" in argv
        user_idx = argv.index("--user")
        pass_idx = argv.index("--pass")
        assert argv[user_idx + 1] == "root", (
            f"default user should be root, got {argv[user_idx + 1]!r}"
        )
        assert argv[pass_idx + 1] == "root", (
            f"default pass should be root, got {argv[pass_idx + 1]!r}"
        )

    def test_spawn_surreal_passes_custom_creds_in_argv(self):
        """surreal_user/surreal_pass kwargs appear in the built argv, not hardcoded root."""
        mock_proc = self._mock_proc()

        with patch("yadgar._surreal_runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
            from yadgar._surreal_runner import spawn_surreal

            spawn_surreal(
                port=9999, data_dir="/tmp/test", surreal_user="myuser", surreal_pass="mypass"
            )

        argv = mock_popen.call_args[0][0]
        assert "--user" in argv
        assert "--pass" in argv
        user_idx = argv.index("--user")
        pass_idx = argv.index("--pass")
        assert argv[user_idx + 1] == "myuser", (
            f"expected 'myuser' in argv after --user, got {argv[user_idx + 1]!r}"
        )
        assert argv[pass_idx + 1] == "mypass", (
            f"expected 'mypass' in argv after --pass, got {argv[pass_idx + 1]!r}"
        )

    def test_spawn_surreal_custom_creds_not_mixed_with_popen_kwargs(self):
        """surreal_user/surreal_pass do NOT leak into Popen kwargs."""
        mock_proc = self._mock_proc()

        with patch("yadgar._surreal_runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
            from yadgar._surreal_runner import spawn_surreal

            spawn_surreal(
                port=9999,
                data_dir="/tmp/test",
                surreal_user="myuser",
                surreal_pass="mypass",
            )

        popen_kwargs = mock_popen.call_args[1]
        assert "surreal_user" not in popen_kwargs, "surreal_user must not leak into Popen kwargs"
        assert "surreal_pass" not in popen_kwargs, "surreal_pass must not leak into Popen kwargs"


class TestResolveDatabaseCreds:
    """_resolve_db_creds() returns the correct (user, pass) per env-var precedence.

    FAILS before the fix: function does not yet exist.

    Precedence (matches _build_http_client exactly):
      1. SURREAL_USER / SURREAL_PASS   (default pass "root" when user is set)
      2. YADGAR_RW_USER / YADGAR_RW_PASS
      3. YADGAR_DB_USER / YADGAR_DB_PASS
      4. root / root
    """

    def test_surreal_user_wins(self, monkeypatch):
        monkeypatch.setenv("SURREAL_USER", "surreal_admin")
        monkeypatch.setenv("SURREAL_PASS", "surreal_secret")
        monkeypatch.setenv("YADGAR_RW_USER", "rw_user")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw_pass")
        monkeypatch.setenv("YADGAR_DB_USER", "db_user")
        monkeypatch.setenv("YADGAR_DB_PASS", "db_pass")

        from yadgar.vacuum import _resolve_db_creds

        user, password = _resolve_db_creds()
        assert user == "surreal_admin"
        assert password == "surreal_secret"

    def test_surreal_user_without_pass_defaults_root(self, monkeypatch):
        monkeypatch.setenv("SURREAL_USER", "surreal_admin")
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)

        from yadgar.vacuum import _resolve_db_creds

        user, password = _resolve_db_creds()
        assert user == "surreal_admin"
        assert password == "root"

    def test_yadgar_rw_user_wins_over_db_user(self, monkeypatch):
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.setenv("YADGAR_RW_USER", "rw_user")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw_pass")
        monkeypatch.setenv("YADGAR_DB_USER", "db_user")
        monkeypatch.setenv("YADGAR_DB_PASS", "db_pass")

        from yadgar.vacuum import _resolve_db_creds

        user, password = _resolve_db_creds()
        assert user == "rw_user"
        assert password == "rw_pass"

    def test_yadgar_db_user_fallback(self, monkeypatch):
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_RW_PASS", raising=False)
        monkeypatch.setenv("YADGAR_DB_USER", "db_user")
        monkeypatch.setenv("YADGAR_DB_PASS", "db_pass")

        from yadgar.vacuum import _resolve_db_creds

        user, password = _resolve_db_creds()
        assert user == "db_user"
        assert password == "db_pass"

    def test_root_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_RW_PASS", raising=False)
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_PASS", raising=False)

        from yadgar.vacuum import _resolve_db_creds

        user, password = _resolve_db_creds()
        assert user == "root"
        assert password == "root"


class TestBuildAndVerifySideDbCreds:
    """_build_and_verify_side_db resolves env creds and passes them to spawn_surreal.

    FAILS before the fix: spawn_surreal is called with no cred kwargs, so the
    side backend always starts with root/root regardless of env.
    """

    def _run_side_build_health_fail(self, monkeypatch):
        """Drive _build_and_verify_side_db with _wait_for_health → False so it
        short-circuits right after spawn_surreal (before any HTTP calls).
        Returns the list of dicts capturing each spawn_surreal call's kwargs."""
        import tempfile
        from pathlib import Path

        spawn_calls: list[dict] = []

        def _fake_spawn(port, data_dir, **kwargs):
            spawn_calls.append({"port": port, "data_dir": data_dir, **kwargs})
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            return mock_proc

        with tempfile.TemporaryDirectory() as td:
            side_path = Path(td) / "surreal_db.building-test"
            filtered = Path(td) / "export.surql"
            filtered.write_bytes(b"INSERT INTO memory {};")

            with patch("yadgar._surreal_runner.spawn_surreal", side_effect=_fake_spawn):
                with patch("yadgar.vacuum._wait_for_health", return_value=False):
                    with patch("yadgar._surreal_runner.teardown_surreal_proc"):
                        from yadgar.vacuum import _build_and_verify_side_db

                        _build_and_verify_side_db(
                            "http://127.0.0.1:8080",
                            filtered,
                            side_path,
                            {"memory": 1},
                        )

        return spawn_calls

    def test_side_build_passes_env_creds_to_spawn_surreal_surreal_user(self, monkeypatch):
        """SURREAL_USER set → spawn_surreal called with those creds."""
        monkeypatch.setenv("SURREAL_USER", "surreal_admin")
        monkeypatch.setenv("SURREAL_PASS", "surreal_secret")
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)

        spawn_calls = self._run_side_build_health_fail(monkeypatch)

        assert spawn_calls, "spawn_surreal was never called"
        call = spawn_calls[0]
        assert call.get("surreal_user") == "surreal_admin", (
            f"expected surreal_user='surreal_admin', got {call.get('surreal_user')!r}"
        )
        assert call.get("surreal_pass") == "surreal_secret", (
            f"expected surreal_pass='surreal_secret', got {call.get('surreal_pass')!r}"
        )

    def test_side_build_passes_rw_creds_when_surreal_user_unset(self, monkeypatch):
        """YADGAR_RW_USER set, SURREAL_USER unset → spawn_surreal gets rw creds."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.setenv("YADGAR_RW_USER", "rw_user")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw_pass")
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)

        spawn_calls = self._run_side_build_health_fail(monkeypatch)

        assert spawn_calls, "spawn_surreal was never called"
        call = spawn_calls[0]
        assert call.get("surreal_user") == "rw_user", (
            f"expected surreal_user='rw_user', got {call.get('surreal_user')!r}"
        )
        assert call.get("surreal_pass") == "rw_pass"

    def test_side_build_passes_db_creds_when_only_db_user_set(self, monkeypatch):
        """Only YADGAR_DB_USER set → spawn_surreal gets db creds."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_RW_PASS", raising=False)
        monkeypatch.setenv("YADGAR_DB_USER", "db_user")
        monkeypatch.setenv("YADGAR_DB_PASS", "db_pass")

        spawn_calls = self._run_side_build_health_fail(monkeypatch)

        assert spawn_calls, "spawn_surreal was never called"
        call = spawn_calls[0]
        assert call.get("surreal_user") == "db_user"
        assert call.get("surreal_pass") == "db_pass"

    def test_side_build_uses_root_when_no_creds_set(self, monkeypatch):
        """No cred env vars → spawn_surreal called with root/root."""
        monkeypatch.delenv("SURREAL_USER", raising=False)
        monkeypatch.delenv("SURREAL_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_RW_PASS", raising=False)
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_PASS", raising=False)

        spawn_calls = self._run_side_build_health_fail(monkeypatch)

        assert spawn_calls, "spawn_surreal was never called"
        call = spawn_calls[0]
        assert call.get("surreal_user") == "root"
        assert call.get("surreal_pass") == "root"
