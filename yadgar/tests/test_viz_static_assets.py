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


class TestS22OctahedronForWiki:
    """S2.2: Wiki nodes must use OctahedronGeometry."""

    def test_octahedron_geometry_present(self) -> None:
        html = _html()
        assert "OctahedronGeometry" in html, (
            "OctahedronGeometry missing — wiki nodes won't be visibly distinct"
        )

    def test_sphere_geometry_present_for_memory(self) -> None:
        html = _html()
        assert "SphereGeometry" in html, (
            "SphereGeometry missing — memory nodes need sphere shape in 3D"
        )

    def test_nodeThreeObject_present_in_3d_init(self) -> None:
        html = _html()
        assert ".nodeThreeObject(" in html, (
            ".nodeThreeObject( not set — shapes won't be custom in 3D mode"
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


class TestV510701LightingFix:
    """v5.10.7.1: _makeNodeThreeObject must use MeshBasicMaterial (unlit), not MeshLambertMaterial.

    Lambert requires scene lights; ForceGraph3D adds none by default → nodes render dark/fragmented.
    Basic is unlit — colour always visible regardless of scene lighting.
    """

    @staticmethod
    def _node_obj_block(html: str) -> str:
        """Extract the _makeNodeThreeObject function body via brace-depth scan."""
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
        return "\n".join(func_lines)

    def test_mesh_basic_material_present_in_node_obj(self) -> None:
        html = _html()
        block = self._node_obj_block(html)
        assert block, "_makeNodeThreeObject function not found in index.html"
        assert "MeshBasicMaterial" in block, (
            "_makeNodeThreeObject uses something other than MeshBasicMaterial — "
            "nodes may render dark without scene lights"
        )

    def test_mesh_lambert_material_absent_in_node_obj(self) -> None:
        html = _html()
        block = self._node_obj_block(html)
        assert block, "_makeNodeThreeObject function not found in index.html"
        assert "MeshLambertMaterial" not in block, (
            "_makeNodeThreeObject still uses MeshLambertMaterial — "
            "nodes will render as dark fragments (no scene lights in ForceGraph3D)"
        )
