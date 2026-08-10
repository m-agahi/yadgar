"""C4b (0047 PR#40 remediation) — the enqueue-stamp gap C3 left open.

C3 stamped ``wiki_add`` and stopped there. Four writers were left behind, and
C4's builder flagged them as a C5 blocker: C5 deletes the derivation tiers, so
any write path that never supplies ``project_id`` goes fail-loud. One of the
four is ``memorize`` — the highest-volume write path in the system.

What this file pins:

* ``memorize`` stamps UNCONDITIONALLY at enqueue (Car M stamped only when the
  caller passed ``project=``; the default path — i.e. almost every call — went
  to the drainer unstamped and re-derived inside a container that cannot).
* The stamp survives the whole replay chain, through **both** store branches.
  ``phase_store`` takes the curator branch whenever a curator and an embedding
  are present, which is the production configuration — a test that only covers
  ``_direct_insert`` would be green over an unfixed production path.
* The ``anchor`` chain (core tool → drainer → ``CheckpointRestore.anchor_memory``)
  carries the same value. ``anchor`` had no ``project`` parameter at all: C3's
  surface scan keyed on tools taking ``directory``, and ``anchor`` takes
  ``context``.
* ``agent_prompt_save`` — the one row-MINTING writer in ``admin_exec/wiki.py`` —
  threads the caller's value into ``WikiAddOptions`` and into its
  ``insert_wiki_page`` fallback.

The strongest assertions patch ``derive_project_id`` to RAISE: a row that still
carries the caller's value proves it came from the enqueue stamp and not from a
derivation that happened to agree (ADR-0080 gate-blindness).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.write_exec import MemorizeContext


def _exploding_classifier(*_a: Any, **_kw: Any) -> tuple[str, str]:
    raise AssertionError("derive_project_id must not be called on a stamped write path")


class _FakeMemoryStorage:
    """Records every dict handed to ``insert_memory``."""

    def __init__(self) -> None:
        self.memories: list[dict] = []

    def insert_memory(self, memory: dict, **_: Any) -> int:
        self.memories.append(dict(memory))
        return len(self.memories)

    # ── no-op tails the store phases call after the insert ──────────────────
    def update_memory_fields(self, *_a: Any, **_kw: Any) -> None:
        return None

    def update_memory_scores(self, *_a: Any, **_kw: Any) -> None:
        return None

    def upsert_file_hash(self, *_a: Any, **_kw: Any) -> None:
        return None

    def get_memory(self, *_a: Any, **_kw: Any) -> dict | None:
        return None

    def protect_memory(self, *_a: Any, **_kw: Any) -> None:
        return None


def _assemble(cls: Any, *args: Any, **kwargs: Any) -> Any:
    """Build a REAL collaborator around test doubles.

    The doubles above are structural stand-ins, not ``StorageEngine`` /
    ``EmbeddingEngine`` subclasses — subclassing to satisfy the checker would
    tie every double to the real signatures and defeat the point. Routing the
    construction through an ``Any``-typed callable keeps the doubles honest
    without a ``type: ignore`` or a ``cast``.
    """
    return cls(*args, **kwargs)


def _fake_embeddings() -> MagicMock:
    embeddings = MagicMock()
    embeddings.encode.return_value = b""
    embeddings.get_model_name.return_value = "fake"
    return embeddings


def _ctx(project_id: str | None) -> MemorizeContext:
    ctx = MemorizeContext(
        content="c4b memory",
        context="/home/max/git/yadgar",
        tags=["c4b"],
        is_protected=False,
        provenance_agent=None,
        tier=None,
        valid_until=None,
        ttl_days=None,
        reason="",
        project_id=project_id,
    )
    ctx.embedding = [0.0]
    return ctx


def _run_phase_store(ctx: MemorizeContext, storage: _FakeMemoryStorage, curator: Any) -> None:
    """Drive ``phase_store`` with everything but storage/curator stubbed out."""
    from yadgar.backend.write_exec._memorize_phases import _phase_store as store_mod

    embeddings = _fake_embeddings()
    buffer = MagicMock()
    with (
        patch.object(store_mod._lifecycle, "_get_storage", return_value=storage),
        patch.object(store_mod._lifecycle, "_get_embeddings", return_value=embeddings),
        patch.object(store_mod._lifecycle, "_get_buffer", return_value=buffer),
        patch.object(store_mod._st, "_curator", curator),
        patch.object(store_mod._st, "_consolidation", None),
        patch.object(store_mod._st, "_pool", None),
        patch.object(store_mod, "_file_hash", return_value=None),
        patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier),
    ):
        store_mod.phase_store(ctx)


# ── 1. memorize: core stamps unconditionally ─────────────────────────────────


class TestMemorizeStampsUnconditionally:
    """Car M's conditional stamp left the DEFAULT path exactly as broken.

    ``memorize(project=None)`` is the overwhelming majority of calls. If the
    stamp only fires on an explicit override, every one of them reaches the
    drainer unattributed.
    """

    def _capture_payload(self, **kwargs: Any) -> dict:
        from yadgar.core.server.tools.memorize import memorize

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured.update(payload)
            return "c4b-job-id"

        with (
            patch(
                "yadgar.core.server.tools.memorize._get_file_queue",
                return_value=type("FQ", (), {"enqueue": staticmethod(fake_enqueue)})(),
            ),
            patch(
                "yadgar.core.server.tools.memorize.resolve_effective_project",
                side_effect=lambda project, directory, session_project: (  # noqa: ARG005
                    project or "session/derived"
                ),
            ),
        ):
            result = memorize(
                content="c4b memory", context="/home/max/git/yadgar", tags=[], **kwargs
            )
        assert result.get("stored") is True
        return captured

    def test_payload_carries_project_id_without_explicit_project(self) -> None:
        assert self._capture_payload()["project_id"] == "session/derived"

    def test_explicit_project_still_wins(self) -> None:
        assert self._capture_payload(project="quinyx/aws2slack")["project_id"] == "quinyx/aws2slack"

    def test_wait_path_payload_is_stamped_too(self) -> None:
        """``wait=True`` builds the same payload through a different branch."""
        # NOTE: the ``tools`` package re-exports the ``memorize`` FUNCTION
        # under the module's own name, so neither ``from ... import memorize``
        # nor ``import ....tools.memorize as m`` yields the module. String
        # patch targets resolve through ``sys.modules`` and do.
        from yadgar.core.server.tools.memorize import memorize

        captured: dict = {}

        class _FQ:
            @staticmethod
            def enqueue(op: str, payload: dict) -> str:  # noqa: ARG004
                captured.update(payload)
                return "c4b-wait-job"

            @staticmethod
            def wait_for_job(job_id: str, timeout: float) -> dict:  # noqa: ARG004
                return {"status": "archived"}

        with (
            patch("yadgar.core.server.tools.memorize._get_file_queue", return_value=_FQ()),
            patch("yadgar.core.server.tools.memorize._forward_admin", return_value={}),
            patch(
                "yadgar.core.server.tools.memorize.resolve_effective_project",
                side_effect=lambda project, directory, session_project: (  # noqa: ARG005
                    project or "session/derived"
                ),
            ),
        ):
            memorize(content="c4b memory", context="/home/max/git/yadgar", tags=[], wait=True)

        assert captured["project_id"] == "session/derived"


# ── 2. memorize: the stamp survives the replay chain ─────────────────────────


class TestMemorizeReplayCarriesTheEnqueueValue:
    """payload → _apply_inner → run_memorize_replay → MemorizeContext → insert."""

    def test_apply_inner_forwards_project_id_to_the_replay(self) -> None:
        from yadgar.backend.queue_drainer import apply as apply_mod

        seen: dict = {}

        def _spy(**kwargs: Any) -> dict:
            seen.update(kwargs)
            return {}

        apply_obj = apply_mod._ApplyMixin.__new__(apply_mod._ApplyMixin)
        with patch("yadgar.backend.write_exec.run_memorize_replay", _spy):
            apply_obj._apply_inner(  # type: ignore[attr-defined]
                {
                    "op": "memorize",
                    "payload": {
                        "content": "c4b memory",
                        "context": "/home/max/git/yadgar",
                        "tags": [],
                        "project_id": "a/b",
                    },
                }
            )
        assert seen["project_id"] == "a/b"

    def test_memorize_context_carries_project_id(self) -> None:
        assert (
            MemorizeContext(
                content="x",
                context="/tmp",
                tags=[],
                is_protected=False,
                provenance_agent=None,
                tier=None,
                valid_until=None,
                ttl_days=None,
                reason="",
            ).project_id
            is None
        )
        assert _ctx("a/b").project_id == "a/b"

    def test_direct_insert_branch_stamps_the_caller_value(self) -> None:
        """No curator → ``_store_direct`` → ``_direct_insert``."""
        storage = _FakeMemoryStorage()
        _run_phase_store(_ctx("a/b"), storage, curator=None)
        assert storage.memories[0]["project_id"] == "a/b"

    def test_curator_branch_stamps_the_caller_value(self) -> None:
        """The PRODUCTION branch: a curator plus an embedding.

        ``phase_store`` prefers the curator whenever one is wired, so a fix
        that only reaches ``_direct_insert`` leaves production re-deriving.
        ``_find_similar_memories`` is emptied so the curator takes its CREATE
        path — the merge branch UPDATEs an existing row and never inserts, so
        the assertion below would not fire at all.
        """
        from yadgar.backend.curation import MemoryCurator

        storage = _FakeMemoryStorage()
        curator = _assemble(MemoryCurator, storage, _fake_embeddings(), MagicMock(), MagicMock())
        curator._find_similar_memories = lambda embedding, min_sim=0.6: []

        _run_phase_store(_ctx("a/b"), storage, curator=curator)
        assert storage.memories[0]["project_id"] == "a/b"


# ── 3. the anchor chain ──────────────────────────────────────────────────────


class TestAnchorChainCarriesProjectId:
    """core ``anchor`` → drainer → ``CheckpointRestore.anchor_memory`` → insert."""

    def _capture_payload(self, **kwargs: Any) -> dict:
        from yadgar.core.server.tools import misc as misc_mod

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured.update(payload)
            return "c4b-anchor-job"

        with (
            patch.object(
                misc_mod,
                "_get_file_queue",
                return_value=type("FQ", (), {"enqueue": staticmethod(fake_enqueue)})(),
            ),
            patch.object(
                misc_mod,
                "resolve_effective_project",
                side_effect=lambda project, directory, session_project: (  # noqa: ARG005
                    project or "session/derived"
                ),
            ),
        ):
            result = misc_mod.anchor(content="c4b anchor", context="/home/max/git/yadgar", **kwargs)
        assert result.get("queued") is True
        return captured

    def test_anchor_accepts_a_keyword_only_project(self) -> None:
        from yadgar.core.server.tools.misc import anchor

        param = inspect.signature(anchor).parameters.get("project")
        assert param is not None, "anchor() must accept project= like every other scoped writer"
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_payload_carries_project_id_without_explicit_project(self) -> None:
        assert self._capture_payload()["project_id"] == "session/derived"

    def test_explicit_project_still_wins(self) -> None:
        assert self._capture_payload(project="quinyx/aws2slack")["project_id"] == "quinyx/aws2slack"

    def test_invalid_project_returns_the_error_envelope(self) -> None:
        """A malformed override must not raise out of the MCP boundary."""
        from yadgar.core.server.tools.misc import anchor

        result = anchor(
            content="c4b anchor",
            context="/home/max/git/yadgar",
            project=123,  # type: ignore[arg-type]
        )
        assert result.get("queued") is not True
        assert "project must be a string" in str(
            result.get("reason", "") or result.get("error", "")
        )

    def test_apply_inner_forwards_project_id_to_the_anchor_replay(self) -> None:
        from yadgar.backend.queue_drainer import apply as apply_mod

        seen: dict = {}

        def _spy(**kwargs: Any) -> dict:
            seen.update(kwargs)
            return {}

        apply_obj = apply_mod._ApplyMixin.__new__(apply_mod._ApplyMixin)
        with patch("yadgar.backend.write_exec.run_anchor_replay", _spy):
            apply_obj._apply_inner(  # type: ignore[attr-defined]
                {
                    "op": "anchor",
                    "payload": {
                        "content": "c4b anchor",
                        "context": "/home/max/git/yadgar",
                        "project_id": "a/b",
                    },
                }
            )
        assert seen["project_id"] == "a/b"

    def test_anchor_memory_stamps_the_row_without_any_classifier(self) -> None:
        from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore

        storage = _FakeMemoryStorage()
        replay: Any = CheckpointRestore.__new__(CheckpointRestore)
        replay._storage = storage
        replay._embeddings = _fake_embeddings()
        replay._settings = MagicMock(REPLAY_ANCHOR_HEAT=1.0)

        with patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier):
            replay.anchor_memory(
                "c4b anchor",
                "/home/max/git/yadgar",
                ["_anchor"],
                project_id="a/b",
            )
        assert storage.memories[0]["project_id"] == "a/b"


# ── 4. agent_prompt_save — the row-minting writer in admin_exec/wiki.py ──────


class _FakeWikiStorage:
    def __init__(self) -> None:
        self.pages: list[dict] = []

    def get_wiki_page_by_slug(self, slug: str) -> dict | None:  # noqa: ARG002
        return None

    def insert_wiki_page(self, page: dict, **_: Any) -> int:
        self.pages.append(dict(page))
        return len(self.pages)

    def get_max_version_for_page(self, page_id: int) -> int:  # noqa: ARG002
        return 1

    def _extract_id(self, raw: Any) -> Any:
        return raw


class TestAgentPromptSaveThreadsProjectId:
    """The only writer in ``admin_exec/wiki.py`` that MINTS a row.

    Everything else in that module is a ``page_id``-keyed edit of a row whose
    ``project_id`` was stamped by whoever inserted it.
    """

    def _backend_save(self, storage: _FakeWikiStorage, wiki: Any, **extra: Any) -> dict:
        from yadgar.backend.admin_exec import wiki as admin_wiki

        payload = {
            "slug": "agent-prompt-c4b",
            "title": "Agent Prompt: c4b",
            "full_content": "## Purpose\n\np\n\n## Prompt\n\nbody",
            "tags": ["agent-prompt"],
            "pattern": "c4b",
            "purpose": "p",
            "directory": "/home/max/git/yadgar",
            **extra,
        }
        with (
            patch.object(admin_wiki._st, "_wiki", wiki),
            patch.object(admin_wiki, "_get_storage", return_value=storage),
            patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier),
        ):
            return admin_wiki.agent_prompt_save(payload)

    def test_wiki_add_options_receive_the_payload_project_id(self) -> None:
        seen: dict = {}

        class _Wiki:
            @staticmethod
            def add(*_a: Any, **kwargs: Any) -> dict:
                seen["opts"] = kwargs["opts"]
                return {"id": 3}

        self._backend_save(_FakeWikiStorage(), _Wiki(), project_id="a/b")
        assert seen["opts"].project_id == "a/b"

    def test_fallback_insert_stamps_the_payload_project_id(self) -> None:
        """``_st._wiki is None`` — the direct-``insert_wiki_page`` arm."""
        storage = _FakeWikiStorage()
        self._backend_save(storage, None, project_id="a/b")
        assert storage.pages[0]["project_id"] == "a/b"

    def test_core_tool_forwards_a_project_id(self) -> None:
        from yadgar.core.server.tools import agent_prompts as ap

        seen: list[dict] = []

        def _spy(op: str, payload: dict) -> dict:
            seen.append({"op": op, **payload})
            return {"saved": True, "version": 1, "slug": payload.get("slug"), "page_id": 1}

        with (
            patch.object(ap, "_forward_admin", _spy),
            patch.object(
                ap,
                "resolve_effective_project",
                side_effect=lambda project, directory, session_project: (  # noqa: ARG005
                    project or "session/derived"
                ),
            ),
        ):
            ap.agent_prompt_save(
                pattern="c4b", content="body", directory="/home/max/git/yadgar", purpose="p"
            )

        page_writes = [s for s in seen if s["op"] == "agent_prompt_save"]
        assert page_writes and page_writes[0]["project_id"] == "session/derived"

    def _discipline_forwards(self, **kwargs: Any) -> list[dict]:
        """Drive ``discipline_save`` — the session-having caller — end to end."""
        from yadgar.core.server.tools import agent_prompts as ap

        seen: list[dict] = []

        def _spy(op: str, payload: dict) -> dict:
            seen.append({"op": op, **payload})
            return {"saved": True, "version": 1, "slug": payload.get("slug"), "page_id": 1}

        with (
            patch.object(ap, "_forward_admin", _spy),
            patch.object(ap, "_read_agent_prompt", return_value=None),
            patch.object(
                ap,
                "resolve_effective_project",
                side_effect=lambda project, directory, session_project: (  # noqa: ARG005
                    project or "session/derived"
                ),
            ),
        ):
            ap.discipline_save(name="c4b-discipline", content="body", purpose="p", **kwargs)
        return [s for s in seen if s["op"] == "agent_prompt_save"]

    def test_discipline_page_write_forwards_a_project_id(self) -> None:
        """The SECOND ``agent_prompt_save`` forward site (C3: BOTH sites)."""
        writes = self._discipline_forwards()
        assert writes and writes[0]["project_id"] == "session/derived"

    def test_discipline_explicit_project_still_wins(self) -> None:
        writes = self._discipline_forwards(project="quinyx/aws2slack")
        assert writes and writes[0]["project_id"] == "quinyx/aws2slack"

    def test_seeder_path_stamps_the_global_default(self) -> None:
        """``_seed_discipline_pages`` calls the helper directly, sessionless.

        Its declared reach is ``directory="global"``; the stamp matches it
        rather than being omitted, because C4's DLQ gate treats a MISSING
        project_id as a defect and ``"global"`` as a live scope value.
        """
        from yadgar.core.server.tools import agent_prompts as ap

        seen: list[dict] = []

        def _spy(op: str, payload: dict) -> dict:
            seen.append({"op": op, **payload})
            return {"saved": True, "version": 1, "slug": payload.get("slug"), "page_id": 1}

        with patch.object(ap, "_forward_admin", _spy):
            ap._save_discipline_page("c4b-seeded", "p", "body")

        writes = [s for s in seen if s["op"] == "agent_prompt_save"]
        assert writes and writes[0]["project_id"] == "global"


# ── 5. the regression guard ──────────────────────────────────────────────────


_STAMP_SITES: tuple[tuple[str, str, str], ...] = (
    ("yadgar/core/server/tools/memorize.py", "memorize", "project_id"),
    ("yadgar/core/server/tools/misc.py", "anchor", "project_id"),
    ("yadgar/core/server/tools/wiki.py", "wiki_add", "project_id"),
    ("yadgar/core/server/tools/agent_prompts.py", "agent_prompt_save", "project_id"),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _func_ast(relpath: str, func_name: str) -> ast.FunctionDef:
    source = (_repo_root() / relpath).read_text(encoding="utf-8")
    func = next(
        (
            n
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and n.name == func_name
        ),
        None,
    )
    assert func is not None, f"{relpath}: no function named {func_name}"
    return func


def _stamp_values(node: ast.AST, key: str) -> list[ast.expr]:
    """Every expression written into *key* — kwarg, subscript-assign, dict entry."""
    values: list[ast.expr] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.keyword) and sub.arg == key:
            values.append(sub.value)
        elif isinstance(sub, ast.Assign) and any(
            isinstance(t, ast.Subscript)
            and isinstance(t.slice, ast.Constant)
            and t.slice.value == key
            for t in sub.targets
        ):
            values.append(sub.value)
        elif isinstance(sub, ast.Dict):
            for dk, dv in zip(sub.keys, sub.values, strict=True):
                if isinstance(dk, ast.Constant) and dk.value == key:
                    values.append(dv)
    return values


class TestNoWriterRegressesToAConditionalStamp:
    """The exact shape C4b deletes: ``x if project else None``.

    A behavioural assertion alone would not catch a re-introduced conditional
    that still happens to fire under the fixture's inputs, and a grep over
    comments would be worse still — this train has twice shipped a docstring
    asserting a property the code did not have. This reads the AST and asserts
    the value flowing into the ``project_id`` stamp is not a conditional
    expression and not guarded by an ``if`` that tests the ``project``
    parameter.
    """

    @pytest.mark.parametrize(("relpath", "func_name", "key"), _STAMP_SITES)
    def test_stamp_is_not_conditional(self, relpath: str, func_name: str, key: str) -> None:
        func = _func_ast(relpath, func_name)
        stamps = _stamp_values(func, key)
        assert stamps, f"{relpath}:{func_name} does not stamp {key!r} at all"
        for value in stamps:
            assert not isinstance(value, ast.IfExp), (
                f"{relpath}:{func_name} stamps {key!r} through a conditional expression "
                f"({ast.unparse(value)}) — the Car M shape C4b deleted. The stamp must be "
                "unconditional: the default path is the one that was broken."
            )

    @pytest.mark.parametrize(("relpath", "func_name", "key"), _STAMP_SITES)
    def test_stamp_is_not_nested_under_an_if_on_project(
        self, relpath: str, func_name: str, key: str
    ) -> None:
        func = _func_ast(relpath, func_name)
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if "project" not in names:
                continue
            assert not _stamp_values(node, key), (
                f"{relpath}:{func_name} stamps {key!r} inside `if "
                f"{ast.unparse(node.test)}` — that is the conditional stamp again, "
                "just spelled as a statement."
            )
