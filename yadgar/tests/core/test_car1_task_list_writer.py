"""Car 1 — task-list mirror write routed through Car 0's canonical path (§1.4 TDD).

The shipped stop-hook template step 4c told the model to ``wiki_add(page_type=
"task_list", NO branch_hint)`` to land canonical — but Car 0's router
(``_check_wiki_add_context``) decides purely on trusted ``gitness``, not
``page_type``. In a git dir with no branch_hint that write hits flow 2b →
hard-reject ``missing_branch``. The mirror never persisted.

Car 1 adds a dedicated, sanctioned server-side task-list writer
(``wiki_write_task_list``) that routes through ``_wiki_write_canonical`` (flow 1:
``branch=None`` + ``_internal``, page_type baked in). The sanction is STRUCTURAL —
the tool is purpose-built and bounded to the ``{project}-task-list`` slug — not a
spoofable ``page_type`` arg (which §0.6 KILLED as a gate).

These tests exercise the REAL write THROUGH the daemon gate (enqueue → drainer →
DB) — the coverage hole that let the hard-rejected write ship. Mirrors the Car 0
``test_flow1_...`` harness.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_GIT_DIR = "/home/user/car1-git-proj"
_NONGIT_DIR = "/home/user/car1-nongit-proj"


@pytest.fixture(autouse=True)
def _engines(tmp_path_factory):
    from yadgar.core import server

    tmp_path = tmp_path_factory.mktemp("car1")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_dir_branch_cache():
    from yadgar.core.server.tools import _dir_branch

    _dir_branch._get_cache().clear()
    yield
    _dir_branch._get_cache().clear()


def _enforce_on(monkeypatch):
    monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "true")
    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")


def _seed_dir(directory: str, gitness: bool, default_branch):
    from yadgar.core.forward import _forward_admin

    _forward_admin(
        "upsert_dir_branch_context",
        {"directory": directory, "gitness": gitness, "default_branch": default_branch},
    )


def _get_page(slug: str):
    from yadgar.core import server

    return server._wiki._storage.get_wiki_page_by_slug(slug)


_PAGE = (
    "# demo task list\n\n"
    "## Meta\n- project: demo\n- open: 1 · completed: 0\n\n"
    "## task:0001\n- subject: ship car 1\n- status: in_progress\n"
    "- modified: 2026-07-15T00:00:00Z\n"
)


class TestGatedWrite:
    def test_git_dir_no_branch_hint_lands_canonical(self, monkeypatch, _unit_backend_harness):
        """THE REGRESSION: task-list write in a git dir with NO branch_hint lands
        canonical (branch IS NULL) — NOT hard-rejected missing_branch."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_write_task_list(
            project="demo",
            content=_PAGE,
            directory=_GIT_DIR,
        )
        assert res.get("error") is None, res
        assert res.get("stored") is True, res
        drainer.drain_now()

        page = _get_page("demo-task-list")
        assert page is not None, "task-list page must land, not be DLQ'd missing_branch"
        assert page.get("branch") is None  # canonical slot despite git dir

    def test_nongit_dir_lands_canonical(self, monkeypatch, _unit_backend_harness):
        """Flow 1/3: non-git project → task-list write still lands canonical."""
        _enforce_on(monkeypatch)
        _seed_dir(_NONGIT_DIR, gitness=False, default_branch=None)
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_write_task_list(
            project="demo",
            content=_PAGE,
            directory=_NONGIT_DIR,
        )
        assert res.get("stored") is True, res
        drainer.drain_now()

        page = _get_page("demo-task-list")
        assert page is not None
        assert page.get("branch") is None

    def test_canonical_page_readable_from_feature_branch(self, monkeypatch, _unit_backend_harness):
        """The canonical page resolves under a feature-branch caller (§25 step-2) —
        guards the read-nudge regression the mirror exists to serve."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        drainer = _unit_backend_harness
        from yadgar.core import server

        server.wiki_write_task_list(project="demo", content=_PAGE, directory=_GIT_DIR)
        drainer.drain_now()

        read = server.wiki_read(
            "demo-task-list", directory=_GIT_DIR, branch_hint="feat/some-branch"
        )
        assert read.get("slug") == "demo-task-list", read
        assert "task:0001" in read.get("content", "")

    def test_canonical_page_readable_from_nongit_read(self, monkeypatch, _unit_backend_harness):
        """A reader in a non-git project resolves its OWN canonical task-list page
        (no branch context) — §25 step-2 (dir + branch IS NULL)."""
        _enforce_on(monkeypatch)
        _seed_dir(_NONGIT_DIR, gitness=False, default_branch=None)
        drainer = _unit_backend_harness
        from yadgar.core import server

        server.wiki_write_task_list(project="demo", content=_PAGE, directory=_NONGIT_DIR)
        drainer.drain_now()

        # Non-git reader: no branch_hint, page resolves via the canonical slot.
        read = server.wiki_read("demo-task-list", directory=_NONGIT_DIR)
        assert read.get("slug") == "demo-task-list", read

    def test_never_sessioned_dir_lands_canonical_not_flow4_reject(
        self, monkeypatch, _unit_backend_harness
    ):
        """Item 4: a never-session'd directory (no _seed_dir → no dir_branch row)
        must NOT hit flow-4 missing_branch. The sanctioned writer routes through
        _wiki_write_canonical directly, bypassing the flow table entirely — so an
        unknown dir still lands canonical. Pins the boundary against a future
        'why two paths?' simplification that would route the writer through
        _check_wiki_add_context (which WOULD flow-4-reject an unknown dir)."""
        _enforce_on(monkeypatch)
        # Deliberately NO _seed_dir — this dir is unknown to the trusted store.
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_write_task_list(
            project="demo",
            content=_PAGE,
            directory="/home/user/car1-never-sessioned",
        )
        assert res.get("error") is None, res
        assert res.get("stored") is True, res
        drainer.drain_now()

        page = _get_page("demo-task-list")
        assert page is not None, "unknown-dir task-list write must NOT be flow-4 rejected"
        assert page.get("branch") is None  # canonical despite never-session'd dir

    def test_replaces_slug_on_second_write(self, monkeypatch, _unit_backend_harness):
        """Second write to the same project overwrites (replace_slug baked in) —
        one canonical page, not a duplicate."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        drainer = _unit_backend_harness
        from yadgar.core import server

        server.wiki_write_task_list(project="demo", content=_PAGE, directory=_GIT_DIR)
        drainer.drain_now()
        updated = _PAGE.replace("in_progress", "completed")
        server.wiki_write_task_list(project="demo", content=updated, directory=_GIT_DIR)
        drainer.drain_now()

        page = _get_page("demo-task-list")
        assert page is not None
        assert page.get("branch") is None
        assert "completed" in page.get("content", "")


class TestBoundaryPreserved:
    def test_raw_wiki_add_task_list_still_rejects_in_git_dir(
        self, monkeypatch, _unit_backend_harness
    ):
        """PIN THE BOUNDARY: a RAW wiki_add(page_type="task_list", no branch_hint)
        in a git dir STILL rejects. page_type is NOT a canonical gate (§0.6 KILLED
        it). Canonical is reachable only via the sanctioned wiki_write_task_list
        tool. This stops a future 'simplification' back into the forgeable hole."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        from yadgar.core import server

        res = server.wiki_add(
            title="demo task list",
            content=_PAGE,
            page_type="task_list",
            tags=["task-list"],
            directory=_GIT_DIR,
        )
        assert res.get("error") == "missing_branch", res
        assert res.get("stored") is False


class TestSanction:
    def test_writer_is_registered_tool(self):
        """wiki_write_task_list is exported as a model-callable tool."""
        from yadgar.core import server

        assert hasattr(server, "wiki_write_task_list")

    def test_writer_is_non_power_tool(self):
        """The automatic mirror runs every checkpoint, so the writer must be a
        NON-power @_tool() (registered in ALL profiles incl. minimal) — matching
        the primary-path wiki_add it replaces. A power tool is dropped under
        YADGAR_PROFILE=minimal, which would silently re-break the mirror with an
        approval/omission instead of a working canonical write."""
        import inspect

        from yadgar.core.server.tools import wiki as wiki_tools

        mod_src = inspect.getsource(wiki_tools)
        idx = mod_src.index("def wiki_write_task_list(")
        decorator_line = mod_src[:idx].rstrip().splitlines()[-1].strip()
        assert decorator_line == "@_tool()", (
            f"wiki_write_task_list must be non-power @_tool(); got {decorator_line!r}"
        )

    def test_writer_has_no_internal_or_branch_param(self):
        """The sanctioned writer exposes NO _internal / branch escape hatch — the
        canonical decision is baked in, not a caller arg."""
        import inspect

        from yadgar.core.server.tools import wiki as wiki_tools

        params = set(inspect.signature(wiki_tools.wiki_write_task_list).parameters)
        assert "_internal" not in params
        assert "branch" not in params
        assert "branch_hint" not in params
        assert "page_type" not in params
