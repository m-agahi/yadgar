#!/usr/bin/env python3
"""stdlib-urllib HTTPError/response-leak lint (Car 0036, py3.14 ResourceWarning).

WHY THIS EXISTS
---------------
On Python 3.14, ``urllib.error.HTTPError`` is itself a response object holding
a file wrapper (a ``tempfile._TemporaryFileWrapper`` via ``addbase``). Catching
it and dropping the reference — the standard fail-open shape
(``except HTTPError: return default``) — never closes that wrapper; its
deallocator fires a spurious ``ResourceWarning`` at an arbitrary later GC that
pytest-xdist mis-attributes to an unrelated test (fatal under the zero-warning
gate, ADR-0087). The SAME leak applies to a successful response: ``urlopen()``
raises ``HTTPError`` *before* entering a ``with`` block, so ``with urlopen(...)
as resp:`` protects the success path only — the exception path needs an
independent, explicit close.

v5.164 (Car G4/G5) fixed the pattern in ``runtime_config_client.py`` and
``session-start-context.py`` — this guard is the anti-recurrence artifact so
the next fail-open urllib client doesn't reintroduce it.

WHAT THIS GUARD CHECKS
-----------------------
Two independent rules, both required:

  (a) except-HTTPError-not-closed: an ``except (urllib.error.)HTTPError as e:``
      handler whose body never closes ``e`` — neither ``e.close()`` nor a call
      that passes ``e`` as an argument to a helper (e.g. ``_close_quietly(e)``,
      the pattern ``runtime_config_client.py`` and ``session-start-context.py``
      use). An UNBOUND handler (``except HTTPError:`` with no ``as name``) is
      always a violation — nothing to close without a name.

  (b) urlopen-not-closed: a call to ``urlopen`` (``urllib.request.urlopen``, an
      aliased import, or a bare-name pass-through wrapper whose name ends in
      "urlopen", e.g. ``_safe_urlopen``) whose result is not:
        - the context expression of a ``with``/``async with`` (directly, or
          wrapped in ``contextlib.closing(...)``), or
        - assigned to a name that is later ``.close()``'d anywhere in the
          same enclosing function.
      A bare-expression call (return value discarded) or a call returned
      directly to the caller (pass-through, caller owns closing) are both
      violations under this rule.

Both rules are heuristic, same spirit as check_route_literals.py /
check_health_endpoint_semantics.py: AST-shape matching, not full data-flow.
False positives are expected on legitimate pass-through wrappers (the callee
closes it) — governed via the allowlist, not by weakening the rule.

SCOPE — same boundary as check_dead_capability.py / check_health_endpoint_
semantics.py: only the shipped ``yadgar/`` package (excluding tests). This is
NOT merely "keep it fast" — benchmarks/, scripts/ (dev/ops one-shots), and
docs/ are out-of-process tooling never part of the fail-open client surface
this guard protects, AND at least one such file uses Python-3.14-only
permissive except-tuple grammar (``except A, B, C:`` without parens) that
raises SyntaxError under older interpreters — pre-commit's "system" hook can
resolve a different ``python`` than the project's pinned 3.14 venv, and a
SyntaxError-skipped file would otherwise silently vanish its own allowlist
coverage (a real STALE false-positive, reproduced and root-caused during
Car 0036). Scoping to the package that must parse under every supported
interpreter with `from __future__ import annotations` and no exotic grammar
sidesteps the whole class.

Allowlist: .urllib-httperror-close-allowlist.json — {"path:line": {"rule":
"a"|"b", "rationale": ...}}, rationale >= 40 chars. A STALE entry (the site no
longer violates) is itself a hard error, same governance as the other
allowlist-backed checkers in this repo.

Usage:
  python scripts/check_urllib_httperror_close.py                    # check, exit 0/1
  python scripts/check_urllib_httperror_close.py --list-violations  # baseline, ignores allowlist, always exits 0
  python scripts/check_urllib_httperror_close.py --repo-root /path

Exit codes:
  0  every violation is allowlisted with a rationale (or none exist)
  1  an ungoverned violation, a STALE allowlist entry, or a malformed allowlist entry
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWLIST_NAME = ".urllib-httperror-close-allowlist.json"
_MIN_RATIONALE = 40

_SKIP_DIR_PARTS = frozenset({"tests", ".venv", "node_modules", ".git", "__pycache__"})


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


def _iter_py_files(repo_root: Path):
    """Yield (path, rel) for every non-test .py file under the shipped ``yadgar/``
    package — same scope boundary as check_dead_capability.py /
    check_health_endpoint_semantics.py. Deliberately excludes benchmarks/,
    scripts/ (dev/ops one-shots), and docs/ — those are not part of the
    shipped client surface this guard protects (see the allowlist rationale
    on the one legitimate exception, daemon/runtime.py::_safe_urlopen).
    """
    pkg = repo_root / "yadgar"
    if not pkg.is_dir():
        return
    for py in sorted(pkg.rglob("*.py")):
        try:
            rel = py.relative_to(repo_root)
        except ValueError:  # pragma: no cover - defensive
            continue
        if any(part in _SKIP_DIR_PARTS for part in rel.parts):
            continue
        yield py, rel


# ---------------------------------------------------------------------------
# Rule (a) — except HTTPError not closed
# ---------------------------------------------------------------------------


class _UrllibErrorAliases:
    """Per-file import aliases that resolve to ``urllib.error`` / ``HTTPError``.

    Scopes rule (a) to the stdlib ``urllib.error.HTTPError`` specifically —
    other libraries define their own ``HTTPError`` (e.g. ``httpx.HTTPError``)
    that do NOT have the py3.14 tempfile-wrapper leak this guard targets, and
    a name-only match (``node.attr == "HTTPError"``) false-positives on them.
    """

    def __init__(self) -> None:
        self.root_aliases: set[str] = set()  # `import urllib.error` -> {"urllib"}
        self.direct_aliases: set[str] = set()  # `import urllib.error as _err` -> {"_err"}
        self.bare_names: set[str] = set()  # `from urllib.error import HTTPError [as X]`

    @classmethod
    def collect(cls, tree: ast.Module) -> _UrllibErrorAliases:
        aliases = cls()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name != "urllib.error":
                        continue
                    if alias.asname:
                        aliases.direct_aliases.add(alias.asname)
                    else:
                        aliases.root_aliases.add("urllib")
            elif isinstance(node, ast.ImportFrom):
                if node.module != "urllib.error":
                    continue
                for alias in node.names:
                    if alias.name == "HTTPError":
                        aliases.bare_names.add(alias.asname or "HTTPError")
        return aliases

    def matches(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Attribute):
            if node.attr != "HTTPError":
                return False
            value = node.value
            # urllib.error.HTTPError
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "error"
                and isinstance(value.value, ast.Name)
                and value.value.id in self.root_aliases
            ):
                return True
            # _err.HTTPError (import urllib.error as _err)
            if isinstance(value, ast.Name) and value.id in self.direct_aliases:
                return True
            return False
        if isinstance(node, ast.Name):
            return node.id in self.bare_names
        return False


def _is_httperror_type(node: ast.expr | None, aliases: _UrllibErrorAliases) -> bool:
    """True if *node* names urllib.error.HTTPError, directly or in a type tuple."""
    if node is None:
        return False
    if isinstance(node, ast.Tuple):
        return any(_is_httperror_type(elt, aliases) for elt in node.elts)
    return aliases.matches(node)


def _handler_closes_name(handler: ast.ExceptHandler, name: str) -> bool:
    """True if *name* is ``.close()``'d, or passed as an arg to a helper call."""
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            # name.close()
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "close"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
            ):
                return True
            # helper(name) / helper(x, name, ...) — e.g. _close_quietly(http_exc)
            all_args = list(node.args) + [kw.value for kw in node.keywords]
            for arg in all_args:
                if isinstance(arg, ast.Name) and arg.id == name:
                    return True
    return False


def _collect_rule_a(tree: ast.Module, rel: Path) -> dict[str, str]:
    """Return {"file:line": "reason"} for rule (a) violations."""
    aliases = _UrllibErrorAliases.collect(tree)
    if not (aliases.root_aliases or aliases.direct_aliases or aliases.bare_names):
        return {}  # file never imports urllib.error — nothing to check
    violations: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_httperror_type(node.type, aliases):
            continue
        lineno = getattr(node, "lineno", 0)
        key = f"{rel.as_posix()}:{lineno}"
        if node.name is None:
            violations[key] = "except HTTPError with no 'as name' binding — cannot close it"
            continue
        if not _handler_closes_name(node, node.name):
            violations[key] = (
                f"except HTTPError as {node.name}: handler body never closes "
                f"{node.name} (no {node.name}.close() and no helper call passed {node.name})"
            )
    return violations


# ---------------------------------------------------------------------------
# Rule (b) — urlopen result not closed
# ---------------------------------------------------------------------------


def _is_urlopen_call(node: ast.expr) -> bool:
    call = node
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == "urlopen"
    if isinstance(func, ast.Name):
        return func.id == "urlopen" or func.id.endswith("urlopen")
    return False


def _unwrap_closing(node: ast.expr) -> ast.expr:
    """If *node* is ``contextlib.closing(x)`` / ``closing(x)``, return x."""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "closing" and node.args:
            return node.args[0]
    return node


class _ParentAnnotator(ast.NodeVisitor):
    """Single pass: annotate every node with .parent and .enclosing_func."""

    def __init__(self) -> None:
        self._func_stack: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        node.enclosing_func = self._func_stack[-1] if self._func_stack else None  # type: ignore[attr-defined]
        is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_func:
            self._func_stack.append(node)
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]
            self.visit(child)
        if is_func:
            self._func_stack.pop()


def _name_closed_in_function(func_node: ast.AST | None, name: str) -> bool:
    """True if *name*.close() is called anywhere in *func_node*, or *name* is
    used (bare or via contextlib.closing) as a `with` context expression.
    """
    if func_node is None:
        return False
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "close"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
            ):
                return True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = _unwrap_closing(item.context_expr)
                if isinstance(expr, ast.Name) and expr.id == name:
                    return True
    return False


def _collect_rule_b(tree: ast.Module, rel: Path) -> dict[str, str]:
    """Return {"file:line": "reason"} for rule (b) violations."""
    annotator = _ParentAnnotator()
    annotator.visit(tree)

    violations: dict[str, str] = {}
    for node in ast.walk(tree):
        if not _is_urlopen_call(node):
            continue
        parent = getattr(node, "parent", None)
        lineno = getattr(node, "lineno", 0)
        key = f"{rel.as_posix()}:{lineno}"

        # (1) with urlopen(...) as x:  /  with closing(urlopen(...)) as x:
        if isinstance(parent, ast.withitem):
            continue
        if isinstance(parent, ast.Call):
            grandparent = getattr(parent, "parent", None)
            gp_func = parent.func
            gp_name = (
                gp_func.attr if isinstance(gp_func, ast.Attribute) else getattr(gp_func, "id", None)
            )
            if gp_name == "closing" and isinstance(grandparent, ast.withitem):
                continue

        # (2) resp = urlopen(...); ... resp.close() somewhere in the function
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            func_node = getattr(node, "enclosing_func", None)
            if names and all(_name_closed_in_function(func_node, n) for n in names):
                continue
            violations[key] = (
                f"urlopen() assigned to {names!r} but never .close()'d "
                f"(and not used in a `with`) in the enclosing function"
            )
            continue

        # (3) return urlopen(...) — pass-through; caller owns closing.
        if isinstance(parent, ast.Return):
            violations[key] = (
                "urlopen() result returned directly — caller must close it (verify, then allowlist)"
            )
            continue

        # (4) bare-expression statement — result discarded, never closed.
        if isinstance(parent, ast.Expr):
            violations[key] = (
                "urlopen() called as a bare expression — result discarded without closing"
            )
            continue

        # (5) anything else (passed inline to another call, etc.) — conservative flag.
        violations[key] = "urlopen() result used inline — not provably closed"

    return violations


# ---------------------------------------------------------------------------
# Collection + allowlist
# ---------------------------------------------------------------------------


def collect_violations(repo_root: Path) -> dict[str, str]:
    """Return {"file:line": reason} across both rules for the whole tree."""
    violations: dict[str, str] = {}
    for py, rel in _iter_py_files(repo_root):
        try:
            src = py.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover - defensive
            continue
        violations.update(_collect_rule_a(tree, rel))
        violations.update(_collect_rule_b(tree, rel))
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
                f"STALE allowlist entry {site!r}: it no longer violates the urllib "
                f"HTTPError/response-close rule — remove it from {_ALLOWLIST_NAME}"
            )

    for site in sorted(violations):
        if site in allowlist:
            continue
        errors.append(f"UNCLOSED-URLLIB-RESPONSE: {site} — {violations[site]}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="stdlib-urllib HTTPError/response-leak lint (Car 0036, py3.14 ResourceWarning)."
    )
    parser.add_argument(
        "--list-violations",
        action="store_true",
        help="Print every violation (ignores the allowlist) and exit 0 — baseline measure",
    )
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.list_violations:
        violations = collect_violations(repo_root)
        print(f"unclosed urllib response/HTTPError call sites: {len(violations)}")
        for site in sorted(violations):
            print(f"   {site}   <- {violations[site]}")
        return 0

    errors = check(repo_root)
    if errors:
        print("urllib HTTPError/response-close lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("urllib HTTPError/response-close lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
