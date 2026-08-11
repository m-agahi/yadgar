"""C5b (0047 PR#40 §2 AMENDMENT 2) — the chokepoint bypasses.

C5 made ``_resolve_project_id_for_write`` raise when the caller names no
project. Four writers in ``_shared/storage/wiki.py`` never reached it: each
issued its own raw ``CREATE type::record('memory', $id) SET …`` with no
``project_id`` in the SET clause. Because the column is ``option<string>``
that is **worse than the raise C5 designed** — it wrote unattributed rows
silently, and no ``GLOBAL_FALLBACK`` / ``"unresolved"`` / ``local/`` grep
could see it.

The guard below is the part that generalises. It is AST-level over every
``CREATE type::record('memory'`` string constant under ``_shared/storage/**``,
so a fifth writer added later is caught the moment it lands rather than the
next time someone audits the file by hand. Adjacent string literals fold into
one ``ast.Constant`` at parse time, which is what makes "the SET clause names
``project_id``" answerable from the constant alone.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from yadgar._shared.errors import UnresolvedProjectError
from yadgar.tests.core.conftest import TEST_PROJECT_ID

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_STORAGE_ROOT = _REPO_ROOT / "yadgar" / "_shared" / "storage"

#: The raw-write signature every bypass shared. Matched inside a folded
#: string constant, never against the raw file text — a comment quoting the
#: SQL must not be able to red the guard, and a f-string-assembled statement
#: must not be able to hide from it.
_RAW_MEMORY_CREATE = "CREATE type::record('memory'"


def _raw_memory_create_sites() -> list[tuple[pathlib.Path, int, str]]:
    """Every folded string constant under ``_shared/storage`` that CREATEs a memory."""
    sites: list[tuple[pathlib.Path, int, str]] = []
    for path in sorted(_STORAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _RAW_MEMORY_CREATE in node.value
            ):
                sites.append((path, node.lineno, node.value))
    return sites


class TestRawMemoryCreateGuard:
    """No raw memory INSERT may omit ``project_id``. This is the regression net."""

    def test_the_sweep_finds_sites_at_all(self):
        """Positive control: a guard that matches nothing passes vacuously.

        If the SQL is ever assembled some other way this test reds first and
        names the real problem — a silent zero-site sweep would otherwise let
        the assertion below go green forever.
        """
        assert _raw_memory_create_sites(), (
            f"no {_RAW_MEMORY_CREATE!r} constant found under {_STORAGE_ROOT} — "
            "the guard below can no longer see the write path it protects"
        )

    def test_every_raw_memory_create_binds_project_id(self):
        offenders = [
            f"{path.relative_to(_REPO_ROOT)}:{lineno}"
            for path, lineno, sql in _raw_memory_create_sites()
            # ``project_id = $`` and not merely ``project_id``: a hardcoded
            # literal owner is the same silent-wrong-answer failure ADR-0227
            # forbids, wearing a bound-looking name.
            if "project_id = $" not in sql
        ]
        assert not offenders, (
            "raw memory CREATE routes around the _resolve_project_id_for_write "
            f"chokepoint (no bound project_id in the SET clause): {offenders}"
        )


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine; per-test isolation via the conftest wipe."""
    return module_storage


class TestUpsertsStampTheCallersProjectId:
    """The three surviving singleton writers own their row via the caller."""

    def test_project_init_row_carries_the_caller_value(self, storage):
        directory = "/proj/c5b-init"
        storage.upsert_project_init(directory, "# init", project_id=TEST_PROJECT_ID)
        rows = storage._q(
            "SELECT project_id FROM memory WHERE directory_context = $dir "
            "AND '_project_init' INSIDE tags",
            {"dir": directory},
        )
        assert [r.get("project_id") for r in rows] == [TEST_PROJECT_ID]

    def test_project_init_unnamed_raises_and_writes_nothing(self, storage):
        directory = "/proj/c5b-init-unnamed"
        with pytest.raises(UnresolvedProjectError):
            storage.upsert_project_init(directory, "# init")
        rows = storage._q(
            "SELECT project_id FROM memory WHERE directory_context = $dir",
            {"dir": directory},
        )
        assert rows == [], "an unnamed write must not leave an unattributed row behind"

    def test_active_work_row_carries_the_caller_value(self, storage):
        directory = "/proj/c5b-active"
        storage.upsert_active_work(directory, "working on x", project_id=TEST_PROJECT_ID)
        rows = storage._q(
            "SELECT project_id FROM memory WHERE directory_context = $dir "
            "AND '_active_work' INSIDE tags",
            {"dir": directory},
        )
        assert [r.get("project_id") for r in rows] == [TEST_PROJECT_ID]

    def test_active_work_unnamed_raises_and_writes_nothing(self, storage):
        directory = "/proj/c5b-active-unnamed"
        with pytest.raises(UnresolvedProjectError):
            storage.upsert_active_work(directory, "working on x")
        rows = storage._q(
            "SELECT project_id FROM memory WHERE directory_context = $dir",
            {"dir": directory},
        )
        assert rows == []

    def test_prelude_marker_row_carries_the_caller_value(self, storage):
        directory = "/proj/c5b-prelude"
        storage.upsert_dispatch_prelude_marker(directory, project_id=TEST_PROJECT_ID)
        rows = storage._q(
            "SELECT project_id FROM memory WHERE directory_context = $dir "
            "AND '_dispatch_prelude' INSIDE tags",
            {"dir": directory},
        )
        assert [r.get("project_id") for r in rows] == [TEST_PROJECT_ID]

    def test_prelude_marker_unnamed_raises_and_writes_nothing(self, storage):
        directory = "/proj/c5b-prelude-unnamed"
        with pytest.raises(UnresolvedProjectError):
            storage.upsert_dispatch_prelude_marker(directory)
        rows = storage._q(
            "SELECT project_id FROM memory WHERE directory_context = $dir",
            {"dir": directory},
        )
        assert rows == []


class TestPromptUsageBypassIsGone:
    """The fourth bypass had no honest owner, so it was deleted rather than stamped.

    ``increment_prompt_usage`` wrote ONE global ``_prompt_usage`` row keyed by
    pattern and aggregating every project's dispatches. Stamping it would have
    had to invent an owner, which is precisely the manufactured-identity
    pathology ADR-0227 forbids. Car I already replaced it (``agent_pattern.uses``,
    a reach-global SQL integer with no ``project_id`` column by design, D40) and
    deregistered the admin op — the storage methods were the leftovers.
    """

    def test_storage_methods_are_gone(self):
        from yadgar._shared.storage import StorageEngine

        assert not hasattr(StorageEngine, "increment_prompt_usage")
        assert not hasattr(StorageEngine, "get_prompt_usage_counts")

    def test_no_prompt_usage_row_is_written_anywhere(self):
        offenders = [
            f"{path.relative_to(_REPO_ROOT)}:{lineno}"
            for path, lineno, sql in _raw_memory_create_sites()
            if "_prompt_usage" in sql
        ]
        assert not offenders, f"the _prompt_usage memory row is back: {offenders}"


class TestBackendAdminOpsThreadProjectId:
    """The backend halves pass the wire value straight through — no re-derivation."""

    def test_bootstrap_project_store_threads_it(self, monkeypatch):
        from unittest.mock import MagicMock

        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.project import bootstrap_project_store

        storage = MagicMock()
        storage.upsert_project_init.return_value = {"id": 1}
        storage.get_block.return_value = None
        monkeypatch.setattr(_st, "_storage", storage)

        bootstrap_project_store(
            {"resolved": "/proj/root", "content": "# init", "project_id": TEST_PROJECT_ID}
        )
        storage.upsert_project_init.assert_called_once_with(
            "/proj/root", "# init", project_id=TEST_PROJECT_ID
        )

    def test_update_active_work_threads_it(self, monkeypatch):
        from unittest.mock import MagicMock

        import yadgar.backend.admin_exec.project as project_mod

        storage = MagicMock()
        storage.upsert_active_work.return_value = {"previous_content": None, "new_memory": {}}
        monkeypatch.setattr(project_mod, "_get_storage", lambda: storage)

        project_mod.update_active_work(
            {"resolved": "/proj/root", "content": "work", "project_id": TEST_PROJECT_ID}
        )
        storage.upsert_active_work.assert_called_once_with(
            "/proj/root", "work", project_id=TEST_PROJECT_ID
        )

    def test_record_prelude_marker_threads_it(self, monkeypatch):
        from unittest.mock import MagicMock

        import yadgar.backend.admin_exec.project as project_mod

        storage = MagicMock()
        monkeypatch.setattr(project_mod, "_get_storage", lambda: storage)

        out = project_mod.record_prelude_marker(
            {"directory": "/proj/root", "project_id": TEST_PROJECT_ID}
        )
        assert out == {"recorded": True}
        storage.upsert_dispatch_prelude_marker.assert_called_once_with(
            "/proj/root", project_id=TEST_PROJECT_ID
        )

    def test_seed_store_threads_the_payload_project_id_into_the_init_draft(self, monkeypatch):
        """The fifth caller: ``seed.py`` already held a ``project_id`` and dropped it.

        It threads the value into ``_store_one`` for every seeded memory and
        then wrote the ``_project_init`` draft without it — the one row in the
        seed that arrived unattributed.
        """
        from unittest.mock import MagicMock

        import yadgar.backend.admin_exec.seed as seed_mod

        storage = MagicMock()
        monkeypatch.setattr(seed_mod, "_get_storage", lambda: storage)
        monkeypatch.setattr(seed_mod, "_get_embeddings", lambda: MagicMock())
        monkeypatch.setattr(seed_mod, "_get_thermo", lambda: MagicMock())
        monkeypatch.setattr(seed_mod, "_delete_existing_seed_memories", lambda *a, **k: 0)

        seed_mod.seed_store(
            {
                "root": "/proj/root",
                "memories": [],
                "init_content": "# init",
                "project_id": TEST_PROJECT_ID,
            }
        )
        storage.upsert_project_init.assert_called_once_with(
            "/proj/root", "# init", project_id=TEST_PROJECT_ID
        )


class TestCoreShellsResolveBeforeForwarding:
    """The MCP tool call is the only participant that can see the session."""

    def test_bootstrap_project_sends_project_id_on_the_wire(self, monkeypatch):
        import yadgar.core.server.tools.project as project_mod

        seen: dict = {}

        def _capture(op, payload):
            seen["op"] = op
            seen["payload"] = payload
            return {}

        monkeypatch.setattr(project_mod, "_forward_admin", _capture)
        monkeypatch.setattr(project_mod, "_resolve_project_root", lambda d: d)

        project_mod.bootstrap_project(
            directory="/proj/root", content="# init", project=TEST_PROJECT_ID
        )
        assert seen["op"] == "bootstrap_project_store"
        assert seen["payload"]["project_id"] == TEST_PROJECT_ID

    def test_bootstrap_project_unnamed_raises_before_any_write(self, monkeypatch):
        import yadgar.core.server.tools.project as project_mod

        def _never(op, payload):
            raise AssertionError("an unnamed bootstrap_project must not reach the write")

        monkeypatch.setattr(project_mod, "_forward_admin", _never)
        monkeypatch.setattr(project_mod, "_resolve_project_root", lambda d: d)

        with pytest.raises(UnresolvedProjectError):
            project_mod.bootstrap_project(directory="/proj/root", content="# init")

    def test_update_active_work_sends_project_id_on_the_wire(self, monkeypatch):
        import yadgar.core.server.tools.project as project_mod

        seen: dict = {}

        def _capture(op, payload):
            seen["payload"] = payload
            return {}

        monkeypatch.setattr(project_mod, "_forward_admin", _capture)
        monkeypatch.setattr(project_mod, "_resolve_project_root", lambda d: d)
        monkeypatch.setattr(project_mod, "_register_active_work_directory", lambda r: None)

        project_mod.update_active_work(
            directory="/proj/root", content="work", project=TEST_PROJECT_ID
        )
        assert seen["payload"]["project_id"] == TEST_PROJECT_ID

    def test_update_active_work_unnamed_raises_before_any_write(self, monkeypatch):
        import yadgar.core.server.tools.project as project_mod

        def _never(op, payload):
            raise AssertionError("an unnamed update_active_work must not reach the write")

        monkeypatch.setattr(project_mod, "_forward_admin", _never)
        monkeypatch.setattr(project_mod, "_resolve_project_root", lambda d: d)

        with pytest.raises(UnresolvedProjectError):
            project_mod.update_active_work(directory="/proj/root", content="work")


class TestPreludeMarkerSkipsRatherThanBreakingTheRead:
    """``agent_dispatch_prelude`` is a READ tool that happens to nudge a marker.

    Raising for want of a project would break prompt assembly over telemetry, so
    the marker write takes C4's declared skip-and-count path instead: the row is
    never written unattributed, the skip is counted, and the prelude still
    returns. The visible cost — stated rather than hidden — is that a caller who
    passes no ``project=`` now records no marker at all.
    """

    def test_named_project_forwards_the_marker(self, monkeypatch):
        import yadgar.core.server.tools.dispatch_helper as dh

        seen: dict = {}
        monkeypatch.setattr(dh, "_forward_admin", lambda op, p: seen.update(op=op, payload=p))
        dh._record_prelude_marker(None, "/proj/root", TEST_PROJECT_ID)
        assert seen["op"] == "record_prelude_marker"
        assert seen["payload"]["project_id"] == TEST_PROJECT_ID

    def test_unnamed_project_skips_the_forward_and_counts(self, monkeypatch):
        """Both halves are asserted, and the forward half RECORDS rather than raises.

        A ``_never`` stub that raises proves nothing here: the forward already
        sits inside a broad ``except Exception: logger.debug(...)`` swallow, so
        an AssertionError raised from it would be eaten and the test would red
        for the wrong reason (or, with a weaker assertion, pass). Recording the
        calls and asserting the list is empty survives the swallow.
        """
        import yadgar.core.server.tools.dispatch_helper as dh

        forwarded: list = []
        counted: list = []
        monkeypatch.setattr(dh, "_forward_admin", lambda op, payload: forwarded.append(op))
        monkeypatch.setattr(dh, "observe_project_id_skip", lambda writer: counted.append(writer))

        dh._record_prelude_marker(None, "/proj/root", None)
        assert forwarded == [], "an unnamed marker write must not reach the wire"
        assert counted == ["dispatch_prelude_marker"]
