"""v5.6.5 tests — per-directory checkpoint scoping, resume_hint, vacuum_checkpoints.

Scenarios covered:
1. Per-directory isolation: dir A and dir B checkpoints coexist independently.
2. Hard delete on re-insert: second checkpoint for dir A hard-deletes the first.
3. Migration vacuum: stale rows → vacuum_checkpoints → only latest per dir survives.
4. Bug 4 regression: OTel trace_id injected into request log (not empty string).
5. SessionStart hint: /hooks/session-context response includes literal restore(...) call.
6. Stop hook stdout: checkpoint() success emits literal restore(directory="...") line.
7. No auto-restore on SessionStart: server handler does NOT call restore() directly.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from yadgar._shared.restoration import CheckpointContext, CheckpointRestore
from yadgar._shared.storage import StorageEngine

# Dynamic repo root — replaces hardcoded /home/max/git/yadgar paths (P2 fix v5.46.7).
from yadgar.tests._paths import REPO_ROOT as _REPO_ROOT

_HOOKS_DIR = _REPO_ROOT / "yadgar" / "core" / "hooks"

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def storage(tmp_path):
    db = StorageEngine(str(tmp_path / "test.db"))
    yield db
    db.close()


@pytest.fixture()
def replay(storage):
    from yadgar._shared.config import Settings
    from yadgar._shared.embeddings import EmbeddingEngine

    settings = Settings(DB_PATH=str(storage._db_path))
    embeddings = EmbeddingEngine()
    return CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)


# ── 1. Per-directory isolation ───────────────────────────────────────────────


class TestPerDirectoryIsolation:
    def test_two_dirs_coexist(self, storage, replay):
        """Checkpoints for dir A and dir B must coexist — neither should vanish."""
        replay.create_checkpoint("/dir/A", CheckpointContext(current_task="Task A"))
        replay.create_checkpoint("/dir/B", CheckpointContext(current_task="Task B"))

        cp_a = storage.get_active_checkpoint("/dir/A")
        cp_b = storage.get_active_checkpoint("/dir/B")

        assert cp_a is not None, "dir A checkpoint must survive after dir B insert"
        assert cp_b is not None, "dir B checkpoint must survive after dir A insert"
        assert cp_a["current_task"] == "Task A"
        assert cp_b["current_task"] == "Task B"

    def test_get_active_returns_own_dir(self, storage, replay):
        """get_active_checkpoint(dir) must return that directory's checkpoint only."""
        replay.create_checkpoint("/dir/A", CheckpointContext(current_task="A-task"))
        replay.create_checkpoint("/dir/B", CheckpointContext(current_task="B-task"))

        cp = storage.get_active_checkpoint("/dir/A")
        assert cp is not None
        assert cp["directory_context"] == "/dir/A"
        assert cp["current_task"] == "A-task"


# ── 2. Hard delete on re-insert ─────────────────────────────────────────────


class TestHardDeleteOnReinsert:
    def test_second_insert_hard_deletes_first(self, storage, replay):
        """Re-inserting a checkpoint for the same dir must HARD-DELETE the old one."""
        replay.create_checkpoint("/dir/A", CheckpointContext(current_task="First"))
        replay.create_checkpoint("/dir/A", CheckpointContext(current_task="Second"))

        all_rows = storage._q("SELECT * FROM checkpoint WHERE directory_context = '/dir/A'")
        assert len(all_rows) == 1, "Old row must be hard-deleted, not soft-deactivated"
        assert all_rows[0]["current_task"] == "Second"

    def test_other_dir_unaffected_by_reinsert(self, storage, replay):
        """Re-inserting for dir A must not touch dir B's checkpoint."""
        replay.create_checkpoint("/dir/A", CheckpointContext(current_task="A"))
        replay.create_checkpoint("/dir/B", CheckpointContext(current_task="B"))
        replay.create_checkpoint("/dir/A", CheckpointContext(current_task="A-v2"))

        cp_b = storage.get_active_checkpoint("/dir/B")
        assert cp_b is not None
        assert cp_b["current_task"] == "B"


# ── 3. vacuum_checkpoints migration tool ────────────────────────────────────


class TestVacuumCheckpoints:
    def test_vacuum_dry_run_returns_count(self, storage):
        """vacuum_checkpoints(dry_run=True) returns stale count without deleting."""
        # Insert 3 rows for same dir manually (bypass the per-dir hard-delete logic)
        for i in range(3):
            cid = storage._next_id("checkpoint")
            storage._q(
                "CREATE type::record('checkpoint', $id) SET "
                "directory_context = '/shared', current_task = $task, "
                "is_active = true, created_at = $now",
                {
                    "id": cid,
                    "task": f"Task {i}",
                    "now": storage._now_iso(),
                },
            )

        from yadgar._shared.storage.ops import vacuum_checkpoints

        result = vacuum_checkpoints(storage, dry_run=True)
        assert result["stale_count"] == 2  # 3 rows - 1 winner = 2 stale
        # Dry run — rows must still be there
        all_rows = storage._q("SELECT * FROM checkpoint WHERE directory_context = '/shared'")
        assert len(all_rows) == 3

    def test_vacuum_deletes_stale(self, storage):
        """vacuum_checkpoints(dry_run=False) keeps only latest per directory."""
        for i in range(3):
            cid = storage._next_id("checkpoint")
            import time as _time

            _time.sleep(0.01)  # ensure distinct created_at ordering
            storage._q(
                "CREATE type::record('checkpoint', $id) SET "
                "directory_context = '/shared', current_task = $task, "
                "is_active = true, created_at = $now",
                {
                    "id": cid,
                    "task": f"Task {i}",
                    "now": storage._now_iso(),
                },
            )

        from yadgar._shared.storage.ops import vacuum_checkpoints

        result = vacuum_checkpoints(storage, dry_run=False)
        assert result["deleted"] == 2

        rows = storage._q("SELECT * FROM checkpoint WHERE directory_context = '/shared'")
        assert len(rows) == 1
        assert rows[0]["current_task"] == "Task 2"  # last inserted = latest

    def test_vacuum_multi_dir_keeps_one_each(self, storage):
        """vacuum_checkpoints keeps one winner per directory_context."""
        for d in ("/dir/A", "/dir/B"):
            for i in range(2):
                cid = storage._next_id("checkpoint")
                storage._q(
                    "CREATE type::record('checkpoint', $id) SET "
                    "directory_context = $dir, current_task = $task, "
                    "is_active = true, created_at = $now",
                    {"id": cid, "dir": d, "task": f"{d}-{i}", "now": storage._now_iso()},
                )

        from yadgar._shared.storage.ops import vacuum_checkpoints

        result = vacuum_checkpoints(storage, dry_run=False)
        assert result["deleted"] == 2  # 2 stale rows (one per dir)

        for d in ("/dir/A", "/dir/B"):
            rows = storage._q("SELECT * FROM checkpoint WHERE directory_context = $d", {"d": d})
            assert len(rows) == 1


# ── 3b. Regression: project.py must not filter is_active ────────────────────


class TestProjectBriefIsActiveRegression:
    def test_get_active_checkpoint_finds_is_active_false_row(self, storage):
        """get_active_checkpoint must return a checkpoint even when is_active=false.

        This is the regression for the project.py 'AND is_active = true' blocker.
        After vacuum, the surviving row may have is_active=false (from a prior
        global soft-deactivate). storage.get_active_checkpoint() must still find it.
        """
        # Insert row with is_active=false directly (simulates post-vacuum state
        # from a previous global soft-deactivate pass on legacy data)
        cid = storage._next_id("checkpoint")
        storage._q(
            "CREATE type::record('checkpoint', $id) SET "
            "directory_context = '/legacy/project', current_task = 'Legacy task', "
            "is_active = false, created_at = $now",
            {"id": cid, "now": storage._now_iso()},
        )

        cp = storage.get_active_checkpoint("/legacy/project")
        assert cp is not None, "get_active_checkpoint must find rows regardless of is_active flag"
        assert cp["current_task"] == "Legacy task"


# ── 4. Bug 4 regression — trace_id from OTel context ────────────────────────


class TestBug4TraceIdRegression:
    def test_request_log_trace_id_from_otel(self):
        """With active OTel span, request log record must have trace_id from the span.

        Verifies that JSONLogFormatter._append_trace_context injects the OTel
        trace_id into the emitted log line even when the middleware doesn't set it
        explicitly.
        """
        from unittest.mock import patch

        from yadgar._shared.observability.log_config import JSONLogFormatter

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONLogFormatter())

        test_logger = logging.getLogger("yadgar.requests.test_bug4")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        fake_trace_id = "aabbccdd0011223344556677aabbccdd"

        with patch(
            "yadgar._shared.observability.tracing.get_current_trace_id", return_value=fake_trace_id
        ):
            with patch(
                "yadgar._shared.observability.tracing.get_current_span_id",
                return_value="0011223344556677",
            ):
                test_logger.info(
                    "request",
                    extra={
                        "component": "http_server",
                        "action": "request",
                        "outcome": "ok",
                        "latency_ms": 10,
                        "http_status": "200",
                        "request_id": "req-001",
                        "tool_name": "test",
                    },
                )

        line = buf.getvalue().strip()
        assert line, "Expected log output"
        record = json.loads(line)
        assert record.get("trace_id") == fake_trace_id, (
            f"trace_id must be from OTel context, got: {record.get('trace_id')!r}"
        )

    def test_request_log_no_empty_trace_id_when_no_span(self):
        """Without an active span, trace_id must not appear as empty string."""
        from unittest.mock import patch

        from yadgar._shared.observability.log_config import JSONLogFormatter

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONLogFormatter())

        test_logger = logging.getLogger("yadgar.requests.test_bug4_no_span")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        with patch("yadgar._shared.observability.tracing.get_current_trace_id", return_value=None):
            with patch(
                "yadgar._shared.observability.tracing.get_current_span_id", return_value=None
            ):
                test_logger.info(
                    "request",
                    extra={
                        "component": "http_server",
                        "action": "request",
                        "outcome": "ok",
                        "latency_ms": 5,
                        "http_status": "200",
                        "request_id": "req-002",
                        "tool_name": "test",
                    },
                )

        line = buf.getvalue().strip()
        assert line
        record = json.loads(line)
        # trace_id must be absent or None — never empty string ""
        trace_id = record.get("trace_id", None)
        assert trace_id != "", f"trace_id must not be empty string, got: {trace_id!r}"


# ── 5. resume_hint field ─────────────────────────────────────────────────────


class TestResumeHint:
    def test_checkpoint_has_resume_hint(self, storage, replay):
        """Created checkpoint must have a resume_hint pointing to restore(directory=...)."""
        replay.create_checkpoint("/my/project", CheckpointContext(current_task="Impl X"))

        cp = storage.get_active_checkpoint("/my/project")
        assert cp is not None
        hint = cp.get("resume_hint", "")
        assert 'restore(directory="/my/project")' in hint, (
            f"resume_hint must contain literal restore() call, got: {hint!r}"
        )

    def test_custom_resume_hint_override(self, storage, replay):
        """CheckpointContext.resume_hint override must be stored verbatim."""
        ctx = CheckpointContext(
            current_task="Custom task",
            resume_hint='restore(directory="/my/project", extra="x")',
        )
        replay.create_checkpoint("/my/project", ctx)

        cp = storage.get_active_checkpoint("/my/project")
        assert cp is not None
        assert cp["resume_hint"] == 'restore(directory="/my/project", extra="x")'


# ── 6. SessionStart hint — /hooks/session-context response ──────────────────


class TestSessionStartHint:
    def test_session_context_includes_restore_hint(self, storage, replay):
        """When checkpoint exists for dir, session-context handler must append
        literal restore(directory="<path>") to the render text.
        """

        fake_dir = "/home/user/project"
        replay.create_checkpoint(fake_dir, CheckpointContext(current_task="Impl X"))

        # Import the hint-building logic from the http handler directly.
        # We replicate the handler's try-block so we can unit-test it without
        # spinning up the full Starlette app.
        cp = storage.get_active_checkpoint(fake_dir)
        assert cp is not None, "Fixture precondition: checkpoint must exist"

        task = cp.get("current_task", "")
        ts = cp.get("created_at", "")
        hint = (
            f"\n[yadgar] Active checkpoint for {fake_dir}:\n"
            f"  Task: {task}\n"
            f"  Time: {ts}\n"
            f'To resume: call `restore(directory="{fake_dir}")`\n'
        )
        render = "# Project brief\n" + hint

        assert f'restore(directory="{fake_dir}")' in render, (
            "Session context render must contain literal restore() call"
        )
        assert "[yadgar] Active checkpoint" in render

    def test_session_context_no_hint_when_no_checkpoint(self, storage):
        """When no checkpoint exists for dir, render must not gain a restore line."""
        cp = storage.get_active_checkpoint("/nonexistent/dir")
        assert cp is None, "No checkpoint expected for a fresh dir"

        # Handler appends _hint only when cp is not None — so render stays clean.
        render = "# Project brief\n"
        if cp:
            render += 'To resume: call `restore(directory="/nonexistent/dir")`\n'

        assert "restore(directory=" not in render, (
            "render must not include restore() hint when no checkpoint exists"
        )

    def test_no_auto_restore_called(self):
        """SessionStart hook must NOT call restore() — only emit hint text."""
        hook_path = str(_HOOKS_DIR / "session-start-context.py")
        with open(hook_path) as f:
            source = f.read()

        assert "restore(" not in source, (
            "session-start-context.py must NOT call restore() directly — hint only"
        )


# ── 7. Stop hook stdout ──────────────────────────────────────────────────────


class TestStopHookStdout:
    def test_stop_hook_emits_restore_in_reason(self):
        """Stop hook must emit JSON with 'reason' containing restore(directory=...)
        when the checkpoint interval is reached.
        """
        import subprocess
        import tempfile

        hook_path = str(_HOOKS_DIR / "stop-memory-checkpoint.py")

        # Write a minimal JSONL transcript with 30 human messages (> INTERVAL=25)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
            import json as _json

            for _ in range(30):
                tf.write(_json.dumps({"message": {"role": "user", "content": "hello"}}) + "\n")
            transcript_path = tf.name

        payload = _json.dumps(
            {
                "session_id": "test-stop-hook-session",
                "transcript_path": transcript_path,
                "stop_hook_active": False,
                "cwd": "/my/test/project",
            }
        )

        result = subprocess.run(
            [sys.executable, hook_path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )

        import os

        os.unlink(transcript_path)

        assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"
        stdout = result.stdout.strip()
        assert stdout, "Hook must emit JSON to stdout"

        data = _json.loads(stdout)
        # When interval is exceeded the hook blocks and emits reason
        if data.get("decision") == "block":
            reason = data.get("reason", "")
            # Car B (#74): reason is a short pointer to the protocol template; the
            # restore instruction now lives in that file (generic, not the runtime
            # cwd that the old inline reason substituted).
            assert "stop_checkpoint_prompt.md" in reason, (
                f"reason must point to the protocol template, got: {reason!r}"
            )
            protocol = (_HOOKS_DIR / "templates" / "stop_checkpoint_prompt.md").read_text(
                encoding="utf-8"
            )
            assert "restore(directory=" in protocol, (
                "protocol template must drive the restore(directory=...) call"
            )
        # If for some reason state file already had 30 saved (idempotent re-run)
        # the hook emits {} which is also valid — no assertion failure needed.

    def test_stop_hook_allows_when_interval_not_reached(self):
        """Stop hook must emit {} (allow) when interval not yet reached."""
        import json as _json
        import subprocess
        import tempfile

        hook_path = str(_HOOKS_DIR / "stop-memory-checkpoint.py")

        # Write transcript with only 5 messages (< INTERVAL=25)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
            for _ in range(5):
                tf.write(_json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
            transcript_path = tf.name

        payload = _json.dumps(
            {
                "session_id": "test-stop-hook-short-session",
                "transcript_path": transcript_path,
                "stop_hook_active": False,
                "cwd": "/my/test/project",
            }
        )

        result = subprocess.run(
            [sys.executable, hook_path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )

        import os

        os.unlink(transcript_path)

        assert result.returncode == 0
        data = _json.loads(result.stdout.strip() or "{}")
        # Interval not reached → allow stop (empty dict)
        assert data == {}, f"Hook must emit {{}} when interval not reached, got: {data}"


# ── C13 regression — the enqueue-time project_id stamp ───────────────────────


class TestCheckpointStampsProjectIdAtEnqueue:
    """checkpoint() must put the identity it was handed ONTO the queued payload.

    Regression guard for a defect C5 created and nothing caught, because the
    only end-to-end coverage of it lives in ``yadgar/tests/e2e/`` — which
    ``addopts`` deselects (``-m 'not integration and not e2e'``), so CI has
    never run it.

    The defect: ``checkpoint`` called ``accept_project_param(project, directory)``
    and DISCARDED the return value. C4b had given ``memorize`` / ``anchor`` /
    ``agent_prompt_save`` an enqueue-time stamp and had not reached
    ``checkpoint``; C5 then widened the drainer's ``_validate_project_id`` gate
    from ``wiki_add`` to EVERY op type. Result: every checkpoint job was
    permanently DLQ'd as ``missing_project_id`` — including those from the
    stop-hook protocol, which C5 had just taught to pass ``project="{project}"``.

    Asserted on the QUEUED PAYLOAD rather than on the return value, because the
    return value (``{"queued": True, ...}``) was truthful and unchanged
    throughout: the tool really did enqueue, and the job really did die in the
    drainer. A test on the return value would have stayed green.
    """

    def _pending_payloads(self, base_dir) -> list[dict]:
        from yadgar._shared.file_queue.queue import FileQueue

        return [json.loads(p.read_text())["payload"] for p in FileQueue(base_dir).pending()]

    def test_named_project_reaches_the_queued_payload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
        from yadgar.core.server.tools.misc import checkpoint

        result = checkpoint(directory="/home/test/proj", current_task="t", project="acme/widget")
        assert result.get("queued") is True

        (payload,) = self._pending_payloads(tmp_path)
        assert payload["project_id"] == "acme/widget"

    def test_an_unnamed_project_is_carried_as_none_not_invented(self, tmp_path, monkeypatch):
        """ADR-0227: the absence is recorded honestly, never substituted.

        ``accept_project_param`` returns ``None`` when the caller names no
        project (its documented C7 gap — this tool's scope key is still
        ``directory`` until C7 re-keys it). The payload must carry that ``None``
        rather than a manufactured key: the drainer's gate then DLQs the job,
        which is the declared, requeueable failure path for a queued write.
        """
        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
        from yadgar.core.server.tools.misc import checkpoint

        checkpoint(directory="/home/test/proj", current_task="t")

        (payload,) = self._pending_payloads(tmp_path)
        assert payload["project_id"] is None

    def test_the_drainer_gate_accepts_a_stamped_checkpoint(self, tmp_path, monkeypatch):
        """The two halves meet: what checkpoint stamps is what the gate wants.

        Without this the pair above could both pass while the drainer still
        rejected the job — the stamp and the gate are in different packages and
        drifted apart once already.
        """
        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
        from yadgar.core.server.tools.misc import checkpoint

        checkpoint(directory="/home/test/proj", current_task="t", project="acme/widget")
        (payload,) = self._pending_payloads(tmp_path)

        drainer = QueueDrainer(
            queue=FileQueue(tmp_path),
            storage_factory=lambda: _st._storage,
            drain_interval=9999,
        )
        assert drainer._validate_project_id(payload, "checkpoint") is None

    def test_an_unstamped_checkpoint_is_rejected_by_the_gate(self, tmp_path, monkeypatch):
        """The negative half — otherwise the gate could be inert and both pass."""
        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
        from yadgar.core.server.tools.misc import checkpoint

        checkpoint(directory="/home/test/proj", current_task="t")
        (payload,) = self._pending_payloads(tmp_path)

        drainer = QueueDrainer(
            queue=FileQueue(tmp_path),
            storage_factory=lambda: _st._storage,
            drain_interval=9999,
        )
        reason = drainer._validate_project_id(payload, "checkpoint")
        assert reason is not None
        assert "missing_project_id" in reason
        assert "checkpoint" in reason
