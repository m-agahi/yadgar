"""Meta-tests for the health-endpoint semantics lint (Car 0091).

The config-file-only ADR-0019 pin (test_core_health_probe_liveness_pin.py)
covers flake.nix/Dockerfile/docker-compose.yml only; it cannot see a Python
call site building the same URL. Three Python call sites drifted onto bare
/health for over a month with nothing to catch it. This guard closes that
gap with an allowlist-governed AST lint, same shape as check_route_literals.py.

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


ches = _load("check_health_endpoint_semantics.py")


def _make_repo(tmp_path: Path, source: str, allowlist: dict | None = None) -> Path:
    pkg = tmp_path / "yadgar" / "core"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(textwrap.dedent(source), encoding="utf-8")
    if allowlist is not None:
        (tmp_path / ".health-endpoint-allowlist.json").write_text(
            json.dumps(allowlist), encoding="utf-8"
        )
    return tmp_path


class TestFilters:
    def test_bare_readiness_url_is_a_violation(self) -> None:
        assert ches.is_bare_health_probe("http://127.0.0.1:8765/health") is True

    def test_liveness_url_is_not_a_violation(self) -> None:
        assert ches.is_bare_health_probe("http://127.0.0.1:8765/health/live") is False

    def test_base_url_interpolation_shape_is_a_violation(self) -> None:
        """The dominant call-site shape in this codebase: f"{base}/health"."""
        assert ches.is_bare_health_probe("{}/health") is True

    def test_base_url_interpolation_liveness_is_not_a_violation(self) -> None:
        assert ches.is_bare_health_probe("{}/health/live") is False

    def test_prose_with_a_space_is_not_a_violation(self) -> None:
        """Docstrings/log messages like 'Poll GET <url>/health until 200...'."""
        assert ches.is_bare_health_probe("[vacuum] waiting for {}/health ...") is False

    def test_bare_constant_with_no_scheme_or_hole_is_not_a_violation(self) -> None:
        """auth_middleware's _EXEMPT_PATHS frozenset entries — a namespace
        listing, not an assembled request URL."""
        assert ches.is_bare_health_probe("/health") is False

    def test_query_string_is_stripped_before_the_tail_check(self) -> None:
        assert ches.is_bare_health_probe("http://x/health?verbose=1") is True

    def test_unrelated_suffix_is_not_a_violation(self) -> None:
        assert ches.is_bare_health_probe("http://x/healthcheck") is False


class TestCollection:
    def test_call_site_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health")\n',
        )
        violations = ches.collect_violations(repo)
        assert any("mod.py" in k for k in violations), violations

    def test_liveness_call_site_is_not_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health/live")\n',
        )
        assert ches.collect_violations(repo) == {}

    def test_route_declaration_is_not_a_call_site(self, tmp_path: Path) -> None:
        """A decorator declaring the route must not be flagged as a probe."""
        repo = _make_repo(
            tmp_path,
            '@mcp_server.custom_route("/health", methods=["GET"])\n'
            "async def health_check(request):\n"
            "    return None\n",
        )
        assert ches.collect_violations(repo) == {}

    def test_docstring_prose_is_not_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    """Poll GET <url>/health until 200 or timeout."""\n    return None\n',
        )
        assert ches.collect_violations(repo) == {}


class TestUngovernedViolation:
    def test_ungoverned_bare_health_hard_fails(self, tmp_path: Path) -> None:
        """THE defect shape Car 0091 fixed: a bare /health call site with no
        rationale on file must be a hard error, not silently accepted."""
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health")\n',
        )
        errors = ches.check(repo)
        assert any("BARE-HEALTH-PROBE" in e and "mod.py" in e for e in errors), errors

    def test_liveness_call_site_is_clean(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health/live")\n',
        )
        assert not ches.check(repo)


class TestAllowlistGovernance:
    _GOOD_RATIONALE = (
        "CLI status command wants the full readiness payload (db/embed/version) "
        "to display to the operator — genuinely needs /health, not liveness."
    )

    def test_allowlisted_site_passes(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health")\n',
            allowlist={"yadgar/core/mod.py:2": {"rationale": self._GOOD_RATIONALE}},
        )
        assert not ches.check(repo)

    def test_stale_allowlist_entry_hard_fails(self, tmp_path: Path) -> None:
        """An allowlisted site that no longer probes bare /health must be
        removed — otherwise the allowlist absorbs the signal permanently."""
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health/live")\n',
            allowlist={"yadgar/core/mod.py:2": {"rationale": self._GOOD_RATIONALE}},
        )
        assert any("STALE" in e for e in ches.check(repo)), ches.check(repo)

    def test_short_rationale_hard_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            'def caller(url):\n    return get(f"{url}/health")\n',
            allowlist={"yadgar/core/mod.py:2": {"rationale": "too short"}},
        )
        assert any("rationale" in e for e in ches.check(repo))


def test_real_tree_is_clean() -> None:
    """The shipped call sites + allowlist exit 0."""
    errors = ches.check(_REPO_ROOT)
    assert not errors, f"health-endpoint semantics lint must be clean: {errors}"


def test_the_three_fixed_call_sites_are_no_longer_flagged() -> None:
    """Regression pin for the Car 0091 fix itself — if any of these three
    call sites ever drifts back onto bare /health, this fails alongside the
    lint (the lint would also fail, since these sites are NOT allowlisted)."""
    violations = ches.collect_violations(_REPO_ROOT)
    for site in violations:
        assert "daemon/daemon.py" not in site or "_embed_health_ok" not in site, violations
    flagged_files = {site.split(":")[0] for site in violations}
    assert "yadgar/core/update/orchestrator.py" not in flagged_files, violations
    assert "yadgar/core/cli/update.py" not in flagged_files, violations
