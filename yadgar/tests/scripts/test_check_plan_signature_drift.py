"""Tests for scripts/check_plan_signature_drift.py (cross-document signature drift).

The checker builds a signature map from fenced ```python blocks under
docs/plans/** (archive/ excluded) and FAILS when a call site elsewhere in the
corpus passes a kwarg that name's signature does not accept.

The RED case is the real 2026-08-09 defect: car A defines `create_task_row`
without `origin` or `directory`; car E called it with both. Fixture text is
pasted inline (NOT read from the live corpus) so the test keeps testing the
defect after the corpus is repaired.

Run:
  uv run pytest yadgar/tests/scripts/test_check_plan_signature_drift.py
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).parent.parent.parent.parent / "scripts" / "check_plan_signature_drift.py"

# ---------------------------------------------------------------------------
# Verbatim corpus fixtures (copied from the real docs at dae4be0e)
# ---------------------------------------------------------------------------

# docs/plans/0047-car-A-ledger-tables.md — the canonical signature. Note it is
# BODILESS (no colon, no body), which is a SyntaxError for a bare ast.parse;
# the checker must synthesize a parseable def. Keep it bodiless.
CAR_A_SIGNATURE = """\
Ledger CRUD methods to add to `MariaStorageEngine`:

```python
# task
async def create_task_row(self, *, project_id: str, title: str, status: str = "pending",
                          state: str | None = "open", active_form: str | None = None,
                          plan_path: str | None = None, body_slug: str | None = None) -> dict
async def list_task_rows(self, *, project_id: str, status: str | None = None) -> list[dict]
async def get_task_row(self, task_id: int) -> dict | None
async def update_task_row(self, task_id: int, **fields) -> None
```
"""

# docs/plans/0047-car-E-task-seed-session-hooks.md:78 — the pre-fix violation.
CAR_E_VIOLATING_CALL = (
    '- Map to `create_task_row(project_id=, origin="yadgar", title=subject, '
    "active_form=, state=, plan_path=, body_slug=, directory=)`.\n"
)


def run_script(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
    )


def _make_corpus(tmp_path: Path, files: dict[str, str], subdir: str = "docs/plans") -> Path:
    """Write {relative_name: text} into <tmp>/docs/plans and return the root."""
    plans = tmp_path / subdir
    plans.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        target = plans / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text))
    return tmp_path


# ---------------------------------------------------------------------------
# RED — the real defect
# ---------------------------------------------------------------------------


def test_real_car_e_drift_is_flagged(tmp_path):
    """The actual 2026-08-09 defect: origin= and directory= are not in car A's sig."""
    root = _make_corpus(
        tmp_path,
        {
            "0047-car-A-ledger-tables.md": CAR_A_SIGNATURE,
            "0047-car-E-task-seed-session-hooks.md": CAR_E_VIOLATING_CALL,
        },
    )
    res = run_script(root)

    assert res.returncode == 1, (
        f"expected drift to FAIL the gate\nstdout={res.stdout}\nstderr={res.stderr}"
    )
    combined = res.stdout + res.stderr
    assert "origin" in combined, f"must name the offending kwarg `origin`\n{combined}"
    assert "directory" in combined, f"must name the offending kwarg `directory`\n{combined}"
    assert "0047-car-E-task-seed-session-hooks.md" in combined, "must report the violating file"
    assert "0047-car-A-ledger-tables.md" in combined, (
        "must report where the canonical signature lives"
    )
    assert "create_task_row" in combined


def test_violation_reports_line_number(tmp_path):
    """Report is file:line so the offending call is directly addressable."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "car-e.md": "intro line\n\nsecond line\n\n" + CAR_E_VIOLATING_CALL,
        },
    )
    res = run_script(root)
    assert res.returncode == 1
    assert "car-e.md:5" in res.stdout + res.stderr, res.stdout + res.stderr


def test_drift_inside_fenced_block_is_flagged(tmp_path):
    """Drift in a fenced code block counts too, not only inline-backticked prose."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "car-e.md": """\
            ```python
            row = await create_task_row(project_id=pid, title=t, bogus_kwarg=1)
            ```
            """,
        },
    )
    res = run_script(root)
    assert res.returncode == 1
    assert "bogus_kwarg" in res.stdout + res.stderr


# ---------------------------------------------------------------------------
# Vacuity — legitimate prose forms must NOT fire
# ---------------------------------------------------------------------------


def test_clean_corpus_passes(tmp_path):
    """Every kwarg present in the signature → exit 0."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "car-e.md": "- Map to `create_task_row(project_id=, title=subject, body_slug=)`.\n",
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_illustrative_forms_are_not_drift(tmp_path):
    """Bare (), (...), (…) and elided-value kwargs on valid names are prose, not drift."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": """\
            Call `create_task_row()` from the seed path.
            Then `list_task_rows(...)` for the open set.
            Unicode ellipsis: `get_task_row(…)`.
            Valid kwarg with elided value: `list_task_rows(project_id=...)`.
            """,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_undefined_function_is_not_checked(tmp_path):
    """Names with no in-corpus definition (MCP tools, stdlib) are out of scope."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": "Use `wiki_add(title=..., content=..., anything_at_all=1)` here.\n",
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_kwargs_signature_is_unconstrained(tmp_path):
    """`update_task_row(self, task_id, **fields)` accepts anything → never flagged."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": "Then `update_task_row(task_id=5, whatever_field=1, another=2)`.\n",
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_definition_block_is_not_a_call_site(tmp_path):
    """The signature block itself must not self-report as a violating call."""
    root = _make_corpus(tmp_path, {"car-a.md": CAR_A_SIGNATURE})
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_multiple_definitions_take_union(tmp_path):
    """A car restating an abbreviated signature must not fail the fuller call."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": """\
            ```python
            async def create_adr_row(self, *, project_id: str, title: str, tier: str | None = None) -> dict
            ```
            """,
            "car-f.md": """\
            ```python
            async def create_adr_row(self, *, project_id: str, title: str, status: str = "open") -> dict
            ```
            Call it as `create_adr_row(project_id=p, title=t, tier=x, status=s)`.
            """,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_nested_call_kwargs_not_attributed_to_outer(tmp_path):
    """Depth-1 extraction only: inner call kwargs belong to the inner call."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": "`create_task_row(project_id=derive(some_inner_kwarg=1), title=t)`\n",
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_equals_inside_string_literal_is_not_a_kwarg(tmp_path):
    """`foo(bar="x=y")` must not invent a kwarg named `x`."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": '`create_task_row(project_id="a=b", title="c=d")`\n',
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_archive_is_excluded_from_scan(tmp_path):
    """Archived plans legitimately cite retired signatures → not scanned."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "archive/old-plan.md": CAR_E_VIOLATING_CALL,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_archive_definitions_do_not_seed_the_map(tmp_path):
    """A name defined ONLY in archive/ is not checkable in live plans."""
    root = _make_corpus(
        tmp_path,
        {
            "archive/car-a.md": CAR_A_SIGNATURE,
            "live.md": CAR_E_VIOLATING_CALL,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_empty_corpus_passes(tmp_path):
    root = _make_corpus(tmp_path, {})
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


# ---------------------------------------------------------------------------
# Suppression rules — the car-H / prose-mention false-positive classes
# ---------------------------------------------------------------------------

# docs/plans/0047-car-H-tier-subsystem-rollups.md:46-47,55 verbatim shape: the
# car that EXTENDS a signature declares it in prose, then calls it downstream.
CAR_F_ADR_SIGNATURE = """\
```python
def adr_add(directory: str, title: str, status: str) -> dict
def adr_list(directory: str, status: str | None = None, limit: int = 50, offset: int = 0) -> dict
```
"""


def test_annotated_declaration_is_not_a_call(tmp_path):
    """`adr_add(..., tier: str | None = None) -> dict` declares, it does not call."""
    root = _make_corpus(
        tmp_path,
        {
            "car-f.md": CAR_F_ADR_SIGNATURE,
            "car-h.md": "- `adr_add(..., tier: str | None = None, subsystem: str | None = None) -> dict` — adds two optional.\n",
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_declaration_extends_signature_for_downstream_calls(tmp_path):
    """Car H declares the extension, so car H's own later call is clean."""
    root = _make_corpus(
        tmp_path,
        {
            "car-f.md": CAR_F_ADR_SIGNATURE,
            "car-h.md": """\
            - `adr_add(..., tier: str | None = None, subsystem: str | None = None) -> dict` — adds two optional.

            3. **RED** — `adr_add(directory=d, title=t, tier="binding", subsystem="storage")` creates a row.
            """,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_arrow_only_declaration_is_absorbed(tmp_path):
    """A declaration with no annotations but a `-> dict` still extends the map."""
    root = _make_corpus(
        tmp_path,
        {
            "car-f.md": CAR_F_ADR_SIGNATURE,
            "car-h.md": """\
            - `adr_list(directory, status=None, tier="binding", limit=50, offset=0) -> dict` — gains `tier`.

            Later: `adr_list(directory=d, status=s, tier="binding")` is then valid.
            """,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_leading_ellipsis_call_is_skipped_but_not_absorbed(tmp_path):
    """`f(..., x=1)` elides the real args → no claim; and it grants nothing."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "elided.md": '`create_task_row(..., bogus_one="a", bogus_two="b")`\n',
            "real.md": '`create_task_row(bogus_one="a", bogus_two="b")`\n',
        },
    )
    res = run_script(root)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "real.md" in combined, "the non-elided call must still be flagged"
    assert "elided.md" not in combined, f"leading `...` must suppress\n{combined}"


def test_single_kwarg_mention_is_not_drift(tmp_path):
    """One kwarg names a parameter; it does not enumerate a call."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": """\
            If `list_task_rows(subsystem=...)` becomes hot, add the index.
            v7 features: `get_task_row(synthesize=True)`.
            """,
        },
    )
    res = run_script(root)
    assert res.returncode == 0, res.stdout + res.stderr


def test_two_unknown_kwargs_still_fire(tmp_path):
    """The minimum-kwarg rule is 2, not 'any number' — the boundary holds."""
    root = _make_corpus(
        tmp_path,
        {
            "car-a.md": CAR_A_SIGNATURE,
            "prose.md": "`list_task_rows(subsystem=x, tier=y)`\n",
        },
    )
    res = run_script(root)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "subsystem" in res.stdout + res.stderr
