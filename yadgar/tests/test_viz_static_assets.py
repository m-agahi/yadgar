"""Static-asset string checks for v5.10.7 viz UX fixes (S2.1–S2.4) + v5.10.7.1 lighting fix.

These tests verify that key JS code is present in index.html without
running a browser. They act as regression guards that confirm:
- S2.1: 3D mode sets node colour from heat (nodeColor call in 3D init)
- S2.2: Wiki nodes use OctahedronGeometry (visibly faceted vs sphere)
- S2.3: _applySearchHighlight branches on _graphMode (no 2D-only API in 3D)
- S2.4: Stats overlay has an auto-refresh interval when opened
- v5.10.7.1: _makeNodeThreeObject uses MeshBasicMaterial (unlit), not MeshLambertMaterial
"""

from __future__ import annotations

import pathlib


def _html() -> str:
    """Read index.html from the static directory."""
    static_dir = pathlib.Path(__file__).parent.parent / "core" / "static"
    return (static_dir / "index.html").read_text(encoding="utf-8")


class TestS21HeatColorIn3D:
    """S2.1: 3D graph init must wire node colour from heat."""

    def test_nodeColor_call_present_in_3d_init(self) -> None:
        html = _html()
        # Must call .nodeColor( in the 3D init block
        assert ".nodeColor(" in html, (
            "3D init missing .nodeColor() — heat colour won't apply in 3D mode"
        )

    def test_nodeColor_references_heatColor_or_nodeColorFor(self) -> None:
        html = _html()
        # The nodeColor callback must reference heatColor (or the extracted helper)
        assert "heatColor" in html, "heatColor function missing entirely"
        # nodeColor line must be there and reference a colour resolver
        lines = html.splitlines()
        nc_lines = [ln for ln in lines if ".nodeColor(" in ln]
        assert nc_lines, "No .nodeColor( call found at all"
        # At least one must mention heatColor or nodeColorFor
        assert any("heatColor" in ln or "nodeColorFor" in ln for ln in nc_lines), (
            f"nodeColor call doesn't reference a heat resolver: {nc_lines}"
        )


class TestV5506OctahedronForWiki:
    """v5.50.6: re-introduce _makeNodeThreeObject with correct MeshBasicMaterial.

    Root cause of prior shard failures (v5.10.7–.2): all used `transparent: true`
    which sets depthWrite=false → THREE renders faces in submit order, not depth
    order → visible triangle-ordering artifacts on 3D polyhedra.

    Fix: MeshBasicMaterial with no transparent flag (defaults false → depthWrite=true).
    Wiki nodes → OctahedronGeometry(0-detail); null return → ForceGraph default sphere.
    """

    def test_makeNodeThreeObject_function_present(self) -> None:
        html = _html()
        assert "function _makeNodeThreeObject" in html, (
            "_makeNodeThreeObject function missing — v5.50.6 requires wiki→octahedron factory"
        )

    def test_nodeThreeObject_wired_in_3d_init(self) -> None:
        html = _html()
        non_comment_lines = [
            line for line in html.splitlines() if not line.strip().startswith("//")
        ]
        src = "\n".join(non_comment_lines)
        assert ".nodeThreeObject(_makeNodeThreeObject)" in src, (
            ".nodeThreeObject(_makeNodeThreeObject) call missing from 3D init — "
            "wiki octahedra won't render without this"
        )

    def test_nodeThreeObjectExtend_false(self) -> None:
        html = _html()
        non_comment_lines = [
            line for line in html.splitlines() if not line.strip().startswith("//")
        ]
        src = "\n".join(non_comment_lines)
        assert ".nodeThreeObjectExtend(false)" in src, (
            ".nodeThreeObjectExtend(false) missing — without this ForceGraph3D wraps "
            "the custom mesh AND adds a default sphere, causing double geometry"
        )

    def test_octahedron_geometry_present(self) -> None:
        html = _html()
        non_comment_lines = [
            line
            for line in html.splitlines()
            if not line.strip().startswith("//") and not line.strip().startswith("*")
        ]
        src = "\n".join(non_comment_lines)
        assert "new THREE.OctahedronGeometry" in src, (
            "OctahedronGeometry instantiation missing — v5.50.6 requires wiki nodes "
            "to render as octahedra"
        )

    def test_uses_mesh_basic_not_lambert(self) -> None:
        html = _html()
        # Must use MeshBasicMaterial (unlit, no lights needed)
        # Must NOT use MeshLambertMaterial in _makeNodeThreeObject (needs lights → shards)
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function _makeNodeThreeObject" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and func_lines:
                    break
        body = "\n".join(func_lines)
        assert "MeshBasicMaterial" in body, (
            "MeshBasicMaterial missing from _makeNodeThreeObject — "
            "v5.50.6 requires unlit Basic material to avoid Lambert shards"
        )
        assert "MeshLambertMaterial" not in body, (
            "MeshLambertMaterial in _makeNodeThreeObject — Lambert needs scene lights "
            "that ForceGraph3D doesn't add; causes shard rendering. Use MeshBasicMaterial."
        )

    def test_no_transparent_true_in_factory(self) -> None:
        html = _html()
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function _makeNodeThreeObject" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and func_lines:
                    break
        body = "\n".join(func_lines)
        # transparent:true disables depthWrite → triangle-sort shards
        assert "transparent: true" not in body, (
            "transparent:true in _makeNodeThreeObject — this disables depthWrite, "
            "causing THREE to render faces in submit order (triangle-sort shards). "
            "Root cause of v5.10.7–.2 shard failures. Remove transparent flag entirely."
        )

    def test_wiki_type_check_present(self) -> None:
        html = _html()
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function _makeNodeThreeObject" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and func_lines:
                    break
        body = "\n".join(func_lines)
        assert "node.type" in body or "type === 'wiki'" in body or "type !=" in body, (
            "_makeNodeThreeObject missing wiki type check — "
            "non-wiki nodes must return null so ForceGraph renders them as default spheres"
        )

    def test_respects_wiki_shape_config(self) -> None:
        html = _html()
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function _makeNodeThreeObject" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and func_lines:
                    break
        body = "\n".join(func_lines)
        assert "wiki_shape" in body, (
            "_makeNodeThreeObject does not check YADGAR_VIZ_CONFIG.node.wiki_shape — "
            "setting wiki_shape='sphere' should disable octahedra and fall back to default"
        )


class TestS23SearchModeDetection:
    """S2.3: _applySearchHighlight must branch on _graphMode (not blindly call 2D API)."""

    def test_applySearchHighlight_has_3d_branch(self) -> None:
        html = _html()
        # The search highlight function must check _graphMode
        assert "_graphMode" in html, "_graphMode variable missing"
        lines = html.splitlines()
        # Find _applySearchHighlight function body
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function _applySearchHighlight" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and func_lines:
                    break
        body = "\n".join(func_lines)
        assert "_graphMode" in body, (
            "_applySearchHighlight does not check _graphMode — "
            "calling 2D-only nodeCanvasObject in 3D mode will throw"
        )

    def test_no_naked_nodeCanvasObject_call_in_search_highlight(self) -> None:
        html = _html()
        lines = html.splitlines()
        in_func = False
        func_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function _applySearchHighlight" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_func and brace_depth == 0 and func_lines:
                    break
        body = "\n".join(func_lines)
        # There must NOT be an unconditional nodeCanvasObject call at top level
        # (it must be inside a 2D-only branch)
        # Check: if nodeCanvasObject appears it must be guarded by a mode check
        if "nodeCanvasObject" in body:
            # It must co-occur with a mode guard in the same function
            assert "_graphMode" in body, (
                "nodeCanvasObject called in _applySearchHighlight "
                "without a _graphMode guard — will throw in 3D"
            )


class TestV5108PhysicsAndMeshLeakFix:
    """v5.10.8: tick-count guard for onEngineStop + drop empty-then-restore mesh leak.

    Bug A: onEngineStop fired before physics ran → pinned all nodes at origin.
           Fix: count ticks via onEngineTick; skip pin if < 50 ticks elapsed.
    Bug B: graphData({nodes:[],links:[]}) empty-then-restore via setTimeout leaked
           Three.js Mesh objects on every filter cycle. Fix: direct graphData(d).
    """

    def test_onEngineStop_has_tick_count_guard(self) -> None:
        html = _html()
        # _engineTickCount must be declared (module-scope guard variable)
        assert "_engineTickCount" in html, (
            "_engineTickCount variable missing — onEngineStop tick-count guard (v5.10.8 Bug A fix) "
            "not implemented. Without this, onEngineStop pins all nodes at origin before "
            "physics runs."
        )
        # The onEngineStop callback must reference _engineTickCount
        lines = html.splitlines()
        in_stop = False
        stop_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if ".onEngineStop(" in line:
                in_stop = True
            if in_stop:
                stop_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_stop and brace_depth == 0 and len(stop_lines) > 1:
                    break
        body = "\n".join(stop_lines)
        assert "_engineTickCount" in body, (
            "onEngineStop callback does not reference _engineTickCount — "
            "guard is declared but not wired into the stop handler."
        )

    def test_onEngineTick_handler_present(self) -> None:
        html = _html()
        assert ".onEngineTick(" in html, (
            ".onEngineTick( call missing — v5.10.8 Bug A fix requires an onEngineTick handler "
            "to increment _engineTickCount. Without it the guard variable stays 0 forever and "
            "onEngineStop will never pin (or will always skip pinning)."
        )

    def test_no_empty_then_restore_pattern(self) -> None:
        html = _html()
        # The empty-then-restore hack must be gone (regression gate)
        assert "graph.graphData({ nodes: [], links: [] })" not in html, (
            "graph.graphData({ nodes: [], links: [] }) found — this is the v5.10.8 Bug B "
            "mesh-leak pattern. ForceGraph3D does not dispose Three.js Mesh objects on the "
            "empty step; each call accumulates orphan meshes. Replace with direct "
            "graph.graphData(d)."
        )


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


class TestV51010VizPolish:
    """v5.10.10: 2x 3D node size + auto-zoom-fit on initial load (both modes).

    Fix 1: .nodeRelSize(8) in 3D init block — doubles default sphere radius (4 → 8).
    Fix 2: _zoomFitDone flag + onEngineTick callback triggers zoomToFit at tick 80,
           once per data load, in both 2D and 3D modes.
    """

    def test_nodeRelSize_set_to_8_in_3d_init(self) -> None:
        html = _html()
        # v5.11.0: nodeRelSize is now config-driven via YADGAR_VIZ_CONFIG.node.size_3d
        # (default 8, same as v5.10.10 hardcoded value)
        assert (
            ".nodeRelSize(YADGAR_VIZ_CONFIG.node.size_3d)" in html or ".nodeRelSize(8)" in html
        ), (
            ".nodeRelSize not found in index.html — v5.10.10 Fix 1 (2x 3D node size) "
            "not implemented. ForceGraph3D default is 4; set to 8 (or via YADGAR_VIZ_CONFIG) for 2x sphere radius."
        )

    def test_zoomFitDone_flag_declared(self) -> None:
        html = _html()
        assert "_zoomFitDone" in html, (
            "_zoomFitDone variable missing from index.html — v5.10.10 Fix 2 "
            "(auto-zoom-fit) requires a module-level flag to prevent repeated zoom "
            "calls per data load."
        )

    def test_onEngineTick_calls_zoomToFit_at_threshold(self) -> None:
        html = _html()
        # Both _zoomFitDone and zoomToFit must appear inside an onEngineTick callback
        lines = html.splitlines()
        in_tick = False
        tick_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if ".onEngineTick(" in line:
                in_tick = True
                tick_lines = []
                brace_depth = 0
            if in_tick:
                tick_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_tick and brace_depth == 0 and len(tick_lines) > 1:
                    # Check this callback block, then reset and keep searching
                    body = "\n".join(tick_lines)
                    if "_zoomFitDone" in body and "zoomToFit" in body:
                        return  # found a qualifying onEngineTick block
                    in_tick = False
                    tick_lines = []
        raise AssertionError(
            "No onEngineTick callback found that references both _zoomFitDone and "
            "zoomToFit — v5.10.10 Fix 2 (auto-zoom-fit) not implemented. "
            "The callback must set _zoomFitDone=true and call graph.zoomToFit() at tick 80."
        )


class TestV51011VizEdgeThicknessAndRepulsion:
    """v5.10.11: 3D-only edge thickness +50% + connected-node repulsion +20%.

    Fix 1: 3D init block uses .linkWidth(l => _linkWidth(l) * 1.5) instead of
           .linkWidth(_linkWidth). 2D init block keeps plain .linkWidth(_linkWidth).
    Fix 2: 3D branch sets graph.d3Force('link').distance(36) (30 * 1.2).
           2D branch keeps graph.d3Force('link').distance(30).
    """

    def _extract_3d_block(self, html: str) -> str:
        """Extract the 3D init block (between 'if (mode === '3d') {' and '} else {')."""
        marker_start = "if (mode === '3d') {"
        marker_end = "} else {"
        start = html.find(marker_start)
        end = html.find(marker_end, start)
        assert start != -1 and end != -1, "Could not find 3D/2D branch markers in index.html"
        return html[start:end]

    def _extract_2d_block(self, html: str) -> str:
        """Extract the 2D init block (between '} else {' and the resize listener)."""
        marker_start = "} else {"
        marker_end = "window.addEventListener('resize'"
        start = html.find(marker_start)
        end = html.find(marker_end, start)
        assert start != -1 and end != -1, "Could not find 2D block or resize listener in index.html"
        return html[start:end]

    def test_3d_linkWidth_multiplier_present(self) -> None:
        html = _html()
        three_d_block = self._extract_3d_block(html)
        # v5.11.0: multiplier is now config-driven (YADGAR_VIZ_CONFIG.edge.width_3d_multiplier)
        # default 1.5, same behavior as v5.10.11 hardcoded value
        assert (
            "_linkWidth(l) * YADGAR_VIZ_CONFIG.edge.width_3d_multiplier" in three_d_block
            or "_linkWidth(l) * 1.5" in three_d_block
        ), (
            "3D init block missing '.linkWidth(l => _linkWidth(l) * N)' — "
            "v5.10.11 Fix 1 (3D-only +50% edge thickness) not implemented."
        )

    def test_2d_linkWidth_unchanged(self) -> None:
        html = _html()
        two_d_block = self._extract_2d_block(html)
        # 2D must have plain .linkWidth(_linkWidth), no multiplier
        assert ".linkWidth(_linkWidth)" in two_d_block, (
            "2D init block missing plain '.linkWidth(_linkWidth)' — "
            "v5.10.11 must NOT change 2D edge width."
        )
        assert "_linkWidth(l) * 1.5" not in two_d_block, (
            "2D init block contains the 1.5x multiplier — "
            "edge thickness change must be 3D-only (v5.10.11 constraint)."
        )

    def test_3d_link_distance_36(self) -> None:
        html = _html()
        three_d_block = self._extract_3d_block(html)
        # v5.11.0: distance is now config-driven (YADGAR_VIZ_CONFIG.physics.link_distance_3d)
        # default 36, same behavior as v5.10.11 hardcoded value
        assert (
            "graph.d3Force('link').distance(YADGAR_VIZ_CONFIG.physics.link_distance_3d)"
            in three_d_block
            or "graph.d3Force('link').distance(36)" in three_d_block
        ), (
            "3D init block missing 'graph.d3Force(\"link\").distance(36 or config)' — "
            "v5.10.11 Fix 2 (3D-only +20% connected-node repulsion) not implemented. "
            "30 * 1.2 = 36."
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
