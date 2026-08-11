"""C9a (0047 PR#40 §5) — the ``directory`` residue lint, scoped to ``yadgar/_shared``.

ADR-0225 retires ``directory`` as a scoping and identity concept and names a
**residue sweep** as the enforcement mechanism ("without it the ADR is a
code-review promise").  C15 ships that sweep repo-wide as a
``scripts/check_*.py`` + pre-commit hook.  **This module is C9a's share of it**,
written here so the car has a RED of its own and so C15 inherits a
tree-scoped allowlist that already carries a reason per entry rather than
having to reconstruct one from the diff.

**The identifier-shaped surface is what counts.**  A raw ``git grep`` over this
tree returns 360 hits and roughly a third of them are the English word
"directory" inside prose — the branch train's measured lesson (19 of
``core/vacuum/``'s hits were the word "branch" in a comment).  So the walk is an
**AST walk over function parameter names**, not a text scan: prose cannot fail
it, and a renamed parameter cannot hide from it.

**Two directions, matching ``scripts/check_capability_coverage.py``'s shape:**

1. **Residue** — a residue-token parameter in ``yadgar/_shared`` that is not in
   ``_ALLOWLIST`` fails.  This is what stops the tree regressing.
2. **Stale allowlist** — an allowlist entry whose module or function no longer
   exists fails.  ``_stale_policy`` here is **hard-fail**, not warn: every entry
   below names a specific carve-out or a specific downstream car, so an entry
   that has lost its subject means that car landed and the reason is now a lie.

The allowlist is keyed on ``<path>::<function>``, never on a line number —
sixteen cars have already moved these lines and more will.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SHARED_ROOT = pathlib.Path(__file__).resolve().parents[2] / "_shared"

#: Parameter names that mean "a project was identified by its directory".
#: ``directory_context`` is included even though the COLUMN survives to the next
#: PR — carve-out 2 is about the stored column, not about parameters named after
#: it, and the allowlist below states which is which per entry.
_RESIDUE_TOKENS: frozenset[str] = frozenset(
    {
        "directory",
        "caller_dir",
        "caller_directory",
        "directory_context",
        "project_directory",
        "target_directory",
        "watch_directory",
    }
)

# ── the reasons, stated once ─────────────────────────────────────────────────

_CARVE_3 = (
    "carve-out 3 — a genuine filesystem path, not a scoping key. The value is "
    "opened, globbed, walked or watched; it is never compared against a project "
    "identity. ADR-0225 excludes these by rule."
)
_CARVE_2 = (
    "carve-out 2 — a parameter over the stored ``directory_context`` column, "
    "which ADR-0225 keeps alive until the NEXT PR because the backfill derives "
    "from it. Re-keying the parameter without the column would make the caller "
    "pass ``owner/repo`` into ``WHERE directory_context = $dir`` and match zero "
    "rows."
)
_C10 = (
    "plan §5 C10 judgement site — the plan names this function by line and "
    "prescribes a redesign (not a rename), so C9a must not pre-empt it."
)
_C11 = (
    "plan §5 C11 — the backing table has no ``project_id`` column until "
    "migration 033. Migration 031 declared ``project_id`` on ``wiki_page`` and "
    "``memory`` ONLY, so there is nothing to re-key onto here yet."
)
_C9B = (
    "C9b-coupled — the table (``memory`` / ``wiki_page``) does carry "
    "``project_id``, but every non-test caller lives in ``yadgar/backend`` "
    "which is C9b's tree. Renaming the parameter and re-keying the WHERE must "
    "land in the same commit as the callers or the query silently matches "
    "nothing; C9a's territory is ``_shared`` only."
)
_NO_OWNER = (
    "GAP — ``runtime_config`` carries its own ``directory`` COLUMN and is "
    "absent from plan §5 C11's four-table list (memory_block / episode / "
    "action_log / queue). It has no ``project_id`` and no owning car. Reported "
    "by C9a for C11 and C15."
)

#: ``<path relative to yadgar/_shared>::<function name>`` → reason.
#: Every entry is a deliberate exclusion; none is "not got to yet".
_ALLOWLIST: dict[str, str] = {
    # ── carve-out 3 — real filesystem paths ──────────────────────────────────
    "file_queue/queue.py::_find_terminal": _CARVE_3,
    "restoration/transcript_parse.py::_list_worktrees": _CARVE_3,
    "runtime/lifecycle.py::init_engines": _CARVE_3,
    "server_helpers/server_helpers.py::_resolve_project_root": _CARVE_3,
    "server_helpers/server_helpers.py::_worktree_root_from_path_heuristics": _CARVE_3,
    "server_helpers/server_helpers.py::_worktree_canonical_root": _CARVE_3,
    # cache_epoch hashes its argument into a counter FILE NAME. Its only
    # producer is ``server_helpers._bump_epoch_for_context``, which feeds it a
    # ``_resolve_project_root`` path — itself carve-out 3. Renaming the
    # parameter here without replacing that producer with a session-minted
    # project_id (ADR-0227 host minting) would label a resolved path as an
    # identity. The seam spans _shared + backend; see the C9a report.
    "runtime/cache_epoch.py::_counter_path": _CARVE_3,
    "runtime/cache_epoch.py::bump_epoch": _CARVE_3,
    "runtime/cache_epoch.py::_current_epoch": _CARVE_3,
    # ── carve-out 2 — the directory_context column ───────────────────────────
    "storage/_project_id_writer.py::_resolve_project_id_for_write": _CARVE_2,
    "storage/narrative.py::get_beliefs_for_subject": _CARVE_2,
    "storage/user.py::insert_profile": _CARVE_2,
    "storage/user.py::get_profiles_for_entity": _CARVE_2,
    "storage/wiki.py::get_wiki_page_by_slug_directory": _CARVE_2,
    "wiki/store.py::_gate_dir_eligible": _CARVE_2,
    "wiki/store.py::read_by_directory": _CARVE_2,
    "wiki/store.py::find_similar_wiki_pages": _CARVE_2,
    "wiki/store.py::_collect_similar_candidates": _CARVE_2,
    # ── plan §5 C10 judgement sites ──────────────────────────────────────────
    "rules_engine/rules_engine.py::get_applicable_rules": _C10,
    "rules_engine/rules_engine.py::apply_rules": _C10,
    "restoration/transcript_parse.py::capture_in_flight": _C10,
    # ── plan §5 C11 — no project_id column until migration 033 ───────────────
    "sensory_buffer/sensory_buffer.py::capture": _C11,
    "sensory_buffer/sensory_buffer.py::capture_action": _C11,
    "storage/queue.py::insert_action_log": _C11,
    "storage/ops.py::get_active_checkpoint": _C11,
    "storage/narrative.py::get_narratives_for_directory": _C11,
    "storage/blocks.py::_canonical_dir": _C11,
    "storage/blocks.py::_block_dir_clause": _C11,
    "storage/blocks.py::_count_blocks_in_scope": _C11,
    "storage/blocks.py::create_block": _C11,
    "storage/blocks.py::get_block": _C11,
    "storage/blocks.py::update_block": _C11,
    "storage/blocks.py::delete_block": _C11,
    "storage/blocks.py::list_blocks": _C11,
    "storage/blocks.py::replace_block": _C11,
    "storage/blocks.py::append_block": _C11,
    # ── no owning car — reported upward ──────────────────────────────────────
    "storage/runtime_config.py::_canonical_config_dir": _NO_OWNER,
    "storage/runtime_config.py::_config_dir_clause": _NO_OWNER,
    "storage/runtime_config.py::set_config_row": _NO_OWNER,
    "storage/runtime_config.py::get_config_row": _NO_OWNER,
    "storage/runtime_config.py::list_config_rows": _NO_OWNER,
    "storage/runtime_config.py::delete_config_row": _NO_OWNER,
    # ── C9b-coupled — re-keyable, but the callers are not C9a's ──────────────
    "metacognition/gap_detection.py::detect_gaps": _C9B,
    "storage/memory.py::get_memories_for_directory": _C9B,
    "storage/memory.py::get_memories_by_store_type": _C9B,
    "storage/memory.py::get_anchored_memories_scoped": _C9B,
    "storage/memory.py::get_recent_memories_since": _C9B,
    "storage/wiki.py::list_wiki_pages": _C9B,
    "storage/wiki.py::list_wiki_catalog": _C9B,
    "storage/wiki.py::upsert_project_init": _C9B,
    "storage/wiki.py::upsert_active_work": _C9B,
    "storage/wiki.py::upsert_dispatch_prelude_marker": _C9B,
    "wiki/store.py::list_pages": _C9B,
    "wiki/store.py::_autolink_title_map": _C9B,
    "wiki/store.py::autolink": _C9B,
}

#: The FIVE signatures C9a renamed to ``project_id``. Pinned so a later car
#: cannot reintroduce the token under the same name. (The sixth edit in the car
#: is a CALL SITE — ``astrocyte_pool`` now reads ``project_id`` off the memory
#: row — which has no signature to pin and is covered by Direction 1 instead.)
_SWEPT: tuple[str, ...] = (
    "thermodynamics/thermodynamics.py::compute_surprise",
    "knowledge_graph/knowledge_graph.py::extract_entities_typed",
    "knowledge_graph/knowledge_graph.py::_extract_entities_typed_inner",
    "metacognition/coverage.py::assess_coverage",
    "blocks_render/blocks_render.py::render_blocks_section",
)

#: Anti-vacuity floors (ADR-0080). A walk that silently found nothing would
#: make every assertion below trivially green. Both numbers are measured on the
#: C9a ref, and both are LOWER bounds — later cars only remove entries.
_MIN_MODULES_WALKED = 100
_MIN_RESIDUE_SITES = 40


def _walk() -> dict[str, set[str]]:
    """Map ``<relpath>::<function>`` → the residue tokens in its signature."""
    found: dict[str, set[str]] = {}
    for path in sorted(_SHARED_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_SHARED_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args
            names = {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)}
            hits = names & _RESIDUE_TOKENS
            if hits:
                found[f"{rel}::{node.name}"] = hits
    return found


def _modules_walked() -> int:
    return sum(1 for _ in _SHARED_ROOT.rglob("*.py"))


def _symbol_exists(entry: str) -> bool:
    """True when ``<relpath>::<function>`` still names a real function."""
    rel, _, func = entry.partition("::")
    path = _SHARED_ROOT / rel
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func
        for n in ast.walk(tree)
    )


class TestWalkIsNotVacuous:
    """ADR-0080 — an empty scan must not read as a clean tree."""

    def test_the_tree_was_actually_walked(self) -> None:
        n = _modules_walked()
        assert n >= _MIN_MODULES_WALKED, (
            f"only {n} modules found under {_SHARED_ROOT} — the walk did not "
            "reach the tree, so every assertion below would be vacuously true"
        )

    def test_the_allowlist_still_describes_a_real_surface(self) -> None:
        found = _walk()
        assert len(found) >= _MIN_RESIDUE_SITES, (
            f"only {len(found)} residue-token signatures found — below the "
            f"measured floor of {_MIN_RESIDUE_SITES}. Either the AST walk "
            "broke or the sweep finished; if it genuinely finished, LOWER the "
            "floor deliberately rather than letting the guard rot."
        )


class TestDirectionOneResidue:
    """No new ``directory`` parameter may appear in ``yadgar/_shared``."""

    def test_no_unallowlisted_residue_parameter(self) -> None:
        found = _walk()
        offenders = sorted(set(found) - set(_ALLOWLIST))
        assert not offenders, (
            "residue `directory`-family parameters in yadgar/_shared that are "
            "not in the C9a allowlist:\n  "
            + "\n  ".join(f"{k} ({', '.join(sorted(found[k]))})" for k in offenders)
            + "\n\nADR-0225 retires `directory` as a scoping key. Either rename "
            "the parameter to `project_id`, or add it to `_ALLOWLIST` above "
            "WITH a stated reason naming the carve-out or the owning car."
        )

    @pytest.mark.parametrize("entry", _SWEPT)
    def test_swept_signature_still_exists(self, entry: str) -> None:
        """``_SWEPT`` gets the same Direction-2 check as ``_ALLOWLIST``.

        Without it a typo — or a later rename of one of these functions — makes
        ``test_the_symbols_c9a_swept_are_gone`` silently green forever, since
        a name that is not in the tree can never appear in the walk.
        """
        assert _symbol_exists(entry), (
            f"C9a's swept-symbol pin names {entry!r}, which no longer exists. "
            "The function moved or was renamed — re-point the entry rather "
            "than leaving a pin that can never fire."
        )

    def test_the_symbols_c9a_swept_are_gone(self) -> None:
        """The five signatures C9a renamed to ``project_id`` stay renamed."""
        found = _walk()
        regressed = [s for s in _SWEPT if s in found]
        assert not regressed, (
            f"C9a renamed these to `project_id`; a residue token is back: {regressed}"
        )


class TestDirectionTwoStaleAllowlist:
    """An allowlist entry that has lost its subject is a lie, not a leftover."""

    @pytest.mark.parametrize("entry", sorted(_ALLOWLIST))
    def test_allowlist_entry_still_exists(self, entry: str) -> None:
        rel, _, func = entry.partition("::")
        path = _SHARED_ROOT / rel
        assert path.is_file(), (
            f"allowlist entry {entry!r} names {rel!r}, which does not exist. "
            "The module moved or was deleted — remove the entry (or re-point "
            "it) rather than leaving a reason attached to nothing."
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert func in names, (
            f"allowlist entry {entry!r} names a function {func!r} that no "
            f"longer exists in {rel}. Its carve-out reason now describes "
            "nothing; delete the entry."
        )

    def test_no_allowlist_entry_has_lost_its_residue(self) -> None:
        """An entry whose signature was already swept must be deleted."""
        found = _walk()
        clean = sorted(set(_ALLOWLIST) - set(found))
        assert not clean, (
            "these allowlist entries no longer take a `directory`-family "
            f"parameter and must be removed from `_ALLOWLIST`: {clean}"
        )

    def test_every_entry_states_a_reason(self) -> None:
        empty = sorted(k for k, v in _ALLOWLIST.items() if not v or len(v) < 40)
        assert not empty, (
            f"allowlist entries with no usable stated reason: {empty}. C15 "
            "inherits these verbatim — a bare entry is not reviewable."
        )
