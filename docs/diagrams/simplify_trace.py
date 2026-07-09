#!/usr/bin/env python3
"""Simplified two-lane trace diagrams from capture_trace.py span JSONs.

Companion to `trace_to_boxes.py` (which keeps EVERY distinct stage — the
detailed `mcp-traces/*.svg` view). This tool produces the SIMPLIFIED view in
the style of `out/recall-cold-trace-2026-07-04.png`: two lanes (blue CORE
left, orange BACKEND right), rounded boxes for MAJOR pipeline stages only,
numbered arrows following the data flow, title carrying tool + trace id +
total + the key-cost callout.

Simplification rules
--------------------
* keep: the tool boundary (entry box), stages >= 1% of total_ms (capped at a
  10 ms absolute keep-floor), every lane-crossing hop (the forward POSTs),
  and a terminal "result returned" box;
* collapse: micro-span runs and plumbing wrappers (`_q`/`_q_server`/`POST`
  http chains, offload scaffolding) fold into their nearest semantic
  ancestor's wall time;
* aggregate storms: >= 4 identical sibling spans become ONE box
  ("name xN, total"); the 42k `_cosine_similarity` explosion is one box;
* cap: at most MAX_BOXES boxes per diagram (smallest non-mandatory stages
  dropped; lane-crossings / storms / error stages are never dropped).

Rendering reuses generate.py's DiagramSpec -> DOT pipeline (same palette as
the hand-authored reference), so no new dependency and no change to
generate.py behavior. A custom "error" node style is injected at runtime for
known-failing stages (e.g. restore's `_predict_memories`).

Usage:
    python docs/diagrams/simplify_trace.py out/recall-cold.json [more.json...]
    python docs/diagrams/simplify_trace.py --all          # every out/*.json
    python docs/diagrams/simplify_trace.py --all --stats  # box counts only

Outputs: out/<label>-trace-2026-07-09.{dot,svg,png}
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from generate import (  # noqa: E402
    NODE_STYLES,
    Cluster,
    DiagramSpec,
    Edge,
    Node,
    _fmt_ms,
    render_dot,
)

OUT_DIR = HERE / "out"
CAPTURE_DATE = "2026-07-09"
RELEASE = "core 5.117 / backend 5.30"

MAX_BOXES = 20  # total boxes incl. entry + terminal
STORM_MIN = 4  # >=N identical siblings -> aggregate into one box
PUSH_COVER = 0.55  # children must explain >=55% of a span to replace it
KEEP_FLOOR_MS = 10.0  # absolute keep threshold cap ("or >=10ms" rule)

# red box for known-failing stages (injected style — generate.py untouched)
NODE_STYLES["error"] = {
    "shape": "box",
    "style": "rounded,filled,bold",
    "fillcolor": "#fbd9d9",
    "color": "#b03030",
    "fontcolor": "#7a1f1f",
}

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
# never replaced by its storage-level children (the reference diagram stops
# at retrieval-stage granularity).
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

# Hand-tuned per-label annotations (subtitle notes + error-stage marking).
# Sourced from mcp-tool-traces-2026-07-09.md.
NOTES: dict[str, dict] = {
    "restore": {
        "note": "ERROR — _predict_memories raises TypeError (task #16), stage marked red",
        "error_spans": ["CheckpointRestore._predict_memories"],
    },
    "check_invariants": {
        "note": "backend /admin runs ~34 s but core client TIMES OUT at 30 s — MCP call always errors",
        "terminal_label": "✖ MCP client timeout at 30 s — tool ERRORS,\\nbackend keeps burning CPU to @34s",
        "terminal_type": "error",
        "opaque_note": "slow SurrealDB invariant scan — THE wall",
    },
    "check_invariants-cold": {
        "note": "backend /admin runs ~34 s but core client TIMES OUT at 30 s — MCP call always errors",
        "terminal_label": "✖ MCP client timeout at 30 s — tool ERRORS,\\nbackend keeps burning CPU to @34s",
        "terminal_type": "error",
        "opaque_note": "slow SurrealDB invariant scan — THE wall",
    },
    "audit_anchors": {
        "note": "boundary span DROPPED (span-queue full) — 42 k cosine explosion runs in CORE",
    },
    "audit_anchors-apply": {
        "note": "boundary span DROPPED (span-queue full) — 42 k cosine explosion runs in CORE",
    },
    "memorize-cold": {
        "note": "R3 enqueue-only boundary — WriteGate/embed/store moved to (dead) drainer"
    },
    "memorize-hot": {
        "note": "same spans as cold: dup-reject no longer at boundary (drainer-side now)"
    },
    "anchor": {"note": "R3 enqueue-only boundary"},
    "anchor-hot": {"note": "R3 enqueue-only boundary"},
    "checkpoint": {"note": "R3 enqueue-only boundary"},
    "checkpoint-hot": {"note": "R3 enqueue-only boundary"},
    "wiki_add": {"note": "enqueue-only; 69 ms = check_write_policy; drainer half absent (dead)"},
    "wiki_add-hot": {"note": "second enqueue — no dup check at boundary"},
    "recall-hot": {
        "note": "repeat query: CE spans ~0 ms (CE cache hit on repeat) — 6x faster than cold"
    },
    "recall-cold": {"note": "CE 3-pass = the wall (cold model)"},
    "project_brief-hot": {"note": "epoch-cache HIT on catalog repeat"},
    "wiki_delete": {"note": "traced the not-found path (probe page never committed)"},
}


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
    # aggregate fields (storm boxes)
    count: int = 1
    # display hint: semantic owner for opaque plumbing boxes ("in <stage>")
    note: str = ""

    @property
    def short(self) -> str:
        parts = self.name.split(".")
        if len(parts) >= 2 and parts[-2][:1].isupper():
            return ".".join(parts[-2:])
        return parts[-1]

    @property
    def is_plumbing(self) -> bool:
        return bool(PLUMBING.search(self.name))


def _contains(p: Span, s: Span) -> bool:
    """START-containment with jitter tolerance.

    Only the child's start must fall inside the parent window: a child may
    OUTLIVE its parent (async work, or check_invariants' backend /admin span
    running on past the core client's 30 s timeout).
    """
    slack = max(0.5, 0.01 * p.dur)
    return s.rel >= p.rel - 0.1 and s.rel <= (p.rel + p.dur) + slack


def build_tree(spans: list[dict]) -> Span:
    """Reconstruct the tree from start-ordered spans.

    Parent = most recent span at depth-1 whose time window CONTAINS the child.
    A plain last-seen-per-depth stack misparents when concurrent subtrees
    interleave (e.g. recall's memory arm vs wiki arm), so containment is
    checked and we scan back through earlier depth-1 spans when needed.
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


def crossing(s: Span) -> bool:
    return s.parent is not None and s.parent.depth >= 0 and s.svc != s.parent.svc


def subtree_has(s: Span, pred) -> bool:
    if pred(s):
        return True
    return any(subtree_has(c, pred) for c in s.children)


def _is_error(s: Span, error_names: list[str]) -> bool:
    return any(s.name.endswith(e) for e in error_names)


def _keep(s: Span, thr: float, error_names: list[str]) -> bool:
    if s.count >= STORM_MIN:
        return s.dur >= thr
    return (
        s.dur >= thr
        or crossing(s)
        or _is_error(s, error_names)
        or subtree_has(s, lambda x: crossing(x) or _is_error(x, error_names))
    )


def _show_opaque(k: Span, own: Span | None, opaque_thr: float, total: float) -> bool:
    if k.dur < opaque_thr:
        return False
    return (
        own is None
        or own.depth < 0
        or k.dur > own.dur  # outlives its semantic owner
        or k.dur >= 0.25 * total  # or IS the wall outright
    )


def _kept_children(
    s: Span,
    owner: Span | None,
    thr: float,
    opaque_thr: float,
    total: float,
    error_names: list[str],
) -> list[Span]:
    """Kept children of s, expanded THROUGH plumbing wrappers.

    A plumbing wrapper with nothing to show beneath it is normally folded
    into its parent, EXCEPT when it is itself enormous (e.g. the 28 s
    SurrealDB query behind check_invariants' timeout death) — dropping
    that would hide the wall. Such opaque boxes carry an "in <stage>"
    note pointing at their nearest semantic owner.
    """
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
            # opaque plumbing worth showing (e.g. the 28 s SurrealDB
            # query behind check_invariants' 30 s timeout) — folding
            # it would under-report the wall
            if own is not None and own.depth >= 0:
                k.note = f"in {own.short}"
            out.append(k)
    return out


def _forced(kids: list[Span], opaque_thr: float, error_names: list[str]) -> bool:
    return any(
        crossing(k)
        or _is_error(k, error_names)
        or (k.count >= 50 and k.dur >= opaque_thr)  # big storm: own box
        or subtree_has(k, lambda x: crossing(x) or _is_error(x, error_names))
        for k in kids
    )


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
    # Never replace a semantic pipeline stage by only storage/cache-level
    # children — the reference stops at stage granularity.
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


def _merge_repeats(stages: list[Span], error_names: list[str]) -> list[Span]:
    """Merge scattered same-name repeats (e.g. check_invariants' 8x _count_q
    under different parents — siblings-only storm aggregation misses them)."""
    by_name: dict[tuple[str, str], list[Span]] = {}
    for s in stages:
        by_name.setdefault((s.name, s.svc), []).append(s)
    for (name, svc), group in by_name.items():
        # threshold 4, not 3: recall's THREE cross-encoder passes are
        # semantically distinct stages and must stay separate (reference style)
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


def _fallback_stages(tool: Span) -> list[Span]:
    # all-micro trace (e.g. get_rules, 5 ms): show the top few children
    # by duration anyway so the diagram still has a pipeline.
    cands = sorted(
        (k for k in aggregate_storms(tool) if not k.is_plumbing),
        key=lambda s: -s.dur,
    ) or sorted(aggregate_storms(tool), key=lambda s: -s.dur)
    return cands[:4]


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


def select_stages(tool: Span, total: float, error_names: list[str]) -> list[Span]:
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


# --------------------------------------------------------------------------- #
# Spec building
# --------------------------------------------------------------------------- #


def _lane(svc: str) -> str:
    return "backend" if svc == "yadgar-backend" else "core"


def _node_type(s: Span, error_names: list[str]) -> str:
    if any(s.name.endswith(e) for e in error_names):
        return "error"
    if MODEL_RE.search(s.name):
        return "model"
    if IO_RE.search(s.name):
        return "io"
    return "compute"


def _fmt_at(rel: float, total: float) -> str:
    return f"@{rel:.1f}ms" if total < 100 else f"@{rel:.0f}ms"


def _pathline(name: str) -> str:
    p = name.removeprefix("yadgar.")
    return ("…" + p[-49:]) if len(p) > 50 else p


def find_tool_span(root: Span, tool_base: str) -> Span | None:
    hits: list[Span] = []

    def walk(s: Span) -> None:
        if s.name == tool_base:
            hits.append(s)
        for c in s.children:
            walk(c)

    walk(root)
    return hits[0] if hits else None


def _entry_node(tool_base: str, dropped_boundary: bool, tool: Span, total: float) -> Node:
    dispatch = (
        f"{tool_base} (boundary span DROPPED)"
        if dropped_boundary
        else f"MCP dispatch → {tool_base}"
    )
    lbl = (
        f"{tool_base.removeprefix('tool.')}() entry\\n"
        + dispatch
        + f"\\n{_fmt_at(0 if dropped_boundary else tool.rel, total)}"
    )
    return Node(
        id="entry",
        label=lbl,
        cluster="core",
        type="compute",
        time_ms=None if dropped_boundary else round(tool.rel, 2),
    )


def _stage_nodes(
    stages: list[Span],
    meta: dict,
    error_names: list[str],
    total: float,
) -> list[Node]:
    nodes: list[Node] = []
    used: set[str] = set()
    for i, s in enumerate(stages):
        nid = f"s{i}"
        used.add(nid)
        head = s.short + (f" ×{s.count}" if s.count > 1 else "")
        if s.note:
            # machine-derived owner attribution can be misled by interleaved
            # spans — allow a hand-tuned per-label override
            head += f" ({meta.get('opaque_note', s.note)})"
        lbl = f"{head}\\n{_pathline(s.name)}\\n{_fmt_at(s.rel, total)}"
        if any(s.name.endswith(e) for e in error_names):
            lbl = "✖ ERROR  " + lbl
        nodes.append(
            Node(
                id=nid,
                label=lbl,
                cluster=_lane(s.svc),
                type=_node_type(s, error_names),
                time_ms=round(s.dur, 2),
            )
        )
    return nodes


def _callout(stages: list[Span], meta: dict, total: float) -> str:
    if not stages:
        return ""
    top = max(stages, key=lambda s: s.dur)
    pct = 100.0 * top.dur / total if total else 0.0
    head = top.short + (f" ×{top.count}" if top.count > 1 else "")
    if top.note:
        head += f" ({meta.get('opaque_note', top.note)})"
    return f" · key cost: {head} = {_fmt_ms(top.dur)} ({pct:.0f}%)"


def _clusters(stages: list[Span]) -> list[Cluster]:
    result = [Cluster(id="core", label="Core process (yadgar-core)", kind="core")]
    if any(_lane(s.svc) == "backend" for s in stages):
        result.append(Cluster(id="backend", label="Backend (yadgar-backend)", kind="backend"))
    return result


def build_spec(data: dict) -> DiagramSpec:
    label = data["label"]
    total = float(data["total_ms"])
    tool_base = str(data["tool_span"]).split(" ")[0]
    meta = NOTES.get(label, {})
    error_names = meta.get("error_spans", [])

    root = build_tree(data["spans"])
    tool = find_tool_span(root, tool_base)
    dropped_boundary = tool is None
    if tool is None:
        tool = root  # audit_anchors: boundary span dropped -> flat forest
        tool.dur = total
        tool.svc = "yadgar-core"

    stages = select_stages(tool, total, error_names)

    tail = total - (tool.rel + tool.dur)
    terminal_text = meta.get("terminal_label", "result returned")
    done_node = Node(
        id="done",
        label=f"{terminal_text}\\n{_fmt_at(total, total)}",
        cluster="core",
        type=meta.get("terminal_type", "compute"),
        time_ms=round(tail, 2) if tail > 0.05 else None,
    )

    nodes = [_entry_node(tool_base, dropped_boundary, tool, total)]
    nodes.extend(_stage_nodes(stages, meta, error_names, total))
    nodes.append(done_node)

    chain = ["entry"] + [f"s{i}" for i in range(len(stages))] + ["done"]
    edges = [
        Edge(src=a, dst=b, order=order)
        for order, (a, b) in enumerate(zip(chain, chain[1:], strict=False), start=1)
    ]

    subtitle = (
        f"trace {data['trace_id'][:8]} · total {_fmt_ms(total)}{_callout(stages, meta, total)}"
    )
    if meta.get("note"):
        subtitle += f"\\n{meta['note']}"

    return DiagramSpec(
        title=f"{label} — simplified trace ({CAPTURE_DATE}, {RELEASE})",
        subtitle=subtitle,
        rankdir="TB",
        total_ms=total,
        show_total=False,  # total already in the subtitle (real trace total)
        clusters=_clusters(stages),
        nodes=nodes,
        edges=edges,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def render(json_path: Path, stats_only: bool = False) -> Path | None:
    data = json.loads(json_path.read_text())
    spec = build_spec(data)
    n_boxes = len(spec.nodes)
    print(f"{data['label']:28s} spans={data['span_count']:>6} -> boxes={n_boxes}")
    if stats_only:
        return None
    stem = OUT_DIR / f"{data['label']}-trace-{CAPTURE_DATE}"
    dot_src = render_dot(spec)
    dot_path = stem.with_suffix(".dot")
    dot_path.write_text(dot_src)
    dot_bin = shutil.which("dot")
    if not dot_bin:
        print("  (graphviz `dot` not on PATH — wrote .dot only)", file=sys.stderr)
        return dot_path
    for fmt in ("svg", "png"):
        subprocess.run(
            [dot_bin, f"-T{fmt}", str(dot_path), "-o", str(stem) + f".{fmt}"],
            check=True,
        )
    return stem.with_suffix(".png")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("jsons", nargs="*", help="capture JSON file(s)")
    ap.add_argument("--all", action="store_true", help="all out/*.json captures")
    ap.add_argument("--stats", action="store_true", help="print box counts only")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.jsons]
    if args.all:
        paths = sorted(OUT_DIR.glob("*.json"))
    if not paths:
        ap.error("provide capture JSON path(s) or --all")

    for p in paths:
        render(p, stats_only=args.stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
