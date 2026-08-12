"""Tests for Car L's write-path ``project_id`` stamps.

Two LIVE write paths stamp ``project_id`` alongside ``directory_context``:

1. ``yadgar.backend.consolidation.cleanup._try_store_action_summary`` — the
   nightly action-log summarizer writes a memory per group; without a
   ``project_id`` stamp the backfill migration is a one-way trapdoor
   (post-migration writes would lack the column).

2. ``yadgar.backend.queue_drainer.apply`` — the ``wiki_add`` replay
   branch forwards the per-item ``directory_context`` to
   ``run_wiki_add_replay``; the same trapdoor applies if it doesn't
   also stamp ``project_id``.

**C4 (0047 PR#40 §5) inverted how the value ARRIVES, not whether it is
stamped.** Car L had both paths call ``derive_project_id(directory)``
inside the backend container — which has no git binary and no host project
mounts, so the call could only ever manufacture ``local/<basename>``
(ADR-0227 §1.1). Both now receive an explicit value from the caller that
can see the session, and the assertions below pin that.

**C13: the guard is re-pointed, not dropped.** These tests patched the
classifier to EXPLODE so a surviving derivation would fail loudly. C5 deleted
``derive_project_id``, so there is nothing left to patch — ``patch()`` resolves
its target at ``__enter__`` and raises ``AttributeError`` on an absent
attribute. ``identity_mint_absent()`` stands in its place at the same lines and
asserts the stronger fact: the mint cannot be reached because it does not
exist. See ``yadgar/tests/backend/_mint_absent.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from yadgar.backend.consolidation import cleanup
from yadgar.tests.backend._mint_absent import identity_mint_absent


class _FakeStorage:
    """Records every ``insert_memory`` call."""

    def __init__(self) -> None:
        self.inserts: list[dict] = []

    def insert_memory(self, memory: dict, **_: Any) -> int:
        self.inserts.append(dict(memory))
        return len(self.inserts)


class TestCleanupTryStoreActionSummaryStampsProjectId:
    """The nightly consolidation path stamps ``project_id`` on writes."""

    def _build_cleanup(self, storage: _FakeStorage) -> Any:
        # The cleanup class is a mixin; we instantiate it bare with the
        # bits it touches. The real ``_try_store_action_summary`` reads
        # self._embeddings, self._storage — provide minimal stand-ins.
        cleanup_obj = cleanup._CleanupMixin()
        cleanup_obj._storage = storage  # type: ignore[attr-defined]
        embeddings = MagicMock()
        embeddings.encode.return_value = b""
        embeddings.get_model_name.return_value = "fake"
        cleanup_obj._embeddings = embeddings  # type: ignore[attr-defined]
        return cleanup_obj

    def test_action_summary_has_project_id(self) -> None:
        storage = _FakeStorage()
        obj = self._build_cleanup(storage)
        with identity_mint_absent():
            result = obj._try_store_action_summary(
                content="summary content",
                directory="/home/max/git/yadgar",
                project_id="m-agahi/yadgar",
                group_ids=[1, 2, 3],
            )
        assert result == 1
        assert len(storage.inserts) == 1
        memory = storage.inserts[0]
        assert memory["directory_context"] == "/home/max/git/yadgar"
        assert memory["project_id"] == "m-agahi/yadgar"

    def test_action_summary_project_id_for_non_git_dir(self) -> None:
        """A non-git tree gets the caller's value too — nothing is derived here."""
        storage = _FakeStorage()
        obj = self._build_cleanup(storage)
        with identity_mint_absent():
            obj._try_store_action_summary(
                content="summary",
                directory="/home/user/projects/standalone",
                project_id="m-agahi/standalone",
                group_ids=[4],
            )
        memory = storage.inserts[0]
        # C4: the caller's value, verbatim. Never ``local/<basename>`` — that
        # is the container-derived fallback ADR-0227 deletes.
        assert memory["project_id"] == "m-agahi/standalone"
        assert not memory["project_id"].startswith("local/")


class TestApplyWikiAddReplaysStampsProjectId:
    """The drainer's wiki_add replay branch stamps ``project_id``.

    ``_ApplyMixin`` is the dispatch wrapper (a mixin on the drainer that
    exposes ``_apply_inner``). We instantiate it bare via ``__new__`` so
    no other mixin init runs. The Car L test contract is that the
    payload dict that flows to ``run_wiki_add_replay`` has
    ``project_id`` set alongside ``directory_context`` — C4: forwarded from
    the enqueue-time stamp, never recomputed here.
    """

    def test_wiki_add_branch_stamps_project_id(self) -> None:
        from yadgar.backend.queue_drainer import apply as apply_mod

        apply_obj = apply_mod._ApplyMixin.__new__(apply_mod._ApplyMixin)
        apply_obj._fill_wiki_add_defaults = MagicMock(side_effect=lambda p: p)  # type: ignore[attr-defined]

        captured: dict = {}

        def _fake_replay(p: dict) -> None:
            captured.update(p)

        with patch(
            "yadgar.backend.write_exec.run_wiki_add_replay",
            side_effect=_fake_replay,
            create=True,
        ):
            with identity_mint_absent():
                apply_obj._apply_inner(  # type: ignore[attr-defined]
                    {
                        "op": "wiki_add",
                        "payload": {
                            "directory_context": "/home/max/git/yadgar",
                            # C4: stamped at enqueue time by the core tool (C3).
                            # A payload without it is DLQ'd by
                            # ``_validate_project_id`` before it ever reaches here.
                            "project_id": "m-agahi/yadgar",
                            "slug": "x",
                            "content": "y",
                        },
                    }
                )

        assert captured["directory_context"] == "/home/max/git/yadgar"
        assert captured["project_id"] == "m-agahi/yadgar"
