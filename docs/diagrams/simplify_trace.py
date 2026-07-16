#!/usr/bin/env python3
"""Simplified two-lane trace diagrams from capture_trace.py span JSONs.

Companion to `trace_to_boxes.py` (which keeps EVERY distinct stage — the
detailed `mcp-traces/*.svg` view). This tool produces the SIMPLIFIED view in
the style of `archive/2026-07-04/recall-cold-trace-2026-07-04.png`: two lanes (blue CORE
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
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Repo root on path so the pure trace-mesh logic imports (docs/ is not a package).
sys.path.insert(0, str(HERE.parent.parent))

from generate import (  # noqa: E402
    NODE_STYLES,
    Cluster,
    DiagramSpec,
    Edge,
    Node,
    _fmt_ms,
    render_dot,
)

# Pure aggregation logic now lives in yadgar/_shared/trace_mesh.py (viz-trace-replay
# Car B). This module is a CONSUMER — it keeps only the DiagramSpec/DOT rendering.
from yadgar._shared.trace_mesh import (  # noqa: E402,F401
    ALIASES,
    IO_RE,
    KEEP_FLOOR_MS,
    LOWLEVEL,
    MAX_BOXES,
    MODEL_RE,
    PLUMBING,
    PUSH_COVER,
    STORM_MIN,
    Span,
    _friendly,
    _humanize,
    _lane,
    _node_type,
    aggregate_storms,
    build_tree,
    crossing,
    find_tool_span,
    select_stages,
    subtree_has,
)

OUT_DIR = HERE / "out"
CAPTURE_DATE = "2026-07-09"
RELEASE = "core 5.117 / backend 5.30"

# red box for known-failing stages (injected style — generate.py untouched)
NODE_STYLES["error"] = {
    "shape": "box",
    "style": "rounded,filled,bold",
    "fillcolor": "#fbd9d9",
    "color": "#b03030",
    "fontcolor": "#7a1f1f",
}

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
# Spec building
# --------------------------------------------------------------------------- #


def _fmt_at(rel: float, total: float) -> str:
    return f"@{rel:.1f}ms" if total < 100 else f"@{rel:.0f}ms"


def _pathline(name: str) -> str:
    """Return a compact 2-segment tail for line-2 of a box label.

    E.g. "yadgar._shared.retrieval.scoring._ScoringMixin._run_fts_bm25"
    → "_ScoringMixin._run_fts_bm25"
    Bare names like "POST" or "GET /admin/dbsize" pass through unchanged.
    """
    parts = name.split(".")
    tail = ".".join(parts[-2:]) if len(parts) >= 2 else name
    return tail


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
        friendly = _friendly(s) + (f" ×{s.count}" if s.count > 1 else "")
        if s.note:
            # machine-derived owner attribution can be misled by interleaved
            # spans — allow a hand-tuned per-label override
            friendly += f" ({meta.get('opaque_note', s.note)})"
        lbl = f"{friendly}\\n{_pathline(s.name)}\\n{_fmt_at(s.rel, total)}"
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
    head = _friendly(top) + (f" ×{top.count}" if top.count > 1 else "")
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
