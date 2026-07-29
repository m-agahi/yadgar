#!/usr/bin/env python3
"""I32 companion — CAPABILITY_REGISTRY prose-token liveness lint.

WHY THIS EXISTS (the incident, 2026-07-29)
------------------------------------------
CAP-CODEGRAPH-001's ``explanation:`` claimed ``CODE_GRAPH_ENABLED`` "survives
only in ``cli/setup.py`` as a host-binary INSTALL trigger".  Commit ``7cd74ea0``
— a PURE CODE CHANGE with zero contract files staged — removed that last read.
The registry stayed wrong for ~2h until a human noticed.

``check_capability_coverage.py`` (I32) ran on that PR and PASSED.  It is wired
UNCONDITIONALLY in CI (no ``files:`` predicate), so this was never a trigger
problem: the hole is that ``check()`` covers four ENUMERABLE identifier surfaces
(settings / tools / migrations / BC) and never reads ``explanation:`` or
``wiring:`` prose at all.  Assertion-scope < claim-scope.  That script's own
docstring is honest about the gap ("status accuracy is a human/review
responsibility") — the gap was documented, just not gated.

Why a text grep would NOT have worked: at ``7cd74ea0``,
``git grep -w CODE_GRAPH_ENABLED -- yadgar/`` still returned four non-test hits,
and ALL FOUR were docstrings or ``#`` comments.  Liveness has to be measured over
EXECUTABLE code, which is what the AST gives for free — comments never enter the
AST, and docstrings are identifiable as the first ``Expr(Constant(str))`` of a
Module/ClassDef/FunctionDef body.

═══════════════════════════════════════════════════════════════════════════════
THE CEILING — read this before trusting a green run
═══════════════════════════════════════════════════════════════════════════════
This guard detects IDENTIFIER DEATH, NOT PROSE TRUTH.  It is a ratchet and a
nudge that forces a look, in the same family as I32's "status accuracy is a
human responsibility".  It is NOT a proof of registry correctness.  Do not
advertise it as one.  Specifically:

  * It fires when a cited identifier stops existing.  It CANNOT detect a claim
    that is wrong ABOUT A LIVE IDENTIFIER — "`X` defaults to false" when X
    defaults to true is invisible here.
  * Liveness includes non-docstring STRING LITERALS (``os.environ["FOO"]``,
    ``getattr(s, "FOO")``).  A token named only in a log message therefore reads
    as live.  Tightening this would break the env-var lookup shapes the repo
    actually uses; the looser rule is deliberate.
  * Once a token is allowlisted as archaeology it is PERMANENTLY outside the
    guard — a LATER false claim about that same token is invisible.
    ``CODE_GRAPH_ENABLED``, the token that motivated this whole lint, is
    allowlisted on day one (the corrected prose deliberately says it is read
    nowhere), so this exact token is outside the guard from here on.  That is
    not a bug; it is the price of a liveness-shaped check.
  * Docstring prose rots identically and is NOT gated (``code_graph/config.py``
    carried the same false claim in its module docstring).  Gating docstrings
    would multiply the claim surface ~100x with no measurement behind it.

Deliberately a SEPARATE script from ``check_capability_coverage.py``: that
script's contract is catalogue-completeness over four enumerable surfaces, and
mixing a heuristic prose check into it would muddy a clean invariant.

Allowlist: ``.registry-prose-allowlist.json`` — ``{token: {rationale}}``,
rationale >= 40 chars, and a STALE entry is a HARD ERROR (a token that comes back
to life, or that the registry stops citing, must be de-allowlisted).  That stale
rule is what keeps the allowlist from becoming a write-only dumping ground.
Governance mirrors ``.complexity-allowlist.json`` (I30).

Usage:
  python scripts/check_registry_prose_liveness.py               # check, exit 0/1
  python scripts/check_registry_prose_liveness.py --list-dead   # unresolved tokens
  python scripts/check_registry_prose_liveness.py --repo-root /path

Exit codes:
  0  every cited token is live or governed by an allowlist entry
  1  one or more DEAD-CLAIM / STALE / malformed-allowlist violations
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWLIST_NAME = ".registry-prose-allowlist.json"
_MIN_RATIONALE = 40

# An identifier-shaped claim: UPPER_SNAKE, >=5 chars, and containing at least one
# underscore.  The underscore requirement is load-bearing — without it the status
# enum values (SHADOW, DORMANT, WRITE) and other English words in backticks read
# as identifiers and the guard demands executable references to prose.
_CLAIM_RE = re.compile(r"^[A-Z][A-Z0-9_]{4,}$")
# Inline code span. `[^`\n]+` (not `[^`]+`) so an unbalanced backtick cannot pair
# across a newline and swallow the rest of the document.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Fenced code blocks must be removed BEFORE inline spans are matched, or the ```
# delimiters pair up with inline backticks and mis-tokenise everything after the
# first fence.  Measured: without this the real registry yields 0 claims instead
# of 380 — a silently-empty guard, which is the exact failure mode this whole
# plan exists to eliminate.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Identifier-shaped substrings inside a (non-docstring) string literal.
_TOKEN_IN_STR_RE = re.compile(r"[A-Z][A-Z0-9_]{4,}")

# Non-Python files where an env var legitimately lives (plain token scan — these
# are not Python, so there is no docstring/comment distinction worth drawing).
_CONFIG_GLOBS: tuple[str, ...] = (
    "flake.nix",
    "pyproject.toml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Dockerfile*",
    "entrypoint*.sh",
    "yadgar/core/hooks/*.sh",
)


# ---------------------------------------------------------------------------
# Claim collection — what the registry asserts exists
# ---------------------------------------------------------------------------
def collect_claims(registry_text: str) -> set[str]:
    """Every backtick-quoted identifier-shaped token in the registry."""
    claims: set[str] = set()
    for tok in _BACKTICK_RE.findall(_FENCE_RE.sub("", registry_text)):
        tok = tok.strip()
        if "_" not in tok:
            continue
        if _CLAIM_RE.match(tok):
            claims.add(tok)
    return claims


# ---------------------------------------------------------------------------
# Liveness collection — what EXECUTABLE code actually references
# ---------------------------------------------------------------------------
def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Object ids of every Constant node that is a docstring.

    Must be pre-collected: ``ast.walk`` visits docstring Constants like any other
    node, so skipping ``body[0]`` during the walk does not work.
    """
    ids: set[int] = set()
    doc_owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, doc_owners):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def live_tokens_in_source(src: str) -> set[str]:
    """Identifier-shaped tokens referenced by EXECUTABLE code in *src*.

    Comments are absent from the AST for free.  Docstrings are excluded
    explicitly.  Everything else — names, attributes, call kwargs, def/class
    names, and identifier-shaped substrings of ordinary string literals — counts.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()

    doc_ids = _docstring_constant_ids(tree)
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            tokens.add(node.name)
        elif isinstance(node, ast.arg):
            tokens.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            tokens.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in doc_ids:
                continue  # docstring — prose, not a reference
            tokens.update(_TOKEN_IN_STR_RE.findall(node.value))
    return tokens


def _is_test_path(path: Path, repo_root: Path) -> bool:
    try:
        parts = path.relative_to(repo_root).parts
    except ValueError:  # pragma: no cover - defensive
        parts = path.parts
    return "tests" in parts


def collect_live_tokens(repo_root: Path) -> set[str]:
    """Union of executable-code tokens across non-test Python + config files."""
    tokens: set[str] = set()

    pkg = repo_root / "yadgar"
    if pkg.is_dir():
        for py in sorted(pkg.rglob("*.py")):
            if _is_test_path(py, repo_root):
                continue
            try:
                tokens |= live_tokens_in_source(py.read_text(encoding="utf-8"))
            except OSError:  # pragma: no cover - defensive
                continue

    for pattern in _CONFIG_GLOBS:
        for cfg in sorted(repo_root.glob(pattern)):
            if not cfg.is_file():
                continue
            try:
                tokens.update(_TOKEN_IN_STR_RE.findall(cfg.read_text(encoding="utf-8")))
            # Parenthesised tuple required — CI compiles on <py3.14 where the
            # bare `except X, Y:` form is a SyntaxError. fmt:skip keeps ruff
            # (py314 target, PEP 758) from stripping the parens back to the bare
            # form. Same treatment as check_backend_bump.py.
            except (OSError, UnicodeDecodeError):  # fmt: skip  # pragma: no cover
                continue

    return tokens


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def load_allowlist(path: Path) -> dict:
    """Load the allowlist, or {} when absent. A malformed file is a hard error."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object of {{token: {{rationale}}}}")
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------
def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    if repo_root is None:
        repo_root = _REPO_ROOT
    registry_file = repo_root / "docs" / "contracts" / "CAPABILITY_REGISTRY.md"
    if not registry_file.is_file():
        return [f"registry not found at {registry_file}"]

    claims = collect_claims(registry_file.read_text(encoding="utf-8"))
    live = collect_live_tokens(repo_root)

    try:
        allowlist = load_allowlist(repo_root / _ALLOWLIST_NAME)
    except ValueError as exc:
        return [f"MALFORMED allowlist: {exc}"]

    errors: list[str] = []

    # Allowlist integrity FIRST — governance failures are hard regardless of the
    # liveness verdict (same posture as I30's allowlist checks).
    for token, meta in sorted(allowlist.items()):
        rationale = (meta or {}).get("rationale", "") if isinstance(meta, dict) else ""
        if len(rationale.strip()) < _MIN_RATIONALE:
            errors.append(
                f"MALFORMED allowlist entry `{token}`: rationale must be >= "
                f"{_MIN_RATIONALE} chars (got {len(rationale.strip())}) — say WHY the "
                "registry legitimately cites a token no executable code references"
            )
        if token in live:
            errors.append(
                f"STALE allowlist entry `{token}`: the token is referenced by executable "
                "code again — remove it from " + _ALLOWLIST_NAME
            )
        elif token not in claims:
            errors.append(
                f"STALE allowlist entry `{token}`: the registry no longer cites it — "
                "remove it from " + _ALLOWLIST_NAME
            )

    for token in sorted(claims - live - set(allowlist)):
        errors.append(
            f"DEAD-CLAIM: CAPABILITY_REGISTRY cites `{token}` but no executable code "
            "references it (comments and docstrings do not count). Either the prose is "
            f"stale — fix it — or add a governed entry to {_ALLOWLIST_NAME}."
        )

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="I32 companion — registry prose liveness")
    parser.add_argument(
        "--list-dead",
        action="store_true",
        help="Print every cited-but-dead token (ignores the allowlist) and exit 0",
    )
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.list_dead:
        registry_file = repo_root / "docs" / "contracts" / "CAPABILITY_REGISTRY.md"
        claims = collect_claims(registry_file.read_text(encoding="utf-8"))
        dead = sorted(claims - collect_live_tokens(repo_root))
        print(f"=== cited tokens ({len(claims)}) · dead ({len(dead)}) ===")
        for tok in dead:
            print(tok)
        return 0

    errors = check(repo_root)
    if errors:
        print("CAPABILITY_REGISTRY prose-liveness lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("CAPABILITY_REGISTRY prose-liveness lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
