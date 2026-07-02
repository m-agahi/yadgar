#!/usr/bin/env python3
"""YAML-driven system-flow diagram generator for yadgar subsystems.

Renders clustered, ordered flow diagrams with per-component timing labels.
Each diagram is authored purely as a YAML spec (see ``specs/`` and README.md);
adding a new diagram requires NO code change here — drop a new ``*.yaml`` in the
specs directory and re-run.

Rendering strategy
------------------
We emit Graphviz DOT source directly and shell out to the ``dot`` binary
(SVG + PNG). We deliberately do NOT depend on the python ``graphviz`` package:
it is a thin DOT-string builder that shells to ``dot`` anyway, so hand-writing
DOT keeps this a docs-only tool that adds nothing to the project's
``pyproject.toml`` — it reads YAML via ``ruamel.yaml`` (already a project
dependency), falling back to ``PyYAML`` when run standalone.

If ``dot`` is unavailable we still write the ``.dot`` source and a companion
Mermaid ``.mmd`` file, and report that no raster/vector image was produced.

CLI
---
    python docs/diagrams/generate.py <spec.yaml> [-o out/stem]
    python docs/diagrams/generate.py --all            # render every spec in specs/

``-o`` sets the output *stem*; both ``<stem>.svg`` and ``<stem>.png`` are
emitted from it. Without ``-o`` the stem is ``out/<spec-filename-without-ext>``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# YAML loader: prefer ruamel.yaml (already a project dependency); fall back to
# PyYAML so the tool also runs standalone outside the project venv.
try:
    from ruamel.yaml import YAML as _RuamelYAML

    def _load_yaml(text: str) -> Any:
        return _RuamelYAML(typ="safe").load(text)
except ImportError:  # pragma: no cover - fallback path
    import yaml

    def _load_yaml(text: str) -> Any:
        return yaml.safe_load(text)

# --------------------------------------------------------------------------- #
# Directory conventions
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
SPECS_DIR = HERE / "specs"
OUT_DIR = HERE / "out"

# Output image formats emitted per spec (all via `dot -T<fmt>`).
IMAGE_FORMATS = ("svg", "png")

# --------------------------------------------------------------------------- #
# Styling defaults (overridable per node/edge via `type` -> style maps below)
# --------------------------------------------------------------------------- #

# Cluster (subgraph) background tints, keyed by a coarse "kind" so the palette
# stays consistent across diagrams. Unknown kinds fall back to CLUSTER_DEFAULT.
CLUSTER_STYLES: dict[str, dict[str, str]] = {
    "core": {"bgcolor": "#eef4fb", "color": "#4a72a8", "fontcolor": "#26456b"},
    "backend": {"bgcolor": "#fbf0ea", "color": "#b5744a", "fontcolor": "#7a4620"},
    "external": {"bgcolor": "#f0f0f0", "color": "#888888", "fontcolor": "#555555"},
}
CLUSTER_DEFAULT = {"bgcolor": "#f5f5f5", "color": "#999999", "fontcolor": "#444444"}

# Node styles keyed by `type`. Unknown types fall back to NODE_DEFAULT.
NODE_STYLES: dict[str, dict[str, str]] = {
    "compute": {"shape": "box", "style": "rounded,filled", "fillcolor": "#dbe9fb"},
    "io": {"shape": "box", "style": "rounded,filled", "fillcolor": "#fce3d6"},
    "cache": {"shape": "box", "style": "rounded,filled,dashed", "fillcolor": "#e7f6e7"},
    "model": {"shape": "box", "style": "filled", "fillcolor": "#f5e1f5"},
    "store": {"shape": "cylinder", "style": "filled", "fillcolor": "#fce3d6"},
    "gate": {"shape": "diamond", "style": "filled", "fillcolor": "#fdf3c8"},
    "start": {"shape": "circle", "style": "filled", "fillcolor": "#d6d6d6"},
    "end": {"shape": "doublecircle", "style": "filled", "fillcolor": "#d6d6d6"},
}
NODE_DEFAULT = {"shape": "box", "style": "rounded,filled", "fillcolor": "#ffffff"}

# Edge styles keyed by `type`. Unknown types fall back to EDGE_DEFAULT.
EDGE_STYLES: dict[str, dict[str, str]] = {
    "flow": {"color": "#333333", "style": "solid"},
    "skip": {"color": "#999999", "style": "dashed"},
    "async": {"color": "#6a8f3a", "style": "dashed"},
}
EDGE_DEFAULT = {"color": "#333333", "style": "solid"}


# --------------------------------------------------------------------------- #
# Spec model (forward-compatible: unknown keys ignored, sane defaults applied)
# --------------------------------------------------------------------------- #


@dataclass
class Cluster:
    id: str
    label: str = ""
    kind: str = ""  # coarse style key: core | backend | external | ...

    @property
    def style_kind(self) -> str:
        # `kind` wins; else infer from id so `id: core`/`id: backend` just work.
        return self.kind or self.id


@dataclass
class Node:
    id: str
    label: str = ""
    cluster: str | None = None
    time_ms: float | None = None
    type: str = "compute"


@dataclass
class Edge:
    src: str
    dst: str
    order: int | None = None
    label: str = ""
    type: str = "flow"


@dataclass
class DiagramSpec:
    title: str = ""
    subtitle: str = ""
    rankdir: str = "TB"  # TB | LR
    total_ms: float | None = None  # explicit override; else summed from nodes
    show_total: bool = True
    clusters: list[Cluster] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def load_spec(path: Path) -> DiagramSpec:
    """Parse a YAML spec into a DiagramSpec. Unknown keys are ignored."""
    raw = _load_yaml(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    clusters = [
        Cluster(
            id=str(c["id"]),
            label=str(c.get("label", c["id"])),
            kind=str(c.get("kind", "")),
        )
        for c in raw.get("clusters", [])
        if isinstance(c, dict) and "id" in c
    ]

    nodes = [
        Node(
            id=str(n["id"]),
            label=str(n.get("label", n["id"])),
            cluster=(str(n["cluster"]) if n.get("cluster") is not None else None),
            time_ms=_as_float(n.get("time_ms")),
            type=str(n.get("type", "compute")),
        )
        for n in raw.get("nodes", [])
        if isinstance(n, dict) and "id" in n
    ]

    edges = [
        Edge(
            src=str(e["src"]),
            dst=str(e["dst"]),
            order=(int(e["order"]) if e.get("order") is not None else None),
            label=str(e.get("label", "")),
            type=str(e.get("type", "flow")),
        )
        for e in raw.get("edges", [])
        if isinstance(e, dict) and "src" in e and "dst" in e
    ]

    return DiagramSpec(
        title=str(raw.get("title", path.stem)),
        subtitle=str(raw.get("subtitle", "")),
        rankdir=str(raw.get("rankdir", "TB")),
        total_ms=_as_float(raw.get("total_ms")),
        show_total=bool(raw.get("show_total", True)),
        clusters=clusters,
        nodes=nodes,
        edges=edges,
    )


# --------------------------------------------------------------------------- #
# DOT rendering
# --------------------------------------------------------------------------- #


def _esc(text: str) -> str:
    r"""Escape a string for a double-quoted DOT identifier/label.

    Double-quotes are escaped. Backslashes are escaped too, EXCEPT the DOT
    line-break escapes ``\n`` / ``\l`` / ``\r``, which are preserved so a spec
    author can write ``label: "line one\nline two"`` and get two lines.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in "nlr":
            out.append(text[i : i + 2])  # keep DOT line-break escape intact
            i += 2
            continue
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _node_label(node: Node) -> str:
    """Build the visible node label, appending a timing line when present."""
    label = node.label or node.id
    if node.time_ms is not None:
        label = f"{label}\\n{_fmt_ms(node.time_ms)}"
    return label


def _fmt_ms(ms: float) -> str:
    """Format a millisecond value for display (``ms`` under 1 s, else ``s``)."""
    if ms >= 1000:
        return f"{ms / 1000:g} s"
    return f"{ms:g} ms"


def _attr_str(attrs: dict[str, str]) -> str:
    return ", ".join(f'{k}="{_esc(v)}"' for k, v in attrs.items())


def _computed_total(spec: DiagramSpec) -> float | None:
    if spec.total_ms is not None:
        return spec.total_ms
    times = [n.time_ms for n in spec.nodes if n.time_ms is not None]
    return sum(times) if times else None


def _emit_node(node: Node, indent: str) -> str:
    style = {**(NODE_STYLES.get(node.type, NODE_DEFAULT))}
    style["label"] = _node_label(node)
    return f'{indent}"{_esc(node.id)}" [{_attr_str(style)}];'


def _graph_label(spec: DiagramSpec) -> str | None:
    """Title + subtitle + total-time annotation, joined as the graph label."""
    bits: list[str] = []
    if spec.title:
        bits.append(spec.title)
    if spec.subtitle:
        bits.append(spec.subtitle)
    if spec.show_total:
        total = _computed_total(spec)
        if total is not None:
            bits.append(f"total ≈ {_fmt_ms(total)}")
    return chr(10).join(bits) if bits else None


def _cluster_lines(cluster: Cluster, members: list[Node]) -> list[str]:
    """Emit a `subgraph cluster_<id>` block (the prefix is what draws the box)."""
    cstyle = CLUSTER_STYLES.get(cluster.style_kind, CLUSTER_DEFAULT)
    out = [
        f'  subgraph "cluster_{_esc(cluster.id)}" {{',
        f'    label="{_esc(cluster.label)}";',
        "    style=filled;",
        f'    bgcolor="{cstyle["bgcolor"]}";',
        f'    color="{cstyle["color"]}";',
        f'    fontcolor="{cstyle["fontcolor"]}";',
        '    fontname="Helvetica-Bold";',
    ]
    out.extend(_emit_node(n, "    ") for n in members)
    out.append("  }")
    return out


def _edge_line(edge: Edge) -> str:
    estyle = {**(EDGE_STYLES.get(edge.type, EDGE_DEFAULT))}
    parts = [f'{k}="{_esc(v)}"' for k, v in estyle.items()]
    elabel = edge.label or (str(edge.order) if edge.order is not None else "")
    if elabel:
        parts.append(f'label="{_esc(elabel)}"')
    return f'  "{_esc(edge.src)}" -> "{_esc(edge.dst)}" [{", ".join(parts)}];'


def render_dot(spec: DiagramSpec) -> str:
    """Render a DiagramSpec to Graphviz DOT source."""
    lines: list[str] = [
        "digraph flow {",
        f'  rankdir="{_esc(spec.rankdir)}";',
        # newrank=true lets ranks span clusters so a sequential chain crossing
        # clusters (core<->backend) still reads in flow order instead of dot
        # laying each cluster out independently. compound=true tidies edges.
        "  newrank=true;",
        "  compound=true;",
        "  nodesep=0.35;",
        "  ranksep=0.55;",
        '  graph [fontname="Helvetica", fontsize=11, labelloc="t"];',
        '  node  [fontname="Helvetica", fontsize=10];',
        '  edge  [fontname="Helvetica", fontsize=9];',
    ]

    label = _graph_label(spec)
    if label is not None:
        lines.append(f'  label="{_esc(label)}";')

    # Cluster membership lookup.
    nodes_by_cluster: dict[str | None, list[Node]] = {}
    for node in spec.nodes:
        nodes_by_cluster.setdefault(node.cluster, []).append(node)

    for cluster in spec.clusters:
        lines.extend(_cluster_lines(cluster, nodes_by_cluster.get(cluster.id, [])))

    # Nodes with no (or unknown) cluster live at the top level.
    known = {c.id for c in spec.clusters}
    for cluster_id, cluster_nodes in nodes_by_cluster.items():
        if cluster_id in known:
            continue
        lines.extend(_emit_node(n, "  ") for n in cluster_nodes)

    # Edges, sorted by `order` when provided (ordered sequential flow).
    def edge_key(e: Edge) -> tuple[int, int]:
        return (0, e.order) if e.order is not None else (1, 0)

    lines.extend(_edge_line(e) for e in sorted(spec.edges, key=edge_key))

    lines.append("}")
    return "\n".join(lines) + "\n"


def _mermaid(spec: DiagramSpec) -> str:
    """Best-effort Mermaid fallback (used only when `dot` is missing)."""
    lines = [f"flowchart {'LR' if spec.rankdir == 'LR' else 'TB'}"]
    for cluster in spec.clusters:
        lines.append(f'  subgraph {cluster.id}["{cluster.label}"]')
        for node in [n for n in spec.nodes if n.cluster == cluster.id]:
            lbl = node.label or node.id
            if node.time_ms is not None:
                lbl = f"{lbl} ({_fmt_ms(node.time_ms)})"
            lines.append(f'    {node.id}["{lbl}"]')
        lines.append("  end")
    known = {c.id for c in spec.clusters}
    for node in [n for n in spec.nodes if n.cluster not in known]:
        lbl = node.label or node.id
        lines.append(f'  {node.id}["{lbl}"]')
    for edge in sorted(spec.edges, key=lambda e: (e.order is None, e.order or 0)):
        arrow = f'-- "{edge.label}" -->' if edge.label else "-->"
        lines.append(f"  {edge.src} {arrow} {edge.dst}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Output orchestration
# --------------------------------------------------------------------------- #


def _stem_for(spec_path: Path, out_arg: str | None) -> Path:
    if out_arg:
        stem = Path(out_arg)
        # Strip a known image extension so `-o foo.svg` still emits both.
        if stem.suffix.lower().lstrip(".") in IMAGE_FORMATS or stem.suffix == ".dot":
            stem = stem.with_suffix("")
        return stem
    return OUT_DIR / spec_path.stem


def render_spec(spec_path: Path, out_arg: str | None = None) -> list[Path]:
    """Render one spec. Returns the list of files written.

    Always writes the ``.dot`` source. When ``dot`` is on PATH, also writes an
    image per :data:`IMAGE_FORMATS`; otherwise writes a ``.mmd`` Mermaid fallback.
    """
    spec = load_spec(spec_path)
    dot_source = render_dot(spec)

    stem = _stem_for(spec_path, out_arg)
    stem.parent.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    dot_path = stem.with_suffix(".dot")
    dot_path.write_text(dot_source)
    written.append(dot_path)

    dot_bin = shutil.which("dot")
    if not dot_bin:
        mmd_path = stem.with_suffix(".mmd")
        mmd_path.write_text(_mermaid(spec))
        written.append(mmd_path)
        print(
            f"WARNING: `dot` not found on PATH — wrote DOT + Mermaid only "
            f"({dot_path.name}, {mmd_path.name}). Install graphviz to render images.",
            file=sys.stderr,
        )
        return written

    for fmt in IMAGE_FORMATS:
        out_path = stem.with_suffix(f".{fmt}")
        subprocess.run(
            [dot_bin, f"-T{fmt}", str(dot_path), "-o", str(out_path)],
            check=True,
        )
        written.append(out_path)
    return written


def render_all(out_arg: str | None = None) -> list[Path]:
    if out_arg:
        print("NOTE: -o is ignored with --all (stems derive from spec names).", file=sys.stderr)
    specs = sorted(SPECS_DIR.glob("*.yaml")) + sorted(SPECS_DIR.glob("*.yml"))
    if not specs:
        print(f"No specs found in {SPECS_DIR}", file=sys.stderr)
        return []
    written: list[Path] = []
    for spec_path in specs:
        written.extend(render_spec(spec_path))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("spec", nargs="?", help="path to a YAML spec")
    parser.add_argument("-o", "--out", help="output stem (emits <stem>.svg and .png)")
    parser.add_argument(
        "--all", action="store_true", help="render every spec in the specs/ directory"
    )
    args = parser.parse_args(argv)

    if args.all:
        written = render_all(args.out)
    elif args.spec:
        written = render_spec(Path(args.spec), args.out)
    else:
        parser.error("provide a spec path or --all")
        return 2  # unreachable; parser.error exits

    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
