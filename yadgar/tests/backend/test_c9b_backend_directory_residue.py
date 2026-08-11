"""Car C9b (0047 §5 C9a/C9b) — the residue lint, scoped to ``yadgar/backend``.

ADR-0225 names a residue sweep as **the** enforcement mechanism for retiring
``directory`` ("without it the ADR is a code-review promise"). C15 builds the
repo-wide script; this module is the ``backend``-tree assertion C9b owes, written
so C15 can promote it by widening the root and merging the allowlist.

**Shape — fails in BOTH directions**, modelled on
``scripts/check_capability_coverage.py``:

1. **Residue.** A scoping-shaped ``directory`` token in a non-test
   ``yadgar/backend`` module that is not allowlisted fails.
2. **Stale allowlist.** An allowlist entry whose file has vanished, or which no
   longer has any residue, fails — an allowlist that outlives its reason is how
   the next sweep silently loses ground.

**Granularity is per FILE, deliberately, not per line.** Sixteen cars have
already shifted every line number in this tree; a line-keyed allowlist would be
stale on arrival and would fail for reasons that have nothing to do with the
sweep.

**Carve-out 2 is applied as a CLASS, not per entry.** ``directory_context`` is
the live stored column — migration 031 adds ``project_id`` as an additive
``option<string>`` and the C6 backfill *derives from* ``directory_context``, so
the column and every read of it must survive until the drop migration in the
NEXT PR. Stripping the token before matching keeps that out of the allowlist
entirely; enumerating ~100 column reads would produce an allowlist that asserts
nothing.
"""

from __future__ import annotations

import re

from yadgar.tests._paths import REPO_ROOT

#: Direction-1 token forms — the identifier-shaped surface C9b actually swept.
#: English prose is NOT matched: roughly a third of the raw ``directory`` hits in
#: this tree are the word used in a sentence, and the branch train's lesson (19 of
#: ``core/vacuum/``'s hits were the word "branch" in prose) is that sweeping those
#: is how a mechanical car turns into a judgement car.
_TOKEN = re.compile(
    r"""directory=|directory:\s*str|\bcaller_dir\b|\bproject_directory\b|"directory"|'directory'"""
)

#: Carve-out 2, applied as a class: stripped before matching, never allowlisted.
_STORED_COLUMN = re.compile(r"directory_context")

_ROOT = REPO_ROOT / "yadgar" / "backend"

#: Files that legitimately still carry the token, each with the reason C15 cites.
#: Every entry is a boundary C9b must not cross alone — a stored column, a wire
#: contract, a ``_shared`` signature owned by C9a, or a judgement site the plan
#: assigns elsewhere.
ALLOWLIST: dict[str, str] = {
    # ── C10 judgement sites (plan §5 C10 (b)/(d)) — specified design changes,
    #    not renames; C9b is the mechanical car and must not pre-empt them.
    "restoration/checkpoint_restore.py": "C10 (b) — splits into project_id + worktree_path",
    "restoration/__init__.py": "C10 (b) — forwards into checkpoint_restore.restore",
    "admin_exec/restoration.py": "C10 (b) — forwards into checkpoint_restore.restore",
    "write_exec/checkpoint_impl.py": "C10 (b) — forwards into replay.create_checkpoint",
    "admin_exec/adr_seed.py": "C10 (d) — basename() project-name surrogate",
    "retrieval/core.py": (
        "basename(directory) embedding-prefix surrogate — same class as C10 (d), "
        "unowned by the plan; changing it changes stored embedding text"
    ),
    # ── C4's live decisions in this tree — do not undo.
    "consolidation/cleanup.py": "C4 — skip-and-count grouping already decided here",
    # ── Carve-out 2 beyond the column name itself: legacy `directory` columns on
    #    other tables, dropped by the same NEXT-PR migration.
    "causal_discovery/pc.py": "stored episode column — e['directory']",
    "consolidation/cls.py": "stored episode column — ep.get('directory')",
    "prospective/prospective.py": "stored prospective_memory column — target_directory",
    # ── `_shared` signatures owned by C9a; the keyword spelling moves with them.
    # ``cls_store/clustering.py`` is GONE from this dict: C9c renamed
    # ``get_memories_by_store_type``'s parameter to ``project_id`` and re-keyed
    # its WHERE, so both call sites here now spell it ``project_id=`` and the
    # file has no residue left. Direction 2 (stale entries hard-fail) is what
    # forces the removal — the entry could not outlive the boundary it named.
    "admin_exec/blocks.py": "C9a — storage block API (directory=) + core-owned payload key",
    "admin_exec/project.py": "C9a — storage.get_block/create_block(directory=)",
    "write_exec/action_log_impl.py": "C9a — storage.insert_action_log(directory=)",
    "admin_exec/audit.py": "C9a — storage.insert_action_log(directory=) + payload key",
    # ── Wire contracts: keys crossing a process boundary, renamed with the far
    #    side or not at all.
    "embed_service/embed_service_models.py": (
        "core<->backend wire contract; RecallRequest.directory is deliberately "
        "retained (extra='forbid' — images deploy together)"
    ),
    "graph/graph_nodes.py": "viz node payload key consumed by the graph UI",
    "write_exec/_memorize_phases/_phase_post_write.py": "viz node payload key (matches graph_nodes)",
    "queue_drainer/__init__.py": "queue payload key — caller_context",
    "queue_drainer/apply.py": "queue payload key + C10 (b) forwarding",
    "queue_drainer/dlq.py": "DLQ failure_reason taxonomy ('missing_directory') + payload reads",
    "admin_exec/wiki.py": "core-owned admin payload key",
    "admin_exec/seed.py": "core-owned admin payload key; directory + project_id coexist by design",
    # ── Out of scope by plan §6.
    "admin_exec/runtime_config.py": "plan §6 — runtime_config scoping belongs to the knob train",
}


def _residue() -> dict[str, list[tuple[int, str]]]:
    """Map ``path-relative-to-backend`` → the offending ``(lineno, text)`` rows."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        if "tests" in path.parts:
            continue
        rows = [
            (n, line.strip())
            for n, line in enumerate(path.read_text().splitlines(), 1)
            # carve-out 2 as a class: the stored column never counts as residue
            if _TOKEN.search(_STORED_COLUMN.sub("", line))
        ]
        if rows:
            found[str(path.relative_to(_ROOT))] = rows
    return found


def test_no_unallowlisted_directory_residue_in_backend() -> None:
    """Direction 1 — a new scoping ``directory`` in backend fails the sweep."""
    offenders = {f: rows for f, rows in _residue().items() if f not in ALLOWLIST}
    assert not offenders, (
        "unallowlisted `directory` residue in yadgar/backend (ADR-0225):\n"
        + "\n".join(
            f"  {f}\n" + "\n".join(f"    {n}: {t}" for n, t in rows)
            for f, rows in sorted(offenders.items())
        )
        + "\n\nSweep it to project_id, or add an allowlist entry WITH a reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Direction 2 — an allowlist entry that no longer has residue fails.

    Chosen over a warning (``.test-weakening-allowlist.json``'s other
    ``_stale_policy`` option) because this allowlist is small, hand-written, and
    every entry names a boundary that a later car is expected to REMOVE. A
    warning would let the entry outlive the boundary silently, which is the exact
    drift the lint exists to catch.
    """
    residue = _residue()
    stale = sorted(set(ALLOWLIST) - set(residue))
    assert not stale, (
        "stale allowlist entries — the residue is gone, so the entry must go too:\n"
        + "\n".join(f"  {f}  ({ALLOWLIST[f]})" for f in stale)
    )


def test_carve_out_2_is_a_class_not_an_allowlist_entry() -> None:
    """``directory_context`` must never need an allowlist entry to pass.

    Pins the design choice: the stored column is stripped before matching. If a
    later edit turns the class-strip into per-file entries, this fails and says
    why.
    """
    line = '        "directory_context": project_id,'
    assert _TOKEN.search(line) is None or _TOKEN.search(_STORED_COLUMN.sub("", line)) is None, (
        "directory_context matched as residue — carve-out 2 must be applied as a "
        "class strip, not enumerated per file"
    )
