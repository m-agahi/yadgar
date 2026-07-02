"""Smoke tests for the YAML-driven diagram generator (docs/diagrams/generate.py).

The generator is a docs tool, not part of the shipped package, so we import it
by path. Image-producing assertions are skipped when the `dot` binary is absent
so this test stays green on machines without graphviz installed.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "docs" / "diagrams" / "generate.py"

SAMPLE_SPEC = """\
title: "Sample flow"
subtitle: "used by the test suite"
rankdir: TB
clusters:
  - {id: core, label: "Core (--cpus 1)", kind: core}
  - {id: backend, label: "Backend (--cpus 2): surreal + rerank", kind: backend}
nodes:
  - {id: start, label: "query", type: start}
  - {id: embed, label: "query embed", cluster: backend, time_ms: 40, type: model}
  - {id: fts, label: "FTS search (\\"bm25\\")", cluster: backend, time_ms: 120, type: io}
  - {id: fuse, label: "fusion", cluster: core, time_ms: 5, type: compute}
  - {id: done, label: "results", type: end}
edges:
  - {src: start, dst: embed, order: 1}
  - {src: embed, dst: fts, order: 2, label: "candidates"}
  - {src: fts, dst: fuse, order: 3}
  - {src: fuse, dst: done, order: 4}
# unknown top-level key must be ignored (forward-compat contract):
future_field: {anything: goes}
"""


def _load_generator():
    spec = importlib.util.spec_from_file_location("diagram_generate", GEN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: Python 3.14's @dataclass resolves cls.__module__ via
    # sys.modules during class creation, which fails for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generator_module_exists() -> None:
    assert GEN_PATH.is_file(), f"generator missing at {GEN_PATH}"


def test_load_spec_and_render_dot_ignores_unknown_keys(tmp_path: Path) -> None:
    gen = _load_generator()
    spec_path = tmp_path / "sample.yaml"
    spec_path.write_text(SAMPLE_SPEC)

    parsed = gen.load_spec(spec_path)
    assert parsed.title == "Sample flow"
    assert len(parsed.clusters) == 2
    assert len(parsed.nodes) == 5
    assert len(parsed.edges) == 4

    dot = gen.render_dot(parsed)
    # cluster boxes must use the `cluster_` prefix for dot to draw them
    assert "cluster_core" in dot
    assert "cluster_backend" in dot
    # per-node timing labels rendered
    assert "40 ms" in dot
    # total-time annotation = sum of node time_ms (40 + 120 + 5)
    assert "165 ms" in dot
    # embedded quote in a label survived escaping without breaking DOT
    assert '\\"bm25\\"' in dot


def test_render_spec_writes_dot_always(tmp_path: Path) -> None:
    gen = _load_generator()
    spec_path = tmp_path / "sample.yaml"
    spec_path.write_text(SAMPLE_SPEC)
    stem = tmp_path / "out" / "sample"

    written = gen.render_spec(spec_path, out_arg=str(stem))
    dot_out = stem.with_suffix(".dot")
    assert dot_out in written
    assert dot_out.is_file() and dot_out.stat().st_size > 0


@pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz `dot` not installed")
def test_render_spec_emits_svg_and_png(tmp_path: Path) -> None:
    gen = _load_generator()
    spec_path = tmp_path / "sample.yaml"
    spec_path.write_text(SAMPLE_SPEC)
    stem = tmp_path / "out" / "sample"

    written = gen.render_spec(spec_path, out_arg=str(stem))
    svg = stem.with_suffix(".svg")
    png = stem.with_suffix(".png")
    assert svg in written and png in written
    assert svg.is_file() and svg.stat().st_size > 0
    assert png.is_file() and png.stat().st_size > 0
