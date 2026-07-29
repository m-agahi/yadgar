#!/usr/bin/env python3
"""Internal-route existence lint — every cited route literal must be registered.

WHY THIS EXISTS (the incident, 2026-07-29)
------------------------------------------
``yadgar/core/vacuum/__init__.py`` POSTed to ``{yadgar_url}/api/check_invariants``
— a route that was registered NOWHERE.  Six tests mocked that exact URL to return
200, and one even asserted the POST carried a bearer header.  ``check_invariants``
existed only as a backend admin op reachable via the MCP tool, never over HTTP.
Mocked-endpoint tests validated a route that did not exist: the mocks agreed with
the caller, and nothing ever compared either against the route table.

There was no guard at all here — this is a greenfield build, not a repair.

FEASIBILITY WAS MEASURED, NOT ASSUMED
-------------------------------------
The "brittle collector that cries wolf and gets deleted" risk was the right thing
to fear.  It does not materialise at this repo's scale.  A naive unfiltered AST
sweep gives ~30 unresolved literals (unshippable).  The four filters below plus
two-way segment wildcarding collapse that to a handful, of which the survivors
are external services and one label string.  Re-measure with ``--list-unresolved``
if the repo grows a lot of new outbound integrations.

STATED LIMITATIONS — this guard covers the LITERAL surface only
--------------------------------------------------------------
  * UNION ROUTE TABLE ⇒ NO PER-APP TARGETING.  Core routes and backend routes are
    matched against one combined table, so a call aimed at an EXTERNAL service can
    spuriously resolve against one of our own paths (a Tempo ``/api/traces/{}``
    would resolve against our ``/api/traces/recent``).  Discriminating targets
    would need a per-call-site manifest, because base-URL VARIABLE NAMES ARE NOT
    RELIABLE: ``backend_url`` means SurrealDB in ``core/backup/backup.py`` and
    ``core/vacuum/phases.py`` but is checked as a generic ``/health`` elsewhere.
    A manifest was considered and rejected as disproportionate for a
    handful-of-survivors baseline.  Revisit if cross-app misrouting is observed.
  * NO METHOD CHECKING.  A GET against a POST-only route passes.
  * NO DATAFLOW.  Paths assembled from variables (``p = "/api/" + name``) escape
    entirely.
  * NON-TEST CODE ONLY.  Deliberately narrower than "also scan test mocks": the
    production literal is the root fact, and the six mocks were downstream of it.

Allowlist: ``.route-literal-allowlist.json`` — ``{path: {rationale, target}}``,
rationale >= 40 chars, ``target`` in {external, label, dynamic}, and a STALE entry
(an allowlisted path that starts resolving) is a HARD ERROR.

Usage:
  python scripts/check_route_literals.py                    # check, exit 0/1
  python scripts/check_route_literals.py --list-unresolved  # measure the baseline
  python scripts/check_route_literals.py --list-routes      # dump the route table
  python scripts/check_route_literals.py --repo-root /path

Exit codes:
  0  every collected literal resolves against the route table or is allowlisted
  1  one or more UNRESOLVED-ROUTE / STALE / malformed-allowlist violations
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWLIST_NAME = ".route-literal-allowlist.json"
_MIN_RATIONALE = 40
_VALID_TARGETS = frozenset({"external", "label", "dynamic"})

# Decorators that register an HTTP route. `custom_route` is the MCP server's
# (core); the verb methods are the backend's FastAPI app.
_ROUTE_DECORATORS = frozenset({"custom_route", "route", "get", "post", "put", "delete", "patch"})

# Only strings that begin with one of these namespace prefixes are treated as
# route literals. Without this the sweep collects every string in the repo.
_NAMESPACE_PREFIXES: tuple[str, ...] = (
    "/api/",
    "/hooks/",
    "/health",
    "/metrics",
    "/admin",
    "/graph",
    "/viz",
    "/recall",
    "/restore",
    "/consolidate",
    "/embed",
    "/rerank",
    "/read_query",
)

# Placeholder standing in for an f-string interpolation inside a path.
_HOLE = "{}"


# ---------------------------------------------------------------------------
# Rendering AST string nodes to path templates
# ---------------------------------------------------------------------------
def render_node(node: ast.expr) -> str | None:
    """Render a Constant(str) or JoinedStr to a path template.

    An f-string interpolation becomes ``{}`` so ``f"{base}/api/x/{key}"`` renders
    as ``{}/api/x/{}`` and the interpolated segment can later match a route's own
    ``{key}`` placeholder.
    """
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


def extract_path(rendered: str) -> str | None:
    """Return the namespace-prefixed path tail of *rendered*, or None.

    ``f"{base}/api/check_invariants"`` renders as ``{}/api/check_invariants``;
    the path of interest is the ``/api/...`` tail, not the whole template.
    """
    best: int | None = None
    for prefix in _NAMESPACE_PREFIXES:
        idx = rendered.find(prefix)
        if idx != -1 and (best is None or idx < best):
            best = idx
    return rendered[best:] if best is not None else None


# ---------------------------------------------------------------------------
# The four noise filters (measured — see module docstring)
# ---------------------------------------------------------------------------
def apply_filters(rendered: str, routes: set[str]) -> str | None:
    """Return the cleaned path from a rendered string, or None if it is noise.

    ORDER MATTERS.  Rules 1-2 test the WHOLE rendered string, BEFORE the path tail
    is extracted.  Testing the tail instead lets prose through whenever the path
    sits at the END of a sentence: ``"Number of results from /recall."`` has a
    space, but its tail ``"/recall."`` does not.  Measured — that ordering bug
    alone produced 8 spurious survivors, all Pydantic Field descriptions.

    1. reject strings containing a space — config help text, log messages,
       Field(description=...) prose (``"/health (API readiness) ..."``)
    2. reject strings containing ``%`` — printf-style log format strings
       (``"/rerank/%s"`` circuit-breaker messages)
    3. strip at ``?`` — query strings (``"/hooks/file-changed?path="``)
    4. reject a path that ends in ``/`` AND is a strict prefix of a registered
       route — these are the ``auth_middleware`` prefix-match constants
       (``/api/``, ``/hooks/``, ``/api/logs/``, ``/api/control/action/``), which
       are namespace tests, not request targets
    """
    if " " in rendered or "%" in rendered:
        return None
    path = extract_path(rendered)
    if path is None:
        return None
    path = path.split("?", 1)[0]
    if not path:
        return None
    if path.endswith("/") and any(r.startswith(path) and r != path for r in routes):
        return None
    return path


# ---------------------------------------------------------------------------
# Matching — two-way segment wildcarding
# ---------------------------------------------------------------------------
def _segments(path: str) -> list[str]:
    return path.strip("/").split("/")


def _is_wildcard(seg: str) -> bool:
    """A segment is a wildcard if it carries a placeholder on EITHER side.

    Routes declare ``{key}``; call sites render interpolations as ``{}``.  Two-way
    wildcarding is what resolves ``/api/control/maintenance/{}`` against
    ``/api/control/maintenance/enter`` and ``/api/runtime-config/{}{}`` against
    ``/api/runtime-config/{key}``.
    """
    return "{" in seg


def path_matches(literal: str, route: str) -> bool:
    lit, rt = _segments(literal), _segments(route)
    if len(lit) != len(rt):
        return False
    return all(a == b or _is_wildcard(a) or _is_wildcard(b) for a, b in zip(lit, rt, strict=False))


def resolves(literal: str, routes: set[str]) -> bool:
    return any(path_matches(literal, r) for r in routes)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def collect_routes_from_source(src: str) -> set[str]:
    """Every path constant declared by a route decorator in *src*."""
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover - defensive
        return set()
    routes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if _decorator_name(dec) not in _ROUTE_DECORATORS:
                continue
            if not dec.args:
                continue
            rendered = render_node(dec.args[0])
            if rendered and rendered.startswith("/"):
                routes.add(rendered)
    return routes


def collect_literals_from_source(src: str) -> list[tuple[str, int]]:
    """Every rendered string containing a namespace-prefixed path, with its line.

    Route-decorator arguments are excluded — a route declaring itself is not a
    call site, and including them would make every route trivially resolve.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover - defensive
        return []

    declared: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and _decorator_name(dec) in _ROUTE_DECORATORS:
                    if dec.args:
                        declared.add(id(dec.args[0]))

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        if id(node) in declared:
            continue
        rendered = render_node(node)
        if rendered and extract_path(rendered) is not None:
            found.append((rendered, getattr(node, "lineno", 0)))
    return found


def _non_test_py(repo_root: Path) -> list[Path]:
    pkg = repo_root / "yadgar"
    if not pkg.is_dir():
        return []
    out = []
    for py in sorted(pkg.rglob("*.py")):
        try:
            parts = py.relative_to(repo_root).parts
        except ValueError:  # pragma: no cover - defensive
            parts = py.parts
        if "tests" in parts:
            continue
        out.append(py)
    return out


def collect(repo_root: Path) -> tuple[set[str], dict[str, list[str]]]:
    """Return (route_table, {clean_path: [\"file:line\", ...]})."""
    files = _non_test_py(repo_root)
    sources: list[tuple[Path, str]] = []
    routes: set[str] = set()
    for py in files:
        try:
            src = py.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            continue
        sources.append((py, src))
        routes |= collect_routes_from_source(src)

    # Filter rule 4 needs the finished route table, so literals are a second pass.
    literals: dict[str, list[str]] = {}
    for py, src in sources:
        for rendered, lineno in collect_literals_from_source(src):
            clean = apply_filters(rendered, routes)
            if clean is None:
                continue
            try:
                disp = py.relative_to(repo_root).as_posix()
            except ValueError:  # pragma: no cover - defensive
                disp = str(py)
            literals.setdefault(clean, []).append(f"{disp}:{lineno}")
    return routes, literals


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def load_allowlist(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object of {{path: {{rationale, target}}}}")
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------
def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    if repo_root is None:
        repo_root = _REPO_ROOT

    routes, literals = collect(repo_root)
    try:
        allowlist = load_allowlist(repo_root / _ALLOWLIST_NAME)
    except ValueError as exc:
        return [f"MALFORMED allowlist: {exc}"]

    errors: list[str] = []

    for path, meta in sorted(allowlist.items()):
        meta = meta if isinstance(meta, dict) else {}
        rationale = meta.get("rationale", "")
        target = meta.get("target", "")
        if len(rationale.strip()) < _MIN_RATIONALE:
            errors.append(
                f"MALFORMED allowlist entry {path!r}: rationale must be >= "
                f"{_MIN_RATIONALE} chars (got {len(rationale.strip())})"
            )
        if target not in _VALID_TARGETS:
            errors.append(
                f"MALFORMED allowlist entry {path!r}: target {target!r} must be one of "
                f"{sorted(_VALID_TARGETS)}"
            )
        if resolves(path, routes):
            errors.append(
                f"STALE allowlist entry {path!r}: it now resolves against a registered "
                f"route — remove it from {_ALLOWLIST_NAME}"
            )

    for path in sorted(literals):
        if path in allowlist or resolves(path, routes):
            continue
        sites = ", ".join(sorted(set(literals[path])))
        errors.append(
            f"UNRESOLVED-ROUTE: {path!r} is requested at {sites} but no route is "
            f"registered for it. Either the route is missing — add it — or the call "
            f"targets an external service, in which case add a governed entry to "
            f"{_ALLOWLIST_NAME}."
        )

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal-route existence lint")
    parser.add_argument("--list-routes", action="store_true", help="Dump the route table")
    parser.add_argument(
        "--list-unresolved",
        action="store_true",
        help="Print unresolved literals (ignores the allowlist) and exit 0 — the baseline measure",
    )
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.list_routes or args.list_unresolved:
        routes, literals = collect(repo_root)
        if args.list_routes:
            print(f"=== routes ({len(routes)}) ===")
            for r in sorted(routes):
                print(r)
            return 0
        unresolved = {p: s for p, s in literals.items() if not resolves(p, routes)}
        print(f"routes={len(routes)} collected={len(literals)} UNRESOLVED={len(unresolved)}")
        for path in sorted(unresolved):
            print(f"   {path}   <- {', '.join(sorted(set(unresolved[path])))}")
        return 0

    errors = check(repo_root)
    if errors:
        print("route-literal existence lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("route-literal existence lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
