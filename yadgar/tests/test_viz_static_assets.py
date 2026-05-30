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
    static_dir = pathlib.Path(__file__).parent.parent / "static"
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


class TestV510703RevertCustomMesh:
    """v5.10.7.3: custom 3D node mesh REVERTED. Three attempts failed (Lambert v5.10.7,
    Basic v5.10.7.1, conditional transparent v5.10.7.2) — all rendered as fragmented shards.
    Fall back to ForceGraph3D default solid spheres. Regression gates below prevent
    accidental re-introduction without deeper investigation.
    """

    def test_no_makeNodeThreeObject_function(self) -> None:
        html = _html()
        # Function definition must be gone (comment mentioning the removal is OK)
        assert "function _makeNodeThreeObject" not in html, (
            "_makeNodeThreeObject function re-introduced — three attempts at custom mesh "
            "produced fragmented shards. Don't re-add without deeper ForceGraph3D + ThreeJS "
            "investigation. See docs/PLAN_V5_10_7_3_VIZ_REVERT_TO_DEFAULTS.md."
        )

    def test_no_nodeThreeObject_call(self) -> None:
        html = _html()
        # Any .nodeThreeObject( call (not in a comment) is a regression
        lines_with_call = [
            line
            for line in html.splitlines()
            if ".nodeThreeObject(" in line and not line.strip().startswith("//")
        ]
        assert not lines_with_call, (
            f".nodeThreeObject( call present (not in a comment): {lines_with_call[:3]} — "
            "v5.10.7.3 revert dropped this. Re-introducing requires new plan."
        )

    def test_no_octahedron_or_custom_sphere_geometry(self) -> None:
        html = _html()
        # Custom OctahedronGeometry should not be in non-comment code
        non_comment_lines = [
            line
            for line in html.splitlines()
            if not line.strip().startswith("//") and not line.strip().startswith("*")
        ]
        non_comment_src = "\n".join(non_comment_lines)
        assert "new THREE.OctahedronGeometry" not in non_comment_src, (
            "OctahedronGeometry instantiation re-introduced — custom 3D mesh reverted in v5.10.7.3"
        )
        assert "new THREE.SphereGeometry" not in non_comment_src, (
            "SphereGeometry instantiation re-introduced — custom 3D mesh reverted in v5.10.7.3"
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


class TestS24StatsAutoRefresh:
    """S2.4: Stats overlay must refresh on a poll interval, not just on open."""

    def test_stats_refresh_interval_present(self) -> None:
        html = _html()
        # openStats or its helpers must set up an interval
        assert "_statsRefreshInterval" in html or "setInterval" in html, (
            "No setInterval found — Stats panel will show static numbers"
        )

    def test_stats_interval_calls_refreshStats(self) -> None:
        html = _html()
        lines = html.splitlines()
        # Find setInterval calls that reference refreshStats
        interval_lines = [ln for ln in lines if "setInterval" in ln and "refreshStats" in ln]
        assert interval_lines, (
            "No setInterval(refreshStats, ...) call found — "
            "Stats panel won't auto-update after initial open"
        )

    def test_stats_interval_cleared_on_close(self) -> None:
        html = _html()
        # closeStats must clear the interval to avoid leaks
        lines = html.splitlines()
        in_close = False
        close_lines: list[str] = []
        brace_depth = 0
        for line in lines:
            if "function closeStats" in line:
                in_close = True
            if in_close:
                close_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if in_close and brace_depth == 0 and close_lines:
                    break
        body = "\n".join(close_lines)
        assert "clearInterval" in body, (
            "closeStats() does not call clearInterval — interval leaks when stats closed"
        )


# v5.10.7.3: TestV510701LightingFix removed (entire class). Custom mesh reverted —
# regression gates live in TestV510703RevertCustomMesh above.


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
