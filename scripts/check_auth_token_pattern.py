#!/usr/bin/env python3
"""Bearer-token resolution chokepoint guard (bug train car 9).

WHAT THIS GUARD DOES
--------------------
AST-scans ``yadgar/**/*.py`` (tests excluded, mirrors check_ledger_chokepoint /
check_dynamic_span_names) and FAILS on any code that hand-rolls
``os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)`` (or the bracket-subscript
equivalent) OUTSIDE the explicit allowlist.

``yadgar/core/install/auth_token.py`` is the ONE sanctioned bearer-token
resolver: env var first, else parsed out of ``secrets.env``. Its own
docstring documents why it exists — THREE separate hand-rolled copies of
"env, else secrets.env" had drifted, and the third was WRONG (env-only,
which silently 401'd every unsourced host CLI request). A bare
``os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)`` anywhere else in the tree
is exactly that anti-pattern regrowing — this guard exists so it cannot
regrow silently again.

ALLOWED
-------
- An entry in the allowlist file (one ``path:lineno:reason`` per line,
  ``#``-prefixed lines and blank lines ignored). Use sparingly — legitimate
  reasons documented so far (see ``.auth-token-pattern-allowlist.txt``):

    1. Server-side INCOMING-token verification (comparing a request's
       bearer against the server's own configured value) is a different
       operation from a CLIENT resolving what token to SEND — it must read
       the literal env var the server was launched with, not fall back to
       secrets.env.
    2. Code under ``yadgar/backend/`` or ``yadgar/_shared/`` cannot import
       ``yadgar.core.install.auth_token`` at all — the import-linter layer
       contracts forbid both ``backend -> core`` and ``_shared -> core``
       edges (pyproject.toml ``[tool.importlinter]``). These call sites run
       inside containers where docker-compose.yml requires the env var
       (``${YADGAR_MCP_AUTH_TOKEN:?must set ...}``) before the container
       even starts, so the secrets.env fallback buys nothing there anyway.
    3. Portability fallback branches (``except ImportError:`` bodies in the
       hyphenated hook entry-point scripts) that exist specifically to keep
       the hook functional when the yadgar package itself is not
       importable — importing the resolver would defeat the fallback's own
       purpose.
    4. Pre-existing violations already fixed on a sibling branch pending
       merge, where re-applying the identical fix here would only produce
       a merge conflict.

DETECTION SCOPE
---------------
- ``os.environ.get("YADGAR_MCP_AUTH_TOKEN"[, default])`` — any call whose
  ``func`` resolves to ``<name>.environ.get`` (handles ``import os`` and
  ``os.environ`` access) with a first argument matching the token env var
  name, however the module was imported.
- ``os.environ["YADGAR_MCP_AUTH_TOKEN"]`` — the bracket-subscript equivalent.
- Tests are excluded from the scan (mirrors check_ledger_chokepoint / I33).

Usage:
  python scripts/check_auth_token_pattern.py                  # check yadgar/, exit 0/1
  python scripts/check_auth_token_pattern.py --root <dir>     # scan a different root
  python scripts/check_auth_token_pattern.py --allowlist <p>  # allowlist file
  python scripts/check_auth_token_pattern.py --list-all       # print every violation

Exit codes:
  0  no unallowed violations found
  1  one or more violations found
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The env var this guard is about. A single named constant so the AST scan
#: and the human-facing messages can never drift from each other.
TOKEN_ENV_VAR = "YADGAR_MCP_AUTH_TOKEN"

#: Default allowlist file, committed at the repo root (matches the
#: ``.route-literal-allowlist.json`` / ``.complexity-allowlist.json``
#: dotfile convention; plain ``path:lineno:reason`` text mirrors
#: check_ledger_chokepoint's format since violations are line-anchored).
DEFAULT_ALLOWLIST = _REPO_ROOT / ".auth-token-pattern-allowlist.txt"


@dataclass(frozen=True)
class Violation:
    source_file: Path
    lineno: int
    snippet: str


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def _load_allowlist(path: Path | None) -> set[tuple[str, int]]:
    """Parse ``path:lineno:reason`` lines into a ``{(path, lineno)}`` set.

    A missing path or empty file returns an empty set (no allowlist).
    Malformed lines are silently dropped — the allowlist is an opt-in
    permission, never a build-breaker on its own.
    """
    if path is None or not path.is_file():
        return set()
    allowed: set[tuple[str, int]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # path:lineno:reason — reason may contain ':'
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        try:
            lineno = int(parts[1].strip())
        except ValueError:
            continue
        allowed.add((parts[0].strip(), lineno))
    return allowed


# ---------------------------------------------------------------------------
# AST scan
# ---------------------------------------------------------------------------


def _is_environ_get_call(node: ast.Call) -> bool:
    """True when ``node`` is shaped like ``<anything>.environ.get(...)``.

    Matches ``os.environ.get(...)`` regardless of the module alias used to
    import ``os`` (``import os as _os`` etc.) — the rule is the attribute
    chain shape (``.environ.get``), not the specific module name, mirroring
    check_ledger_chokepoint's name-based (not import-path-based) matching.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get"):
        return False
    owner = func.value
    return isinstance(owner, ast.Attribute) and owner.attr == "environ"


def _first_arg_is_token_var(call: ast.Call) -> bool:
    if not call.args:
        return False
    first = call.args[0]
    return isinstance(first, ast.Constant) and first.value == TOKEN_ENV_VAR


def _violations_in_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Module,
    src_file: Path,
) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(func):
        # os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)
        if (
            isinstance(node, ast.Call)
            and _is_environ_get_call(node)
            and _first_arg_is_token_var(node)
        ):
            violations.append(
                Violation(
                    source_file=src_file,
                    lineno=node.lineno,
                    snippet=f'os.environ.get("{TOKEN_ENV_VAR}", ...)',
                )
            )
        # os.environ["YADGAR_MCP_AUTH_TOKEN"]
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == TOKEN_ENV_VAR
        ):
            violations.append(
                Violation(
                    source_file=src_file,
                    lineno=node.lineno,
                    snippet=f'os.environ["{TOKEN_ENV_VAR}"]',
                )
            )
    return violations


def _allowlist_key(src_file: Path) -> str:
    """Normalize a scanned path for allowlist matching.

    Prefers repo-root-relative (portable across machines/CI, and the form
    the committed ``.auth-token-pattern-allowlist.txt`` uses) and falls back
    to the absolute path when *src_file* is not under ``_REPO_ROOT`` — e.g.
    a test scanning a ``tmp_path`` fixture root, mirroring
    check_ledger_chokepoint's test-local absolute-path allowlist entries.
    """
    resolved = src_file.resolve()
    try:
        return str(resolved.relative_to(_REPO_ROOT))
    except ValueError:
        return str(resolved)


def scan_file(src_file: Path, allowed: set[tuple[str, int]]) -> list[Violation]:
    """Parse ``src_file`` and return every unallowed violation in it."""
    try:
        source = src_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(src_file))
    except SyntaxError as exc:
        print(f"WARNING: could not parse {src_file}: {exc}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"WARNING: could not read {src_file}: {exc}", file=sys.stderr)
        return []

    raw = _violations_in_function(tree, src_file)
    key = _allowlist_key(src_file)
    return [v for v in raw if (key, v.lineno) not in allowed]


def _iter_py_files(root: Path) -> list[Path]:
    """Yield in-scope .py files under root (tests excluded, mirrors I33)."""
    files: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel = str(p)
        if "/tests/" in rel or rel.endswith("_test.py") or p.name.startswith("test_"):
            continue
        files.append(p)
    return files


def scan(root: Path | None = None, allowed: set[tuple[str, int]] | None = None) -> list[Violation]:
    """Scan all in-scope files under ``root`` and return every violation."""
    if root is None:
        root = _REPO_ROOT / "yadgar"
    if allowed is None:
        allowed = set()
    violations: list[Violation] = []
    for f in _iter_py_files(root):
        violations.extend(scan_file(f, allowed))
    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bearer-token resolution chokepoint guard — every read of "
            f"{TOKEN_ENV_VAR} must go through "
            "yadgar.core.install.auth_token.resolve_auth_token()."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(_REPO_ROOT / "yadgar"),
        help="Directory to scan (default: yadgar/).",
    )
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST),
        help=f"Path to allowlist file (path:lineno:reason per line, default: {DEFAULT_ALLOWLIST.name}).",
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="Print every violation (same as default failure output).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)
    allowed = _load_allowlist(Path(args.allowlist)) if args.allowlist else set()
    violations = scan(root, allowed)

    for v in violations:
        print(
            f"{v.source_file}:{v.lineno}: hand-rolled `{v.snippet}` bypasses the "
            "sanctioned resolver — route through "
            "yadgar.core.install.auth_token.resolve_auth_token() instead "
            f"(or add an entry to {DEFAULT_ALLOWLIST.name} with a documented reason)."
        )

    if violations:
        print(
            f"\n{len(violations)} auth-token-pattern violation(s) found — every read of "
            f"{TOKEN_ENV_VAR} for OUTBOUND auth must go through resolve_auth_token(). "
            "See scripts/check_auth_token_pattern.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
