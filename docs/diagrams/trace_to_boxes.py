#!/usr/bin/env python3
"""Collapse a captured span-tree (capture_trace.py output) into a rectangle diagram.

Raw traces can carry tens of thousands of spans (per-item instrumentation). For a
readable per-tool architecture picture we collapse to the DISTINCT stage tree:
spans sharing (depth, name) are merged into one box showing summed self-ish time,
call count, and first-seen offset. Emits a Graphviz DOT of plain rectangles
(one box per stage, edges = parent->child), rendered to SVG/PNG if `dot` is on PATH.

Correctness over polish: boxes + times only, no styling beyond service colour.

Usage:
    python docs/diagrams/trace_to_boxes.py <capture.json> [out_basename]
    # -> out_basename.dot (+ .svg/.png if graphviz present)

Box label: <stage name>\\n<total_ms>ms (xN)   where N = span count collapsed.
Edge: parent stage -> child stage (deduped).
"""

import html
import json
import shutil
import subprocess
import sys
from collections import defaultdict

SVC_COLOR = {"yadgar-core": "#e8f0fe", "yadgar-backend": "#fde8e8"}


def collapse(spans):
    """Group spans by (depth, name); return ordered stage records + parent edges by name."""
    groups = defaultdict(lambda: {"count": 0, "dur": 0.0, "rel": 1e18, "svc": ""})
    # map spanId->name not available (capture drops ids); approximate parent via depth order.
    # We reconstruct edges by nearest preceding span at depth-1 in start order.
    stack = {}  # depth -> last stage-key seen
    edges = set()
    for s in spans:
        key = (s["depth"], s["name"])
        g = groups[key]
        g["count"] += 1
        g["dur"] += s["dur_ms"]
        g["rel"] = min(g["rel"], s["rel_ms"])
        g["svc"] = s["svc"]
        parent = stack.get(s["depth"] - 1)
        if parent and parent != key:
            edges.add((parent, key))
        stack[s["depth"]] = key
    return groups, edges


def dot(groups, edges, title):
    def nid(k):
        return f"n{hash(k) & 0xFFFFFFFF}"

    lines = [
        "digraph G {",
        "  rankdir=TB;",
        '  node [shape=box, fontname="monospace", fontsize=10];',
        f'  label="{html.escape(title)}"; labelloc=t; fontname="monospace";',
    ]
    for key, g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[1]["rel"])):
        name = key[1]
        lbl = f"{name}\\n{g['dur']:.0f}ms" + (f" x{g['count']}" if g["count"] > 1 else "")
        color = SVC_COLOR.get(g["svc"], "#ffffff")
        lines.append(
            f'  {nid(key)} [label="{html.escape(lbl)}", style=filled, fillcolor="{color}"];'
        )
    for a, b in edges:
        lines.append(f"  {nid(a)} -> {nid(b)};")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    data = json.load(open(sys.argv[1]))
    base = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1].rsplit(".", 1)[0]
    title = (
        f"{data['label']}  |  {data['total_ms']:.0f}ms  |  {data['span_count']} spans (collapsed)"
    )
    groups, edges = collapse(data["spans"])
    src = dot(groups, edges, title)
    open(base + ".dot", "w").write(src)
    print(f"{base}.dot  ({len(groups)} stages from {data['span_count']} spans)")
    if shutil.which("dot"):
        for fmt in ("svg", "png"):
            try:
                subprocess.run(
                    ["dot", f"-T{fmt}", base + ".dot", "-o", f"{base}.{fmt}"], check=True
                )
                print(f"{base}.{fmt}")
            except subprocess.CalledProcessError as e:  # noqa: PERF203
                print(f"render {fmt} failed: {e}")
    else:
        print("(graphviz `dot` not on PATH — .dot only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
