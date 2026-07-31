#!/usr/bin/env python3
"""Health-endpoint semantics lint — bare readiness /health probes require a
written rationale (Car 0091, ADR-0019 follow-up).

WHY THIS EXISTS
---------------
v5.91.0 (ADR-0019) split the daemon's health surface into /health (readiness,
db/embed-dependent, can 503 on a transiently-busy backend) and /health/live
(liveness, loop-only, answerable without touching a dependency). The pin that
shipped alongside it (test_core_health_probe_liveness_pin.py) covers only the
THREE non-Python healthcheck surfaces (flake.nix, Dockerfile,
docker-compose.yml) — it is config-file-only and cannot see a Python call site
constructing the same URL. Three Python call sites (YadgarDaemon._health_ok,
orchestrator._default_health_check, update._probe_daemon_version) drifted onto
/health for over a month with nothing to catch it (Car 0091, caught by a real
vacuum-adjacent stall where a liveness-shaped wait was gated on a
readiness-dependent endpoint).

WHAT THIS GUARD DOES
---------------------
Scans every non-test .py file under yadgar/ for a URL-shaped string literal
(a plain "http://..." constant, or an f-string built from a base-url variable)
whose path tail is bare "/health" — NOT "/health/live" and not some other
sub-path. Every survivor is a readiness probe. Because several call sites
GENUINELY need readiness (they read db/embed payload fields, or gate a DB
write, or target a service — e.g. the embed backend, or SurrealDB itself —
that exposes no /health/live variant at all), this is NOT a blanket ban: it is
an ALLOWLIST-governed lint, same shape as check_route_literals.py. A new bare
/health call site is a hard error until someone adds a governed entry
explaining why readiness (not liveness) is actually required.

WHAT COUNTS AS "URL-SHAPED"
----------------------------
Only literals that look like an assembled request URL are considered — the
rendered string must start with an http(s) scheme OR with the f-string
interpolation placeholder "{}" (a base-url variable spliced in front of the
path — the dominant shape in this codebase: f"{yadgar_url}/health"). This
deliberately excludes:
  - prose / docstrings ("Poll GET <url>/health until 200...") — filtered by
    the same "rejects a space" rule check_route_literals.py uses (measured).
  - the route DECLARATIONS themselves (@custom_route("/health", ...),
    @app.get("/health")) — a decorator argument is not a call site.
  - bare config/constant listings (auth_middleware.py's _EXEMPT_PATHS
    frozenset) — no scheme or interpolation prefix, so it never reads as a URL.
  - a bare default-parameter value — same reasoning. If this becomes a false
    negative in practice (a caller re-introduces bare /health behind a
    parameterized default), tighten the prefix rule then; not observed today.

Allowlist: .health-endpoint-allowlist.json — {"path:line": {"rationale": ...}},
rationale >= 40 chars. A STALE entry (the site no longer probes bare /health)
is itself a hard error, same governance as check_route_literals.py.

Usage:
  python scripts/check_health_endpoint_semantics.py                    # check, exit 0/1
  python scripts/check_health_endpoint_semantics.py --list-violations  # baseline, ignores allowlist, always exits 0
  python scripts/check_health_endpoint_semantics.py --repo-root /path

Exit codes:
  0  every bare-/health call site is allowlisted with a rationale
  1  an ungoverned bare-/health call site, a STALE allowlist entry, or a
     malformed allowlist entry
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWLIST_NAME = ".health-endpoint-allowlist.json"
_MIN_RATIONALE = 40

# Same decorator set as check_route_literals.py — used ONLY to exclude a route
# DECLARATION's own path argument from being treated as a call site.
_ROUTE_DECORATORS = frozenset({"custom_route", "route", "get", "post", "put", "delete", "patch"})

_HOLE = "{}"


def render_node(node: ast.expr) -> str | None:
    """Render a Constant(str) or JoinedStr to a path template (see check_route_literals.py)."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(_HOLE)
            else:  # pragma: no cover - defensive
                return None
        return "".join(parts)
    return None


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def is_bare_health_probe(rendered: str) -> bool:
    """True iff *rendered* is a URL-shaped literal whose path tail is bare /health.

    Filters (measured, same order matters as check_route_literals.py):
      1. reject a space — prose/docstrings, never a real request target.
      2. must start with an http(s) scheme or the f-string hole "{}" — a bare
         constant with neither prefix is not being assembled into a URL here
         (config listings, decorator declarations, parameter defaults).
      3. strip at "?" — query strings.
      4. the remaining path must end EXACTLY in "/health" — "/health/live"
         ends in "/live" and never matches; "/healthcheck" never matches
         either (exact suffix, not substring).
    """
    if " " in rendered:
        return False
    if not (
        rendered.startswith("http://")
        or rendered.startswith("https://")
        or rendered.startswith(_HOLE)
    ):
        return False
    path = rendered.split("?", 1)[0]
    return path.endswith("/health")


def collect_violations(repo_root: Path) -> dict[str, list[str]]:
    """Return {"file:line": [rendered, ...]} for every bare-/health call site."""
    pkg = repo_root / "yadgar"
    violations: dict[str, list[str]] = {}
    if not pkg.is_dir():
        return violations

    for py in sorted(pkg.rglob("*.py")):
        try:
            rel = py.relative_to(repo_root)
        except ValueError:  # pragma: no cover - defensive
            rel = py
        if "tests" in rel.parts:
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover - defensive
            continue

        declared: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and _decorator_name(dec) in _ROUTE_DECORATORS
                        and dec.args
                    ):
                        declared.add(id(dec.args[0]))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            if id(node) in declared:
                continue
            rendered = render_node(node)
            if rendered is None or not is_bare_health_probe(rendered):
                continue
            lineno = getattr(node, "lineno", 0)
            key = f"{rel.as_posix()}:{lineno}"
            violations.setdefault(key, []).append(rendered)
    return violations


def load_allowlist(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object of {{'file:line': {{rationale}}}}")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    if repo_root is None:
        repo_root = _REPO_ROOT

    violations = collect_violations(repo_root)
    try:
        allowlist = load_allowlist(repo_root / _ALLOWLIST_NAME)
    except ValueError as exc:
        return [f"MALFORMED allowlist: {exc}"]

    errors: list[str] = []

    for site, meta in sorted(allowlist.items()):
        meta = meta if isinstance(meta, dict) else {}
        rationale = meta.get("rationale", "")
        if len(rationale.strip()) < _MIN_RATIONALE:
            errors.append(
                f"MALFORMED allowlist entry {site!r}: rationale must be >= "
                f"{_MIN_RATIONALE} chars (got {len(rationale.strip())})"
            )
        if site not in violations:
            errors.append(
                f"STALE allowlist entry {site!r}: it no longer probes bare /health — "
                f"remove it from {_ALLOWLIST_NAME}"
            )

    for site in sorted(violations):
        if site in allowlist:
            continue
        renders = ", ".join(sorted(set(violations[site])))
        errors.append(
            f"BARE-HEALTH-PROBE: {site} probes readiness /health ({renders}) with no "
            f"governed rationale. Either switch to /health/live (liveness — the caller "
            f"only needs 'is the process up', see ADR-0019), or add a governed entry to "
            f"{_ALLOWLIST_NAME} explaining why readiness is genuinely required."
        )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Health-endpoint semantics lint")
    parser.add_argument(
        "--list-violations",
        action="store_true",
        help="Print every bare-/health call site (ignores the allowlist) and exit 0 — baseline measure",
    )
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.list_violations:
        violations = collect_violations(repo_root)
        print(f"bare-/health call sites: {len(violations)}")
        for site in sorted(violations):
            print(f"   {site}   <- {', '.join(sorted(set(violations[site])))}")
        return 0

    errors = check(repo_root)
    if errors:
        print("health-endpoint semantics lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("health-endpoint semantics lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
