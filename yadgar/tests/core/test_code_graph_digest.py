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
        # C10(e): a monorepo LEAF carries its subdir in the block NAME, so that
        # every leaf keeps its own row once C11 re-keys `directory` onto the
        # per-repo project_id (which would otherwise collapse them all onto one).
        assert payload["block_name"] == "code_graph_svc"
        # directory = canonical_root joined with subdir (exact-match injection scope)
        assert payload["directory"] == "/repo/svc"
        assert payload["skipped"] is False
        assert payload["chars"] == len(payload["content"])
        assert "── code_graph:" in payload["content"]

    def test_block_name_discriminates_monorepo_leaves(self):
        """Two leaves of ONE repo must not share a block name (C10(e))."""
        from yadgar.core.code_graph import digest

        names = {
            digest._digest_block_name({"canonical_root": "/repo", "subdir": sd})
            for sd in ("apps/web", "apps/api", "libs/Foo-2")
        }
        assert names == {"code_graph_apps_web", "code_graph_apps_api", "code_graph_libs_foo_2"}
        # A repo root keeps the bare, back-compatible name.
        assert digest._digest_block_name({"canonical_root": "/repo", "subdir": ""}) == "code_graph"

    def test_block_name_is_a_valid_memory_block_name(self):
        """Names must satisfy memory_block's ^[a-z][a-z0-9_]*$ validator."""
        from yadgar._shared.storage.blocks import _BLOCK_NAME_RE
        from yadgar.core.code_graph import digest

        for sd in ("", "apps/web", "libs/Foo-2", "a/b/c", "UPPER/Case"):
            name = digest._digest_block_name({"canonical_root": "/repo", "subdir": sd})
            assert _BLOCK_NAME_RE.match(name), f"invalid block name {name!r} for subdir {sd!r}"

    def test_build_block_payload_root_no_subdir(self):
        from yadgar.core.code_graph import digest

        payload = digest.build_block_payload(
            _java_arch(), [], {"canonical_root": "/repo", "subdir": ""}
        )
        assert payload["directory"] == "/repo"


# --- Secret-gate interaction (#30, ADR-0121 + ADR-0162) ---------------------
#
# The LIVE code_graph block write is Claude calling the generic ``block_update``
# MCP tool, which runs the SAME ``gate_or_reject`` secret gate as ``wiki_add``.
# The gate's broad AWS-secret heuristic matches an EXACTLY-40-char
# ``[A-Za-z0-9/+]`` run when an ``aws/secret/access/key/token/credential``
# keyword co-occurs — and a full git SHA (exactly 40 hex) or a coincidental
# 40-char identifier/path segment in a benign digest trips it.
#
# The fix is Option B: the digest RENDERER breaks secret-shaped long runs so the
# exactly-40 shape can never form. The gate itself is UNTOUCHED — no other caller
# (memorize / wiki_add / other block writers) is weakened, and a genuine
# high-precision secret planted in digest content is STILL rejected.


def _arch_with_keyword(hotspot_qname: str) -> dict:
    """Arch whose layer name carries an ``access/token/key`` keyword (arming the
    keyword-gated AWS-40 rule) plus one hotspot whose qualified name is ``run``.
    """
    return {
        "project": "svc",
        "languages": [{"language": "Java", "file_count": 1}],
        "layers": [{"name": "AccessTokenKeyService", "layer": "service", "reason": "auth"}],
        "hotspots": [{"qualified_name": hotspot_qname, "fan_in": 7}],
    }


def _arch_with_hotspot(name: str) -> dict:
    """Minimal arch with a single hotspot whose qualified name is ``name``.

    Hotspot qualified names are NOT url-literal-filtered, so an arbitrary planted
    string reaches the rendered digest verbatim.
    """
    return {
        "project": "svc",
        "languages": [{"language": "Java", "file_count": 1}],
        "hotspots": [{"qualified_name": name, "fan_in": 3}],
    }


def _render_and_gate(arch: dict, identity: dict | None = None):
    from yadgar._shared.security.secrets import gate_or_reject
    from yadgar.core.code_graph import digest

    out = digest.render_digest(arch, [], identity or {"canonical_root": "/r", "subdir": ""})
    return gate_or_reject(out), out


class TestSecretGateFalsePositive:
    """RED before the fix, GREEN after: benign digest content that coincidentally
    forms the broad AWS-40 shape must PASS the gate the live block write uses.
    """

    def test_forty_char_identifier_run_passes_gate(self):
        # A bare 40-char identifier segment IS the AWS-40 shape (exactly 40
        # [A-Za-z0-9/+] between the leading space and the following space).
        res, _out = _render_and_gate(_arch_with_keyword("A" * 40))
        assert res is None, f"benign 40-char run must not trip the gate: {res}"

    def test_git_sha_stale_line_passes_gate(self):
        # A real 40-hex git SHA in the stale line + an auth keyword elsewhere.
        sha = "3f5c177a1b2c3d4e5f60718293a4b5c6d7e8f900"
        assert len(sha) == 40
        res, _out = _render_and_gate(
            _arch_with_keyword("Svc.method"),
            {"canonical_root": "/r", "subdir": "", "stale": True, "head_sha": sha},
        )
        assert res is None, f"git SHA in stale line must not trip the gate: {res}"

    def test_defang_actually_breaks_long_runs(self):
        # Structural proof the fix is active: no >=40 pure run survives rendering.
        _res, out = _render_and_gate(_arch_with_keyword("A" * 48))
        assert "A" * 40 not in out, "renderer must break >=40-char secret-shaped runs"

    def test_aiza_length_39_run_untouched(self):
        # AIza keys are exactly 39 chars — below the 40 threshold. A benign 39-run
        # must be left byte-for-byte intact (regression guard on the threshold).
        run39 = "B" * 39
        _res, out = _render_and_gate(_arch_with_hotspot(run39))
        assert run39 in out, "39-char run must be left intact (threshold is >=40)"


class TestSecretGateAdversarialStillRejected:
    """SECURITY-CRITICAL: the FP fix must NOT open a hole. A genuine high-precision
    secret planted in digest content — including one whose pure body is >= the
    defang threshold so the renderer actually transforms it — must STILL be
    rejected. These assertions hold BOTH before and after the fix.
    """

    def test_planted_akia_still_rejected(self):
        # Canonical AWS example key (20 chars, below threshold -> untouched).
        res, _out = _render_and_gate(_arch_with_hotspot("AKIAIOSFODNN7EXAMPLE"))
        assert res is not None and "AWS access key" in res["reason"], res

    def test_planted_ghp_body_over_threshold_still_rejected(self):
        # ghp_ body 44 chars (>= 40) -> renderer chunks it. The ghp_ rule (20-char
        # min body) must still fire on the first chunk.
        token = "ghp_" + "A" * 44
        res, out = _render_and_gate(_arch_with_hotspot(token))
        assert res is not None and "GitHub token" in res["reason"], res
        assert out is not None  # rendered path exercised

    def test_planted_stripe_live_body_over_threshold_still_rejected(self):
        # sk_live_ body 40 chars (>= 40) -> chunked. sk_live_ needs a 24-char min
        # body, so this pins the defang chunk floor at >= 24 (a floor of 20 would
        # silently defang this secret).
        token = "sk_live_" + "A" * 40
        res, _out = _render_and_gate(_arch_with_hotspot(token))
        assert res is not None and "Stripe secret key" in res["reason"], res

    def test_planted_openai_body_over_threshold_still_rejected(self):
        # sk- body 40 chars (>= 40) -> chunked. OpenAI rule needs 20-char min body.
        token = "sk-" + "A" * 40
        res, _out = _render_and_gate(_arch_with_hotspot(token))
        assert res is not None and "OpenAI API key" in res["reason"], res

    def test_planted_pem_still_rejected(self):
        header = "-----BEGIN RSA " + "PRIVATE KEY-----"  # gitleaks:allow
        res, _out = _render_and_gate(_arch_with_hotspot(header))
        assert res is not None and "Private key" in res["reason"], res


# --- Budget reservation for the staleness marker ---------------------------
#
# The `stale @ <sha>` marker used to be the LAST section in the priority order,
# so a naive tail-cut to DIGEST_CHAR_BUDGET threw it away on any realistic repo.
# Measured before the fix, with the fixture below (a single Java service with
# package-qualified names): untruncated 3268 chars, emitted 2000 — `endpoints:`
# AND `stale @` both entirely absent. The marker is now rendered in a
# budget-RESERVED preamble (line 2, right after the header) so its survival is
# structural rather than positional. See BC-CODEGRAPH-8.


def _budget_filling_arch() -> dict:
    """A realistic single-Java-service arch whose untruncated render >> budget.

    Package-qualified layer/hotspot names (``com.acme.…``) are what real Java /
    Go / Python repos emit; each rendered row clears ~88 chars, which is the
    measured per-row overflow threshold at ``DIGEST_CHAR_BUDGET = 2000``.
    """
    return {
        "project": "globalrouter",
        "languages": [{"language": "Java", "file_count": 214}],
        "layers": [
            {
                "name": f"com.acme.globalrouter.component.Component{i:02d}",
                "layer": ("api" if i % 3 == 0 else "service" if i % 3 == 1 else "core"),
                "reason": "orchestrates domain logic across bounded contexts",
            }
            for i in range(20)
        ]
        + [
            {
                "name": "com.acme.globalrouter.GlobalRouterApplication",
                "layer": "entry",
                "reason": "application entry point",
            }
        ],
        "hotspots": [
            {
                "qualified_name": f"com.acme.globalrouter.service.Service{i:02d}.handleRequest",
                "fan_in": 200 - i,
            }
            for i in range(20)
        ],
    }


def _budget_filling_endpoints() -> list:
    return [
        {
            "m.route_method": ("GET" if i % 2 == 0 else "POST"),
            "m.route_path": f"/api/v1/globalrouter/resource{i:02d}/{{resourceId}}/details",
            "m.name": f"handler{i:02d}",
        }
        for i in range(40)
    ]


def _stale_identity() -> dict:
    return {
        "canonical_root": "/repo",
        "subdir": "",
        "stale": True,
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }


class TestStaleSurvivesBudget:
    """AC-1/AC-2: the freshness marker reaches the reader on a digest that
    actually fills the budget — the case every prior stale test dodged by using
    a fixture small enough never to truncate.
    """

    def test_marker_survives_budget_filling_digest(self):
        from yadgar.core.code_graph import config, digest

        out = digest.render_digest(
            _budget_filling_arch(), _budget_filling_endpoints(), _stale_identity()
        )

        # LOAD-BEARING: _truncate returns the text UNCHANGED when it fits, so the
        # ellipsis appears iff truncation actually happened. Without this the test
        # would pass on a digest that never truncated — the exact false-green the
        # original TestStale gave. Do not relax this assertion.
        assert "…" in out, "fixture must actually overflow the budget"
        assert "stale @ " in out
        assert out.splitlines()[1].startswith("stale @ "), out.splitlines()[:3]
        assert len(out) <= config.DIGEST_CHAR_BUDGET

    def test_marker_survives_tiny_budget(self):
        """AC-2: the guarantee is computed, not 'the constant happens to be roomy'."""
        from yadgar.core.code_graph import digest

        out = digest.render_digest(
            _budget_filling_arch(),
            _budget_filling_endpoints(),
            _stale_identity(),
            budget=200,
        )

        assert "…" in out
        assert "stale @ " in out
        assert out.splitlines()[1].startswith("stale @ ")
        assert len(out) <= 200

    def test_fresh_digest_has_no_marker_and_no_blank_line(self):
        """AC-3: an absent marker must not leave an empty preamble line behind."""
        from yadgar.core.code_graph import digest

        out = digest.render_digest(
            _budget_filling_arch(),
            _budget_filling_endpoints(),
            {"canonical_root": "/repo", "subdir": ""},
        )

        assert "stale @" not in out
        assert out.splitlines()[1].startswith("layers:"), out.splitlines()[:3]

    def test_budget_invariant_across_fresh_stale_and_sizes(self):
        """AC-4: len(out) <= budget across fresh/stale x under/over budget x tiny."""
        from yadgar.core.code_graph import config, digest

        fresh = {"canonical_root": "/repo", "subdir": ""}
        small_arch = _java_arch()
        big_arch = _budget_filling_arch()

        for identity in (fresh, _stale_identity()):
            for arch, rows in (
                (small_arch, _endpoint_rows()),
                (big_arch, _budget_filling_endpoints()),
            ):
                default_out = digest.render_digest(arch, rows, identity)
                assert len(default_out) <= config.DIGEST_CHAR_BUDGET
                for budget in (60, 200, 1000):
                    out = digest.render_digest(arch, rows, identity, budget=budget)
                    assert len(out) <= budget, (budget, len(out))

    def test_payload_chars_bounded_when_stale_and_truncated(self):
        """AC-4: build_block_payload's chars mirror the bounded content."""
        from yadgar.core.code_graph import config, digest

        payload = digest.build_block_payload(
            _budget_filling_arch(), _budget_filling_endpoints(), _stale_identity()
        )
        assert payload["chars"] == len(payload["content"])
        assert payload["chars"] <= config.DIGEST_CHAR_BUDGET
        assert "stale @ " in payload["content"]

    def test_defang_still_covers_body_when_stale_preamble_present(self):
        """AC-5: splitting preamble/body must NOT narrow the #30 defang guard."""
        from yadgar._shared.security.secrets import gate_or_reject
        from yadgar.core.code_graph import digest

        arch = _arch_with_keyword("A" * 40)
        out = digest.render_digest(arch, [], _stale_identity())

        assert "stale @ " in out
        assert "A" * 40 not in out, "body runs must still be defanged"
        assert gate_or_reject(out) is None


# --- Section-budget fairness (Car 0087, ADR-0162) ---------------------------
#
# render_digest used to render all four body sections into ONE shared budget
# and truncate the joined blob with a single tail-cut. Whichever section came
# first (layers) ate the whole budget on any real repo with real content, and
# `endpoints:` — LAST in priority order — was truncated away entirely, or
# worse, cut MID-LINE: the live `code_graph` memory block for this very repo
# shipped `endpoints:\n  PATCH /` — a truncated route fragment, not a real
# one. Sections now each get an individually water-filled share of
# body_budget, so a later section always gets SOME budget and a shown line is
# never a mid-string cut.


class TestSectionBudgetFairness:
    def test_endpoints_not_starved_by_earlier_sections(self):
        """RED before the fix: a concrete endpoint LINE must survive even
        though layers/hotspots alone would consume the entire old shared
        budget. Also proves the fixture actually overflows (an earlier
        section shows its own truncation marker) so this isn't a false-green
        on a digest that never truncated in the first place.
        """
        from yadgar.core.code_graph import digest

        out = digest.render_digest(
            _budget_filling_arch(),
            _budget_filling_endpoints(),
            {"canonical_root": "/repo", "subdir": ""},
        )

        # the FIRST (alphabetically lowest path) endpoint must be present — a
        # whole rendered route LINE, not just the "endpoints:" header.
        assert "GET /api/v1/globalrouter/resource00/{resourceId}/details" in out

        # confirm the fixture really did overflow: an earlier, higher-priority
        # section had to drop rows and show its own truncation marker —
        # otherwise this test would pass even under the OLD starving
        # implementation simply because nothing ever overflowed.
        i_layers = out.index("layers:")
        i_hot = out.index("hotspots:")
        layers_region = out[i_layers:i_hot]
        assert "… (" in layers_region, "fixture must overflow layers' soft cap/share"

    def test_redistribution_frees_unused_share_for_endpoints(self):
        """A section with a tiny demand (one short entry-point name, no
        hotspots at all) must free its unused share for a hungrier section
        (endpoints) rather than the share sitting reserved-but-idle.
        """
        from yadgar.core.code_graph import digest

        arch = _budget_filling_arch()
        # collapse layers to a single row and drop hotspots entirely so
        # entry-points' tiny demand and endpoints' large demand are the only
        # real competitors for the redistributed budget.
        arch["layers"] = [{"name": "Main", "layer": "entry", "reason": "application entry point"}]
        arch["hotspots"] = []

        out = digest.render_digest(
            arch,
            _budget_filling_endpoints(),
            {"canonical_root": "/repo", "subdir": ""},
            budget=300,
        )
        assert len(out) <= 300
        assert "GET /api/v1/globalrouter/resource00/{resourceId}/details" in out

    def test_determinism_at_forced_truncation_budget(self):
        """Every section is forced to truncate at this budget; output must
        still be byte-identical across repeated calls (pure function — the
        allocator iterates a list in fixed order and uses integer // and %
        only, never a set or a float).
        """
        from yadgar.core.code_graph import digest

        arch = _budget_filling_arch()
        rows = _budget_filling_endpoints()
        identity = {"canonical_root": "/repo", "subdir": ""}
        a = digest.render_digest(arch, rows, identity, budget=250)
        b = digest.render_digest(arch, rows, identity, budget=250)
        assert a == b
        assert len(a) <= 250
