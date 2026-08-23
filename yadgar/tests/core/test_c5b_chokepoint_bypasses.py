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

Car F7: that folding claim stopped being universally true the moment an
f-string joined the concatenation. Car F1 rewrote ``memory.py``'s SQL builder
so the ``embedding = ...`` fragment is computed and interpolated:

    "CREATE type::record('memory', $id) SET "
    f"content = $content, {emb_assign}, tags = $tags, "
    "source_episode_id = $source_episode_id, "
    ...

Adjacent string/f-string literals still fold into ONE node at parse time, but
that node is now ``ast.JoinedStr``, not ``ast.Constant`` — and ``ast.walk``
still visits the ``ast.Constant`` fragments NESTED inside it (before and after
the interpolation) as separate nodes. Matching bare ``ast.Constant`` against
``_RAW_MEMORY_CREATE`` therefore finds only the FIRST fragment (up to
``{emb_assign}``), which never reaches ``project_id = $project_id`` further
down the same statement — a false positive that flags a correctly-bound write
as a bypass. ``_joined_str_text`` reconstructs the JoinedStr's full logical
text (each interpolation replaced with a placeholder, since its runtime value
is not statically knowable) so the guard sees the whole statement either way —
an f-string must not be able to hide a real bypass, and must not be able to
manufacture a fake one either.
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


#: Placeholder substituted for each f-string interpolation when reconstructing
#: a ``JoinedStr``'s logical text. Never collides with real SQL: it contains
#: neither ``project_id`` nor the CREATE signature, so it can only ever make a
#: match FAIL to trigger less often, never more — a bypass hiding an
#: interpolated ``project_id = $project_id`` fragment is not a pattern any
#: writer in this codebase uses, and this guard does not need to model it.
_INTERPOLATION_PLACEHOLDER = "�"


def _joined_str_text(node: ast.JoinedStr) -> str:
    """Reconstruct a ``JoinedStr``'s full logical text.

    Constant fragments are used verbatim; each ``FormattedValue``
    (an f-string interpolation, e.g. ``{emb_assign}``) is replaced with
    ``_INTERPOLATION_PLACEHOLDER`` since its runtime value is not statically
    knowable from the AST alone. This is what lets the substring checks below
    see the WHOLE statement, not just the fragment before the first
    interpolation.
    """
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append(_INTERPOLATION_PLACEHOLDER)
    return "".join(parts)


def _raw_memory_create_sites(
    root: pathlib.Path = _STORAGE_ROOT,
) -> list[tuple[pathlib.Path, int, str]]:
    """Every folded string (``Constant`` or reconstructed ``JoinedStr``) under
    *root* that CREATEs a memory.

    ``root`` defaults to ``_STORAGE_ROOT`` (the guard's real target) and is
    only overridden by the discrimination tests below, which scan a synthetic
    ``tmp_path`` module instead of the real chokepoint.
    """
    sites: list[tuple[pathlib.Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Car F7: Constant fragments that live INSIDE a JoinedStr are visited
        # twice by ast.walk — once as a child of the JoinedStr, once directly.
        # Skip the direct visit for those (id()-keyed, since Constant nodes
        # aren't hashable-by-value-safe across positions) so each concatenated
        # statement is matched exactly once, via whichever node covers its
        # FULL text: the JoinedStr when an interpolation is present, the bare
        # Constant otherwise.
        joined_str_child_ids = {
            id(part)
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            for part in node.values
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = _joined_str_text(node)
                if _RAW_MEMORY_CREATE in text:
                    sites.append((path, node.lineno, text))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in joined_str_child_ids
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


class TestGuardHandlesFStringConcatenation:
    """Car F7: the guard must discriminate an f-string BOTH ways.

    ``memory.py:120``'s real builder folds an f-string into the concatenation
    (Car F1's ``emb_assign`` interpolation) and DOES bind ``project_id``
    further down the same statement — the guard must not flag it. A raw
    CREATE that hides an unbound write behind an f-string interpolation must
    still be caught — the guard must not let the f-string launder it either.
    Each test writes a synthetic module to ``tmp_path`` and scans THAT
    (``_raw_memory_create_sites(root=tmp_path)``), not the real
    ``_shared/storage`` tree, so this is independent of what happens to land
    in the real chokepoint later.
    """

    def test_fstring_builder_with_bound_project_id_passes(self, tmp_path):
        """The real F1 pattern: project_id is bound, just past the interpolation."""
        (tmp_path / "fake_storage.py").write_text(
            "emb_assign = 'embedding = $embedding'\n"
            "sql = (\n"
            "    \"CREATE type::record('memory', $id) SET \"\n"
            '    f"content = $content, {emb_assign}, tags = $tags, "\n'
            '    "project_id = $project_id, "\n'
            '    "is_protected = $is_protected"\n'
            ")\n",
            encoding="utf-8",
        )
        sites = _raw_memory_create_sites(root=tmp_path)
        assert sites, "guard must still find the site through the JoinedStr"
        offenders = [f"{p}:{ln}" for p, ln, sql in sites if "project_id = $" not in sql]
        assert not offenders, f"legitimate f-string builder false-flagged as a bypass: {offenders}"

    def test_fstring_disguised_bypass_is_still_caught(self, tmp_path):
        """A genuine bypass hiding behind an f-string interpolation must still fail."""
        (tmp_path / "fake_bypass.py").write_text(
            "owner = 'hardcoded-owner'\n"
            "sql = (\n"
            "    \"CREATE type::record('memory', $id) SET \"\n"
            '    f"content = $content, owner = {owner!r}, tags = $tags"\n'
            ")\n",
            encoding="utf-8",
        )
        sites = _raw_memory_create_sites(root=tmp_path)
        assert sites, "guard must find the site at all (positive control)"
        offenders = [f"{p}:{ln}" for p, ln, sql in sites if "project_id = $" not in sql]
        assert offenders, (
            "a raw CREATE with no bound project_id must be caught even when "
            "assembled via an f-string interpolation"
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

    def test_prelude_marker_is_not_protected(self, storage):
        """C7a: _dispatch_prelude markers must age out — no decay-proof slot.

        The marker is content-free telemetry ("dispatch_prelude marker"), so
        holding a permanent ``is_protected`` slot per directory crowds out
        real anchors (34/106 protected rows in this project were markers).
        The other two ``is_protected = true`` sites belong to ``upsert_active_work``
        and ``upsert_project_init`` — different concerns, untouched here.
        """
        directory = "/proj/c7a-prelude-not-protected"
        storage.upsert_dispatch_prelude_marker(directory, project_id=TEST_PROJECT_ID)
        rows = storage._q(
            "SELECT is_protected FROM memory WHERE directory_context = $dir "
            "AND '_dispatch_prelude' INSIDE tags",
            {"dir": directory},
        )
        assert len(rows) == 1, rows
        assert not rows[0].get("is_protected"), rows[0]


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
