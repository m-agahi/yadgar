"""test_viz_config_defaults.py — v5.50.0 three-way config registry tests.

Tests:
- VIZ_EDGE_WIDTH_3D_MULTIPLIER default changed to 1.8
- VIZ_PHYSICS_CHARGE_STRENGTH default changed to -18.0
- VIZ_EDGE_OPACITY new setting defaults to 0.9
- VIZ_WIKI_SHAPE new setting defaults to 'octahedron'
- VIZ_EDGE_VARIANT new setting defaults to 'C'
- config_registry.py has matching ConfigEntry defaults
- config_yaml.py has matching KNOBS descriptions
- /api/viz/config includes edge.opacity, node.wiki_shape, edge.variant

Run:
  OTEL_SDK_DISABLED=true uv run --extra test pytest yadgar/tests/test_viz_config_defaults.py -p no:xdist -v
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Settings (config.py) defaults
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    """config.py Settings class must have updated defaults for v5.50.0 Variant C."""

    def test_edge_width_3d_multiplier_default_is_1_8(self) -> None:
        """Variant C: VIZ_EDGE_WIDTH_3D_MULTIPLIER must default to 1.8."""
        # Unset env override so we get the class default
        os.environ.pop("YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER", None)
        from yadgar.config import Settings  # noqa: PLC0415

        s = Settings()
        assert s.VIZ_EDGE_WIDTH_3D_MULTIPLIER == 1.8, (
            f"Expected 1.8, got {s.VIZ_EDGE_WIDTH_3D_MULTIPLIER}"
        )

    def test_physics_charge_strength_default_is_minus_18(self) -> None:
        """v5.50.0: VIZ_PHYSICS_CHARGE_STRENGTH must default to -18.0."""
        os.environ.pop("YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH", None)
        from yadgar.config import Settings  # noqa: PLC0415

        s = Settings()
        assert s.VIZ_PHYSICS_CHARGE_STRENGTH == -18.0, (
            f"Expected -18.0, got {s.VIZ_PHYSICS_CHARGE_STRENGTH}"
        )

    def test_viz_edge_opacity_exists_and_defaults_to_0_9(self) -> None:
        """v5.50.0 Variant C: VIZ_EDGE_OPACITY must exist and default to 0.9."""
        os.environ.pop("YADGAR_VIZ_EDGE_OPACITY", None)
        from yadgar.config import Settings  # noqa: PLC0415

        s = Settings()
        assert hasattr(s, "VIZ_EDGE_OPACITY"), (
            "Settings missing VIZ_EDGE_OPACITY — add it to yadgar/config.py"
        )
        assert s.VIZ_EDGE_OPACITY == 0.9, f"Expected 0.9, got {s.VIZ_EDGE_OPACITY}"

    def test_viz_wiki_shape_exists_and_defaults_to_octahedron(self) -> None:
        """v5.50.0: VIZ_WIKI_SHAPE must exist and default to 'octahedron'."""
        os.environ.pop("YADGAR_VIZ_WIKI_SHAPE", None)
        from yadgar.config import Settings  # noqa: PLC0415

        s = Settings()
        assert hasattr(s, "VIZ_WIKI_SHAPE"), (
            "Settings missing VIZ_WIKI_SHAPE — add it to yadgar/config.py"
        )
        assert s.VIZ_WIKI_SHAPE == "octahedron", f"Expected 'octahedron', got {s.VIZ_WIKI_SHAPE!r}"

    def test_viz_edge_variant_exists_and_defaults_to_C(self) -> None:
        """v5.50.0: VIZ_EDGE_VARIANT must exist and default to 'C'."""
        os.environ.pop("YADGAR_VIZ_EDGE_VARIANT", None)
        from yadgar.config import Settings  # noqa: PLC0415

        s = Settings()
        assert hasattr(s, "VIZ_EDGE_VARIANT"), (
            "Settings missing VIZ_EDGE_VARIANT — add it to yadgar/config.py"
        )
        assert s.VIZ_EDGE_VARIANT == "C", f"Expected 'C', got {s.VIZ_EDGE_VARIANT!r}"


# ---------------------------------------------------------------------------
# config_registry.py ConfigEntry defaults
# ---------------------------------------------------------------------------


class TestConfigRegistryEntries:
    """config_registry.py _REGISTRY must have updated/new entries for v5.50.0."""

    def _registry(self):
        from yadgar.config_registry import _REGISTRY  # noqa: PLC0415

        return {e.name: e for e in _REGISTRY}

    def test_edge_width_3d_multiplier_registry_default_is_1_8(self) -> None:
        registry = self._registry()
        assert "YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER" in registry, (
            "YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER missing from config_registry.py"
        )
        entry = registry["YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER"]
        assert entry.default == "1.8", f"Expected default '1.8', got {entry.default!r}"

    def test_physics_charge_strength_registry_default_is_minus_18(self) -> None:
        registry = self._registry()
        assert "YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH" in registry
        entry = registry["YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH"]
        assert entry.default == "-18.0", f"Expected default '-18.0', got {entry.default!r}"

    def test_viz_edge_opacity_in_registry(self) -> None:
        registry = self._registry()
        assert "YADGAR_VIZ_EDGE_OPACITY" in registry, (
            "YADGAR_VIZ_EDGE_OPACITY missing from config_registry.py _REGISTRY"
        )
        entry = registry["YADGAR_VIZ_EDGE_OPACITY"]
        assert entry.default == "0.9", f"Expected default '0.9', got {entry.default!r}"
        assert entry.kind == "float", f"Expected kind 'float', got {entry.kind!r}"

    def test_viz_wiki_shape_in_registry(self) -> None:
        registry = self._registry()
        assert "YADGAR_VIZ_WIKI_SHAPE" in registry, (
            "YADGAR_VIZ_WIKI_SHAPE missing from config_registry.py _REGISTRY"
        )
        entry = registry["YADGAR_VIZ_WIKI_SHAPE"]
        assert entry.default == "octahedron", (
            f"Expected default 'octahedron', got {entry.default!r}"
        )
        assert entry.kind == "string", f"Expected kind 'string', got {entry.kind!r}"

    def test_viz_edge_variant_in_registry(self) -> None:
        registry = self._registry()
        assert "YADGAR_VIZ_EDGE_VARIANT" in registry, (
            "YADGAR_VIZ_EDGE_VARIANT missing from config_registry.py _REGISTRY"
        )
        entry = registry["YADGAR_VIZ_EDGE_VARIANT"]
        assert entry.default == "C", f"Expected default 'C', got {entry.default!r}"
        assert entry.kind == "string", f"Expected kind 'string', got {entry.kind!r}"


# ---------------------------------------------------------------------------
# config_yaml.py KNOBS
# ---------------------------------------------------------------------------


class TestConfigYamlFieldMeta:
    """config_yaml.py FIELD_META dict must have entries for new/updated viz knobs."""

    def _field_meta(self):
        from yadgar.config_yaml import FIELD_META  # noqa: PLC0415

        return FIELD_META

    def test_viz_edge_opacity_in_field_meta(self) -> None:
        meta = self._field_meta()
        assert "viz_edge_opacity" in meta, (
            "viz_edge_opacity missing from config_yaml.py FIELD_META dict"
        )
        assert meta["viz_edge_opacity"].get("section") == "viz_config", (
            "viz_edge_opacity must be in section 'viz_config'"
        )

    def test_viz_wiki_shape_in_field_meta(self) -> None:
        meta = self._field_meta()
        assert "viz_wiki_shape" in meta, (
            "viz_wiki_shape missing from config_yaml.py FIELD_META dict"
        )
        assert meta["viz_wiki_shape"].get("section") == "viz_config"

    def test_viz_edge_variant_in_field_meta(self) -> None:
        meta = self._field_meta()
        assert "viz_edge_variant" in meta, (
            "viz_edge_variant missing from config_yaml.py FIELD_META dict"
        )
        assert meta["viz_edge_variant"].get("section") == "viz_config"


# ---------------------------------------------------------------------------
# /api/viz/config response includes new fields
# ---------------------------------------------------------------------------


class TestVizConfigApiResponse:
    """api_viz_config() must include edge.opacity, node.wiki_shape, edge.variant."""

    def test_api_viz_config_includes_edge_opacity(self) -> None:
        """GET /api/viz/config must include edge.opacity."""
        import inspect  # noqa: PLC0415

        from yadgar.server import http  # noqa: PLC0415

        src = inspect.getsource(http.api_viz_config)
        assert "opacity" in src, (
            "api_viz_config missing 'opacity' key — "
            "add edge.opacity: s.VIZ_EDGE_OPACITY to the response dict"
        )

    def test_api_viz_config_includes_node_wiki_shape(self) -> None:
        """GET /api/viz/config must include node.wiki_shape."""
        import inspect  # noqa: PLC0415

        from yadgar.server import http  # noqa: PLC0415

        src = inspect.getsource(http.api_viz_config)
        assert "wiki_shape" in src, (
            "api_viz_config missing 'wiki_shape' key — "
            "add node.wiki_shape: s.VIZ_WIKI_SHAPE to the response dict"
        )

    def test_api_viz_config_includes_edge_variant(self) -> None:
        """GET /api/viz/config must include edge.variant."""
        import inspect  # noqa: PLC0415

        from yadgar.server import http  # noqa: PLC0415

        src = inspect.getsource(http.api_viz_config)
        assert "variant" in src, (
            "api_viz_config missing 'variant' key — "
            "add edge.variant: s.VIZ_EDGE_VARIANT to the response dict"
        )


# ---------------------------------------------------------------------------
# YADGAR_VIZ_CONFIG JS defaults in index.html
# ---------------------------------------------------------------------------


class TestVizConfigJsDefaults:
    """index.html YADGAR_VIZ_CONFIG must reflect updated v5.50.0 defaults."""

    def _html(self) -> str:
        import pathlib  # noqa: PLC0415

        static_dir = pathlib.Path(__file__).parent.parent / "static"
        return (static_dir / "index.html").read_text(encoding="utf-8")

    def test_js_default_edge_width_3d_is_1_8(self) -> None:
        """YADGAR_VIZ_CONFIG in index.html must default edge width to 1.8."""
        html = self._html()
        # The JS default object should use 1.8 (changed from 1.5)
        assert "width_3d_multiplier: 1.8" in html or "width_3d_multiplier:1.8" in html, (
            "YADGAR_VIZ_CONFIG in index.html missing 'width_3d_multiplier: 1.8' — "
            "update JS hardcoded default from 1.5 to 1.8"
        )

    def test_js_default_physics_charge_is_minus_18(self) -> None:
        """YADGAR_VIZ_CONFIG in index.html must default charge to -18."""
        html = self._html()
        assert "charge_strength: -18" in html or "charge_strength:-18" in html, (
            "YADGAR_VIZ_CONFIG in index.html missing 'charge_strength: -18' — "
            "update JS hardcoded default from -12 to -18"
        )

    def test_js_default_edge_opacity_present(self) -> None:
        """YADGAR_VIZ_CONFIG in index.html must include edge.opacity default."""
        html = self._html()
        assert "opacity: 0.9" in html or "opacity:0.9" in html, (
            "YADGAR_VIZ_CONFIG in index.html missing 'opacity: 0.9' — "
            "add edge.opacity to the JS default config object"
        )
