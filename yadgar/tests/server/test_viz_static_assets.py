"""Static-asset string checks for the viz index.html (no browser needed).

ADR-0135 (galaxy-only): the 2D/3D force-directed renderer was removed and galaxy
(galaxy-view.js raw Three.js) is the SOLE renderer. The old force-graph guards
(S2.1 heat-in-3D, S2.2 wiki octahedra, S2.3 _graphMode search branch, mesh-leak,
nodeRelSize, 2D/3D linkWidth) were deleted with the renderer they pinned.

Surviving guards:
- loadGraph() still orphan-filters edges (feeds the popup connection counts).
- loadGraph() still fetches /api/viz/config before render.
- TestADR0135GalaxyRenderMode: galaxy is unconditional; the `graph` global +
  render-mode machinery are gone; visibility / heat / boot route to _galaxyView.
"""

from __future__ import annotations

import pathlib


def _html() -> str:
    """Read index.html from the static directory."""
    static_dir = pathlib.Path(__file__).parent.parent.parent / "core" / "static"
    return (static_dir / "index.html").read_text(encoding="utf-8")


class TestV5109OrphanEdgeFilter:
    """v5.10.9: loadGraph() must filter orphan links before passing to force-graph library.

    Root cause: force-graph.min.js throws 'node not found: entity:NNN' when a link
    references an ID not in the node set. One orphan edge is enough to crash the
    physics simulation entirely (engine tick count = 0, all nodes clump at origin).

    Fix: filter allLinks before graph.graphData(...) call in loadGraph().
    Tests verify the filter code and the console.warn observability guard are present.
    """

    def test_loadGraph_filters_orphan_links(self) -> None:
        html = _html()
        assert "nodeIdSet" in html, (
            "nodeIdSet missing from loadGraph() — v5.10.9 orphan-edge filter not implemented. "
            "Without this, force-graph.min.js throws 'node not found: entity:NNN' and the "
            "physics simulation never starts."
        )
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "async function loadGraph()" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and len(func_lines) > 1:
                    break
        body = "\n".join(func_lines)
        assert "nodeIdSet" in body, (
            "nodeIdSet filter not found inside loadGraph() body — filter may be in wrong scope"
        )
        assert "allLinks.filter" in body, (
            "allLinks.filter call missing inside loadGraph() — edges are not being filtered"
        )

    def test_loadGraph_logs_dropped_count(self) -> None:
        html = _html()
        lines = html.splitlines()
        warn_lines = [
            ln for ln in lines if "console.warn" in ln and ("orphan" in ln or "dropped" in ln)
        ]
        assert warn_lines, (
            "console.warn with 'orphan' or 'dropped' missing from loadGraph() — "
            "orphan drops are silent; backend drift won't be observable in DevTools."
        )


class TestV5110VizConfigFetch:
    """v5.11.0: loadGraph() must fetch /api/viz/config before rendering.

    Fix 1: A loadVizConfig() async function (or equivalent) fetches /api/viz/config
           and stores the result in window.YADGAR_VIZ_CONFIG.
    Fix 2: YADGAR_VIZ_CONFIG is referenced in node-color / link-color / nodeRelSize
           call sites, replacing hardcoded constants.
    """

    def test_loadGraph_fetches_viz_config(self) -> None:
        html = _html()
        # loadGraph must invoke fetch('/api/viz/config') or call a loadVizConfig helper
        assert "/api/viz/config" in html, (
            "'/api/viz/config' missing from index.html — v5.11.0 requires loadGraph() "
            "to fetch viz config from the backend before initialising the graph."
        )
        # The fetch must happen inside or before loadGraph
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "async function loadGraph()" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and len(func_lines) > 1:
                    break
        body = "\n".join(func_lines)
        assert "/api/viz/config" in body or "loadVizConfig" in body, (
            "loadGraph() does not call fetch('/api/viz/config') or loadVizConfig() — "
            "v5.11.0 requires viz config to be fetched before graph initialisation."
        )

    def test_viz_constants_reference_config(self) -> None:
        html = _html()
        # YADGAR_VIZ_CONFIG must be declared and referenced in node/edge color logic
        assert "YADGAR_VIZ_CONFIG" in html, (
            "YADGAR_VIZ_CONFIG missing from index.html — v5.11.0 requires a global "
            "config object populated from /api/viz/config to drive viz constants."
        )
        # Must be referenced in color/sizing call sites
        lines = html.splitlines()
        config_ref_lines = [ln for ln in lines if "YADGAR_VIZ_CONFIG" in ln]
        assert len(config_ref_lines) >= 3, (
            f"YADGAR_VIZ_CONFIG referenced only {len(config_ref_lines)} time(s) — "
            "expected at least 3 (node color, edge color, physics/layout). "
            "Hardcoded constants must be replaced."
        )


def _static_dir() -> pathlib.Path:
    return pathlib.Path(__file__).parent.parent.parent / "core" / "static"


class TestADR0135GalaxyRenderMode:
    """ADR-0135: galaxy is a SEPARATE raw-Three.js renderer (galaxy-view.js), not
    the #209 physics-freeze inside 3d-force-graph. These guard the wiring + the
    graph-null routing that keeps the viz alive on an SSE tick / filter in galaxy.

    Render / picking / teardown are the user's browser smoke-check (no harness);
    the layout MATH lives in galaxy-view.test.js (vitest). These string checks are
    the "allowlist" the plan asks for: proof the module exists, is imported, and
    that the risky graph-null sites are routed rather than left to crash.
    """

    def test_galaxy_view_module_file_exists(self) -> None:
        assert (_static_dir() / "galaxy-view.js").is_file(), (
            "galaxy-view.js missing — ADR-0135 galaxy render mode not present."
        )
        assert (_static_dir() / "galaxy-view.css").is_file(), (
            "galaxy-view.css missing — galaxy chrome styles not present."
        )

    def test_galaxy_view_exposes_public_surface(self) -> None:
        js = (_static_dir() / "galaxy-view.js").read_text(encoding="utf-8")
        assert "window._galaxyView" in js, (
            "galaxy-view.js does not expose window._galaxyView — index.html drives "
            "the scene through this global surface."
        )
        for fn in ("mount", "destroy", "setVisible", "patchHeat", "relayout"):
            assert fn in js, f"window._galaxyView.{fn} missing from galaxy-view.js"

    def test_galaxy_reuses_loaded_three_not_a_second_global(self) -> None:
        js = (_static_dir() / "galaxy-view.js").read_text(encoding="utf-8")
        assert "window.THREE" in js, (
            "galaxy-view.js must reuse the already-loaded window.THREE (r0.158), not "
            "load a second THREE global (a 2nd THREE clobbers the shared WebGL ctx)."
        )
        # Must not create a 2nd THREE global via a script tag / three.min.js load.
        # (Bare mentions of 'cdnjs'/'0.160' in comments are fine — check for an
        # actual injected loader.)
        assert "three.min.js" not in js and "<script src" not in js, (
            "galaxy-view.js appears to load its own Three.js — ADR-0135 requires "
            "reusing the r0.158 already loaded by index.html."
        )

    def test_index_imports_galaxy_view_module(self) -> None:
        html = _html()
        assert "./galaxy-view.js" in html, (
            "index.html does not import galaxy-view.js — the module block must import "
            "it so window._galaxyView is populated."
        )

    def test_galaxy_is_the_sole_renderer_no_force_graph(self) -> None:
        """Galaxy-only: the force-directed renderer and its render-mode toggle were
        removed. There is no `graph` global, no _isGalaxy/_renderMode/_layoutModePref
        switch, and no initGraph — galaxy always owns the canvas."""
        html = _html()
        assert "let graph = null" not in html and "let graph=null" not in html, (
            "the `graph` global is back — galaxy is the sole renderer, there must be "
            "no force-directed `graph` variable."
        )
        for gone in (
            "function initGraph",
            "_isGalaxy",
            "_renderMode",
            "_layoutModePref",
            "toggleLayoutMode",
        ):
            assert gone not in html, (
                f"'{gone}' still present — galaxy is unconditional; the FG renderer + "
                "render-mode machinery must be fully removed."
            )

    def test_applyFilters_routes_to_galaxy_unconditionally(self) -> None:
        """applyFilters must always stamp __visible and route to _galaxyView (no
        `if (!graph) return;` bail, no FG branch)."""
        html = _html()
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function applyFilters()" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and len(func_lines) > 1:
                    break
        body = "\n".join(func_lines)
        assert "if (!graph) return;" not in body and "!_isGalaxy" not in body, (
            "applyFilters() must not gate on the removed `graph`/_isGalaxy — it runs "
            "unconditionally in galaxy-only mode."
        )
        assert "_galaxyView.setVisible" in body, (
            "applyFilters() must route visibility to _galaxyView.setVisible."
        )

    def test_sse_heat_updated_routes_to_galaxy(self) -> None:
        """SSE heat_updated must patch the galaxy scene (patchHeat), never a null
        force `graph`."""
        html = _html()
        idx = html.find("heat_updated")
        assert idx != -1
        window = html[idx : idx + 1400]
        assert "_galaxyView.patchHeat" in window, (
            "SSE heat_updated must route the live heat patch to _galaxyView.patchHeat "
            "(the FG graph.nodeColor/nodeCanvasObject repaint was removed)."
        )
        assert "graph.nodeColor" not in window and "graph.nodeCanvasObject" not in window, (
            "SSE heat_updated still calls the removed FG graph.* repaint APIs."
        )

    def test_boot_mounts_galaxy_only(self) -> None:
        """The boot IIFE must NOT init any FG renderer — galaxy is unconditional and
        loadGraph mounts _galaxyView once the payload arrives."""
        html = _html()
        assert "initGraph(_graphMode)" not in html and "if (!_isGalaxy()) initGraph" not in html, (
            "boot IIFE still references the removed initGraph/_graphMode FG init."
        )
        assert "document.body.classList.add('galaxy-active')" in html, (
            "boot IIFE must permanently set body.galaxy-active (CSS keys #right "
            "suppression + overlay handling off it; galaxy is the only mode now)."
        )

    def test_teardown_disposes_and_forces_context_loss(self) -> None:
        js = (_static_dir() / "galaxy-view.js").read_text(encoding="utf-8")
        assert "forceContextLoss()" in js, (
            "galaxy destroy() must call renderer.forceContextLoss() so the WebGL "
            "context is released (the ~16-context ceiling caps mode switching)."
        )
        assert "cancelAnimationFrame" in js, (
            "galaxy destroy()/pause() must cancelAnimationFrame — a leaked RAF keeps "
            "burning CPU after teardown."
        )
        assert "removeEventListener" in js, (
            "galaxy teardown must removeEventListener on the named bound handlers "
            "(the mockup's anonymous arrow listeners were unremovable)."
        )
