"""C10 (f) (0047 PR#40 remediation §5) — ``context`` stops being a scope key.

``memorize``/``anchor`` name their directory parameter ``context``, which is
why no ``directory`` grep in the survey ever reached it. It was **dual-purpose**:
the scope key stamped into ``memory.directory_context`` AND a real filesystem
path fed to ``_file_hash``/``upsert_file_hash`` for staleness detection. Left
alone, ``memorize`` would take ``project`` *and* a directory under another
name — the two-keys-for-one-concept state ADR-0225 exists to delete.

The split this file pins (the plan's decision, same shape as judgement site (b)):

* ``project`` carries scope. The ``directory_context`` stamp comes from the
  resolved ``project_id``, **never** from ``context`` — through BOTH store
  branches (``_direct_insert`` and the curator), because ``phase_store``
  prefers the curator whenever a curator and an embedding are present, so a
  change reaching only one arm goes green in a curator-less harness and stays
  broken in production (the C4b lesson).
* ``context`` survives as an **optional real path** used only for staleness
  hashing (carve-out 3). Absent → no hash, which is the existing best-effort
  contract, not a new failure mode.
* A ``context`` that is free-text prose does not become a scope key. This is
  not hypothetical: the live corpus holds **18 distinct ``directory_context``
  values on ``memory`` that are prose, not paths** — the three used below are
  real values read out of the corpus (``db_inspect``, 2026-08-10). Callers were
  already treating ``context`` as a description because the parameter's name
  invites it; splitting the roles is what stops the class, not a stricter
  docstring.

SCOPE — ``anchor`` is deliberately NOT in this car. ``anchor_memory`` has no
staleness use at all (``file_hash`` is hardcoded ``None``), so its ``context``
is *purely* a scope key: making it optional and moving its stamp are the same
change, and that change cannot be made green here. ``restore(directory=<path>)``
→ ``get_anchored_memories_scoped`` still matches ``directory_context = $dir``,
so an anchor stamped with a ``project_id`` becomes unreachable — and both files
that would fix it (``_shared/storage/memory.py``, ``backend/restoration/
checkpoint_restore.py``) belong to sibling cars running in parallel. Re-keying a
write without re-keying the read underneath it in the same commit is the failure
mode this plan names explicitly, so anchor moves with those readers, not here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from yadgar._shared.write_exec import MemorizeContext
from yadgar.tests.backend._mint_absent import identity_mint_absent

# Three REAL ``directory_context`` values from the live corpus — prose a caller
# passed to ``memorize(context=)`` because the parameter's name invited it.
PROSE_CONTEXTS = [
    "Hard rule from Max set 2026-05-07 — must survive compaction and never be ignored",
    "debugging opsecrets nixos-quinyx",
    "code-review v5.0.0 PR status and key lessons from this session",
]


class _FakeMemoryStorage:
    """Records every dict handed to ``insert_memory`` + every staleness write."""

    def __init__(self) -> None:
        self.memories: list[dict] = []
        self.file_hashes: list[tuple] = []

    def insert_memory(self, memory: dict, **_: Any) -> int:
        self.memories.append(dict(memory))
        return len(self.memories)

    def upsert_file_hash(self, filepath, hash_value) -> None:
        self.file_hashes.append((filepath, hash_value))

    # ── no-op tails the store phases call after the insert ──────────────────
    def update_memory_fields(self, *_a: Any, **_kw: Any) -> None:
        return None

    def update_memory_scores(self, *_a: Any, **_kw: Any) -> None:
        return None

    def get_memory(self, *_a: Any, **_kw: Any) -> dict | None:
        return None

    def protect_memory(self, *_a: Any, **_kw: Any) -> None:
        return None


def _fake_embeddings() -> MagicMock:
    embeddings = MagicMock()
    embeddings.encode.return_value = b""
    embeddings.get_model_name.return_value = "fake"
    return embeddings


def _ctx(context: str | None, project_id: str | None = "a/b") -> MemorizeContext:
    ctx = MemorizeContext(
        content="c10f memory",
        context=context,
        tags=["c10f"],
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


def _run_phase_store(
    ctx: MemorizeContext,
    storage: _FakeMemoryStorage,
    curator: Any,
    file_hash: Any,
) -> MagicMock:
    """Drive ``phase_store`` with everything but storage/curator/hash stubbed.

    Returns the ``_file_hash`` spy so a test can assert what it was handed —
    the staleness arm is the ONLY surviving consumer of ``context``.
    """
    from yadgar.backend.write_exec._memorize_phases import _phase_store as store_mod

    with (
        patch.object(store_mod._lifecycle, "_get_storage", return_value=storage),
        patch.object(store_mod._lifecycle, "_get_embeddings", return_value=_fake_embeddings()),
        patch.object(store_mod._lifecycle, "_get_buffer", return_value=MagicMock()),
        patch.object(store_mod._st, "_curator", curator),
        patch.object(store_mod._st, "_consolidation", None),
        patch.object(store_mod._st, "_pool", None),
        patch.object(store_mod, "_file_hash", file_hash) as spy,
        identity_mint_absent(),
    ):
        store_mod.phase_store(ctx)
    return spy


def _real_curator(storage: _FakeMemoryStorage) -> Any:
    """A REAL ``MemoryCurator`` whose similarity search finds nothing.

    The curator arm must be exercised through the real ``curate_on_remember``
    → ``insert_new_memory`` chain: that is where the stamp actually lives, and
    a MagicMock curator would assert nothing about it.
    """
    from yadgar.backend.curation import MemoryCurator

    curator: Any = MemoryCurator.__new__(MemoryCurator)
    curator._storage = storage
    curator._embeddings = _fake_embeddings()
    curator._settings = MagicMock(CURATION_SIMILARITY_THRESHOLD=0.9)
    curator._find_similar_memories = lambda *_a, **_kw: []
    return curator


# ── 1. the stamp comes from project_id, never from context ───────────────────


class TestDirectoryContextStampComesFromProjectId:
    """Both store branches, asserted separately (the C4b lesson)."""

    def test_direct_insert_branch_stamps_project_id_not_context(self) -> None:
        storage = _FakeMemoryStorage()
        ctx = _ctx("/some/path", project_id="a/b")
        _run_phase_store(ctx, storage, curator=None, file_hash=MagicMock(return_value=None))
        assert storage.memories[0]["directory_context"] == "a/b"
        assert storage.memories[0]["directory_context"] != "/some/path"

    def test_curator_branch_stamps_project_id_not_context(self) -> None:
        """``phase_store`` prefers this arm whenever curator + embedding exist."""
        storage = _FakeMemoryStorage()
        ctx = _ctx("/some/path", project_id="a/b")
        _run_phase_store(
            ctx, storage, curator=_real_curator(storage), file_hash=MagicMock(return_value=None)
        )
        assert storage.memories[0]["directory_context"] == "a/b"
        assert storage.memories[0]["directory_context"] != "/some/path"

    def test_prose_context_never_becomes_a_scope_key(self) -> None:
        """The 18 live prose values are the reason this car exists."""
        for prose in PROSE_CONTEXTS:
            for curator_factory in (lambda _s: None, _real_curator):
                storage = _FakeMemoryStorage()
                ctx = _ctx(prose, project_id="m-agahi/yadgar")
                _run_phase_store(
                    ctx,
                    storage,
                    curator=curator_factory(storage),
                    file_hash=MagicMock(return_value=None),
                )
                assert storage.memories[0]["directory_context"] == "m-agahi/yadgar"
                assert prose not in storage.memories[0].values()


# ── 2. context is an OPTIONAL real path, used only for staleness ─────────────


class TestContextIsAnOptionalStalenessPath:
    def test_absent_context_still_stamps_scope_and_records_no_hash(self) -> None:
        storage = _FakeMemoryStorage()
        ctx = _ctx(None, project_id="a/b")
        spy = _run_phase_store(
            ctx, storage, curator=None, file_hash=MagicMock(return_value="deadbeef")
        )
        # Best-effort contract: no path → no hash attempt, no hash row.
        spy.assert_not_called()
        assert storage.file_hashes == []
        assert storage.memories[0]["file_hash"] is None
        # Scope is unaffected by the missing path.
        assert storage.memories[0]["directory_context"] == "a/b"

    def test_real_path_still_hashes_and_registers(self) -> None:
        """GREEN-unchanged: staleness detection survives the split."""
        storage = _FakeMemoryStorage()
        ctx = _ctx("/real/file.py", project_id="a/b")
        spy = _run_phase_store(
            ctx, storage, curator=None, file_hash=MagicMock(return_value="deadbeef")
        )
        spy.assert_called_once_with("/real/file.py")
        assert storage.file_hashes == [("/real/file.py", "deadbeef")]
        assert storage.memories[0]["file_hash"] == "deadbeef"

    def test_curator_branch_forwards_the_hash_too(self) -> None:
        storage = _FakeMemoryStorage()
        ctx = _ctx("/real/file.py", project_id="a/b")
        _run_phase_store(
            ctx,
            storage,
            curator=_real_curator(storage),
            file_hash=MagicMock(return_value="deadbeef"),
        )
        assert storage.memories[0]["file_hash"] == "deadbeef"
        assert storage.file_hashes == [("/real/file.py", "deadbeef")]


# ── 3. the MCP surface accepts a call with no context at all ─────────────────


class TestToolSurfaceAcceptsAbsentContext:
    def _capture(self, **kwargs: Any) -> dict:
        from yadgar.core.server.tools.memorize import memorize

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured.update(payload)
            return "c10f-job-id"

        with (
            patch(
                "yadgar.core.server.tools.memorize._get_file_queue",
                return_value=type("FQ", (), {"enqueue": staticmethod(fake_enqueue)})(),
            ),
            patch(
                "yadgar.core.server.tools.memorize.resolve_effective_project",
                side_effect=lambda project, directory, session_project, tool: (  # noqa: ARG005
                    project or "session/derived"
                ),
            ),
        ):
            result = memorize(content="c10f memory", tags=[], **kwargs)
        assert result.get("stored") is True
        return captured

    def test_memorize_without_context_succeeds_and_is_still_scoped(self) -> None:
        payload = self._capture(project="a/b")
        assert payload["project_id"] == "a/b"
        assert payload.get("context") is None

    def test_memorize_with_path_context_keeps_it_on_the_wire(self) -> None:
        payload = self._capture(context="/real/file.py", project="a/b")
        assert payload["context"] == "/real/file.py"
        assert payload["project_id"] == "a/b"


# ── 4. the drainer tolerates a payload with no context ───────────────────────


class TestDrainerToleratesAbsentContext:
    def test_memorize_apply_does_not_keyerror(self) -> None:
        from yadgar.backend.queue_drainer import apply as apply_mod

        seen: dict = {}

        def _spy(**kwargs: Any) -> dict:
            seen.update(kwargs)
            return {}

        apply_obj = apply_mod._ApplyMixin.__new__(apply_mod._ApplyMixin)
        with patch("yadgar.backend.write_exec.run_memorize_replay", _spy):
            apply_obj._apply_inner(  # type: ignore[attr-defined]
                {"op": "memorize", "payload": {"content": "x", "tags": [], "project_id": "a/b"}}
            )
        assert seen["context"] is None
        assert seen["project_id"] == "a/b"
