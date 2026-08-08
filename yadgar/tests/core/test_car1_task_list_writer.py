"""The sanctioned task-list mirror writer (``wiki_write_task_list``).

The stop-hook checkpoint protocol calls this writer so the harness task list
survives ``/clear`` / session exit. It routes through the server-side
``_wiki_write_canonical`` seam, which sets the server-only ``_internal`` token.
The sanction is STRUCTURAL — the tool is purpose-built and bounded to the
``{project}-task-list`` slug — not a spoofable ``page_type`` arg.

ADR-0215/0216: this file used to be built entirely on Car 0's four-flow branch
router (git dir + no branch_hint → hard-reject ``missing_branch``, non-git dir →
canonical, etc.). Branch scoping is removed, the flow table is gone, and every
page is reachable by directory alone — so those tests were deleted. What remains
is the branch-agnostic coverage of the surviving seam: the replace-slug
behaviour and the structural sanction assertions.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_DIR = "/home/user/car1-proj"


@pytest.fixture(autouse=True)
def _engines(tmp_path_factory):
    from yadgar.core import server

    tmp_path = tmp_path_factory.mktemp("car1")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


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
    def test_replaces_slug_on_second_write(self, monkeypatch, _unit_backend_harness):
        """Second write to the same project overwrites (replace_slug baked in) —
        one canonical page, not a duplicate."""
        monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")
        drainer = _unit_backend_harness
        from yadgar.core import server

        server.wiki_write_task_list(project="demo", content=_PAGE, directory=_DIR)
        drainer.drain_now()
        updated = _PAGE.replace("in_progress", "completed")
        server.wiki_write_task_list(project="demo", content=updated, directory=_DIR)
        drainer.drain_now()

        page = _get_page("demo-task-list")
        assert page is not None
        assert "completed" in page.get("content", "")

    def test_page_readable_by_directory_alone(self, monkeypatch, _unit_backend_harness):
        """The written page resolves by slug + directory, with no branch context —
        the read the restore-nudge depends on."""
        monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")
        drainer = _unit_backend_harness
        from yadgar.core import server

        server.wiki_write_task_list(project="demo", content=_PAGE, directory=_DIR)
        drainer.drain_now()

        read = server.wiki_read("demo-task-list", directory=_DIR)
        assert read.get("slug") == "demo-task-list", read
        assert "task:0001" in read.get("content", "")


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
        approval/omission instead of a working write."""
        import inspect

        from yadgar.core.server.tools import wiki as wiki_tools

        mod_src = inspect.getsource(wiki_tools)
        idx = mod_src.index("def wiki_write_task_list(")
        decorator_line = mod_src[:idx].rstrip().splitlines()[-1].strip()
        assert decorator_line == "@_tool()", (
            f"wiki_write_task_list must be non-power @_tool(); got {decorator_line!r}"
        )

    def test_writer_has_no_internal_or_branch_param(self):
        """The sanctioned writer exposes NO _internal escape hatch — the canonical
        decision is baked in, not a caller arg."""
        import inspect

        from yadgar.core.server.tools import wiki as wiki_tools

        params = set(inspect.signature(wiki_tools.wiki_write_task_list).parameters)
        assert "_internal" not in params
        assert "branch" not in params
        assert "branch_hint" not in params
        assert "page_type" not in params
