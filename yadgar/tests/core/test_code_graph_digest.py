"""TDD tests for the code_graph digest renderer (Car C, ADR-0162).

``render_digest`` is a PURE function: architecture dict + endpoint rows + identity
→ compact markdown digest, hard-capped at DIGEST_CHAR_BUDGET, deterministic
(sorted, no timestamps) so golden output is stable.

Coverage
--------
1. Header / layers / hotspots present + ordered (priority: header > layers >
   hotspots > endpoints).
2. ``routes[]`` (URL-literal noise) NEVER leaks into the digest.
3. Endpoints rendered from route_method rows; ``(none extracted)`` on empty.
4. Deterministic: same input → identical bytes (tie-broken sort).
5. Budget: output ≤ DIGEST_CHAR_BUDGET; a huge input truncates + stays ≤ budget
   with the ellipsis marker counted.
6. Stale line rendered from identity when present.
7. Endpoint-row extraction is tolerant of the (unverified) key shape.
"""

from __future__ import annotations


def _java_arch() -> dict:
    """A realistic Java/Spring get_architecture(all) dict (documented shape).

    Includes a NOISE ``routes[]`` of URL literals that MUST be ignored, and an
    ``entry`` layer so entry-points can be derived from ``layers`` (there is no
    ``entry_points`` key in the measured shape).
    """
    return {
        "project": "manage-validation",
        "total_nodes": 744,
        "total_edges": 1930,
        "node_labels": [{"label": "Method", "count": 420}, {"label": "Class", "count": 180}],
        "edge_types": [{"type": "CALLS", "count": 1200}, {"type": "IMPORTS", "count": 730}],
        "languages": [
            {"language": "Java", "file_count": 88},
            {"language": "XML", "file_count": 6},
        ],
        "packages": ["com.quinyx.validation", "com.quinyx.validation.api"],
        "hotspots": [
            {"name": "validate", "qualified_name": "ValidationService.validate", "fan_in": 42},
            {"name": "resolve", "qualified_name": "GroupResolver.resolve", "fan_in": 18},
            {"name": "check", "qualified_name": "RuleChecker.check", "fan_in": 18},
        ],
        "boundaries": [],
        "layers": [
            {
                "name": "ValidationController",
                "layer": "api",
                "reason": "has HTTP route definitions",
            },
            {
                "name": "ValidationService",
                "layer": "service",
                "reason": "orchestrates domain logic",
            },
            {"name": "Main", "layer": "entry", "reason": "application entry point"},
        ],
        "clusters": [],
        "file_tree": [],
        # NOISE — URL literals; the renderer must IGNORE this entirely.
        "routes": ["/validate", "/by-group/{id}", "/internal/v1/health"],
    }


def _endpoint_rows() -> list:
    """Rows as returned by the route_method Cypher (key shape unverified → tolerant)."""
    return [
        {"m.route_method": "POST", "m.route_path": "/validate", "m.name": "validate"},
        {"m.route_method": "GET", "m.route_path": "/by-group/{id}", "m.name": "byGroup"},
    ]


class TestStructure:
    def test_header_layers_hotspots_present_and_ordered(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(
            _java_arch(),
            _endpoint_rows(),
            {"canonical_root": "/repo", "subdir": ""},
        )

        # header first
        assert out.startswith("── code_graph:")
        assert "Java" in out  # language in header

        # priority order: header < layers < hotspots < endpoints (by index)
        i_layers = out.index("layers:")
        i_hot = out.index("hotspots:")
        i_end = out.index("endpoints:")
        assert i_layers < i_hot < i_end

        # layer reason surfaces
        assert "api" in out
        assert "has HTTP route definitions" in out
        # top hotspot by fan_in
        assert "ValidationService.validate" in out
        assert "42" in out

    def test_entry_points_derived_from_layers(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(_java_arch(), [], {"canonical_root": "/repo", "subdir": ""})
        # entry-point derived from layers[layer==entry], not a fictional key
        assert "Main" in out


class TestRoutesNoise:
    def test_routes_array_never_leaks(self):
        from yadgar.core.code_graph import digest

        arch = _java_arch()
        # populated URL-literal routes that must NOT appear
        arch["routes"] = ["/secret-noise-route", "/another/{noise}"]
        out = digest.render_digest(arch, [], {"canonical_root": "/repo", "subdir": ""})

        assert "/secret-noise-route" not in out
        assert "/another/{noise}" not in out


class TestEndpoints:
    def test_endpoints_rendered_from_route_method_rows(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(
            _java_arch(), _endpoint_rows(), {"canonical_root": "/repo", "subdir": ""}
        )
        assert "POST /validate" in out
        assert "GET /by-group/{id}" in out

    def test_endpoints_none_extracted_on_empty(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(_java_arch(), [], {"canonical_root": "/repo", "subdir": ""})
        assert "endpoints: (none extracted)" in out

    def test_endpoint_row_extraction_tolerant(self):
        """Bare-key rows (no ``m.`` prefix) must also extract — key shape unverified."""
        from yadgar.core.code_graph import digest

        rows = [{"route_method": "PUT", "route_path": "/x", "name": "putX"}]
        out = digest.render_digest(_java_arch(), rows, {"canonical_root": "/repo", "subdir": ""})
        assert "PUT /x" in out


class TestDeterminism:
    def test_same_input_same_bytes(self):
        from yadgar.core.code_graph import digest

        a = digest.render_digest(
            _java_arch(), _endpoint_rows(), {"canonical_root": "/repo", "subdir": ""}
        )
        b = digest.render_digest(
            _java_arch(), _endpoint_rows(), {"canonical_root": "/repo", "subdir": ""}
        )
        assert a == b

    def test_equal_fan_in_hotspots_stable_order(self):
        """Two hotspots with equal fan_in must sort by qualified_name (tie-break)."""
        from yadgar.core.code_graph import digest

        out = digest.render_digest(_java_arch(), [], {"canonical_root": "/repo", "subdir": ""})
        # GroupResolver.resolve (fan_in 18) sorts before RuleChecker.check (fan_in 18)
        assert out.index("GroupResolver.resolve") < out.index("RuleChecker.check")


class TestBudget:
    def test_under_budget(self):
        from yadgar.core.code_graph import config, digest

        out = digest.render_digest(
            _java_arch(), _endpoint_rows(), {"canonical_root": "/repo", "subdir": ""}
        )
        assert len(out) <= config.DIGEST_CHAR_BUDGET

    def test_huge_input_truncates_within_budget(self):
        from yadgar.core.code_graph import digest

        arch = _java_arch()
        # blow up hotspots + layers so the raw render far exceeds a small budget
        arch["hotspots"] = [
            {"name": f"fn{i}", "qualified_name": f"pkg.Class{i}.method{i}", "fan_in": 1000 - i}
            for i in range(500)
        ]
        arch["layers"] = [
            {"name": f"Comp{i}", "layer": "service", "reason": "x" * 40} for i in range(500)
        ]
        budget = 500
        out = digest.render_digest(
            arch, [], {"canonical_root": "/repo", "subdir": ""}, budget=budget
        )
        assert len(out) <= budget
        # ellipsis marker present when truncated, and it is COUNTED in the budget
        assert "…" in out

    def test_default_budget_from_config(self):
        from yadgar.core.code_graph import config, digest

        arch = _java_arch()
        arch["hotspots"] = [
            {"name": f"fn{i}", "qualified_name": f"pkg.C{i}.m{i}", "fan_in": 9000 - i}
            for i in range(2000)
        ]
        out = digest.render_digest(arch, [], {"canonical_root": "/repo", "subdir": ""})
        assert len(out) <= config.DIGEST_CHAR_BUDGET


class TestStale:
    def test_stale_line_rendered_when_present(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(
            _java_arch(),
            [],
            {"canonical_root": "/repo", "subdir": "", "stale": True, "head_sha": "abc1234"},
        )
        assert "stale @ abc1234" in out

    def test_no_stale_line_when_fresh(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(_java_arch(), [], {"canonical_root": "/repo", "subdir": ""})
        assert "stale @" not in out


class TestURLLiteralLayerNoise:
    """Bug: the digest's ``layers:`` (and ``entry-points:``) sections are built
    from the raw ``get_architecture()`` ``layers`` list, which — on at least
    one real repo — carried rows whose ``name`` was a URL-path fragment (e.g. a
    hardcoded test-fixture email used as a route path segment) rather than a
    real package/module/component name. ADR-0162 says "Route nodes =
    URL-literal noise, ignore" for endpoints; this is the SAME noise class
    leaking through a different aspect (``layers``), and it can carry PII
    (emails) when the indexed repo's test fixtures embed them in route
    strings. Neither the ``name`` nor the fragment must ever reach the
    rendered digest.
    """

    def _arch_with_url_literal_layers(self) -> dict:
        return {
            "project": "svc",
            "languages": [{"language": "Go", "file_count": 20}],
            "hotspots": [],
            "layers": [
                # real layer/component names — must survive filtering.
                {"name": "Handler", "layer": "api", "reason": "has HTTP route definitions"},
                {"name": "Main", "layer": "entry", "reason": "application entry point"},
                # URL-literal noise mirroring the real leak shape (fake
                # placeholder, not the real coworker's email):
                # e.g. from a route string like
                # "/gr/v1/shard/email/test.user@example.com/9".
                {
                    "name": "test.user@example.com",
                    "layer": "api",
                    "reason": "has HTTP route definitions",
                },
                {
                    "name": "9/shard/automaticlogin",
                    "layer": "api",
                    "reason": "has HTTP route definitions",
                },
                # same leak shape but landing in the entry layer instead —
                # entry-points is a SECOND read of the same layers list.
                {
                    "name": "test.user@example.com",
                    "layer": "entry",
                    "reason": "application entry point",
                },
            ],
            "routes": [],
        }

    def test_url_literal_layer_names_never_leak(self):
        from yadgar.core.code_graph import digest

        arch = self._arch_with_url_literal_layers()
        out = digest.render_digest(arch, [], {"canonical_root": "/repo", "subdir": ""})

        # neither leaked fragment appears anywhere in the rendered digest.
        assert "example.com" not in out
        assert "test.user@" not in out
        assert "9/shard/automaticlogin" not in out

        # real layer/component names are unaffected.
        assert "Handler" in out
        assert "Main" in out

    def test_url_literal_entry_point_never_leaks(self):
        """The URL-literal ``layer=='entry'`` row must not surface as an
        entry-point either — entry-points is a second read of ``layers``.
        """
        from yadgar.core.code_graph import digest

        arch = self._arch_with_url_literal_layers()
        out = digest.render_digest(arch, [], {"canonical_root": "/repo", "subdir": ""})

        i_entry = out.index("entry-points:")
        entry_line = out[i_entry : out.index("\n", i_entry) if "\n" in out[i_entry:] else len(out)]
        assert "example.com" not in entry_line
        assert "Main" in entry_line


class TestBuiltinAndGenericLayerNoise:
    """Second layer-noise class (task #58), distinct from the URL-literal leak
    and NOT caught by ``_looks_like_url_literal``'s shape rules (no ``@``,
    embedded ``/``, leading digit, or dotted TLD):

      1. Python BUILTINS (``len``, ``dict``, ``str``, ``range``, ``list``,
         ``int``, ``type``, ``object`` …) surface as ``core:`` layers with a
         ``high fan-in (N in, 0 out)`` reason — the indexer counts every
         reference to the builtin *name* as fan-in and mislabels it a module.
      2. GENERIC short route-path fragments (``db``, ``jsonl``, ``test``)
         surface as ``api:`` layers with a ``has HTTP route definitions``
         reason — the same route-noise class as the URL-literal leak, but a
         fragment too short/plain to trip the shape rules.

    Both must be filtered while real names — ``Handler``, ``api.v1.handlers``
    (route-reason but credible), and ``core`` (short lowercase but NOT
    route-derived) — survive.
    """

    def _arch(self) -> dict:
        return {
            "project": "svc",
            "languages": [{"language": "Python", "file_count": 30}],
            "hotspots": [],
            "layers": [
                # real names — must survive filtering.
                {"name": "Handler", "layer": "api", "reason": "has HTTP route definitions"},
                {"name": "api.v1.handlers", "layer": "api", "reason": "has HTTP route definitions"},
                # short lowercase name but NOT route-derived → the reason gate
                # must spare it (discriminates name-shape from reason-gated).
                {"name": "core", "layer": "core", "reason": "orchestrates domain logic"},
                {"name": "Main", "layer": "entry", "reason": "application entry point"},
                # Python builtins mislabelled as high-fan-in layers — noise.
                # Minimal synthetic fan-in numbers (never the real repo's).
                {"name": "len", "layer": "core", "reason": "high fan-in (11 in, 0 out)"},
                {"name": "dict", "layer": "core", "reason": "high fan-in (7 in, 0 out)"},
                {"name": "str", "layer": "core", "reason": "high fan-in (17 in, 0 out)"},
                {"name": "range", "layer": "core", "reason": "high fan-in (3 in, 0 out)"},
                {"name": "list", "layer": "core", "reason": "high fan-in (5 in, 0 out)"},
                # builtins beyond the obvious five — dir(builtins), not a 5-name list.
                {"name": "int", "layer": "core", "reason": "high fan-in (4 in, 0 out)"},
                {"name": "type", "layer": "core", "reason": "high fan-in (2 in, 0 out)"},
                {"name": "object", "layer": "core", "reason": "high fan-in (2 in, 0 out)"},
                # generic short route-path fragments mislabelled as api layers — noise.
                {"name": "db", "layer": "api", "reason": "has HTTP route definitions"},
                {"name": "jsonl", "layer": "api", "reason": "has HTTP route definitions"},
                {"name": "test", "layer": "api", "reason": "has HTTP route definitions"},
            ],
            "routes": [],
        }

    def test_builtin_named_layers_never_leak(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(self._arch(), [], {"canonical_root": "/repo", "subdir": ""})
        for builtin in ("len", "dict", "str", "range", "list", "int", "type", "object"):
            assert f"core: {builtin}" not in out, f"builtin {builtin!r} leaked as a layer"

    def test_generic_route_fragment_layers_never_leak(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(self._arch(), [], {"canonical_root": "/repo", "subdir": ""})
        for frag in ("db", "jsonl", "test"):
            assert f": {frag} —" not in out, f"generic route fragment {frag!r} leaked as a layer"

    def test_real_layer_names_survive_filtering(self):
        from yadgar.core.code_graph import digest

        out = digest.render_digest(self._arch(), [], {"canonical_root": "/repo", "subdir": ""})
        assert "Handler" in out
        assert "api.v1.handlers" in out
        # short lowercase, but its reason is not route-derived → spared.
        assert "core: core" in out
        assert "Main" in out


class TestBlockPayload:
    def test_build_block_payload_shape(self):
        """The C→D seam: refresh emits a block payload dict, not a block write."""
        from yadgar.core.code_graph import digest

        payload = digest.build_block_payload(
            _java_arch(),
            _endpoint_rows(),
            {"canonical_root": "/repo", "subdir": "svc"},
        )
        assert payload["block_name"] == "code_graph"
        # directory = canonical_root joined with subdir (exact-match injection scope)
        assert payload["directory"] == "/repo/svc"
        assert payload["skipped"] is False
        assert payload["chars"] == len(payload["content"])
        assert "── code_graph:" in payload["content"]

    def test_build_block_payload_root_no_subdir(self):
        from yadgar.core.code_graph import digest

        payload = digest.build_block_payload(
            _java_arch(), [], {"canonical_root": "/repo", "subdir": ""}
        )
        assert payload["directory"] == "/repo"
