"""Tests for Hippocampal Replay restoration engine.

T2 Car B: mirrors yadgar/backend/restoration/checkpoint_restore.py (moved from
_shared behind the backend POST /restore forward).
"""

import sys

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.restoration.contract import CheckpointContext
from yadgar._shared.storage import StorageEngine
from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it.
_TEST_PROJECT = "m-agahi/yadgar"


@pytest.fixture
def temp_db(tmp_path):
    # surrealkv needs a directory path (not an existing file)
    yield str(tmp_path / "test.db")


@pytest.fixture
def engines(temp_db):
    settings = Settings(DB_PATH=temp_db)
    storage = StorageEngine(temp_db)
    embeddings = EmbeddingEngine()
    replay = CheckpointRestore(
        storage=storage,
        embeddings=embeddings,
        settings=settings,
    )
    yield storage, embeddings, replay
    storage.close()


class TestCheckpoints:
    def test_create_checkpoint(self, engines):
        storage, embeddings, replay = engines
        ctx = CheckpointContext(
            current_task="Implementing feature X",
            files_being_edited=["src/main.py", "src/utils.py"],
            key_decisions=["Use async for IO"],
            next_steps=["Write tests"],
        )
        result = replay.create_checkpoint("/test/project", ctx)
        assert result["status"] == "created"
        assert result["checkpoint_id"] > 0

    def test_checkpoint_supersedes(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint("/test", CheckpointContext(current_task="Task 1"))
        replay.create_checkpoint("/test", CheckpointContext(current_task="Task 2"))

        active = storage.get_active_checkpoint()
        assert active is not None
        assert active["current_task"] == "Task 2"

        # Only one active checkpoint
        rows = storage._q("SELECT * FROM checkpoint WHERE is_active = true")
        assert len(rows) == 1

    def test_epoch_tracking(self, engines):
        storage, embeddings, replay = engines
        assert storage.get_current_epoch() == 0

        replay.create_checkpoint("/test", CheckpointContext(current_task="T1"))
        assert storage.get_current_epoch() == 0

        new_epoch = storage.increment_epoch()
        assert new_epoch == 1


class TestAnchor:
    def test_anchor_memory(self, engines):
        storage, embeddings, replay = engines
        mid = replay.anchor_memory(
            content="Always use PostgreSQL not SQLite",
            context="/test/project",
            tags=["database", "decision"],
            reason="Architecture decision",
            project_id=_TEST_PROJECT,
        )
        assert mid > 0

        mem = storage.get_memory(mid)
        assert mem["is_protected"] == 1
        assert mem["importance"] == 1.0
        assert "_anchor" in mem["tags"]

    def test_anchor_heat(self, engines):
        storage, embeddings, replay = engines
        mid = replay.anchor_memory(
            content="Critical fact",
            context="/test",
            tags=[],
            project_id=_TEST_PROJECT,
        )
        mem = storage.get_memory(mid)
        assert mem["heat"] == 1.0


class TestPreCompactDrain:
    def test_drain_creates_epoch(self, engines):
        storage, embeddings, replay = engines
        result = replay.pre_compact_drain("/test")
        assert result["status"] == "drained"
        assert result["epoch"] == 1

    def test_drain_auto_checkpoint(self, engines):
        storage, embeddings, replay = engines
        result = replay.pre_compact_drain("/test")
        assert result["auto_checkpoint_created"] is True

        active = storage.get_active_checkpoint()
        assert active is not None

    def test_drain_preserves_existing_checkpoint(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint("/test", CheckpointContext(current_task="My task"))
        result = replay.pre_compact_drain("/test")

        # Should update existing, not create new auto
        assert result["auto_checkpoint_created"] is False


class TestRestore:
    def test_restore_empty(self, engines):
        storage, embeddings, replay = engines
        result = replay.restore("/test")
        assert "formatted" in result
        assert result["anchored_memories"] == 0
        assert result["hot_memories"] == 0

    def test_restore_with_checkpoint(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint(
            "/test",
            CheckpointContext(
                current_task="Building feature X",
                files_being_edited=["main.py"],
            ),
        )
        result = replay.restore("/test")
        assert result["checkpoint"] is not None
        assert "Building feature X" in result["formatted"]

    def test_restore_includes_anchored(self, engines):
        storage, embeddings, replay = engines
        replay.anchor_memory(
            content="Use React not Vue",
            context="/test",
            tags=["framework"],
            reason="Team decision",
            project_id=_TEST_PROJECT,
        )
        # C10g: the anchor STAMP and this bucket's reader moved together onto
        # the project_id, so restore must be told which project. Passing only
        # "/test" here is what C10f watched go red before it reverted the
        # half-change — keep both arguments.
        result = replay.restore("/test", project_id=_TEST_PROJECT)
        assert result["anchored_memories"] >= 1
        assert "React" in result["formatted"]

    def test_full_drain_restore_cycle(self, engines):
        storage, embeddings, replay = engines

        # Simulate a session
        replay.create_checkpoint(
            "/test",
            CheckpointContext(
                current_task="Refactoring auth module",
                key_decisions=["Switch to JWT"],
            ),
        )
        replay.anchor_memory(
            content="API key stored in .env",
            context="/test",
            tags=["security"],
            project_id=_TEST_PROJECT,
        )

        # Simulate compaction
        replay.pre_compact_drain("/test")

        # Restore — C10g: path keys the checkpoint sink, project_id keys the
        # anchor sink. This test asserts both in one call, which is why it is
        # the pin for the stamp/reader pair.
        result = replay.restore("/test", project_id=_TEST_PROJECT)
        assert result["checkpoint"] is not None
        assert result["anchored_memories"] >= 1
        assert "Refactoring auth module" in result["formatted"]
        assert "API key" in result["formatted"]


class TestInFlightEnrichment:
    """HOOKS Car 2: transcript_path → in_flight capture in the drain checkpoint,
    round-tripped through StorageEngine and surfaced in restore()."""

    _FIXTURE = str(
        __import__("pathlib").Path(__file__).parent.parent
        / "fixtures"
        / "transcript_in_flight.jsonl"
    )

    def test_drain_writes_in_flight(self, engines):
        storage, embeddings, replay = engines
        replay.pre_compact_drain("/test", transcript_path=self._FIXTURE)
        active = storage.get_active_checkpoint("/test")
        assert active is not None
        in_flight = active.get("in_flight")
        assert in_flight is not None, "in_flight must be written to the checkpoint row"
        # StorageEngine may hand a dict back as a JSON string — the getter must
        # normalize. Agents set must match the fixture's true in-flight set.
        agents = in_flight["agents"] if isinstance(in_flight, dict) else in_flight
        # tolerate JSON-string round-trip
        if isinstance(in_flight, str):
            import json as _json

            agents = _json.loads(in_flight)["agents"]
        assert set(agents) == {"bbbbbbbbbbbbbbbb2", "eeeeeeeeeeeeeeee5"}

    def test_drain_none_transcript_no_in_flight(self, engines):
        """Back-compat: transcript_path=None writes no in_flight (or None)."""
        storage, embeddings, replay = engines
        replay.pre_compact_drain("/test", transcript_path=None)
        active = storage.get_active_checkpoint("/test")
        assert active is not None
        assert not active.get("in_flight")

    def test_restore_surfaces_in_flight(self, engines):
        storage, embeddings, replay = engines
        replay.pre_compact_drain("/test", transcript_path=self._FIXTURE)
        result = replay.restore("/test")
        md = result["formatted"]
        assert "In-Flight At Compaction" in md
        assert "bbbbbbbbbbbbbbbb2" in md

    def test_restore_no_in_flight_no_header(self, engines):
        """Absent in_flight → no In-Flight header in restore markdown."""
        storage, embeddings, replay = engines
        replay.create_checkpoint("/test", CheckpointContext(current_task="plain"))
        result = replay.restore("/test")
        assert "In-Flight At Compaction" not in result["formatted"]

    def test_restore_worktrees_only_no_header(self, engines):
        """in_flight with worktrees but NO agents/shells → no block (a repo always
        has ≥1 worktree; gating on it would surface an empty block every compact)."""
        storage, embeddings, replay = engines
        storage.insert_checkpoint(
            {
                "session_id": "auto-drain",
                "directory_context": "/test",
                "current_task": "wt-only",
                "epoch": 1,
                "in_flight": {"agents": [], "bg_shells": [], "worktrees": ["/w (main)"]},
            }
        )
        result = replay.restore("/test")
        assert "In-Flight At Compaction" not in result["formatted"]


class TestPayloadProvidedInFlight:
    """Car fix-drain-inflight (v5.135): host-side capture.

    In the containerized deploy the backend cannot see the host `.claude`
    transcripts nor the git worktree tree, so parsing there yields an empty
    in_flight. The host-side drain callers now parse in_flight and pass it in the
    /admin payload; the backend persists it VERBATIM (no re-parse, no re-list of
    worktrees that would clobber the host result with []). When the payload
    carries no in_flight, the backend falls back to the in-container parse
    (embedded / dev mode where the paths ARE visible).
    """

    def test_payload_in_flight_persisted_verbatim(self, engines):
        storage, embeddings, replay = engines
        provided = {
            "agents": ["host_agent_1"],
            "bg_shells": ["host_shell_1"],
            "worktrees": ["/host/wt (feat)"],
            "note": "host-captured",
        }
        replay.pre_compact_drain("/test", transcript_path=None, in_flight=provided)
        active = storage.get_active_checkpoint("/test")
        assert active is not None
        stored = active.get("in_flight")
        assert stored is not None
        if isinstance(stored, str):
            import json as _json

            stored = _json.loads(stored)
        assert set(stored["agents"]) == {"host_agent_1"}
        assert set(stored["bg_shells"]) == {"host_shell_1"}
        # The host-provided worktrees must survive — NOT clobbered by an
        # in-container `git worktree list` that would return [].
        assert stored["worktrees"] == ["/host/wt (feat)"]

    def test_payload_in_flight_not_reparsed(self, engines):
        """Payload in_flight branch must NOT touch the transcript parser even
        when a transcript_path is ALSO present — the host result is canonical."""
        storage, embeddings, replay = engines
        from unittest.mock import patch

        provided = {"agents": ["only_host"], "bg_shells": [], "worktrees": [], "note": "x"}
        with patch("yadgar.backend.restoration.checkpoint_restore._list_worktrees") as mock_wt:
            replay.pre_compact_drain(
                "/test", transcript_path="/some/path.jsonl", in_flight=provided
            )
        mock_wt.assert_not_called()
        active = storage.get_active_checkpoint("/test")
        stored = active.get("in_flight")
        if isinstance(stored, str):
            import json as _json

            stored = _json.loads(stored)
        assert set(stored["agents"]) == {"only_host"}

    def test_empty_host_in_flight_is_authoritative(self, engines):
        """A host parse that found nothing returns a truthy empty-lists dict; it
        is authoritative (branch keyed on presence, not truthiness) and must NOT
        trigger the in-container fallback re-parse."""
        storage, embeddings, replay = engines
        from unittest.mock import patch

        empty = {"agents": [], "bg_shells": [], "worktrees": [], "note": "n"}
        with patch("yadgar.backend.restoration.checkpoint_restore._list_worktrees") as mock_wt:
            replay.pre_compact_drain("/test", transcript_path="/some/path.jsonl", in_flight=empty)
        mock_wt.assert_not_called()

    def test_absent_in_flight_falls_back_to_local_parse(self, engines):
        """No payload in_flight + a transcript_path → in-container parse fallback
        (embedded/dev mode). Preserves the pre-fix behaviour."""
        storage, embeddings, replay = engines
        fixture = str(
            __import__("pathlib").Path(__file__).parent.parent
            / "fixtures"
            / "transcript_in_flight.jsonl"
        )
        replay.pre_compact_drain("/test", transcript_path=fixture)
        active = storage.get_active_checkpoint("/test")
        stored = active.get("in_flight")
        assert stored is not None
        if isinstance(stored, str):
            import json as _json

            stored = _json.loads(stored)
        assert set(stored["agents"]) == {"bbbbbbbbbbbbbbbb2", "eeeeeeeeeeeeeeee5"}


class TestCLISubcommandsForwardOnly:
    """T2 Car B: drain/restore CLI are thin HTTP forwarders to the backend.

    The contract under test is unchanged: these subcommands are FORWARD-ONLY —
    no in-core fallback exists anymore (CheckpointRestore moved to
    ``yadgar.backend.restoration``) — so when they cannot reach the backend they
    must fail LOUD, naming ``YADGAR_EMBED_URL``. The forward behavior itself is
    unit-covered in tests/scripts/test_cli_restore_module.py /
    test_cli_drain_module.py.

    **How that contract is expressed changed in the v5.182 bug train, and the
    old expression had become non-deterministic.** These tests used to unset
    ``YADGAR_EMBED_URL`` and assert the "not set" error. Car 5 made
    ``yadgar/__main__.py`` ``setdefault`` the variable to the published host
    port so the host CLI works out of the box, which makes "no URL configured"
    UNREACHABLE — and left the outcome depending on whether a daemon happened to
    be listening on 127.0.0.1:8001. Observed both ways on this branch: a raw
    ``ConnectError`` traceback in CI (nothing listening) and a **clean exit 0
    with real restored content** on a developer box running the daemon. A test
    that passes or fails on ambient machine state is not a guard.

    So the unreachable-backend case is now pinned at a deliberately dead
    address, which is deterministic everywhere, and the assertions are STRICTER
    than before: exit non-zero, ``YADGAR_EMBED_URL`` named, the address actually
    tried echoed back, and no traceback-only failure. A third test pins the
    default itself, which nothing covered before. This is a re-expression of the
    same contract plus new coverage — not a relaxation to accommodate the change
    (the defect task #0139 exists to prevent).
    """

    #: Port 1 (tcpmux) — reserved, never bound by anything in this repo's test
    #: or dev topology, so the connect refusal is deterministic in CI and on a
    #: developer machine with a live daemon alike.
    _DEAD_URL = "http://127.0.0.1:1"

    def _run_cli(self, *args, url=_DEAD_URL):
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "YADGAR_EMBED_URL"}
        if url is not None:
            env["YADGAR_EMBED_URL"] = url
        return subprocess.run(
            [sys.executable, "-m", "yadgar", *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    def _assert_loud_and_actionable(self, result):
        assert result.returncode != 0, (
            f"forward-only CLI silently succeeded against a dead backend: {result.stdout[:400]!r}"
        )
        combined = result.stderr + result.stdout
        assert "YADGAR_EMBED_URL" in combined, (
            f"failure did not name the env var an operator must fix: {result.stderr[-600:]!r}"
        )
        assert "127.0.0.1:1" in combined, (
            "failure did not echo the address actually tried, so the operator "
            f"cannot tell WHICH backend was unreachable: {result.stderr[-600:]!r}"
        )

    def test_cli_drain_fails_loud_when_backend_unreachable(self):
        self._assert_loud_and_actionable(self._run_cli("drain", "/test/project"))

    def test_cli_restore_fails_loud_when_backend_unreachable(self):
        self._assert_loud_and_actionable(self._run_cli("restore", "/test/project"))

    def test_unset_env_is_defaulted_to_the_published_host_port(self):
        """Car 5's default is itself pinned — it had no coverage before.

        Passing ``url=None`` leaves ``YADGAR_EMBED_URL`` genuinely unset, which
        is what a bare host CLI invocation looks like. The default must land on
        the published host port; without it every forwarding subcommand died
        with "not set" on a normal container install.
        """
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "YADGAR_EMBED_URL"}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import yadgar.__main__ as m; m._default_host_embed_url()\n"
                "import os; print(os.environ.get('YADGAR_EMBED_URL', ''))",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        assert result.returncode == 0, result.stderr[-600:]
        assert result.stdout.strip() == "http://127.0.0.1:8001", (
            f"host-CLI default changed or stopped being applied: {result.stdout!r}"
        )


class TestAutoCheckpoint:
    def test_tool_call_tracking(self, engines):
        storage, embeddings, replay = engines
        assert not replay.should_auto_checkpoint()

        for _ in range(50):
            replay.record_tool_call()

        assert replay.should_auto_checkpoint()

    def test_reset_after_checkpoint(self, engines):
        storage, embeddings, replay = engines
        for _ in range(50):
            replay.record_tool_call()
        assert replay.should_auto_checkpoint()

        replay.create_checkpoint("/test", CheckpointContext(current_task="T"))
        assert not replay.should_auto_checkpoint()


class TestRunRestore:
    """run_restore — the POST /restore body (T2 Car B).

    X1 MagicMock-storage safety: every _st slot touched is saved and restored
    in a finally block so mocks never leak into other tests' engine stacks.
    """

    def test_invalidates_map_then_delegates(self):
        """Cross-process staleness fix: the SR matrix is rebuilt per restore
        (transitions are recorded core-side; the backend _dirty flag cannot see
        them), then CheckpointRestore.restore runs with the directory.

        C10g: BOTH scope values are forwarded. ``restore`` fans out to sinks
        that key on different columns, so dropping either here would silently
        unscope half of them."""
        from unittest.mock import MagicMock

        import yadgar._shared.runtime.state as _st
        from yadgar.backend.restoration import CognitiveMap, run_restore

        saved = (_st._storage, _st._embeddings, _st._cognitive_map, _st._replay)
        cmap = MagicMock(spec=CognitiveMap)
        replay = MagicMock()
        replay.restore.return_value = {"formatted": "# R", "epoch": 1}
        _st._storage = MagicMock()
        _st._embeddings = MagicMock()
        _st._cognitive_map = cmap
        _st._replay = replay
        try:
            result = run_restore("/proj", _TEST_PROJECT)
        finally:
            _st._storage, _st._embeddings, _st._cognitive_map, _st._replay = saved

        cmap.invalidate.assert_called_once_with()
        replay.restore.assert_called_once_with(directory="/proj", project_id=_TEST_PROJECT)
        assert result == {"formatted": "# R", "epoch": 1}

    def test_raises_when_engines_missing(self):
        """No storage/embeddings → ensure_restoration_engines cannot compose →
        fail loud (the /restore route must not return an empty 200)."""
        import pytest

        import yadgar._shared.runtime.state as _st
        from yadgar.backend.restoration import run_restore

        saved = (_st._storage, _st._embeddings, _st._cognitive_map, _st._replay)
        _st._storage = None
        _st._embeddings = None
        _st._cognitive_map = None
        _st._replay = None
        try:
            with pytest.raises(RuntimeError, match="CheckpointRestore not initialized"):
                run_restore("/proj")
        finally:
            _st._storage, _st._embeddings, _st._cognitive_map, _st._replay = saved
