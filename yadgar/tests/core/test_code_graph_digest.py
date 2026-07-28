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
