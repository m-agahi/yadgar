"""Tasks #345 — both task-projection docstrings carried stale corpus numbers.

Two docstrings on the master branch at car-C cut time DISAGREED on the same
metric, and the disagreement was the tell — both had been re-pasted from
snapshots taken on different dates:

  * ``yadgar/_shared/storage/sql/ledger_columns.py`` (around line 66):
    "79 open rows rendered 24,889 chars … 315 chars/row"
  * ``yadgar/core/server/tools/task.py`` (around line 567):
    "81 open rows cost 26,242 chars at 11 columns (324/row) against 8,900
    at three (110/row) — a 66.1% reduction."

Different row counts (79 vs 81), different totals (24,889 vs 26,242),
different per-row figures (315 vs 324) — all from "measurement on the live
corpus" on the same day, ostensibly. Both go stale with every commit that
adds or closes a task, and both go stale together — so the fix is to
replace the numbers with qualitative wording and a single reference date.

DC4 follow-up: do not re-paste new numbers. Either:
  (a) carry no measurement, or
  (b) carry a measurement WITH a ``<measurement-date>`` marker that a
      future test/reader can recognise as a snapshot in time.

These tests pin BOTH invariants. The two docstrings must:
  * not disagree (one says N open, the other N' open, with N != N');
  * not carry the specific stale counts (79, 81, 24,889, 26,242, 66.1%);
  * agree on the qualitative shape of the projection (full shape vs lean
    ``id, title, status``).
"""

from __future__ import annotations

import re

import pytest

#: Lines that the prior car pasted verbatim from a 2026-08-16 measurement.
#: Locked here so a future "I'll just refresh the numbers" pass trips the test.
STALE_PATTERNS = (
    r"\b79\s+open rows",
    r"\b81\s+open rows",
    r"\b24,889\b",
    r"\b26,242\b",
    r"\b8,900\b",
    r"\b66\.1\s*%",
    r"\b315\s*chars/row",
    r"\b324\s*/?row",
    r"\b110\s*/?row",
)

_LEDGER_COLUMNS_PATH = "yadgar/_shared/storage/sql/ledger_columns.py"
_TASK_TOOL_PATH = "yadgar/core/server/tools/task.py"

#: Capture the lean-projection rationale block in ``ledger_columns.py``. The
#: docstring is the one starting at the ``TASK_COLUMNS_SUMMARY`` constant
#: line. We grab a fixed window around it so the test isn't sensitive to a
#: few lines of drift above/below.
LEDGER_PROJ_RATIONALE_RE = re.compile(
    r"WHY IT EXISTS:.*?TASK_COLUMNS_SUMMARY\s*=\s*\"[^\"]+\"",
    re.DOTALL,
)

#: The matching rationale in ``task.py`` is the "ROW WIDTH" block in the
#: ``task_list`` tool's docstring.
TASK_TOOL_WIDTH_RE = re.compile(
    r"ROW WIDTH.*?ROW COUNT is untouched",
    re.DOTALL,
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_ledger_columns_projection_rationale_exists() -> None:
    """The lean-projection rationale is still present, so the rewrite did not delete it."""
    src = _read(_LEDGER_COLUMNS_PATH)
    match = LEDGER_PROJ_RATIONALE_RE.search(src)
    assert match is not None, (
        "ledger_columns.py lost its WHY-IT-EXISTS block — the lean "
        "projection (TASK_COLUMNS_SUMMARY) now lacks rationale"
    )


def test_task_tool_width_block_exists() -> None:
    """The task_list tool's ROW WIDTH block is still present."""
    src = _read(_TASK_TOOL_PATH)
    match = TASK_TOOL_WIDTH_RE.search(src)
    assert match is not None, (
        "task.py's task_list docstring lost its ROW WIDTH block — the "
        "verbose=True / summary=False contract is no longer documented"
    )


@pytest.mark.parametrize("pattern", STALE_PATTERNS)
def test_ledger_columns_has_no_stale_measurement(pattern: str) -> None:
    """No stale numeric count from the 2026-08-16 snapshot survives in ledger_columns.py."""
    src = _read(_LEDGER_COLUMNS_PATH)
    rationale_block = LEDGER_PROJ_RATIONALE_RE.search(src)
    assert rationale_block is not None, "rationale block missing — see prior test"
    assert not re.search(pattern, rationale_block.group(0)), (
        f"ledger_columns.py's projection rationale still carries a stale "
        f"corpus number matching {pattern!r}. Replace with qualitative "
        f"wording or tag with a <measurement-date> marker."
    )


@pytest.mark.parametrize("pattern", STALE_PATTERNS)
def test_task_tool_has_no_stale_measurement(pattern: str) -> None:
    """No stale numeric count survives in task.py's ROW WIDTH block."""
    src = _read(_TASK_TOOL_PATH)
    width_block = TASK_TOOL_WIDTH_RE.search(src)
    assert width_block is not None, "ROW WIDTH block missing — see prior test"
    assert not re.search(pattern, width_block.group(0)), (
        f"task.py's ROW WIDTH block still carries a stale corpus number "
        f"matching {pattern!r}. Replace with qualitative wording or tag "
        f"with a <measurement-date> marker."
    )


def test_two_docstrings_do_not_disagree_on_row_count() -> None:
    """The two docstrings must not assert DIFFERENT open-row counts (79 vs 81).

    This is the smell the bug report named: two "live corpus" measurements
    on the same day that contradict each other. If both are removed in
    favour of qualitative wording, this test passes vacuously.
    """
    ledger_src = _read(_LEDGER_COLUMNS_PATH)
    task_src = _read(_TASK_TOOL_PATH)

    ledger_match = re.search(r"\b(\d+)\s+open rows\b", ledger_src)
    task_match = re.search(r"\b(\d+)\s+open rows\b", task_src)
    if ledger_match and task_match:
        assert ledger_match.group(1) == task_match.group(1), (
            f"ledger_columns says {ledger_match.group(1)} open rows, "
            f"task.py says {task_match.group(1)} — the two docstrings disagree "
            f"on the same metric. Remove the counts (qualitative wording) or "
            f"tag both with the same <measurement-date>."
        )


def test_both_docstrings_name_the_lean_projection() -> None:
    """The qualitative contract — 'lean = id, title, status' — survives the rewrite."""
    ledger_src = _read(_LEDGER_COLUMNS_PATH)
    task_src = _read(_TASK_TOOL_PATH)
    ledger_block = LEDGER_PROJ_RATIONALE_RE.search(ledger_src)
    task_block = TASK_TOOL_WIDTH_RE.search(task_src)
    assert ledger_block is not None and task_block is not None
    # Accept either the joined "id, title, status" form or the prose
    # "``id``, ``title`` and ``status``" form — both name the lean shape.
    ledger_names = ("id, title, status" in ledger_block.group(0)) or all(
        token in ledger_block.group(0) for token in ("``id``", "``title``", "``status``")
    )
    task_names = ("id, title, status" in task_block.group(0)) or all(
        token in task_block.group(0) for token in ("``id``", "``title``", "``status``")
    )
    assert ledger_names, "ledger_columns.py no longer names the three-column lean projection"
    assert task_names, "task.py no longer names the three-column lean projection"
