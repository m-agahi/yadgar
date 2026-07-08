"""v5.50.13 — /api/viz/config legend block tests + drift-guards.

Tests:
1. legend.categories keys == WikiStore.CATEGORIES; each has non-empty description.
2. legend.edges keys == EDGE_TYPES keys; each has color + description.
3. legend.node_types includes memory/wiki/entity each with a shape.
4. Drift-guard: every edge type string emitted in graph_api exists in EDGE_TYPES.
5. Drift-guard: every category in CATEGORIES appears in legend (add a cat → test fails).
6. category_colors built by iterating CATEGORIES (no independent literal — all 8 present).
7. edge.color keys match EDGE_TYPES keys (config iteration, not a separate set).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("viz_legend")
    from yadgar.core import server

    server.init_engines(db_path=str(tmp_path / "test.db"), embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _get_legend(tmp_path, monkeypatch) -> dict:
    """Return the full /api/viz/config JSON via test client."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok-legend")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")

    from starlette.testclient import TestClient

    from yadgar._shared.config import get_settings
    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    get_settings.cache_clear()
    client = TestClient(
        BearerAuthMiddleware(_server.mcp_server.streamable_http_app()),
        raise_server_exceptions=False,
    )
    resp = client.get("/api/viz/config", headers={"Authorization": "Bearer tok-legend"})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    get_settings.cache_clear()
    return resp.json()


# ---------------------------------------------------------------------------
# 1. legend.categories matches WikiStore.CATEGORIES
# ---------------------------------------------------------------------------


class TestLegendCategories:
    def test_legend_categories_keys_match_CATEGORIES(self, tmp_path, monkeypatch):
        """legend.categories keys exactly equal WikiStore.CATEGORIES."""
        from yadgar._shared.wiki import WikiStore

        data = _get_legend(tmp_path, monkeypatch)
        assert "legend" in data, "legend block missing from /api/viz/config"
        cats = data["legend"]["categories"]
        returned_keys = {c["key"] for c in cats}
        assert returned_keys == WikiStore.CATEGORIES

    def test_legend_categories_each_has_non_empty_description(self, tmp_path, monkeypatch):
        """Every category entry has a non-empty description string."""
        data = _get_legend(tmp_path, monkeypatch)
        for cat in data["legend"]["categories"]:
            assert isinstance(cat.get("description"), str) and cat["description"].strip(), (
                f"Category '{cat['key']}' has empty/missing description"
            )

    def test_legend_categories_each_has_color(self, tmp_path, monkeypatch):
        """Every category entry has a non-empty color string."""
        data = _get_legend(tmp_path, monkeypatch)
        for cat in data["legend"]["categories"]:
            assert isinstance(cat.get("color"), str) and cat["color"].strip(), (
                f"Category '{cat['key']}' has empty/missing color"
            )


# ---------------------------------------------------------------------------
# 2. legend.edges matches EDGE_TYPES
# ---------------------------------------------------------------------------


class TestLegendEdges:
    def test_legend_edges_keys_match_EDGE_TYPES(self, tmp_path, monkeypatch):
        """legend.edges keys exactly equal EDGE_TYPES keys."""
        from yadgar.core.viz_meta import EDGE_TYPES

        data = _get_legend(tmp_path, monkeypatch)
        edges = data["legend"]["edges"]
        returned_keys = {e["key"] for e in edges}
        assert returned_keys == set(EDGE_TYPES.keys())

    def test_legend_edges_each_has_color(self, tmp_path, monkeypatch):
        """Every edge entry has a non-empty color."""
        data = _get_legend(tmp_path, monkeypatch)
        for edge in data["legend"]["edges"]:
            assert isinstance(edge.get("color"), str) and edge["color"].strip(), (
                f"Edge '{edge['key']}' has empty/missing color"
            )

    def test_legend_edges_each_has_non_empty_description(self, tmp_path, monkeypatch):
        """Every edge entry has a non-empty description."""
        data = _get_legend(tmp_path, monkeypatch)
        for edge in data["legend"]["edges"]:
            assert isinstance(edge.get("description"), str) and edge["description"].strip(), (
                f"Edge '{edge['key']}' has empty/missing description"
            )


# ---------------------------------------------------------------------------
# 3. legend.node_types includes memory/wiki/entity with shapes
# ---------------------------------------------------------------------------


class TestLegendNodeTypes:
    def test_legend_node_types_includes_required_keys(self, tmp_path, monkeypatch):
        """node_types includes 'memory', 'wiki', and 'entity'."""
        data = _get_legend(tmp_path, monkeypatch)
        keys = {nt["key"] for nt in data["legend"]["node_types"]}
        assert "memory" in keys, "memory missing from node_types"
        assert "wiki" in keys, "wiki missing from node_types"
        assert "entity" in keys, "entity missing from node_types"

    def test_legend_node_types_each_has_shape(self, tmp_path, monkeypatch):
        """Every node_type entry has a non-empty shape."""
        data = _get_legend(tmp_path, monkeypatch)
        for nt in data["legend"]["node_types"]:
            assert isinstance(nt.get("shape"), str) and nt["shape"].strip(), (
                f"node_type '{nt['key']}' has empty/missing shape"
            )


# ---------------------------------------------------------------------------
# 4. Drift-guard: every edge type emitted by graph_api exists in EDGE_TYPES
# ---------------------------------------------------------------------------

_NODE_TYPE_STRINGS = frozenset({"memory", "wiki", "entity"})


def _collect_type_values_from_dict(ast_dict_node: object) -> set[str]:
    """Return string values of 'type' keys in a single AST Dict node."""
    import ast

    result: set[str] = set()
    for key, val in zip(ast_dict_node.keys, ast_dict_node.values, strict=True):  # type: ignore[attr-defined]
        if isinstance(key, ast.Constant) and key.value == "type":
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                if val.value not in _NODE_TYPE_STRINGS:
                    result.add(val.value)
    return result


class TestEdgeTypeDriftGuard:
    def test_graph_api_edge_types_all_in_EDGE_TYPES(self):
        """Every 'type' string emitted as an edge in graph_api.py must exist in EDGE_TYPES.

        Add a new edge type → this test fails until EDGE_TYPES is updated.
        """
        import ast
        import inspect

        from yadgar.core import graph_api
        from yadgar.core.viz_meta import EDGE_TYPES

        tree = ast.parse(inspect.getsource(graph_api))
        emitted_types: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                emitted_types |= _collect_type_values_from_dict(node)

        orphans = emitted_types - set(EDGE_TYPES.keys())
        assert not orphans, (
            f"Edge types emitted in graph_api.py but missing from EDGE_TYPES: {orphans}. "
            "Add them to yadgar/viz_meta.py EDGE_TYPES."
        )


# ---------------------------------------------------------------------------
# 5. Drift-guard: every category in CATEGORIES appears in legend
# ---------------------------------------------------------------------------


class TestCategoryDriftGuard:
    def test_every_CATEGORY_in_legend(self, tmp_path, monkeypatch):
        """Every category in WikiStore.CATEGORIES has a legend entry.

        Add a category → this fails until it's described.
        """
        from yadgar._shared.wiki import WikiStore

        data = _get_legend(tmp_path, monkeypatch)
        returned_keys = {c["key"] for c in data["legend"]["categories"]}
        missing = WikiStore.CATEGORIES - returned_keys
        assert not missing, (
            f"Categories in WikiStore.CATEGORIES missing from legend: {missing}. "
            "They must appear in the legend block."
        )


# ---------------------------------------------------------------------------
# 6. category_colors in config built by iterating CATEGORIES
# ---------------------------------------------------------------------------


class TestCategoryColorsIteration:
    def test_category_colors_covers_all_CATEGORIES(self, tmp_path, monkeypatch):
        """node.category_colors has a key for every category in CATEGORIES."""
        from yadgar._shared.wiki import WikiStore

        data = _get_legend(tmp_path, monkeypatch)
        colors = data["node"]["category_colors"]
        for cat in WikiStore.CATEGORIES:
            assert cat in colors, f"Category '{cat}' missing from node.category_colors"

    def test_category_colors_no_extra_hardcoded_keys(self, tmp_path, monkeypatch):
        """node.category_colors has ONLY keys that are in CATEGORIES (no extra literal keys)."""
        from yadgar._shared.wiki import WikiStore

        data = _get_legend(tmp_path, monkeypatch)
        colors = data["node"]["category_colors"]
        extra = set(colors.keys()) - WikiStore.CATEGORIES
        assert not extra, f"Unexpected extra category color keys (not in CATEGORIES): {extra}"


# ---------------------------------------------------------------------------
# 7. edge.color keys match EDGE_TYPES
# ---------------------------------------------------------------------------


class TestEdgeColorIteration:
    def test_edge_color_covers_all_EDGE_TYPES(self, tmp_path, monkeypatch):
        """edge.color has a key for every entry in EDGE_TYPES."""
        from yadgar.core.viz_meta import EDGE_TYPES

        data = _get_legend(tmp_path, monkeypatch)
        edge_color = data["edge"]["color"]
        for key in EDGE_TYPES:
            assert key in edge_color, f"Edge type '{key}' missing from edge.color"
