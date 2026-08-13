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
# ── C11 landed. The old ``_C11`` reason ("the backing table has no
# ``project_id`` column until migration 033") became FALSE the moment 033
# shipped, and Direction 2 exists to catch exactly that kind of lie. These are
# its replacements, split by what is actually true per signature now.

_C11_DUAL_KEY = (
    "C11 (plan §5) SHIPPED — migration 033 declared ``project_id`` on this "
    "table and the writer/reader here now carry it. The ``directory``-family "
    "parameter SURVIVES because the key is transitional, not because the car "
    "did not reach it: nothing backfills these tables "
    "(``project_backfill._TABLES`` is ``('memory', 'wiki_page')`` and plan §8 "
    "defines no step for the rest), so the legacy value is the ONLY way rows "
    "written before C11 stay reachable. Dropping the parameter now would not "
    "be a degraded window — it would be permanent silent loss of the "
    "historical corpus. It goes with the column, in the next PR's drop "
    "migration."
)
_C11_LEGACY_COLUMN_WRITE = (
    "C11 (plan §5) SHIPPED — this writer DUAL-WRITES: it stamps ``project_id`` "
    "(migration 033) and keeps writing the legacy column, so the parameter "
    "stays. Two independent reasons, both load-bearing: ADR-0225 keeps the "
    "legacy columns because the backfill DERIVES from them (a row with an "
    "identity and no path is unattributable in both directions), and three "
    "live consumers read them today — ``causal_discovery/pc.py`` filters "
    "episodes on ``e['directory']``, ``consolidation/cls.py`` reads "
    "``ep.get('directory')``, and ``consolidation/cleanup.py`` takes the "
    "action-log row's ``directory`` as the summary memory's "
    "``directory_context``. Dies with the column, in the drop PR."
)
_C11_DEFERRED_NEEDS_ENVELOPE = (
    "C11 (plan §5) considered this and deferred it ON PURPOSE. The table does "
    "carry ``project_id`` and the predicate could move — but both callers reach "
    "it without a resolved identity, and resolving one means calling "
    "``resolve_effective_project``, which RAISES. ``project_brief`` has no error "
    "envelope and runs on the session-start hook path, so the promotion would "
    "trade an empty bucket for a hard failure on every session start. The "
    "two-condition rule is rename-with-callers or neither; this is neither, "
    "and it unblocks when ``project_brief`` gets an envelope."
)
_C11_SCHEMA_ONLY = (
    "C11 (plan §5) declared the COLUMN here and deliberately stopped. The "
    "two-condition rename rule needs BOTH a ``project_id`` column and a caller "
    "that HOLDS an identity to pass; condition 1 is now met and condition 2 is "
    "not, so re-keying the predicate would silently match zero rows and raise "
    "nothing. ``runtime_config``'s callers thread a real filesystem path from "
    "the host client and the CLI, and ``list_config_rows``' unfiltered arm is "
    "the G2 boot WARMUP — re-key the reads with old rows unreachable and every "
    "knob in the corpus silently reverts to its default. Needs the knob train "
    "(plan §6), not a mechanical rename."
)
# ── C9c's re-reasoning of the former ``_C9B`` bucket ─────────────────────────
# C9a deferred 13 signatures to C9b with the reason "C9a's territory is _shared
# only". That reason DIED with C9c, whose whole remit was those 13 end-to-end.
# C9c swept ONE (``get_memories_by_store_type``) and found the other twelve
# blocked for four DISTINCT reasons that the single ``_C9B`` string hid. They are
# split out below so C11/C15 inherit the real blocker per signature rather than a
# reason that stopped being true.

_C9C_PARAM_COLLISION = (
    "NOT A RENAME — ``project_id`` is ALREADY a parameter of this function "
    "(``*, project_id: str = ''``), threaded from the core tool shell since C5b. "
    "The name is taken, so renaming ``directory`` onto it is a two-parameter "
    "MERGE: one selects rows (``WHERE directory_context = $dir``) and one stamps "
    "them. Collapsing them is a redesign with an owner (C11), not a mechanical "
    "rename, and C9c must not pre-empt it."
)
_C9C_C10_CALLER = (
    "C10-owned caller — the table does carry ``project_id`` and the re-key is "
    "otherwise safe, but every non-test caller lives in C10's tree (``core/**``, "
    "``backend/restoration/**``, ``backend/admin_exec/adr_seed.py``), which was "
    "mid-flight during C9c. The rename and the WHERE re-key must land WITH the "
    "callers or the query silently matches nothing. Reconciled at merge: C10 is "
    "deferring its own identity rename to C11 because two of ``restore()``'s "
    "other sinks (``checkpoint``, ``memory_block``) have no ``project_id`` "
    "column, so ``restore()`` must pass project_id to the memory sinks and "
    "directory to the checkpoint/block sinks until C11 adds them."
)
_C9C_SEMANTIC_SPLIT = (
    "SEMANTIC SPLIT, not a rename — this one function is called with two "
    "incompatible kinds of value. ``backend/admin_exec/staleness.py`` passes "
    "``str(Path(filepath).parent)``, a CHANGED FILE's parent directory, which is "
    "never a project identity; the predictive_coding and narrative callers pass "
    "a real resolved project_id. Re-keying the WHERE onto ``project_id`` would "
    "silently empty the staleness arm (its heat-halving would stop firing and "
    "nothing would raise). Splitting the function is C11's shape."
)
_C9C_COUPLED_PASSTHROUGH = (
    "COUPLED PASS-THROUGH — this function does not query anything itself; it "
    "forwards its argument into a function that is still keyed on "
    "``directory_context`` (blocked above). Renaming the parameter here while "
    "the downstream predicate is untouched is precisely the caller-facing lie "
    "C9a's rule forbids: the caller passes ``owner/repo``, the query matches "
    "zero rows, nothing raises. Unblocks when its callee does."
)
# ``_NO_OWNER`` is GONE. It read: "GAP — ``runtime_config`` carries its own
# ``directory`` COLUMN and is absent from plan §5 C11's four-table list
# (memory_block / episode / action_log / queue). It has no ``project_id`` and no
# owning car." C11 ADOPTED it: migration 033 declares ``runtime_config.project_id``
# (the plan's fourth table, ``queue``, does not exist — the cited site is inside
# ``insert_action_log``'s docstring and the queue is file-backed, so there was a
# free slot and a real table to put in it). The six signatures below are no longer
# ownerless; they carry ``_C11_SCHEMA_ONLY``, which states what is and is not done.

#: ``<path relative to yadgar/_shared>::<function name>`` → reason.
#: Every entry is a deliberate exclusion; none is "not got to yet".
_ALLOWLIST: dict[str, str] = {
    # ── carve-out 3 — real filesystem paths ──────────────────────────────────
    "file_queue/queue.py::_find_terminal": _CARVE_3,
    # ``transcript_parse.py::_list_worktrees`` WAS here as carve-out 3. C10 took
    # judgement site (b) and renamed its parameter to ``worktree_path`` — the
    # carve-out is now expressed in the name, so the entry is stale and
    # Direction 2 rejects it.
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
    # ── plan §5 C10 judgement sites — ALL THREE DISCHARGED, entries removed ──
    # C9a deferred these to C10; C10 landed them (site (a) rules_engine, site
    # (b) transcript_parse). Direction 2 hard-fails on an allowlist entry whose
    # residue is gone, so the entries had to go WITH the sweep. This is the
    # cross-car lint coupling C9c named: a sweep can strand a sibling car's
    # allowlist, and neither car can see the other's tree. Checked at merge.
    # ── plan §5 C11 — SHIPPED; these are the transitional legacy keys ────────
    # NOT "not got to yet". Migration 033 declared the columns and the writers
    # and readers below carry ``project_id`` now; the ``directory`` half is the
    # second arm that keeps the un-backfilled historical corpus reachable.
    "sensory_buffer/sensory_buffer.py::capture": _C11_LEGACY_COLUMN_WRITE,
    "sensory_buffer/sensory_buffer.py::capture_action": _C11_LEGACY_COLUMN_WRITE,
    "storage/queue.py::insert_action_log": _C11_LEGACY_COLUMN_WRITE,
    "storage/ops.py::get_active_checkpoint": _C11_DUAL_KEY,
    "storage/narrative.py::get_narratives_for_directory": _C11_DUAL_KEY,
    "storage/blocks.py::_canonical_dir": _C11_LEGACY_COLUMN_WRITE,
    "storage/blocks.py::_block_project_clause": _C11_DUAL_KEY,
    "storage/blocks.py::_count_blocks_in_scope": _C11_DUAL_KEY,
    "storage/blocks.py::create_block": _C11_LEGACY_COLUMN_WRITE,
    "storage/blocks.py::get_block": _C11_DUAL_KEY,
    "storage/blocks.py::update_block": _C11_DUAL_KEY,
    "storage/blocks.py::delete_block": _C11_DUAL_KEY,
    "storage/blocks.py::list_blocks": _C11_DUAL_KEY,
    "storage/blocks.py::replace_block": _C11_DUAL_KEY,
    "storage/blocks.py::append_block": _C11_DUAL_KEY,
    # ── the GAP C9a reported upward: C11 shipped the SCHEMA, not the re-key ──
    "storage/runtime_config.py::_canonical_config_dir": _C11_SCHEMA_ONLY,
    "storage/runtime_config.py::_config_dir_clause": _C11_SCHEMA_ONLY,
    "storage/runtime_config.py::set_config_row": _C11_SCHEMA_ONLY,
    "storage/runtime_config.py::get_config_row": _C11_SCHEMA_ONLY,
    "storage/runtime_config.py::list_config_rows": _C11_SCHEMA_ONLY,
    "storage/runtime_config.py::delete_config_row": _C11_SCHEMA_ONLY,
    # ── formerly "C9b-coupled"; re-reasoned by C9c, which swept ONE of the 13 ─
    # ``storage/memory.py::get_memories_by_store_type`` is GONE from this dict:
    # C9c renamed it AND re-keyed its WHERE onto ``build_project_scope_clause``.
    # It is pinned in ``_SWEPT`` below instead.
    # ``storage/memory.py::get_memories_for_directory`` and
    # ``::get_anchored_memories_scoped`` are GONE from this dict: C10g renamed
    # both onto ``project_id`` together with the writers and callers that made
    # them safe to move. Pinned in ``_SWEPT`` below instead.
    # C11 looked at this one and DEFERRED it deliberately — the reason is no
    # longer "C10's callers are mid-flight" (they landed), it is the error
    # envelope. Both callers (``core/server/tools/admin_other.py::recent_memories``
    # and ``core/server/tools/project.py::project_brief._build_recent_writes``)
    # would have to resolve a project to pass one, and ``resolve_effective_project``
    # RAISES. ``project_brief`` has no error envelope and runs on the
    # session-start hook path, so promoting this trades one empty bucket for a
    # hard failure on every session start. The rule is rename-with-callers or
    # neither; giving project_brief an envelope first is a separate change with
    # its own blast radius, so this car chose neither and said so.
    "storage/memory.py::get_recent_memories_since": _C11_DEFERRED_NEEDS_ENVELOPE,
    "storage/wiki.py::list_wiki_pages": _C9C_C10_CALLER,
    "storage/wiki.py::list_wiki_catalog": _C9C_C10_CALLER,
    "wiki/store.py::list_pages": _C9C_C10_CALLER,
    "storage/wiki.py::upsert_project_init": _C9C_PARAM_COLLISION,
    "storage/wiki.py::upsert_active_work": _C9C_PARAM_COLLISION,
    "storage/wiki.py::upsert_dispatch_prelude_marker": _C9C_PARAM_COLLISION,
    # ``metacognition/gap_detection.py::detect_gaps`` is GONE from this dict:
    # its callee (``get_memories_for_directory``) moved in C10g, which is the
    # exact condition ``_C9C_COUPLED_PASSTHROUGH`` said would unblock it.
    "wiki/store.py::_autolink_title_map": _C9C_COUPLED_PASSTHROUGH,
    "wiki/store.py::autolink": _C9C_COUPLED_PASSTHROUGH,
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
    # C9c — the only one of C9a's 13 deferred signatures that was clean
    # end-to-end. Unlike the five above (whose arguments are dead or
    # presentation-only), this one is a LIVE predicate: the rename landed
    # together with its WHERE re-key onto ``build_project_scope_clause`` and
    # both of its call sites in ``backend/cls_store/clustering.py``.
    "storage/memory.py::get_memories_by_store_type",
    # C10g — restore's two memory-backed sinks plus the pass-through that feeds
    # one of them. Each moved together with the half that made it safe:
    # ``get_memories_for_directory`` with the C10f memorize stamp already in
    # the tree, ``get_anchored_memories_scoped`` with ``anchor_memory``'s stamp
    # in the SAME commit, and ``detect_gaps`` with its callee. The PREDICATES
    # deliberately stay on ``directory_context`` — that column is where both
    # writers now put the identity; moving onto the ``project_id`` column is
    # C11's table work.
    "storage/memory.py::get_memories_for_directory",
    "storage/memory.py::get_anchored_memories_scoped",
    "metacognition/gap_detection.py::detect_gaps",
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
