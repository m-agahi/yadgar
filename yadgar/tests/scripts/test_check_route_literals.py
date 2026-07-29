"""Meta-tests for the internal-route existence lint (gate-blindness class, 2026-07-29).

``core/vacuum`` POSTed to ``/api/check_invariants`` for months with no route
registered anywhere.  Six tests mocked that exact URL to return 200 — the mocks
agreed with the caller, and nothing ever compared either against the route table.

The design risk was a brittle collector that cries wolf and gets deleted, so the
filter rules below are each pinned to the specific noise class they were measured
against.  A rule that stops earning its keep should fail a test, not quietly
inflate the allowlist.

Non-e2e, hermetic.
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):  # type: ignore[return]
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


crl = _load("check_route_literals.py")


def _make_repo(tmp_path: Path, source: str, allowlist: dict | None = None) -> Path:
    pkg = tmp_path / "yadgar" / "core"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(textwrap.dedent(source), encoding="utf-8")
    if allowlist is not None:
        (tmp_path / ".route-literal-allowlist.json").write_text(
            json.dumps(allowlist), encoding="utf-8"
        )
    return tmp_path


# Already flush-left: this gets CONCATENATED with flush-left call-site snippets
# before textwrap.dedent runs, so an indented literal here would survive dedent
# (no common prefix) and make the fixture a SyntaxError — which collect() swallows,
# turning every assertion in this module vacuously green.
_ROUTE_DECL = (
    '@mcp_server.custom_route("/api/stats", methods=["GET"])\n'
    "async def stats(request):\n"
    "    return None\n"
)


class TestRouteTable:
    def test_recovers_custom_route_paths(self) -> None:
        routes = crl.collect_routes_from_source(_ROUTE_DECL)
        assert routes == {"/api/stats"}

    def test_recovers_fastapi_verb_paths(self) -> None:
        src = '@app.post("/rerank", response_model=RerankResponse)\ndef rerank():\n    pass\n'
        assert crl.collect_routes_from_source(src) == {"/rerank"}

    def test_real_route_table_is_substantial(self) -> None:
        """A collector that silently recovers nothing is the failure mode to fear.

        A green lint over an EMPTY route table would be meaningless — every
        literal would look unresolved, or (worse, after allowlisting) nothing
        would ever be checked. Pin a floor so a decorator-shape change that
        breaks collection fails loudly.
        """
        routes, _ = crl.collect(_REPO_ROOT)
        assert len(routes) >= 60, f"route table collapsed to {len(routes)} routes"


class TestFilters:
    """Each of the four filter rules, pinned to the noise class it kills."""

    def test_rule1_string_with_a_space_is_prose(self) -> None:
        assert crl.apply_filters("/health (API readiness) probe", set()) is None

    def test_rule1_applies_before_tail_extraction(self) -> None:
        """The ordering bug that produced 8 spurious survivors.

        A Field(description=...) ending in a path has a space in the STRING but
        not in the extracted TAIL, so filtering the tail lets it through.
        """
        assert crl.apply_filters("Number of results returned from /recall.", set()) is None

    def test_rule2_printf_format_string_is_not_a_url(self) -> None:
        assert crl.apply_filters("/rerank/%s", set()) is None

    def test_rule3_query_string_is_stripped(self) -> None:
        assert crl.apply_filters("/hooks/file-changed?path=", set()) == "/hooks/file-changed"

    def test_rule4_prefix_match_constant_is_dropped(self) -> None:
        """auth_middleware's namespace constants are tests, not request targets."""
        routes = {"/api/logs/poll"}
        assert crl.apply_filters("/api/logs/", routes) is None

    def test_rule4_keeps_a_trailing_slash_path_that_is_not_a_prefix(self) -> None:
        assert crl.apply_filters("/api/nothing/", {"/api/stats"}) == "/api/nothing/"

    def test_non_namespace_string_is_ignored(self) -> None:
        assert crl.apply_filters("/usr/local/bin", set()) is None


class TestWildcardMatching:
    def test_route_placeholder_matches_a_literal_segment(self) -> None:
        assert crl.path_matches("/api/runtime-config/enabled", "/api/runtime-config/{key}")

    def test_call_site_hole_matches_a_literal_route_segment(self) -> None:
        """f-string interpolation on the CALL side, literal on the ROUTE side."""
        assert crl.path_matches("/api/control/maintenance/{}", "/api/control/maintenance/enter")

    def test_two_way_wildcard_resolves_double_hole(self) -> None:
        assert crl.path_matches("/api/runtime-config/{}{}", "/api/runtime-config/{key}")

    def test_segment_count_must_agree(self) -> None:
        assert not crl.path_matches("/api/stats/extra", "/api/stats")

    def test_unrelated_path_does_not_match(self) -> None:
        assert not crl.path_matches("/api/check_invariants", "/api/stats")


class TestUnresolvedDetection:
    def test_unregistered_namespace_path_is_flagged(self, tmp_path: Path) -> None:
        """THE defect shape: a call to a route nobody registered."""
        repo = _make_repo(
            tmp_path,
            _ROUTE_DECL + '\ndef caller(url):\n    return post(f"{url}/api/check_invariants")\n',
        )
        errors = crl.check(repo)
        assert any("UNRESOLVED-ROUTE" in e and "check_invariants" in e for e in errors), errors

    def test_violation_names_the_call_site(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            _ROUTE_DECL + '\ndef caller(url):\n    return post(f"{url}/api/nope")\n',
        )
        assert any("yadgar/core/mod.py:" in e for e in crl.check(repo)), crl.check(repo)

    def test_registered_route_resolves(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            _ROUTE_DECL + '\ndef caller(url):\n    return get(f"{url}/api/stats")\n',
        )
        assert not crl.check(repo)

    def test_route_declaration_itself_is_not_a_call_site(self, tmp_path: Path) -> None:
        """A decorator arg must not be collected as a literal, or every route
        would trivially resolve against itself and the guard would be vacuous."""
        repo = _make_repo(tmp_path, _ROUTE_DECL)
        _, literals = crl.collect(repo)
        assert "/api/stats" not in literals


class TestAllowlistGovernance:
    _GOOD = {
        "target": "external",
        "rationale": (
            "Ollama's own generate endpoint, reached at a user-configured external host "
            "and therefore not present in our route table."
        ),
    }

    def test_allowlisted_external_path_passes(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            _ROUTE_DECL + '\ndef caller(u):\n    return post(f"{u}/api/generate")\n',
            allowlist={"/api/generate": self._GOOD},
        )
        assert not crl.check(repo)

    def test_stale_allowlist_entry_hard_fails(self, tmp_path: Path) -> None:
        """An allowlisted path that starts resolving must be removed.

        Without this, the allowlist absorbs the signal permanently — the exact
        antipattern this plan exists to fight.
        """
        repo = _make_repo(tmp_path, _ROUTE_DECL, allowlist={"/api/stats": self._GOOD})
        assert any("STALE" in e for e in crl.check(repo)), crl.check(repo)

    def test_short_rationale_hard_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            _ROUTE_DECL,
            allowlist={"/api/generate": {"target": "external", "rationale": "external"}},
        )
        assert any("rationale" in e for e in crl.check(repo))

    def test_invalid_target_hard_fails(self, tmp_path: Path) -> None:
        bad = dict(self._GOOD, target="whatever")
        repo = _make_repo(tmp_path, _ROUTE_DECL, allowlist={"/api/generate": bad})
        assert any("target" in e for e in crl.check(repo))


def test_real_tree_is_clean() -> None:
    """The shipped route table + allowlist exit 0."""
    errors = crl.check(_REPO_ROOT)
    assert not errors, f"route-literal lint must be clean: {errors}"


def test_check_invariants_route_resolves() -> None:
    """Regression pin for the bug that motivated this guard.

    ``core/vacuum`` still POSTs ``/api/check_invariants``; the route now exists
    (``core/server/routes/admin_ops.py``). If the route is ever removed while the
    caller stays, this test fails alongside the lint.
    """
    routes, literals = crl.collect(_REPO_ROOT)
    assert "/api/check_invariants" in literals, (
        "the vacuum call site disappeared — update this test"
    )
    assert crl.resolves("/api/check_invariants", routes), "the route regressed to non-existent"
