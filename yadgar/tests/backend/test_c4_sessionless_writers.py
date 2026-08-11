"""C4 (0047 PR#40 §5) — writers with no session take their declared failure path.

ADR-0227: "Writers with no session — the nightly consolidation cycle, the queue
drainer, the CLI, migrations — have no caller to inherit from and must now fail
loud rather than defaulting."

The failure paths are deliberately DIFFERENT per writer and this module asserts
each one separately:

  * nightly consolidation / derived-memory writers → **skip + count**, never a
    sentinel, and (for the action-log summariser) the skipped rows are still
    marked processed so a poisoned batch cannot live-lock the 200-row window.
  * queue drainer → **DLQ** with ``failure_reason="missing_project_id"``,
    reusing the v5.42.0 taxonomy rather than inventing a path.
  * CLI → **non-zero exit** carrying the actionable mint message.
  * migrations → derive **nothing**.

Every writer here is additionally asserted against the mint: the write must
neither raise nor land ``"global"`` / ``"unresolved"`` / ``local/<basename>``.
Both halves matter — tier 2 of ``_resolve_project_id_for_write`` used to return
``"global"`` for a sentinel ``directory_context`` WITHOUT ever touching the
mint, so a test asserting only "did not raise" passes green while the sentinel
is still being written.

**C13: the container-side half of that guard changes shape.** C4 patched
``derive_project_id`` to raise; C5 deleted the symbol, so the patch is an
``AttributeError`` at ``__enter__``. ``identity_mint_absent()`` replaces it at
the same lines with the stronger claim — the mint does not exist to be reached
(``_mint_absent.py``). The HOST-side mint (``core/hooks/_identity_mint``)
survives by design and is still patched directly, below.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.storage._project_id_writer import resolve_project_id_from_rows
from yadgar.backend.consolidation import cleanup
from yadgar.tests.backend._mint_absent import identity_mint_absent

#: Values no writer may ever stamp as a project_id after C4 (§1.4 / ADR-0227).
FORBIDDEN = ("global", "unresolved")


def assert_not_a_sentinel(value: object) -> None:
    """Fail when *value* is one of the manufactured identities ADR-0227 deletes."""
    assert value not in FORBIDDEN, f"writer stamped the forbidden sentinel {value!r}"
    assert not (isinstance(value, str) and value.startswith("local/")), (
        f"writer stamped the container-derived fallback {value!r}"
    )


def _exploding_mint(*_a: Any, **_kw: Any) -> tuple[str, str]:
    """Stand-in for the classifier that C5 deletes — reaching it is the bug."""
    raise AssertionError("a C4 writer reached the identity mint")


# ── the shared row-sourced resolver ───────────────────────────────────────────


class TestResolveProjectIdFromRows:
    """One distinct identifying project_id → it. Zero or two or more → None."""

    def test_single_project(self) -> None:
        rows = [{"project_id": "m-agahi/yadgar"}, {"project_id": "m-agahi/yadgar"}]
        assert resolve_project_id_from_rows(rows) == "m-agahi/yadgar"

    def test_two_projects_is_unnameable(self) -> None:
        rows = [{"project_id": "m-agahi/yadgar"}, {"project_id": "m-agahi/other"}]
        assert resolve_project_id_from_rows(rows) is None

    def test_no_project_is_unnameable(self) -> None:
        assert resolve_project_id_from_rows([{"project_id": None}, {}]) is None

    def test_sentinels_do_not_name_a_project(self) -> None:
        rows = [{"project_id": "global"}, {"project_id": "unresolved"}, {"project_id": "system"}]
        assert resolve_project_id_from_rows(rows) is None

    def test_sentinels_are_ignored_alongside_a_real_project(self) -> None:
        rows = [{"project_id": "global"}, {"project_id": "m-agahi/yadgar"}]
        assert resolve_project_id_from_rows(rows) == "m-agahi/yadgar"

    def test_empty_input(self) -> None:
        assert resolve_project_id_from_rows([]) is None


# ── cleanup.py — the action-log summariser ────────────────────────────────────


class _FakeStorage:
    """Records ``insert_memory`` payloads and ``mark_actions_processed`` ids."""

    def __init__(self) -> None:
        self.inserts: list[dict] = []
        self.marked: list[int] = []

    def insert_memory(self, memory: dict, **_: Any) -> int:
        self.inserts.append(dict(memory))
        return len(self.inserts)

    def mark_actions_processed(self, ids: list) -> None:
        self.marked.extend(ids)

    def prune_processed_action_log(self, older_than_days: int = 7) -> int:
        return 0


def _build_cleanup(storage: _FakeStorage, rows: list[dict]) -> Any:
    obj = cleanup._CleanupMixin()
    storage.get_unprocessed_actions = lambda limit=200: list(rows)  # type: ignore[attr-defined]
    obj._storage = storage  # type: ignore[attr-defined]
    embeddings = MagicMock()
    embeddings.encode.return_value = b""
    embeddings.get_model_name.return_value = "fake"
    obj._embeddings = embeddings  # type: ignore[attr-defined]
    settings = MagicMock()
    settings.ACTION_LOG_RETENTION_DAYS = 7
    obj._settings = settings  # type: ignore[attr-defined]
    return obj


def _action_row(i: int, project_id: str | None, directory: str = "/home/max/git/yadgar") -> dict:
    row = {
        "id": i,
        "tool_name": "Bash",
        "tool_input_summary": f"cmd {i}",
        "directory": directory,
        "timestamp": "2026-08-10T12:00:00+00:00",
    }
    if project_id is not None:
        row["project_id"] = project_id
    return row


class TestGroupRowsByWindowGroupsByProjectId:
    """Grouping keys on ``project_id``, not on the raw ``directory`` string."""

    def test_one_project_two_checkouts_is_one_group(self) -> None:
        rows = [
            _action_row(1, "m-agahi/yadgar", "/home/max/git/yadgar"),
            _action_row(2, "m-agahi/yadgar", "/home/max/git/yadgar/.claude/worktrees/x"),
            _action_row(3, "m-agahi/yadgar", "/home/max/git/yadgar"),
        ]
        groups, skipped = cleanup._group_rows_by_window(rows)
        assert skipped == []
        assert len(groups) == 1, f"one project split across {len(groups)} buckets"
        assert next(iter(groups)).startswith("m-agahi/yadgar|")

    def test_two_projects_stay_separate(self) -> None:
        rows = [_action_row(1, "m-agahi/yadgar"), _action_row(2, "m-agahi/other")]
        groups, skipped = cleanup._group_rows_by_window(rows)
        assert skipped == []
        assert len(groups) == 2

    def test_rows_without_project_id_are_skipped_not_bucketed(self) -> None:
        rows = [_action_row(1, None), _action_row(2, "m-agahi/yadgar")]
        groups, skipped = cleanup._group_rows_by_window(rows)
        assert skipped == [1]
        assert len(groups) == 1
        for key in groups:
            assert "unknown" not in key, "the phantom 'unknown' bucket is back"

    def test_sentinel_project_id_is_skipped(self) -> None:
        rows = [_action_row(1, "global"), _action_row(2, "unresolved")]
        groups, skipped = cleanup._group_rows_by_window(rows)
        assert groups == {}
        assert sorted(skipped) == [1, 2]


class TestProcessActionLogSkipAndCount:
    """Skip-and-count is loud in metrics and NON-FATAL to the cycle."""

    def test_skipped_rows_are_still_marked_processed(self) -> None:
        """The live-lock guard: unmarked rows keep re-filling the 200-row window."""
        storage = _FakeStorage()
        rows = [_action_row(i, None) for i in (1, 2, 3)]
        obj = _build_cleanup(storage, rows)
        stats = obj._process_action_log()
        assert stats["actions_skipped_no_project"] == 3
        assert sorted(storage.marked) == [1, 2, 3], "skipped rows were never marked processed"
        assert storage.inserts == []

    def test_a_poisoned_batch_does_not_kill_the_cycle(self) -> None:
        storage = _FakeStorage()
        rows = [_action_row(1, None), _action_row(2, None)] + [
            _action_row(i, "m-agahi/yadgar") for i in (3, 4, 5)
        ]
        obj = _build_cleanup(storage, rows)
        stats = obj._process_action_log()
        assert stats["actions_skipped_no_project"] == 2
        assert stats["memories_created"] == 1
        assert sorted(storage.marked) == [1, 2, 3, 4, 5]
        assert storage.inserts[0]["project_id"] == "m-agahi/yadgar"

    def test_the_summariser_never_reaches_the_mint(self) -> None:
        storage = _FakeStorage()
        rows = [_action_row(i, "m-agahi/yadgar") for i in (1, 2, 3)]
        obj = _build_cleanup(storage, rows)
        with identity_mint_absent():
            obj._process_action_log()
        assert_not_a_sentinel(storage.inserts[0]["project_id"])


class TestTryStoreActionSummaryTakesAnExplicitValue:
    """``_try_store_action_summary`` is handed a project_id; it derives nothing."""

    def test_explicit_project_id_is_stamped(self) -> None:
        storage = _FakeStorage()
        obj = _build_cleanup(storage, [])
        with identity_mint_absent():
            assert (
                obj._try_store_action_summary(
                    content="summary content",
                    directory="/home/max/git/yadgar",
                    project_id="m-agahi/yadgar",
                    group_ids=[1, 2, 3],
                )
                == 1
            )
        memory = storage.inserts[0]
        assert memory["project_id"] == "m-agahi/yadgar"
        assert memory["directory_context"] == "/home/max/git/yadgar"


# ── the action_log producer half ──────────────────────────────────────────────


class TestInsertActionLogPersistsProjectId:
    """The row carries the enqueue-time project_id so cleanup can group on it."""

    def test_project_id_is_written(self) -> None:
        from yadgar._shared.storage.queue import _QueueMixin

        captured: dict = {}

        class _Q(_QueueMixin):
            def _next_id(self, _table: str) -> int:
                return 7

            def _q(self, surql: str, params: dict | None = None):
                captured["sql"] = surql
                captured["params"] = params or {}
                return []

        _Q().insert_action_log(
            tool_name="Bash",
            tool_input_summary="s",
            directory="/home/max/git/yadgar",
            session_id="sid",
            timestamp="2026-08-10T12:00:00+00:00",
            project_id="m-agahi/yadgar",
        )
        assert "project_id" in captured["sql"]
        assert captured["params"]["project_id"] == "m-agahi/yadgar"

    def test_replay_forwards_the_payload_stamp(self) -> None:
        from yadgar.backend.write_exec import action_log_impl

        storage = MagicMock()
        with patch.object(action_log_impl, "_get_storage", return_value=storage):
            action_log_impl.run_action_log_replay(
                {
                    "tool_name": "Bash",
                    "summary": "s",
                    "directory": "/home/max/git/yadgar",
                    "session_id": "sid",
                    "timestamp": "2026-08-10T12:00:00+00:00",
                    "project_id": "m-agahi/yadgar",
                }
            )
        assert storage.insert_action_log.call_args.kwargs["project_id"] == "m-agahi/yadgar"


# ── the two dominant_directory callers (+ the third this car found) ───────────


class TestClsPromotionSkipsUnnameableClusters:
    """``promotion.py`` stamps the cluster's project_id or skips it — never 'global'."""

    def _build(self, cluster: list[dict]) -> tuple[Any, _FakeStorage]:
        from yadgar.backend.cls_store.promotion import _PromotionMixin

        obj = _PromotionMixin()
        storage = _FakeStorage()
        storage.search_vectors = lambda *_a, **_kw: []  # type: ignore[attr-defined]
        storage.update_memory_fields = lambda *_a, **_kw: None  # type: ignore[attr-defined]
        storage.get_entity_by_name = lambda _n: {"id": 1}  # type: ignore[attr-defined]
        storage.get_relationships_among_entities = lambda _e: []  # type: ignore[attr-defined]
        storage.insert_relationship = lambda _r: 1  # type: ignore[attr-defined]
        obj._storage = storage  # type: ignore[attr-defined]
        embeddings = MagicMock()
        embeddings.encode.return_value = None
        embeddings.get_model_name.return_value = "fake"
        obj._embeddings = embeddings  # type: ignore[attr-defined]
        obj._settings = MagicMock(CURATION_SIMILARITY_THRESHOLD=0.9)  # type: ignore[attr-defined]
        obj.abstract_to_schema = lambda _m: "the login flow retries on 401"  # type: ignore[attr-defined]
        return obj, storage

    def test_single_project_cluster_is_promoted_with_that_project_id(self) -> None:
        cluster = [
            {"id": 1, "directory_context": "/home/max/git/yadgar", "project_id": "m-agahi/yadgar"},
            {"id": 2, "directory_context": "/home/max/git/yadgar", "project_id": "m-agahi/yadgar"},
        ]
        obj, storage = self._build(cluster)
        with identity_mint_absent():
            assert obj._promote_pattern({"memories": cluster}) is True
        assert storage.inserts[0]["project_id"] == "m-agahi/yadgar"
        assert_not_a_sentinel(storage.inserts[0]["project_id"])

    def test_cross_project_cluster_is_skipped_not_collapsed(self) -> None:
        cluster = [
            {"id": 1, "directory_context": "/a", "project_id": "m-agahi/yadgar"},
            {"id": 2, "directory_context": "/b", "project_id": "m-agahi/other"},
        ]
        obj, storage = self._build(cluster)
        with identity_mint_absent():
            assert obj._promote_pattern({"memories": cluster}) is False
        assert storage.inserts == [], "a cross-project cluster was collapsed to a sentinel"


class TestMemifyDeriveSkipsUnnameablePairs:
    """``strengthen.py`` — the highest-volume sentinel producer in the corpus (D4)."""

    def _collect(self, source_mems: list[dict]) -> tuple[list[dict], dict]:
        from yadgar.backend.curation.strengthen import _collect_derive_inserts

        storage = MagicMock()
        storage.get_relationships_by_types.return_value = [
            {"id": 1, "source_entity_id": 10, "target_entity_id": 11, "weight": 42.0}
        ]
        storage._next_id.return_value = 99
        embeddings = MagicMock()
        embeddings.encode.return_value = b""
        stats = {"derived": 0}
        entity_map = {10: {"id": 10, "name": "alpha"}, 11: {"id": 11, "name": "beta"}}
        with identity_mint_absent():
            out = _collect_derive_inserts(
                storage, embeddings, stats, entity_map, set(), source_mems
            )
        return out, stats

    def test_single_project_pair_carries_that_project_id(self) -> None:
        source_mems = [
            {"content": "alpha and beta", "directory_context": "/x", "project_id": "m-agahi/y"}
        ]
        out, stats = self._collect(source_mems)
        assert len(out) == 1
        assert out[0]["project_id"] == "m-agahi/y"
        assert_not_a_sentinel(out[0]["project_id"])
        assert stats["derived"] == 1

    def test_cross_project_pair_is_skipped_and_not_counted_as_derived(self) -> None:
        source_mems = [
            {"content": "alpha here", "directory_context": "/x", "project_id": "m-agahi/y"},
            {"content": "beta there", "directory_context": "/z", "project_id": "m-agahi/other"},
        ]
        out, stats = self._collect(source_mems)
        assert out == []
        assert stats["derived"] == 0, "a skipped pair still incremented stats['derived']"


class TestDreamInsightSkipsUnnameablePairs:
    """The third sentinel producer — unlisted in the plan's C4 table, same class."""

    def _build(self) -> tuple[Any, _FakeStorage]:
        from yadgar.backend.sleep_compute.dream import _DreamMixin

        obj = _DreamMixin()
        storage = _FakeStorage()
        storage.update_memory_scores = lambda *_a, **_kw: None  # type: ignore[attr-defined]
        obj._storage = storage  # type: ignore[attr-defined]
        embeddings = MagicMock()
        embeddings.encode.return_value = b""
        embeddings.get_model_name.return_value = "fake"
        obj._embeddings = embeddings  # type: ignore[attr-defined]
        return obj, storage

    def test_same_project_pair_is_stamped(self) -> None:
        obj, storage = self._build()
        a = {"id": 1, "content": "a" * 20, "project_id": "m-agahi/yadgar"}
        b = {"id": 2, "content": "b" * 20, "project_id": "m-agahi/yadgar"}
        with identity_mint_absent():
            obj._create_dream_insight(a, b)
        assert storage.inserts[0]["project_id"] == "m-agahi/yadgar"
        assert_not_a_sentinel(storage.inserts[0]["project_id"])

    def test_cross_project_pair_writes_nothing(self) -> None:
        obj, storage = self._build()
        a = {"id": 1, "content": "a" * 20, "project_id": "m-agahi/yadgar"}
        b = {"id": 2, "content": "b" * 20, "project_id": "m-agahi/other"}
        with identity_mint_absent():
            obj._create_dream_insight(a, b)
        assert storage.inserts == []


# ── queue drainer — DLQ, not a default ────────────────────────────────────────


def _wiki_record(**payload_over: Any) -> dict:
    payload = {
        "wiki_schema_version": 2,
        "slug": "s",
        "title": "t",
        "content": "some content that is not degenerate at all",
        "category": "reference",
        "directory_context": "/home/max/git/yadgar",
        "project_id": "m-agahi/yadgar",
    }
    payload.update(payload_over)
    return {"op": "wiki_add", "payload": payload}


class TestDrainerRejectsMissingProjectId:
    """Missing enqueue-time stamp → DLQ with the taxonomy reason, never a default."""

    def test_present_stamp_passes(self) -> None:
        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        assert _DLQMixin()._validate_wiki_add(_wiki_record()) is None

    def test_absent_stamp_is_rejected(self) -> None:
        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        record = _wiki_record()
        record["payload"].pop("project_id")
        assert (_DLQMixin()._validate_wiki_add(record) or "").startswith("missing_project_id")

    @pytest.mark.parametrize("sentinel", ["unresolved", "", "   ", None])
    def test_sentinel_stamp_is_rejected(self, sentinel: str | None) -> None:
        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        record = _wiki_record(project_id=sentinel)
        assert (_DLQMixin()._validate_wiki_add(record) or "").startswith("missing_project_id")

    def test_global_is_rejected_now_that_c5_landed(self) -> None:
        """C5 flipped this one: ``"global"`` is a forbidden sentinel, not a scope.

        Was ``test_global_is_still_accepted_until_c5``, asserting ``is None``.
        C4 left the value accepted DELIBERATELY and said so: while
        ``resolve_effective_project`` still answered every unresolvable tree
        with ``GLOBAL_FALLBACK``, rejecting it here would have DLQ'd every
        legitimate global-scoped write a full car early. C5 deleted the tier
        that produced it and added ``"global"`` to ``_SENTINEL_PROJECT_IDS`` in
        the same edit — the two changes are a matched pair, and this assertion
        is where doing one without the other would show up.
        """
        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        reason = _DLQMixin()._validate_wiki_add(_wiki_record(project_id="global"))
        assert (reason or "").startswith("missing_project_id")

    def test_adr_body_page_carries_the_ledger_project_id(self) -> None:
        """The ADR branch pre-stamps, so the two engines agree on one key.

        ``_write_adr_body_page`` already holds the ``project_id`` the caller
        resolved for the MariaDB ``adr`` row. Stamping it on the body payload
        means the SurrealDB page and the SQL row name the same project, and
        ``_wiki_write_canonical``'s ``if not payload.get("project_id")`` guard
        leaves it alone rather than re-deriving a second answer.
        """
        from yadgar.core.server.tools import adr as adr_tools

        captured: dict = {}

        with patch.object(
            adr_tools,
            "_wiki_write_canonical",
            side_effect=lambda p, wait=False: captured.update(p) or {"stored": True},
        ):
            adr_tools._write_adr_body_page(
                resolved="/home/max/git/yadgar",
                project_id="m-agahi/yadgar",
                adr_id="ADR-0042",
                adr_id_int=42,
                fields={
                    "title": "t",
                    "status": "accepted",
                    "date": "2026-08-10",
                    "context": "c",
                    "decision": "d",
                    "rationale": "r",
                    "alternatives": "a",
                    "consequences": "q",
                    "revisit_trigger": "rt",
                    "supersedes": "none",
                },
            )
        assert captured["project_id"] == "m-agahi/yadgar"
        assert_not_a_sentinel(captured["project_id"])

    def _canonical_payload(self, **over: Any) -> dict:
        payload = {
            "page_type": "task_list",
            "slug": "s",
            "title": "t",
            "content": "c",
            "category": "reference",
            "wiki_schema_version": 2,
            "directory_context": "/home/max/git/yadgar",
        }
        payload.update(over)
        return payload

    def test_the_canonical_writers_stamp_so_internal_is_not_a_hole(self) -> None:
        """``_internal=True`` does not exempt project_id — the writers stamp instead.

        **C13: where the stamp COMES FROM moved, so the double moved with it.**
        C4 let ``_wiki_write_canonical`` resolve the value itself, and this test
        patched ``resolve_effective_project`` to supply it. C5 deleted that
        resolve: with no derivation tier there is nothing left to resolve from,
        so the sanctioned caller must arrive with the stamp already on the
        payload. The property under test — an ``_internal`` payload still
        satisfies the drainer's gate — is unchanged and still asserted; only the
        source of the value differs.
        """
        from yadgar.core.server.tools import wiki as wiki_tools

        enqueued: dict = {}

        class _FQ:
            def enqueue(self, op: str, payload: dict) -> str:
                enqueued.update(payload)
                return "id"

        with patch.object(wiki_tools, "_get_file_queue", return_value=_FQ()):
            wiki_tools._wiki_write_canonical(
                self._canonical_payload(project_id="m-agahi/yadgar"),
                wait=False,
            )
        assert enqueued["_internal"] is True
        assert enqueued["project_id"] == "m-agahi/yadgar"

        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        assert _DLQMixin()._validate_wiki_add({"op": "wiki_add", "payload": enqueued}) is None

    def test_the_canonical_writer_refuses_an_unstamped_payload(self) -> None:
        """The other half: C5 made the missing stamp a raise, not a resolve.

        Without this, the test above would pass against a
        ``_wiki_write_canonical`` that quietly re-introduced a fallback — the
        exact regression ADR-0227 exists to prevent. Nothing is enqueued.
        """
        from yadgar.core.server.tools import wiki as wiki_tools

        enqueued: list[dict] = []

        class _FQ:
            def enqueue(self, op: str, payload: dict) -> str:  # noqa: ARG002
                enqueued.append(payload)
                return "id"

        with (
            patch.object(wiki_tools, "_get_file_queue", return_value=_FQ()),
            pytest.raises(UnresolvedProjectError) as exc,
        ):
            wiki_tools._wiki_write_canonical(self._canonical_payload(), wait=False)

        assert exc.value.payload["error"] == "unresolved_project"
        assert enqueued == []

    def test_reason_maps_to_the_taxonomy_entry(self) -> None:
        from yadgar.backend.queue_drainer import QueueDrainer

        drainer = QueueDrainer.__new__(QueueDrainer)
        reason, meta = drainer._build_rejection_reason_and_meta(
            "missing_project_id: wiki_add payload lacks project_id.",
            _wiki_record(),
            "wiki_add",
        )
        assert reason == "missing_project_id"
        assert meta is not None
        assert meta["field"] == "project_id"
        assert "project=" in meta["hint"]


class TestApplyDoesNotMint:
    """The drainer applies the enqueue-time stamp; it does not compute one."""

    def test_wiki_add_branch_forwards_the_stamp_without_deriving(self) -> None:
        from yadgar.backend.queue_drainer import apply as apply_mod

        apply_obj = apply_mod._ApplyMixin.__new__(apply_mod._ApplyMixin)
        apply_obj._fill_wiki_add_defaults = MagicMock(side_effect=lambda p: p)  # type: ignore[attr-defined]
        captured: dict = {}

        with (
            patch(
                "yadgar.backend.write_exec.run_wiki_add_replay",
                side_effect=lambda p: captured.update(p),
                create=True,
            ),
            identity_mint_absent(),
        ):
            apply_obj._apply_inner(  # type: ignore[attr-defined]
                {
                    "op": "wiki_add",
                    "payload": {
                        "directory_context": "/home/max/git/yadgar",
                        "project_id": "m-agahi/yadgar",
                        "slug": "x",
                        "content": "y",
                    },
                }
            )
        assert captured["project_id"] == "m-agahi/yadgar"
        assert_not_a_sentinel(captured["project_id"])


# ── the CLI — host-side, so it MAY mint; failure is a non-zero exit ───────────


class TestCliProjectResolution:
    """``--project`` wins; absent → the C2 mint; mint failure → non-zero exit."""

    def test_explicit_flag_wins_and_skips_the_mint(self) -> None:
        from yadgar.core.cli._shared import resolve_cli_project

        with patch("yadgar.core.hooks._identity_mint.mint_project_id", side_effect=_exploding_mint):
            assert resolve_cli_project("m-agahi/yadgar", "/tmp") == "m-agahi/yadgar"

    def test_absent_flag_falls_back_to_the_mint(self) -> None:
        from yadgar.core.cli._shared import resolve_cli_project

        with patch(
            "yadgar.core.hooks._identity_mint.mint_project_id", return_value="m-agahi/yadgar"
        ):
            assert resolve_cli_project(None, "/tmp") == "m-agahi/yadgar"

    def test_mint_failure_exits_non_zero_with_the_actionable_message(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        from yadgar.core.cli._shared import resolve_cli_project
        from yadgar.core.hooks._identity_mint import UnresolvableProjectError

        with patch(
            "yadgar.core.hooks._identity_mint.mint_project_id",
            side_effect=UnresolvableProjectError("no remote"),
        ):
            with pytest.raises(SystemExit) as exc:
                resolve_cli_project(None, "/tmp/nowhere")
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert ".yadgar/project-id" in err
        for sentinel in FORBIDDEN:
            assert f"={sentinel}" not in err

    def test_capture_stamps_the_resolved_project_on_the_queue_payload(self) -> None:
        from types import SimpleNamespace

        from yadgar.core.cli.capture import cmd_capture

        enqueued: dict = {}

        class _FQ:
            def enqueue(self, op: str, payload: dict) -> str:
                enqueued["op"] = op
                enqueued["payload"] = payload
                return "id"

        with patch("yadgar._shared.file_queue.queue.FileQueue", return_value=_FQ()):
            cmd_capture(
                SimpleNamespace(
                    tool_name="Bash",
                    summary="s",
                    directory="/home/max/git/yadgar",
                    session="sid",
                    project="m-agahi/yadgar",
                    db_path=None,
                )
            )
        assert enqueued["payload"]["project_id"] == "m-agahi/yadgar"


# ── migrations — derive NOTHING ───────────────────────────────────────────────


class TestMigration031DerivesNothing:
    """031 keeps DEFINE FIELD / DEFINE INDEX only; the backfill is C6's op."""

    def test_no_backfill_helpers_survive(self) -> None:
        from yadgar._shared.storage import migrations

        for gone in ("_m031_backfill_table", "_m031_apply_row", "_m031_apply_unresolved"):
            assert not hasattr(migrations, gone), f"{gone} still mints inside the container"


# ── nightly sweep — NULL project_id is counted, not bucketed ──────────────────


class TestNightlySweepCountsUnprojectedRows:
    def test_rows_without_project_id_are_counted(self) -> None:
        from yadgar.backend.admin_exec import nightly_sweep

        rows = [
            {"id": 1, "project_id": "m-agahi/yadgar"},
            {"id": 2, "project_id": None},
            {"id": 3},
        ]
        projects, skipped = nightly_sweep._dedupe_projects(rows)
        assert projects == ["m-agahi/yadgar"]
        assert skipped == 2
