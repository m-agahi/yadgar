"""C3 (0047 PR#40 remediation) — enqueue-time ``project_id`` stamping.

The defect this car closes: the drainer computed a ``project_id`` the write
path never read. ``apply.py`` set ``p["project_id"]`` and
``run_wiki_add_replay`` dropped it on the floor (``WikiAddOptions`` had no
such field), so ``insert_wiki_page`` re-derived one from ``directory_context``
— inside a container that has no git binary and no host project mounts. Every
drainer-executed write was therefore a sessionless writer.

The C3 contract: the value is resolved ONCE, in the process that has the
session (the core tool), stamped on the queue payload, and carried unchanged
through ``run_wiki_add_replay`` → ``WikiAddOptions`` → ``WikiStore.add`` →
``insert_wiki_page``. The strongest assertion below patches the classifier to
RAISE: a row that still carries ``"a/b"`` proves the value came from the
enqueue stamp and not from a derivation that happened to agree.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.wiki.contract import WikiAddOptions
from yadgar._shared.wiki.store import WikiStore


class _FakeWikiStorage:
    """Records the page dict handed to ``insert_wiki_page``."""

    def __init__(self) -> None:
        self.pages: list[dict] = []

    def get_wiki_page_by_slug(self, slug: str) -> dict | None:  # noqa: ARG002
        return None

    def insert_wiki_page(self, page: dict, **_: Any) -> int:
        self.pages.append(dict(page))
        return len(self.pages)


def _wiki_store(storage: _FakeWikiStorage) -> WikiStore:
    embeddings = MagicMock()
    embeddings.encode.return_value = b""
    embeddings.get_model_name.return_value = "fake"
    store = WikiStore(storage, embeddings)
    store._compute_embedding = lambda title, content: None  # type: ignore[method-assign]
    store._sync_crossrefs = lambda slug, links: None  # type: ignore[method-assign]
    store._link_memories = lambda slug, memory_ids: None  # type: ignore[method-assign]
    return store


def _exploding_classifier(*_a: Any, **_kw: Any) -> tuple[str, str]:
    raise AssertionError("derive_project_id must not be called on the drained write path")


class TestWikiAddOptionsCarriesProjectId:
    """The option bundle is the seam that survives the canonical-write boundary."""

    def test_options_has_project_id_field_defaulting_to_none(self) -> None:
        assert WikiAddOptions().project_id is None

    def test_options_accepts_an_explicit_project_id(self) -> None:
        assert WikiAddOptions(project_id="a/b").project_id == "a/b"


class TestWikiStoreAddStampsCallerProjectId:
    """``WikiStore.add`` threads ``opts.project_id`` onto the inserted page."""

    def test_page_carries_the_caller_project_id(self) -> None:
        storage = _FakeWikiStorage()
        store = _wiki_store(storage)
        with patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier):
            store.add(
                "C3 page",
                "body",
                "reference",
                [],
                opts=WikiAddOptions(directory_context="/home/max/git/yadgar", project_id="a/b"),
            )
        assert storage.pages[0]["project_id"] == "a/b"

    def test_global_scoped_page_type_keeps_its_real_project_id(self) -> None:
        """§1.4 — ownership and reach are different facts.

        ``storage_scope="global"`` rewrites ``directory_context`` to
        ``"global"``. It must NOT also collapse ``project_id``: an
        agent-prompt page found in this repo is still FROM this repo.
        """
        storage = _FakeWikiStorage()
        store = _wiki_store(storage)
        with patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier):
            store.add(
                "C3 library page",
                "body",
                "reference",
                [],
                opts=WikiAddOptions(
                    directory_context="/home/max/git/yadgar",
                    page_type="agent_prompt",
                    project_id="a/b",
                ),
            )
        page = storage.pages[0]
        assert page["directory_context"] == "global"
        assert page["project_id"] == "a/b"


class TestDrainedWikiAddCarriesEnqueueValue:
    """payload → _apply_inner → run_wiki_add_replay → WikiStore.add → insert."""

    @pytest.mark.parametrize("replace_slug", [None, "some-existing-slug"])
    def test_drained_row_carries_enqueue_project_id_without_any_classifier(
        self, replace_slug: str | None
    ) -> None:
        from yadgar.backend.queue_drainer import apply as apply_mod

        storage = _FakeWikiStorage()
        store = _wiki_store(storage)
        if replace_slug is not None:
            # The replace_slug branch is a separate WikiAddOptions construction
            # site in wiki_add_impl — it is a real write path and easy to miss.
            store._storage.get_wiki_page_by_slug = lambda slug: (  # type: ignore[method-assign]
                {"id": 7, "slug": slug} if slug == replace_slug else None
            )

        apply_obj = apply_mod._ApplyMixin.__new__(apply_mod._ApplyMixin)
        apply_obj._fill_wiki_add_defaults = MagicMock(side_effect=lambda p: p)  # type: ignore[attr-defined]

        with (
            patch("yadgar._shared.runtime.state._wiki", store),
            patch("yadgar._shared.runtime.state._file_queue", None),
            patch("yadgar.backend.write_exec.wiki_add_impl._push_event", lambda event: None),
            patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier),
        ):
            apply_obj._apply_inner(  # type: ignore[attr-defined]
                {
                    "op": "wiki_add",
                    "payload": {
                        "title": "C3 drained page",
                        "content": "body",
                        "slug": "c3-drained-page",
                        "directory_context": "/home/max/git/yadgar",
                        "project_id": "a/b",
                        "replace_slug": replace_slug,
                    },
                }
            )

        assert len(storage.pages) == 1
        assert storage.pages[0]["project_id"] == "a/b"


class TestCoreWikiAddStampsUnconditionally:
    """The session-side resolve must reach the wire even without ``project=``.

    Conditional stamping (the Car M shape: stamp only when the caller passed
    ``project=``) leaves the DEFAULT path exactly as broken as before — the
    drainer would still have to derive, inside a container that cannot.
    """

    def _capture_payload(self, **kwargs: Any) -> dict:
        from yadgar.core.server.tools.wiki import wiki_add

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured.update(payload)
            return "fake-job-id"

        with (
            patch("yadgar.core.server.tools.wiki._st._wiki", object()),
            patch(
                "yadgar.core.server.tools.wiki._get_file_queue",
                return_value=type("FQ", (), {"enqueue": staticmethod(fake_enqueue)})(),
            ),
            patch("yadgar.core.server.tools.wiki._check_wiki_add_context", return_value={}),
            patch(
                "yadgar.core.server.tools.wiki.resolve_effective_project",
                side_effect=lambda project, directory, session_project: (
                    project or "session/derived"
                ),
            ),
        ):
            result = wiki_add(title="c3 test", content="body", **kwargs)
        assert result.get("stored") is True
        return captured

    def test_payload_carries_project_id_without_explicit_project(self) -> None:
        payload = self._capture_payload(directory="/home/max/git/yadgar")
        assert payload["project_id"] == "session/derived"

    def test_explicit_project_still_wins(self) -> None:
        payload = self._capture_payload(
            directory="/home/max/git/yadgar", project="quinyx/aws2slack"
        )
        assert payload["project_id"] == "quinyx/aws2slack"


class TestWriteChokepointPrefersTheCallerValue:
    """``_resolve_project_id_for_write`` — C3's mandatory-caller_value contract.

    The derivation tier and the ``"unresolved"`` tier are marked C5: DELETE.
    Until then they must be OBSERVABLE, so a surviving unconverted caller
    shows up in the logs before C5 turns it into a raise.
    """

    def test_caller_value_short_circuits_the_classifier(self) -> None:
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        with patch("yadgar.core.identity.derive_project_id", side_effect=_exploding_classifier):
            out = _resolve_project_id_for_write(
                caller_value="a/b",
                directory_context="/home/max/git/yadgar",
            )
        assert out == "a/b"

    def test_derivation_fallback_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        with (
            caplog.at_level("WARNING", logger="yadgar._shared.storage._project_id_writer"),
            patch(
                "yadgar.core.identity.derive_project_id",
                return_value=("local/yadgar", ""),
            ),
        ):
            out = _resolve_project_id_for_write(
                caller_value=None,
                directory_context="/home/max/git/yadgar",
            )
        assert out == "local/yadgar"
        assert any("derivation fallback" in r.getMessage() for r in caplog.records)

    def test_sentinel_directory_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """``global``/empty is a legitimate C3-era answer, not an unconverted caller."""
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        with caplog.at_level("WARNING", logger="yadgar._shared.storage._project_id_writer"):
            out = _resolve_project_id_for_write(caller_value=None, directory_context="global")
        assert out == "global"
        assert not caplog.records
