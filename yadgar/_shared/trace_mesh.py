"""Pure trace-mesh aggregation — the simplify_trace pipeline, endpoint-ready.

This is the PURE logic half of ``docs/diagrams/simplify_trace.py``: reconstruct
a span tree from a flat, start-ordered Tempo capture, collapse plumbing wrappers,
aggregate span storms (``>= STORM_MIN`` identical siblings → one box), assign
core/backend lanes, and select ``<= MAX_BOXES`` MAJOR pipeline stages. No
DiagramSpec / DOT / render coupling — ``simplify_trace.py`` consumes these names,
and the viz ``/api/traces/{id}/mesh`` endpoint calls :func:`build_mesh` to produce
the fixed-lane replay payload.

Input span shape (from ``docs/diagrams/capture_trace.py`` /
``core/server/routes/traces.py``)::

    {"rel_ms": float, "dur_ms": float, "depth": int, "svc": str, "name": str}

Output of :func:`build_mesh`::

    {
      "nodes": [{"id","label","svc","lane","rel_ms","dur_ms","storm_n","error","type"}...],
      "edges": [{"src","dst","order"}...],
      "timeline_ms": float,        # trace wall time
      "tool": str,                 # boundary tool span name (or "" if dropped)
      "dropped_boundary": bool,    # audit_anchors-class flat forest
    }

I33 (ADR-0074 span budget): the top-level pipeline fns carry a real ``@observe``
stage span (once per trace). The per-span INNER helpers (``_keep``, ``_contains``,
``_merge_repeats``, …) run once per candidate span while parsing a trace that is
ITSELF often a span-storm — so a per-call span would recreate a span explosion
inside the daemon. They carry ``@observe(tier="hot", span=False)`` (no per-call
span, no per-call metric) and are listed in ``.observe-allowlist.json`` ``_span_budget``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from yadgar._shared.observability.observe import observe

# --------------------------------------------------------------------------- #
# Tuning constants (verbatim from simplify_trace.py)
# --------------------------------------------------------------------------- #

MAX_BOXES = 20  # total stage boxes incl. entry + terminal
STORM_MIN = 4  # >=N identical siblings -> aggregate into one box
PUSH_COVER = 0.55  # children must explain >=55% of a span to replace it
KEEP_FLOOR_MS = 10.0  # absolute keep threshold cap ("or >=10ms" rule)

# Plumbing wrappers: never drawn as their own box (unless they cross a lane);
# their time folds into the nearest semantic ancestor.
PLUMBING = re.compile(
    r"(POST /mcp|^POST$|http (send|receive)$|\._q$|\._q_server$|_q_multi_server$"
    r"|_q_with_timeout$|_observe_query_metrics$|_normalize_rows$|_row_to_dict$"
    r"|_extract_id$|run_offloaded$|_ctx_wrap$|_ensure_pool$|_inc_inflight$"
    r"|resolve_knob$|_ring_append$|_get_file_queue$|\.lifecycle\."
    r"|ScopeVersions\.version$|cache\.Cache\.(get|put)$|_estimate_bytes$)"
)

# Storage/cache layer: may appear as boxes, but a SEMANTIC pipeline stage is
# never replaced by its storage-level children.
LOWLEVEL = re.compile(
    r"(\.storage\.|\.cache\.|server_helpers|_shared\.engram|graph_helpers"
    r"|file_queue|\.client\.)"
)

MODEL_RE = re.compile(
    r"embed|rerank|cross_encoder|encode|multi_passage|score_candidates"
    r"|cosine|predict|astrocyte|consensus"
)
IO_RE = re.compile(
    r"^POST|storage\.|_q_server|queue|enqueue|fetch|\bwiki\.|get_wiki"
    r"|\.candidates$|spreading|vector|fts"
)

# Friendly human-readable labels for every span that appears as a kept box.
# Keyed on the `.short` form (last 1-2 dotted segments) — reorg-stable.
ALIASES: dict[str, str] = {
    # ── retrieval pipeline ──────────────────────────────────────────────────
    "_run_fts_bm25": "FTS BM25 keyword search",
    "_collect_vector_scores": "vector KNN search",
    "Retriever.ppr_retrieve": "personalized PageRank",
    "Retriever.spreading_activation": "spreading activation BFS",
    "_search_profiles_and_beliefs": "profile & belief search",
    "_fuse_scores": "convex score fusion",
    "_inject_ce_diversity": "cross-encoder diversity inject",
    "_rerank_engram_links": "engram-link rerank",
    "mmr_rerank": "MMR diversity rerank",
    "LocalMLClient._try_gte_reranker": "cross-encoder rerank (GTE)",
    "RulesEngine.apply_rules": "rules engine — apply rules",
    # ── wiki retrieval ───────────────────────────────────────────────────────
    "WikiStore._collect_wiki_fts_scores": "wiki FTS BM25",
    "WikiStore._collect_wiki_vector_scores": "wiki vector KNN",
    "EmbeddingEngine.encode": "embed query",
    "search_wiki_vectors": "wiki KNN (storage)",
    # ── storage reads ────────────────────────────────────────────────────────
    "get_memories_by_ids": "fetch memories by ID",
    "get_recent_memories_since": "fetch recent memories",
    "get_anchored_memories_scoped": "fetch anchored memories",
    "boost_memories_access": "boost heat on results",
    "MemoryThermodynamics.record_access": "record access heat",
    "get_wiki_page": "fetch wiki page",
    "list_wiki_catalog": "list wiki catalog",
    "get_active_checkpoint": "fetch active checkpoint",
    "get_all_causal_edges": "fetch causal DAG",
    "EngramAllocator.get_slot_statistics": "engram slot statistics",
    # ── cognitive map / restore ──────────────────────────────────────────────
    "CognitiveMap.build_transition_matrix": "build SR transition matrix",
    "CognitiveMap.has_sufficient_data": "check SR data sufficiency",
    "CheckpointRestore._fetch_hot_memories": "fetch hot memories (restore)",
    "CheckpointRestore._fetch_recent_memories_safe": "fetch recent memories (restore)",
    "CheckpointRestore._predict_memories": "SR predictive recall",
    # ── write pipeline / validation ──────────────────────────────────────────
    "phase_validate": "validate memorize inputs",
    "_validate_content_and_provenance": "validate content & provenance",
    "_validate_gate_and_policy": "gate & write-policy check",
    "gate_or_reject": "secret gate check",
    "is_allowlisted": "allowlist check",
    "RulesEngine.check_write_policy": "write-policy rule check",
    "_enqueue": "enqueue memorize job",
    "FileQueue.enqueue": "file-queue enqueue",
    # ── project_brief pipeline ───────────────────────────────────────────────
    "_fetch_presence_rows": "fetch init/active/checkpoint presence",
    "_build_wiki_pages": "build wiki-pages section",
    "_build_hot_memories": "build hot-memories section",
    "_build_anchor_rows_catalog": "build anchor catalog",
    "_scan_stale_wiki_slugs": "scan stale wiki slugs",
    "_slug_prefix": "compute slug prefix",
    "_current_epoch": "epoch cache hit",
    # ── branch / identity helpers ────────────────────────────────────────────
    "_detect_branch": "detect git branch",
    "_detect_branch_cached": "detect git branch (cached)",
    "_get_default_branch_cached": "detect default branch (cached)",
    # ── anchor / checkpoint validation ───────────────────────────────────────
    "_validate_anchor_inputs": "validate anchor inputs",
    "_validate_checkpoint_surrogates": "validate checkpoint fields",
    # ── rules & misc ─────────────────────────────────────────────────────────
    "RulesEngine.get_applicable_rules": "fetch applicable rules",
    "_get_file_queue": "get DLQ file-queue handle",
    # ── forward / admin boundary ─────────────────────────────────────────────
    "recall": "Recall",
    "_forward_to_backend": "forward to backend",
    "_forward_admin": "forward to backend /admin",
    "POST": "HTTP POST to backend",
    "GET /admin/dbsize": "backend DB-size query",
    # ── admin tools / invariant checks ───────────────────────────────────────
    "_count_q": "DB count query",
    "_check_per_table_size": "check per-table size",
    "_check_relationships": "check entity relationships",
    "_check_memory_entity_orphans": "check memory/entity orphans",
    "_check_memory_similarity_link": "check memory similarity links",
    "_check_memory_transition": "check memory transitions",
    "_check_engram_slot_distribution": "check engram slot distribution",
    "_check_wiki_crossref": "check wiki cross-references",
    "_ms_per_table_stats": "per-table row/byte stats",
    # ── audit_anchors pipeline ───────────────────────────────────────────────
    "_fetch_expired_rows": "fetch expired anchors",
    "_fetch_grace_expired_rows": "fetch grace-period anchors",
    "_fetch_cross_project_anchor_pool": "fetch cross-project anchor pool",
    "_fetch_promote_rows": "fetch promote candidates",
    "_fetch_redundant_pairs": "fetch redundant anchor pairs",
    "_cosine_similarity": "cosine similarity",
    # ── backend writes ───────────────────────────────────────────────────────
    "embed": "embed & store (backend)",
    "block_create": "create memory block",
    "bookmark_add": "add bookmark",
    "update_active_work": "update active work",
    "WikiStore.delete": "delete wiki page",
    # ── storage plumbing (opaque-box survivors) ───────────────────────────────
    "_q": "SurrealDB query",
}


@observe(tier="hot", span=False)
def _humanize(name: str) -> str:
    """Fallback label for spans not in ALIASES.

    Strips module path, splits snake_case and CamelCase, title-cases result.
    """
    tail = name.rsplit(".", 1)[-1].lstrip("_")
    tail = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tail)  # split CamelCase
    tail = tail.replace("_", " ")  # split snake_case
    return tail.strip().title() or name


@observe(tier="hot", span=False)
def _friendly(s: Span) -> str:
    """Return the friendly display label for a span (alias or humanized fallback)."""
    return ALIASES.get(s.short, _humanize(s.name))


# --------------------------------------------------------------------------- #
# Span tree
# --------------------------------------------------------------------------- #


@dataclass
class Span:
    name: str
    rel: float
    dur: float
    svc: str
    depth: int
    children: list[Span] = field(default_factory=list)
    parent: Span | None = None
    count: int = 1  # aggregate fields (storm boxes)
    note: str = ""  # display hint: semantic owner for opaque plumbing boxes

    @property
    def short(self) -> str:
        parts = self.name.split(".")
        if len(parts) >= 2 and parts[-2][:1].isupper():
            return ".".join(parts[-2:])
        return parts[-1]

    @property
    def is_plumbing(self) -> bool:
        return bool(PLUMBING.search(self.name))


@observe(tier="hot", span=False)
def _contains(p: Span, s: Span) -> bool:
    """START-containment with jitter tolerance.

    Only the child's start must fall inside the parent window: a child may
    OUTLIVE its parent (async work, or a backend span running on past the core
    client's timeout).
    """
    slack = max(0.5, 0.01 * p.dur)
    return s.rel >= p.rel - 0.1 and s.rel <= (p.rel + p.dur) + slack


@observe(tier="stage")
def build_tree(spans: list[dict]) -> Span:
    """Reconstruct the tree from start-ordered spans.

    Parent = most recent span at depth-1 whose time window CONTAINS the child.
    """
    root = Span(name="<root>", rel=0.0, dur=0.0, svc="yadgar-core", depth=-1)
    by_depth: dict[int, list[Span]] = {}
    for s in spans:
        node = Span(
            name=s["name"],
            rel=s["rel_ms"],
            dur=s["dur_ms"],
            svc=s["svc"],
            depth=s["depth"],
        )
        parent = root
        for cand in reversed(by_depth.get(node.depth - 1, [])):
            if cand.rel > node.rel:
                continue
            if _contains(cand, node):
                parent = cand
                break
        node.parent = parent
        parent.children.append(node)
        by_depth.setdefault(node.depth, []).append(node)
    return root


@observe(tier="hot", span=False)
def aggregate_storms(node: Span) -> list[Span]:
    """Group >=STORM_MIN identical-name siblings into one aggregate span."""
    by_name: dict[str, list[Span]] = {}
    for c in node.children:
        by_name.setdefault(c.name, []).append(c)
    out: list[Span] = []
    seen: set[str] = set()
    for c in node.children:
        if c.name in seen:
            continue
        group = by_name[c.name]
        if len(group) >= STORM_MIN:
            agg = Span(
                name=c.name,
                rel=min(g.rel for g in group),
                dur=sum(g.dur for g in group),
                svc=c.svc,
                depth=c.depth,
                parent=node,
                count=len(group),
            )
            out.append(agg)
            seen.add(c.name)
        else:
            out.extend(group)
            seen.add(c.name)
    out.sort(key=lambda s: s.rel)
    return out


# --------------------------------------------------------------------------- #
# Stage selection (the simplification)
# --------------------------------------------------------------------------- #


@observe(tier="hot", span=False)
def crossing(s: Span) -> bool:
    return s.parent is not None and s.parent.depth >= 0 and s.svc != s.parent.svc


@observe(tier="hot", span=False)
def subtree_has(s: Span, pred) -> bool:
    if pred(s):
        return True
    return any(subtree_has(c, pred) for c in s.children)


@observe(tier="hot", span=False)
def _is_error(s: Span, error_names: list[str]) -> bool:
    return any(s.name.endswith(e) for e in error_names)


@observe(tier="hot", span=False)
def _keep(s: Span, thr: float, error_names: list[str]) -> bool:
    if s.count >= STORM_MIN:
        return s.dur >= thr
    return (
        s.dur >= thr
        or crossing(s)
        or _is_error(s, error_names)
        or subtree_has(s, lambda x: crossing(x) or _is_error(x, error_names))
    )


@observe(tier="hot", span=False)
def _show_opaque(k: Span, own: Span | None, opaque_thr: float, total: float) -> bool:
    if k.dur < opaque_thr:
        return False
    return (
        own is None
        or own.depth < 0
        or k.dur > own.dur  # outlives its semantic owner
        or k.dur >= 0.25 * total  # or IS the wall outright
    )


@observe(tier="hot", span=False)
def _kept_children(
    s: Span,
    owner: Span | None,
    thr: float,
    opaque_thr: float,
    total: float,
    error_names: list[str],
) -> list[Span]:
    """Kept children of s, expanded THROUGH plumbing wrappers."""
    own = s if not s.is_plumbing else owner
    out: list[Span] = []
    for k in aggregate_storms(s):
        if not _keep(k, thr, error_names):
            continue
        if not (k.is_plumbing and not crossing(k) and k.count < STORM_MIN):
            out.append(k)
            continue
        sub = _kept_children(k, own, thr, opaque_thr, total, error_names)
        if sub:
            out.extend(sub)
        elif _show_opaque(k, own, opaque_thr, total):
            if own is not None and own.depth >= 0:
                k.note = f"in {own.short}"
            out.append(k)
    return out


@observe(tier="hot", span=False)
def _forced(kids: list[Span], opaque_thr: float, error_names: list[str]) -> bool:
    return any(
        crossing(k)
        or _is_error(k, error_names)
        or (k.count >= 50 and k.dur >= opaque_thr)  # big storm: own box
        or subtree_has(k, lambda x: crossing(x) or _is_error(x, error_names))
        for k in kids
    )


@observe(tier="hot", span=False)
def _phase_box(s: Span, cover: float, thr: float, error_names: list[str]) -> Span | None:
    """Parent kept as a phase box when its own (self) work is big,
    or ALWAYS for a known-error stage (must stay visible in red)."""
    self_ms = s.dur - cover
    if _is_error(s, error_names) or (self_ms >= max(thr, 0.15 * s.dur) and not s.is_plumbing):
        return Span(
            name=s.name,
            rel=s.rel,
            dur=self_ms if not _is_error(s, error_names) else s.dur,
            svc=s.svc,
            depth=s.depth,
            parent=s.parent,
        )
    return None


@observe(tier="hot", span=False)
def _frontier(
    s: Span, thr: float, opaque_thr: float, total: float, error_names: list[str]
) -> list[Span]:
    """Return the stage boxes for subtree s (s itself is already 'kept')."""
    if s.count >= STORM_MIN:
        return [s]
    kids = _kept_children(s, None, thr, opaque_thr, total, error_names)
    if not kids:
        return [s]
    forced = _forced(kids, opaque_thr, error_names)
    semantic_kids = [k for k in kids if not LOWLEVEL.search(k.name)]
    if not semantic_kids and not LOWLEVEL.search(s.name) and not forced:
        return [s]
    cover = sum(k.dur for k in kids)
    if cover >= PUSH_COVER * s.dur or forced:
        out: list[Span] = []
        phase = _phase_box(s, cover, thr, error_names)
        if phase is not None:
            out.append(phase)
        for k in kids:
            out.extend(_frontier(k, thr, opaque_thr, total, error_names))
        return out
    return [s]


@observe(tier="hot", span=False)
def _merge_repeats(stages: list[Span], error_names: list[str]) -> list[Span]:
    """Merge scattered same-name repeats under different parents."""
    by_name: dict[tuple[str, str], list[Span]] = {}
    for s in stages:
        by_name.setdefault((s.name, s.svc), []).append(s)
    for (name, svc), group in by_name.items():
        # threshold 4, not 3: recall's THREE cross-encoder passes stay separate.
        if len(group) >= 4 and not any(crossing(g) or _is_error(g, error_names) for g in group):
            merged = Span(
                name=name,
                rel=min(g.rel for g in group),
                dur=sum(g.dur for g in group),
                svc=svc,
                depth=group[0].depth,
                parent=group[0].parent,
                count=sum(g.count for g in group),
            )
            stages = [s for s in stages if s not in group]
            stages.append(merged)
    return stages


@observe(tier="hot", span=False)
def _fallback_stages(tool: Span) -> list[Span]:
    """All-micro trace: show the top few children by duration anyway."""
    cands = sorted(
        (k for k in aggregate_storms(tool) if not k.is_plumbing),
        key=lambda s: -s.dur,
    ) or sorted(aggregate_storms(tool), key=lambda s: -s.dur)
    return cands[:4]


@observe(tier="hot", span=False)
def _enforce_cap(stages: list[Span], error_names: list[str]) -> list[Span]:
    """Cap: drop smallest non-mandatory stages until we fit."""

    def mandatory(s: Span) -> bool:
        return crossing(s) or s.count >= STORM_MIN or _is_error(s, error_names)

    budget = MAX_BOXES - 2  # entry + terminal
    while len(stages) > budget:
        droppable = [s for s in stages if not mandatory(s)]
        if not droppable:
            break
        stages.remove(min(droppable, key=lambda s: s.dur))
    return stages


@observe(tier="stage")
def select_stages(tool: Span, total: float, error_names: list[str]) -> list[Span]:
    """Select the <=MAX_BOXES MAJOR pipeline stages for a tool span subtree."""
    thr = min(KEEP_FLOOR_MS, 0.01 * total)
    opaque_thr = max(thr, 0.05 * total)  # opaque plumbing survives only if huge

    stages: list[Span] = []
    for k in _kept_children(tool, None, thr, opaque_thr, total, error_names):
        stages.extend(_frontier(k, thr, opaque_thr, total, error_names))

    stages = _merge_repeats(stages, error_names)

    if not stages:
        stages = _fallback_stages(tool)
    stages.sort(key=lambda s: s.rel)

    return _enforce_cap(stages, error_names)


@observe(tier="hot", span=False)
def find_tool_span(root: Span, tool_base: str) -> Span | None:
    """Find the boundary tool span (e.g. tool.recall) in the tree, or None.

    Iterative pre-order walk (no nested closure — keeps the I33 lint surface flat).
    """
    stack: list[Span] = [root]
    while stack:
        node = stack.pop()
        if node.name == tool_base:
            return node
        # push children in reverse so the leftmost is visited first (pre-order)
        stack.extend(reversed(node.children))
    return None


# --------------------------------------------------------------------------- #
# Lane + node typing
# --------------------------------------------------------------------------- #


@observe(tier="hot", span=False)
def _lane(svc: str) -> str:
    return "backend" if svc == "yadgar-backend" else "core"


@observe(tier="hot", span=False)
def _find_forwarder(tool: Span) -> Span | None:
    """The core-svc hand-off span: the DEEPEST core-lane descendant of ``tool``
    that has a lane-crossing (backend) descendant — i.e. the span that actually
    forwards to the backend. Returns None when the tool is core-only (no crossing)
    or the crossing is directly off the tool span itself.

    Pure. The returned span is core-svc so ``_lane`` places it in the core lane;
    it is distinct from the crossing span itself (which is backend-svc → backend lane).
    """
    best: Span | None = None
    best_depth = -1
    stack: list[Span] = list(tool.children)
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if _lane(node.svc) != "core":
            continue
        # core span that owns a backend crossing somewhere below it
        if any(_lane(c.svc) == "backend" for c in node.children) or subtree_has(
            node, lambda x: _lane(x.svc) == "backend"
        ):
            if node.depth > best_depth:
                best, best_depth = node, node.depth
    return best


@observe(tier="hot", span=False)
def core_boundary_stages(tool: Span) -> list[Span]:
    """Return the 0-2 synthetic CORE-lane lead nodes for a forward-only tool trace.

    In a forward-only recall the heavy pipeline all carries ``svc=yadgar-backend``
    (recall runs in the backend process), so the tool's *descendants* are all
    backend-lane and the core lane renders empty. But the boundary tool span itself
    and the core->backend forwarder genuinely exist core-side — surface them:

    1. **Boundary** — the tool span (e.g. ``tool.recall``) as a core node, dur =
       core self-time (tool.dur - core children dur), floored so the dwell is visible.
    2. **Forwarder** — the core-svc hand-off span (``_find_forwarder``), if present.

    Skipped entirely when the tool span is backend-svc (no phantom core node).
    Pure — returned Spans get laned by ``_lane(svc)`` like any other node.
    """
    if _lane(tool.svc) != "core":
        return []
    core_child_dur = sum(c.dur for c in tool.children if _lane(c.svc) == "core")
    self_ms = max(0.5, tool.dur - core_child_dur)
    boundary = Span(
        name=tool.name,
        rel=tool.rel,
        dur=self_ms,
        svc=tool.svc,
        depth=tool.depth,
        parent=tool.parent,
    )
    out = [boundary]
    fwd = _find_forwarder(tool)
    if fwd is not None:
        out.append(
            Span(
                name=fwd.name,
                rel=fwd.rel,
                dur=fwd.dur,
                svc=fwd.svc,
                depth=fwd.depth,
                parent=fwd.parent,
            )
        )
    return out


@observe(tier="hot", span=False)
def _node_type(s: Span, error_names: list[str]) -> str:
    if any(s.name.endswith(e) for e in error_names):
        return "error"
    if MODEL_RE.search(s.name):
        return "model"
    if IO_RE.search(s.name):
        return "io"
    return "compute"


# --------------------------------------------------------------------------- #
# Mesh payload builder (the /api/traces/{id}/mesh producer)
# --------------------------------------------------------------------------- #


@observe(tier="stage")
def build_mesh(data: dict) -> dict:
    """Build the fixed-lane replay mesh payload from a flat span-list capture.

    ``data`` is the ``capture_trace``-shaped dict:
      {label, tool_span, trace_id, total_ms, span_count, spans:[...]}.

    Returns {nodes, edges, timeline_ms, tool, dropped_boundary, trace_id, label}.
    Never raises on an empty / malformed span list — returns an empty mesh.
    """
    spans = data.get("spans") or []
    total = float(data.get("total_ms") or 0.0)
    tool_base = str(data.get("tool_span") or "").split(" ")[0]
    error_names: list[str] = list(data.get("error_spans") or [])

    if not spans:
        return {
            "nodes": [],
            "edges": [],
            "timeline_ms": total,
            "tool": tool_base,
            "dropped_boundary": False,
            "trace_id": data.get("trace_id", ""),
            "label": data.get("label", ""),
        }

    root = build_tree(spans)
    tool = find_tool_span(root, tool_base)
    dropped_boundary = tool is None
    if tool is None:
        # audit_anchors-class: boundary span dropped -> flat forest.
        tool = root
        tool.dur = total
        tool.svc = "yadgar-core"

    stages = select_stages(tool, total, error_names)

    # Item-1: forward-only tools (recall etc.) run their whole pipeline in the
    # backend process, so select_stages — which returns the tool's DESCENDANTS —
    # yields zero core-lane nodes. Prepend the real core-side boundary + forwarder
    # so the core lane is never empty. Skipped on the dropped-boundary flat forest
    # (tool is root — no genuine tool span to promote).
    if not dropped_boundary:
        lead = core_boundary_stages(tool)
        selected_names = {s.name for s in stages}
        lead = [s for s in lead if s.name not in selected_names]
        stages = lead + stages

    nodes: list[dict] = []
    for i, s in enumerate(stages):
        nodes.append(
            {
                "id": f"s{i}",
                "label": _friendly(s),
                "name": s.name,
                "svc": s.svc,
                "lane": _lane(s.svc),
                "rel_ms": round(s.rel, 2),
                "dur_ms": round(s.dur, 2),
                "storm_n": s.count if s.count > 1 else None,
                "error": bool(_is_error(s, error_names)),
                "type": _node_type(s, error_names),
                "note": s.note or None,
            }
        )

    edges = [{"src": f"s{i}", "dst": f"s{i + 1}", "order": i + 1} for i in range(len(stages) - 1)]

    return {
        "nodes": nodes,
        "edges": edges,
        "timeline_ms": total,
        "tool": tool_base,
        "dropped_boundary": dropped_boundary,
        "trace_id": data.get("trace_id", ""),
        "label": data.get("label", ""),
    }
