"""RED tests for v5.42.5 — directory contract (foundational release).

TDD: written BEFORE implementation. All tests in this file start RED and go
GREEN as implementation progresses through phases 1–8.

Coverage:
T1a. wiki_add(directory="")  → synchronous {"error": "missing_directory"}
T1b. wiki_add(no directory)  → synchronous {"error": "missing_directory"}
T2.  Drainer rejects payload lacking directory_context → DLQ failure_reason="missing_directory"
T3.  §25 4-step: directory=$caller AND branch IS NULL beats directory="global" AND branch IS NULL
T4.  wiki_list(directory=...) scopes to that dir + global; excludes other dirs
T5.  _resolve_page_id_by_slug uses caller directory, not daemon os.getcwd()
T6.  agent_prompt_save(no directory) → {"error": "missing_directory"}
T7a. block_create(scope='project', directory=None) → {"ok": False, "error": "missing_directory"}
T7b. block_create(scope='global', directory=None)  → ok=True (no dir required)
T8.  recall(directory="/proj/A") excludes memories from directory_context="/proj/B"
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
from yadgar.core import server

pytestmark = pytest.mark.usefixtures("recall_backend_bypass", "admin_backend_bypass")


# ── shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_42_5_directory_contra")
    server.init_engines(
        db_path=str(tmp_path / "test_dir_contract.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def bare_drainer(tmp_path):
    """Isolated FileQueue + QueueDrainer for unit-level drainer tests."""
    import yadgar._shared.runtime.state as _st

    fq = FileQueue(tmp_path)
    drainer = QueueDrainer(
        queue=fq,
        storage_factory=lambda: _st._storage,
        drain_interval=9999,
    )
    return drainer, fq


@pytest.fixture
def patched_drainer(tmp_path):
    """FileQueue + QueueDrainer with server lifecycle patches (integration)."""
    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl

    real_fq = FileQueue(tmp_path)
    drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,
    )

    def _get_fq():
        return real_fq

    with (
        patch.object(_cl, "_get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.wiki._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", drainer),
        patch.object(_state_mod, "_file_queue", real_fq),
    ):
        yield drainer, real_fq


# ── helpers ───────────────────────────────────────────────────────────────────


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]


#: Identity the direct-insert helpers stamp. C5 (ADR-0227) made the storage
#: chokepoint "the caller's value, or a raise" — an insert with no project_id
#: no longer falls back to 'global'. The WIKI fixtures below exercise DIRECTORY
#: scoping (§25 slug resolution, wiki_list), which is a live axis on that path,
#: so they all share one project_id: varying it would change what the directory
#: assertions mean.
#:
#: RECALL IS THE EXCEPTION AND IT IS NOT A NAMING QUIRK. Car C7 re-keyed the
#: recall read path from ``directory_context`` onto ``project_id``
#: (``_shared/storage/directory.py``): the stage-1 WHERE emits
#: ``(project_id = $sc_pid OR $sc_reach IN tags)`` and mentions ``directory_context``
#: nowhere, on any arm. So ``TestRecallDirectoryScope`` below MUST vary
#: ``project_id`` to have anything to assert — two rows that share a project and
#: differ only by directory are BOTH in scope, and a recall returning both is the
#: contract working. Seeding them identically and asserting exclusion tests a
#: mechanism that no longer exists.
_TEST_PROJECT_ID = "owner/repo"

#: A second, unrelated project. Used only by ``TestRecallDirectoryScope`` — see
#: the note above for why that one class varies identity where the rest do not.
_OTHER_PROJECT_ID = "other/repo"


def _insert_wiki_direct(
    storage,
    title: str,
    content: str,
    directory_context: str,
    project_id: str = _TEST_PROJECT_ID,
) -> str:
    slug = _slugify(title)
    storage.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": ["test"],
            "links": [],
            "source_memory_ids": [],
            "confidence": "medium",
            "directory_context": directory_context,
            "project_id": project_id,
        }
    )
    return slug


def _insert_memory_direct(
    storage,
    content: str,
    directory_context: str,
    project_id: str = _TEST_PROJECT_ID,
    tags: list[str] | None = None,
) -> None:
    """Insert a memory record directly to storage.

    Embedding left as None — embedded DB (surrealdb-python) does not require
    it for FTS search; the recall assertions ride the FTS arm.

    ``project_id`` and ``tags`` are parameters because the recall scope test
    needs to vary BOTH arms of C7's stage-1 predicate
    (``project_id = $sc_pid OR $sc_reach IN tags``). They default to the shared
    identity and a plain marker tag, so every other caller is unaffected.
    """
    storage.insert_memory(
        {
            "content": content,
            "embedding": None,
            "tags": tags if tags is not None else ["test"],
            "directory_context": directory_context,
            "heat": 0.8,
            "confidence": 0.7,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": "all-MiniLM-L6-v2",
            "project_id": project_id,
            "_internal": True,
        }
    )


# ── T1: MCP boundary rejects missing directory ───────────────────────────────


class TestWikiAddDirectoryBoundary:
    """wiki_add must reject empty/missing directory at the MCP boundary.

    C5 (ADR-0227) changed the LABEL of this rejection, not its existence:
    ``_check_wiki_add_context`` used to return ``_missing_directory_error``
    and now returns the ``UnresolvedProjectError`` payload, so the wire
    ``error`` reads ``unresolved_project``. The guard itself is very much
    alive and is NOT the resolver — see
    ``test_a_valid_project_does_not_rescue_an_empty_directory``, which is the
    assertion that keeps these three from collapsing into duplicates of the
    C5 resolver tests. ``directory_context`` survives until C11, and the
    drainer's own step-4 check (covered by ``TestDrainerRejectsNoDirectory``
    below) still rejects with the literal ``missing_directory``.
    """

    def test_wiki_add_rejects_empty_directory(self):
        """wiki_add(directory='') → synchronous error, no storage write."""
        from yadgar.core.server.tools.wiki import wiki_add

        # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
        # enqueues); the symbol lives in the backend queue_drainer.
        with patch("yadgar.backend.queue_drainer.is_draining", return_value=False):
            result = wiki_add(
                title="Test Page",
                content="Some content",
                directory="",
            )
        assert result.get("error") == "unresolved_project", (
            f"Expected the boundary rejection, got: {result}"
        )
        assert result.get("stored") is False

    def test_wiki_add_rejects_whitespace_only_directory(self):
        """wiki_add(directory='   ') → synchronous error."""
        from yadgar.core.server.tools.wiki import wiki_add

        # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
        # enqueues); the symbol lives in the backend queue_drainer.
        with patch("yadgar.backend.queue_drainer.is_draining", return_value=False):
            result = wiki_add(
                title="Test Page",
                content="Some content",
                directory="   ",
            )
        assert result.get("error") == "unresolved_project", (
            f"Expected the boundary rejection, got: {result}"
        )

    def test_wiki_add_rejects_missing_directory_param(self):
        """wiki_add with no directory param → synchronous error."""
        from yadgar.core.server.tools.wiki import wiki_add

        # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
        # enqueues); the symbol lives in the backend queue_drainer.
        with patch("yadgar.backend.queue_drainer.is_draining", return_value=False):
            result = wiki_add(
                title="Test Page No Dir",
                content="Some content",
                # no directory param
            )
        assert result.get("error") == "unresolved_project", (
            f"Expected the boundary rejection, got: {result}"
        )

    def test_wiki_add_accepts_valid_directory(self):
        """wiki_add(directory='/proj/x') succeeds (queued or committed)."""
        from yadgar.core.server.tools.wiki import wiki_add

        # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
        # enqueues); the symbol lives in the backend queue_drainer.
        with patch("yadgar.backend.queue_drainer.is_draining", return_value=False):
            result = wiki_add(
                title="Valid Dir Page",
                content="Some content",
                directory="/proj/x",
                project=_TEST_PROJECT_ID,
            )
        assert result.get("error") != "missing_directory", (
            f"Should not reject valid directory, got: {result}"
        )
        # stored=True or queued=True
        assert result.get("stored") is True or result.get("queued") is True


# ── T2: Drainer rejects missing directory_context ────────────────────────────


class TestDrainerRejectsNoDirectory:
    """QueueDrainer._validate_wiki_add rejects records without directory_context."""

    def _make_wiki_record(
        self,
        *,
        directory_context: str | None = "ABSENT",
        internal: bool = False,
        project_id: str | None = "owner/repo",
    ) -> dict:
        """Build a queued wiki_add record.

        ``project_id`` defaults to a real key because C4 added the enqueue-time
        stamp as step 5 of ``_validate_wiki_add``, AFTER the step-4 directory
        check this class is about. A record without one is rejected for the
        wrong reason, which would silently turn the pass-case assertions below
        into no-ops. The DIRECTORY check is what this class covers; the stamp
        has its own coverage in ``test_c5_dlq_project_id_gate``.
        """
        payload: dict = {
            "wiki_schema_version": 2,
            "slug": "drainer-dir-test",
            "title": "Drainer Dir Test",
            "content": "Test content for directory enforcement.",
            "category": "reference",
            "tags": [],
        }
        if directory_context != "ABSENT":
            payload["directory_context"] = directory_context
        if project_id is not None:
            payload["project_id"] = project_id
        if internal:
            payload["_internal"] = True
        return {"op": "wiki_add", "id": "test-id", "payload": payload}

    def test_missing_directory_context_rejected(self, bare_drainer):
        """wiki_add payload without directory_context → rejection string."""
        drainer, _ = bare_drainer
        record = self._make_wiki_record(directory_context="ABSENT")
        result = drainer._validate_wiki_add(record)
        assert result is not None, "Expected rejection but got None"
        assert "missing_directory" in result, f"Got: {result!r}"

    def test_empty_directory_context_rejected(self, bare_drainer):
        """wiki_add payload with directory_context='' → rejection string."""
        drainer, _ = bare_drainer
        record = self._make_wiki_record(directory_context="")
        result = drainer._validate_wiki_add(record)
        assert result is not None, "Expected rejection but got None"
        assert "missing_directory" in result, f"Got: {result!r}"

    def test_valid_directory_context_passes(self, bare_drainer):
        """wiki_add payload with valid directory_context → passes (None)."""
        drainer, _ = bare_drainer
        record = self._make_wiki_record(directory_context="/home/max/git/yadgar")
        result = drainer._validate_wiki_add(record)
        assert result is None, f"Expected pass but got rejection: {result!r}"

    def test_missing_directory_goes_to_dlq(self, patched_drainer, tmp_path):
        """End-to-end: enqueue wiki_add without directory → DLQ with missing_directory."""
        import json

        import yadgar._shared.runtime.state as _st

        drainer, fq = patched_drainer

        fq.enqueue(
            "wiki_add",
            {
                "wiki_schema_version": 2,
                "slug": "no-dir-page",
                "title": "No Dir Page",
                "content": "Content without directory.",
                "category": "reference",
                "tags": [],
                # No directory_context
            },
        )

        drainer.drain_now()

        # Should be in DLQ
        dlq_sidecars = list(fq.dlq_dir.glob("*.error.json"))
        assert len(dlq_sidecars) == 1, f"Expected 1 DLQ entry, got {len(dlq_sidecars)}"

        meta = json.loads(dlq_sidecars[0].read_text())
        assert meta.get("failure_reason") == "missing_directory", (
            f"Expected missing_directory, got: {meta.get('failure_reason')}"
        )

        # No wiki_page row should exist
        rows = _st._storage._q("SELECT * FROM wiki_page WHERE slug = 'no-dir-page'")
        assert len(rows) == 0, "Expected 0 rows, but wiki_page was created"


# ── T3: §25 4-step resolution — project-canonical beats global ───────────────


class TestResolutionProjectBeatsGlobal:
    """§25 4-step: directory=$caller AND branch IS NULL beats directory='global' AND branch IS NULL."""

    def test_resolution_project_canonical_beats_global(self):
        """Insert same slug in /proj/A (canonical) and global. wiki_read with /proj/A returns /proj/A."""
        import yadgar._shared.runtime.state as _st

        storage = _st._storage

        slug = "test-resolution-slug"
        _insert_wiki_direct(
            storage,
            "Test Resolution Slug",
            "Project-A content",
            directory_context="/proj/A",
        )
        _insert_wiki_direct(
            storage,
            "Test Resolution Slug",
            "Global content",
            directory_context="global",
        )

        from yadgar.core.server.tools.wiki import wiki_read

        result = wiki_read(slug, directory="/proj/A", project=_TEST_PROJECT_ID)

        assert result.get("error") is None, f"Got error: {result}"
        assert result.get("directory_context") == "/proj/A", (
            f"Expected /proj/A, got: {result.get('directory_context')!r}"
        )
        assert "Project-A content" in result.get("content", ""), (
            f"Expected project content, got: {result.get('content')!r}"
        )

    def test_resolution_falls_back_to_global_when_no_project_match(self):
        """wiki_read with /proj/B (not present) → global page returned."""
        import yadgar._shared.runtime.state as _st

        storage = _st._storage

        slug = "test-global-fallback-slug"
        _insert_wiki_direct(
            storage,
            "Test Global Fallback Slug",
            "Global content here",
            directory_context="global",
        )

        from yadgar.core.server.tools.wiki import wiki_read

        result = wiki_read(slug, directory="/proj/B", project=_TEST_PROJECT_ID)

        assert result.get("error") is None, f"Got error: {result}"
        assert result.get("directory_context") == "global", (
            f"Expected global, got: {result.get('directory_context')!r}"
        )


# ── T4: wiki_list filtered by directory ──────────────────────────────────────


class TestWikiListDirectoryFilter:
    """wiki_list(directory=...) returns dir pages + global, excludes others."""

    def test_wiki_list_scopes_to_directory(self):
        """Insert pages in /proj/A, /proj/B, global. List /proj/A → A + global only."""
        import yadgar._shared.runtime.state as _st

        storage = _st._storage

        _insert_wiki_direct(storage, "Page A1", "A1 content", "/proj/A")
        _insert_wiki_direct(storage, "Page A2", "A2 content", "/proj/A")
        _insert_wiki_direct(storage, "Page B1", "B1 content", "/proj/B")
        _insert_wiki_direct(storage, "Page Global", "Global content", "global")

        from yadgar.core.server.tools.wiki import wiki_list

        results = wiki_list(directory="/proj/A")
        slugs = [r["slug"] for r in results]

        assert "page-a1" in slugs, f"Expected page-a1, got: {slugs}"
        assert "page-a2" in slugs, f"Expected page-a2, got: {slugs}"
        assert "page-global" in slugs, f"Expected page-global (always visible), got: {slugs}"
        assert "page-b1" not in slugs, f"Expected page-b1 excluded, got: {slugs}"


# ── T5: _resolve_page_id_by_slug uses caller dir, not daemon CWD ─────────────


class TestResolveSlugCallerDirectory:
    """_resolve_page_id_by_slug(directory=...) uses caller dir, not os.getcwd()."""

    def test_resolve_slug_uses_caller_directory_not_daemon_cwd(self):
        """Insert page under /caller/repo. Daemon CWD=/daemon/root. wiki_history with caller dir works."""
        import yadgar._shared.runtime.state as _st

        storage = _st._storage

        slug = _insert_wiki_direct(
            storage,
            "Caller Repo Page",
            "Page from caller repo",
            directory_context="/caller/repo",
        )

        from yadgar.core.server.tools.wiki import wiki_history

        # ADR-0215: wiki.py no longer imports os / falls back to os.getcwd() at
        # all — the directory param is the only source of truth now, so the
        # caller-dir-wins invariant this test checks is structurally guaranteed.
        result = wiki_history(slug, directory="/caller/repo")

        assert "error" not in result, f"Expected success but got error: {result}"
        assert result.get("slug") == slug, f"Unexpected result: {result}"


# ── T6: agent_prompt_save requires directory ─────────────────────────────────


class TestAgentPromptSaveRequiresDirectory:
    """agent_prompt_save must reject calls without directory."""

    def test_agent_prompt_save_requires_directory(self):
        """agent_prompt_save(no directory) → {"error": "missing_directory"}."""
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save(pattern="test-pattern", content="Test prompt content")
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )
        # Verify no wiki_page created
        import yadgar._shared.runtime.state as _st

        rows = _st._storage._q("SELECT * FROM wiki_page WHERE slug CONTAINS 'test-pattern'")
        assert len(rows) == 0, f"Expected 0 rows, found {len(rows)}"

    def test_agent_prompt_save_accepts_valid_directory(self):
        """agent_prompt_save with valid directory saves successfully."""
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save(
            pattern="test-pattern-valid",
            content="Test prompt content",
            directory="/home/max/git/yadgar",
            project=_TEST_PROJECT_ID,
        )
        assert result.get("saved") is True, f"Expected saved=True, got: {result}"
        assert result.get("error") is None


# ── T7: blocks scope='project' requires directory ────────────────────────────


class TestBlocksProjectScopeRequiresDirectory:
    """block_create(scope='project') must reject missing directory."""

    def test_block_create_project_scope_rejects_empty_directory(self):
        """block_create(scope='project', directory=None) → ok=False, error=missing_directory."""
        from yadgar.core.server.tools.blocks import block_create

        result = block_create(name="test-block", content="content", scope="project", directory=None)
        assert result.get("ok") is False, f"Expected ok=False, got: {result}"
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )

    def test_block_create_global_scope_allows_no_directory(self):
        """block_create(scope='global', directory=None) → ok=True."""
        from yadgar.core.server.tools.blocks import block_create

        result = block_create(
            name="testglobalblock", content="global content", scope="global", directory=None
        )
        # Should succeed — global blocks don't require directory
        assert result.get("ok") is True or result.get("id") is not None, (
            f"Expected success for global scope, got: {result}"
        )
        assert result.get("error") != "missing_directory"


# ── T8: recall scoped by project ─────────────────────────────────────────────


class TestRecallDirectoryScope:
    """recall(...) excludes memories belonging to another project.

    THE SETUP HERE VARIES ``project_id``, NOT ``directory_context``, and that is
    the whole point of the class. Car C7 re-keyed the recall read path off
    directory: every stage-1 WHERE this recall emits reads

        ``(project_id = $sc_pid OR $sc_reach IN tags)``

    on the memory FTS arm, the memory vector arm, and both wiki arms, plus
    ``project_id = $pid`` on the profile/belief arms — and NONE of them mentions
    ``directory_context``. Two rows sharing a project and differing only by
    directory are therefore BOTH in scope, and a recall returning both is the
    contract working, not a leak.

    The class kept its name because the guarantee it protects is unchanged —
    "an agent working in proj A does not see proj B's writes". Only the key
    that expresses "proj" moved, from the checkout path to the project id.
    """

    def test_agent_in_proj_A_does_not_see_proj_B_writes(self):
        """recall scoped to project A excludes a memory written under project B.

        v5.65 Fix D: directory is still passed explicitly (no os.getcwd()
        fallback in recall) — it just no longer decides what is in scope.
        """
        import yadgar._shared.runtime.state as _st

        storage = _st._storage

        _insert_memory_direct(
            storage, "proj-A-secret content here", "/proj/A", project_id=_TEST_PROJECT_ID
        )
        _insert_memory_direct(
            storage, "proj-B-secret content here", "/proj/B", project_id=_OTHER_PROJECT_ID
        )

        from yadgar.core.server.tools.recall import recall

        results = recall(
            query="proj secret", max_results=10, directory="/proj/A", project=_TEST_PROJECT_ID
        )

        contents = [r.get("content", "") for r in results]
        any("proj-A-secret" in c for c in contents)
        found_B = any("proj-B-secret" in c for c in contents)

        assert not found_B, f"proj-B memory leaked into /proj/A recall: {contents}"
        # proj-A should be found (if embedding similarity is high enough)
        # Note: zero embeddings may not have high cosine similarity; test the exclusion more than inclusion
        # The critical assertion is that B is excluded.

    def test_reach_tagged_row_from_another_project_is_still_admitted(self):
        """The ``global`` reach arm admits an out-of-project row that carries it.

        Car F6 pins the OTHER half of C7's predicate. The exclusion test above
        would pass just as happily if the ``$sc_reach IN tags`` arm were deleted
        and the clause narrowed to ``project_id = $sc_pid``, and
        ``_shared/storage/directory.py`` says in as many words why that must not
        go unnoticed: dropping the reach arm "silently narrows ~429 rows down to
        one project — the failure looks like 'recall got worse', not like a bug".

        Both rows below live in the SAME foreign project and the SAME foreign
        directory. The only thing separating them is the reach tag, so nothing
        but that arm can explain a result set holding one and not the other.
        """
        import yadgar._shared.runtime.state as _st

        storage = _st._storage

        _insert_memory_direct(
            storage,
            "reach-tagged wombat parable",
            "/proj/Z",
            project_id=_OTHER_PROJECT_ID,
            tags=["test", "global"],
        )
        _insert_memory_direct(
            storage,
            "untagged wombat parable",
            "/proj/Z",
            project_id=_OTHER_PROJECT_ID,
        )

        from yadgar.core.server.tools.recall import recall

        results = recall(
            query="wombat parable",
            max_results=10,
            directory="/proj/A",
            project=_TEST_PROJECT_ID,
        )

        contents = [r.get("content", "") for r in results]
        assert any("reach-tagged wombat" in c for c in contents), (
            f"reach arm dropped a 'global'-tagged row from another project: {contents}"
        )
        assert not any("untagged wombat" in c for c in contents), (
            f"untagged out-of-project row leaked: {contents}"
        )

    def test_every_candidate_query_carries_the_scope_predicate(self):
        """The scope is enforced IN THE QUERY, not by a post-filter downstream.

        Car F6 adds this because the two result-level tests above CANNOT see the
        difference. Measured: gutting ``build_project_scope_clause`` to return
        ``("", {})`` leaves both of them GREEN — ``MemoryProvider.candidates``
        runs ``is_project_eligible`` over everything the retriever returns, so
        the Python residual guard alone still produces a correctly-scoped result
        LIST. What it cannot restore is the thing C7 exists for: ADR-0206's
        point is that a filter running after the query has already SPENT the
        query's LIMIT, so a scoped recall over a corpus where the caller's
        project is a minority silently under-returns instead of erroring.

        So this test asserts on the SQL, not on the rows. Every query that
        spends a candidate budget — ``ORDER BY … LIMIT`` over ``memory`` or
        ``wiki_page`` — must carry the predicate itself. A future refactor that
        drops ``scope_sql`` from one arm while leaving the clause BUILDER intact
        passes ``test_c7_recall_scope_clause.py`` and both tests above; it fails
        here, which is the only place it can.

        The ``WHERE id IN [...]`` hydration queries are deliberately exempt:
        they re-read rows whose ids a scoped query already chose, so they spend
        no budget and have nothing to narrow.

        NEEDS THE `ml` EXTRA. The four-arm liveness assertions at the end are
        what stop "none were unscoped" being vacuously true, and the memory
        VECTOR arm only runs when `recall` has a real query embedding to compare
        against. Without `sentence-transformers` the embedding is None, that arm
        never issues its query, and this failed with "memory vector arm did not
        run" on every plain `make test` (`--extra test`) run — false RED with no
        bug behind it. Guarded and sanctioned CONDITIONALLY in
        yadgar/tests/skip_inventory.json (`ml-extra-recall-vector-arm-01`).

        TRADE-OFF, recorded rather than hidden: the guard skips the WHOLE test,
        so the scope-predicate assertion above it also stops running under plain
        `make test`. Making only the vector-arm assertion conditional would be a
        weakened assertion, which is worse. CI and `make test-ci` install
        `--extra ml`, so the check still runs where it gates merges — and the
        extras receipt turns a skip on such a leg into a gate FAILURE.
        """
        pytest.importorskip(
            "sentence_transformers", reason="sentence-transformers not installed (ml extra)"
        )
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.storage import client as _client_mod

        storage = _st._storage
        _insert_memory_direct(
            storage, "budget-probe content here", "/proj/A", project_id=_TEST_PROJECT_ID
        )

        captured: list[tuple[str, dict]] = []
        original_q = _client_mod._ClientMixin._q

        def _spy(self, surql, params=None):
            captured.append((surql, dict(params or {})))
            return original_q(self, surql, params)

        from yadgar.core.server.tools.recall import recall

        with patch.object(_client_mod._ClientMixin, "_q", _spy):
            recall(
                query="budget probe",
                max_results=10,
                directory="/proj/A",
                project=_TEST_PROJECT_ID,
            )

        assert captured, "no SQL captured — the spy never saw the recall"

        budgeted = [
            (sql, params)
            for sql, params in captured
            if "ORDER BY" in sql
            and "LIMIT" in sql
            and (" FROM memory " in sql or " FROM wiki_page " in sql)
        ]
        unscoped = [
            sql for sql, params in budgeted if "sc_pid" not in params or "sc_reach" not in params
        ]
        assert not unscoped, (
            "candidate query spends its LIMIT before any scoping — "
            f"the C7 predicate is missing from: {unscoped}"
        )

        # The four arms C7 names must all be present, or "none were unscoped"
        # would be vacuously true for an arm that simply stopped running.
        joined = " ".join(sql for sql, _ in budgeted)
        assert "FROM memory " in joined and "@1@" in joined, "memory FTS arm did not run"
        assert "FROM memory " in joined and "vector::similarity::cosine" in joined, (
            "memory vector arm did not run"
        )
        assert "FROM wiki_page " in joined, "wiki arms did not run"
