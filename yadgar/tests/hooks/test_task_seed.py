"""TDD — Car C part 1: the mechanical harness task-list seeder.

The seeder writes the Claude Code harness's own on-disk task files
(``<tasks_root>/<session_id>/<N>.json``) at SessionStart so the ledger's open
tasks are present with ZERO model tokens. It replaces a nudge that ordered the
model to hand-mirror ~81 tasks via ``TaskCreate`` (measured: 16,705 output
tokens).

SAFETY: every test here pins ``CLAUDE_CONFIG_DIR`` at a ``tmp_path``. The real
``~/.claude/tasks`` is the LIVE task store of running sessions — a stray write
corrupts them. No test in this file may resolve the real home.

Format facts pinned below were MEASURED against the live store on 2026-08-16
(46 session dirs, 400 task files, read-only survey):

* ``.lock`` is a 0-byte regular file present in **every** dir, including 10
  dirs holding zero task files. It is a dir marker, NOT a busy signal — a
  seeder that skips on its presence never fires at all.
* Key set is ``{id, subject, description, status, blocks, blockedBy}`` with
  ``activeForm`` present in 243/400 files and absent from 156, and ``metadata``
  in 1. Sparse-tolerant.
* ``id`` is a JSON **string** in 50/50 sampled files, matching the file stem.
* ``status`` is only ever ``pending`` / ``in_progress`` / ``completed``.
* ``.highwatermark`` holds a bare integer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tasks_root(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the seeder at a throwaway config dir. NEVER the real ~/.claude."""
    cfg = Path(str(tmp_path)) / "claude-home"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("YADGAR_HARNESS_SEED_DISABLED", raising=False)
    return cfg / "tasks"


_SID = "11111111-2222-3333-4444-555555555555"

_ROWS = [
    {"id": 41, "title": "Fix the ROW constructor bug", "status": "pending"},
    {"id": 55, "title": "Seed the harness task list", "status": "in_progress"},
    {"id": 92, "title": "Trim the nudge", "status": "pending"},
]


def _seed(session_id=_SID, rows=None, **kw):
    from yadgar.core.hooks.task_seed import seed_harness_task_list

    return seed_harness_task_list(session_id, _ROWS if rows is None else rows, **kw)


# ── happy path ────────────────────────────────────────────────────────────────


def test_seeds_a_fresh_dir(tasks_root):
    """The harness creates the dir lazily, so the seeder must mkdir -p."""
    result = _seed()

    assert result["ok"] is True, result
    assert result["seeded"] == 3
    written = sorted(p.name for p in (tasks_root / _SID).glob("*.json"))
    assert written == ["41.json", "55.json", "92.json"]


def test_written_record_matches_the_measured_schema(tasks_root):
    _seed()
    rec = json.loads((tasks_root / _SID / "55.json").read_text())

    # id is a STRING matching the file stem — an int here is a different record.
    assert rec["id"] == "55"
    assert rec["status"] == "in_progress"
    assert rec["blocks"] == []
    assert rec["blockedBy"] == []
    assert "description" in rec, "description is present in all 400 sampled live files"


def test_subject_carries_the_exact_ledger_id(tasks_root):
    """`{id}: {title}` — the ledger SQL id, never a re-sequenced one."""
    _seed()
    rec = json.loads((tasks_root / _SID / "41.json").read_text())
    assert rec["subject"] == "41: Fix the ROW constructor bug"


def test_gaps_are_fine(tasks_root):
    """Ledger ids are sparse (41/55/92). Allocation never reuses gaps, so a
    non-contiguous dir is normal — a majority of live dirs are."""
    _seed()
    assert (tasks_root / _SID / "92.json").exists()
    assert not (tasks_root / _SID / "42.json").exists()


def test_highwatermark_is_the_max_seeded_id(tasks_root):
    """Guarantees the next TaskCreate cannot reuse a seeded id even if every
    seeded file is deleted (allocation is max(file ids, .highwatermark) + 1)."""
    _seed()
    assert (tasks_root / _SID / ".highwatermark").read_text().strip() == "92"


def test_zero_byte_lock_does_not_block_seeding(tasks_root):
    """REGRESSION: `.lock` exists in every live dir, including empty ones. A
    seeder that treats it as a busy signal never fires."""
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / ".lock").touch()

    result = _seed()

    assert result["ok"] is True, result
    assert result["seeded"] == 3


# ── idempotency ───────────────────────────────────────────────────────────────


def test_rerun_writes_nothing_new(tasks_root):
    _seed()
    second = _seed()

    assert second["ok"] is True
    assert second["seeded"] == 0
    assert second["already_present"] == 3


def test_does_not_clobber_a_task_the_session_edited(tasks_root):
    """The session may have completed or renamed a seeded task. Its file wins."""
    d = tasks_root / _SID
    d.mkdir(parents=True)
    edited = {
        "id": "41",
        "subject": "41: Fix the ROW constructor bug",
        "description": "notes the instance added",
        "status": "completed",
        "blocks": [],
        "blockedBy": [],
    }
    (d / "41.json").write_text(json.dumps(edited))

    result = _seed()

    assert result["seeded"] == 2
    assert json.loads((d / "41.json").read_text()) == edited


def test_seeds_alongside_tasks_the_harness_created(tasks_root):
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / ".lock").touch()
    (d / "1.json").write_text(
        json.dumps(
            {
                "id": "1",
                "subject": "harness task",
                "description": "",
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
                "activeForm": "doing harness task",
            }
        )
    )

    result = _seed()

    assert result["ok"] is True
    assert result["seeded"] == 3
    assert (d / "1.json").exists()


# ── guards: a wrong write into an undocumented store is worse than no write ──


def test_guard_trips_on_an_unknown_status_in_an_existing_file(tasks_root):
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / "7.json").write_text(
        json.dumps({"id": "7", "subject": "x", "status": "deferred", "blocks": []})
    )

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "unexpected_schema"
    assert not (d / "41.json").exists(), "a tripped guard must write nothing"


def test_guard_trips_on_an_id_that_is_not_a_string(tasks_root):
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / "7.json").write_text(json.dumps({"id": 7, "subject": "x", "status": "pending"}))

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "unexpected_schema"


def test_guard_trips_on_a_non_numeric_task_file(tasks_root):
    """A json file whose stem is not an integer means the naming scheme moved."""
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / "abc.json").write_text("{}")

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "unexpected_schema"


def test_guard_trips_on_a_non_empty_lock(tasks_root):
    """A `.lock` with bytes in it means the sentinel became a real lock."""
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / ".lock").write_text("12345")

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "unexpected_lock"
    assert not (d / "41.json").exists()


def test_guard_trips_on_a_non_integer_highwatermark(tasks_root):
    d = tasks_root / _SID
    d.mkdir(parents=True)
    (d / ".highwatermark").write_text("not-a-number")

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "unexpected_schema"


@pytest.mark.parametrize("bad", ["", "..", "../../escape", "a/b", "with space", None, 7])
def test_rejects_a_session_id_that_is_not_a_bare_token(tasks_root, bad):
    """Path traversal here writes into an unrelated LIVE session's store."""
    result = _seed(session_id=bad)

    assert result["ok"] is False
    assert result["reason"] == "bad_session_id"
    assert not list(tasks_root.rglob("*.json")) if tasks_root.exists() else True


def test_bails_when_the_claude_config_dir_is_absent(tmp_path, monkeypatch):
    """Not a Claude Code host — do not invent a config tree."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    monkeypatch.delenv("YADGAR_HARNESS_SEED_DISABLED", raising=False)

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "no_config_dir"
    assert not (tmp_path / "nope").exists()


def test_env_kill_switch(tasks_root, monkeypatch):
    monkeypatch.setenv("YADGAR_HARNESS_SEED_DISABLED", "1")

    result = _seed()

    assert result["ok"] is False
    assert result["reason"] == "disabled"
    assert not (tasks_root / _SID).exists()


# ── row validation ────────────────────────────────────────────────────────────


def test_skips_rows_that_cannot_be_written(tasks_root):
    rows = [
        {"id": 41, "title": "good", "status": "pending"},
        {"id": None, "title": "no id", "status": "pending"},
        {"id": 0, "title": "id must be positive", "status": "pending"},
        {"id": 44, "title": "", "status": "pending"},
        {"id": 45, "title": "closed rows are not open work", "status": "completed"},
    ]

    result = _seed(rows=rows)

    assert result["ok"] is True
    assert result["seeded"] == 1
    assert result["skipped"] == 4
    assert sorted(p.name for p in (tasks_root / _SID).glob("*.json")) == ["41.json"]


def test_no_rows_is_a_success_with_nothing_to_do(tasks_root):
    result = _seed(rows=[])

    assert result["ok"] is True
    assert result["seeded"] == 0


def test_every_row_invalid_is_a_failure_so_the_nudge_still_fires(tasks_root):
    result = _seed(rows=[{"id": None, "title": "x", "status": "pending"}])

    assert result["ok"] is False
    assert result["reason"] == "no_valid_rows"


def test_string_ids_from_the_ledger_are_accepted(tasks_root):
    result = _seed(rows=[{"id": "41", "title": "t", "status": "pending"}])

    assert result["seeded"] == 1
    assert json.loads((tasks_root / _SID / "41.json").read_text())["id"] == "41"
