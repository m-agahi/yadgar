"""v5.46.16 TDD — except-tuple py2 syntax sweep (RED).

Python 3 silently treats ``except X, Y:`` as ``except X as Y:`` — NOT
``except (X, Y):``.  Effect: only the first exception type is caught; the
second type is a *local name binding*, shadowing any builtin/variable with
that name.  ``Y`` exceptions propagate uncaught.

This test file enforces that every occurrence of the bare-tuple form is
replaced with the parenthesised form across the yadgar codebase.

Test structure
--------------
T1 (parametrised) — per-site: each known file must contain the parenthesised
    form ``except (<X>, <Y>):`` at/near the audited line.

T2 (project-wide scan) — zero occurrences of the bare-tuple form remain
    anywhere under yadgar/ (*.py).

T3 (behavioural) — embed_service.py:434 ``except asyncio.CancelledError,
    Exception:`` was the worst-case site: broad ``Exception`` was uncaught.
    Verified via static source inspection (async machinery coupling makes
    live async test overkill; static check is sufficient).

Runner note: all checks are pure static (read source, no import of
    yadgar.server or yadgar.backend.embed_service) — avoids the mcp-module
    ImportError that affects several integration tests.
"""

from __future__ import annotations

import re

import pytest

from yadgar.tests._paths import REPO_ROOT

YADGAR_ROOT = REPO_ROOT / "yadgar"

# Regex: parenthesised form — the FIXED form we require
_PAREN_FORM = re.compile(r"except\s+\(\s*[A-Za-z_.]+(\s*,\s*[A-Za-z_.]+)+\s*\)\s*:")

# Regex: bare-tuple old syntax — must be ABSENT everywhere after fix.
# Anchored with (?:$|#|\s*#) to avoid matching docstring examples like
# ``except X, Y:`` (where the colon is followed by a backtick, not end-of-line).
_BARE_FORM = re.compile(
    r"except\s+[A-Za-z_][A-Za-z_.]*\s*,\s*[A-Za-z_][A-Za-z_.]*\s*:(?:\s*(?:#.*)?\s*$)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Sites to fix — (relative path from REPO_ROOT, approximate line, description)
# ---------------------------------------------------------------------------
# 12 sites total (audit found 11; two additional discovered: hooks/db-lockdown-check.py
# and tests/test_loop_heartbeats.py; scope expanded to cover all).

SITES = [
    # T2 Car D packaged these files under their family dirs (ADR-0084 no-lone-files).
    # Re-pointed to the CANONICAL packaged path — the old flat paths are now either
    # absent (daemon/embed_service/conflict_resolver) or PEP-562 shims with no
    # except lines (config_registry/log_config), so the audit must read the target.
    (
        # Car C3 split: container-runtime detection (_get_runtime) moved
        # daemon.py → runtime.py.
        "yadgar/core/daemon/runtime.py",
        "FileNotFoundError, subprocess.TimeoutExpired",
        "runtime.py container-runtime detection",
    ),
    (
        "yadgar/_shared/config/config_registry.py",
        "ValueError, TypeError",
        "config_registry.py prometheus metrics setter",
    ),
    (
        "yadgar/backend/embed_service/embed_service.py",
        "asyncio.CancelledError, Exception",
        "embed_service.py shutdown handler (critical — Exception was escaping)",
    ),
    (
        "yadgar/backend/conflict_resolver/conflict_resolver.py",
        "ValueError, TypeError",
        "conflict_resolver.py similar-result id parse",
    ),
    (
        "yadgar/_shared/observability/log_config.py",
        "PermissionError, OSError",
        "log_config.py fallback log-dir creation",
    ),
    # v5.95.0: the ml_client idle-eviction env-parse (formerly a site here) was
    # DRY'd into the shared resolve_knob() helper in config.py. resolve_knob catches
    # via `except _KNOB_PARSE_ERRORS:` — the specific-catch convention preserved in a
    # py3.14-ruff-safe named-constant form (an inline `except (ValueError, TypeError):`
    # gets rewritten to the PEP-758 bare form the AST hooks reject). The literal-paren
    # regex below can't match a Name, so this site is retired, not repointed.
    (
        "yadgar/core/server/http.py",
        "TypeError, ValueError",
        "server/http.py viz_search node-id parse",
    ),
    (
        "yadgar/core/server/http_bookmarks.py",
        "TypeError, ValueError",
        "server/http_bookmarks.py position int parse",
    ),
    (
        "yadgar/core/server/http_bookmarks.py",
        "ValueError, TypeError",
        "server/http_bookmarks.py wiki-search limit int parse",
    ),
    (
        # Car 0 (multi-client hooks) moved the hook handler bodies out of
        # scripts/hook_runner.py into the shared cli/hook.py; the runner is now a
        # thin re-export shim. The parenthesised-except site travelled with the code.
        "yadgar/core/cli/hook.py",
        "json.JSONDecodeError, ValueError",
        "cli/hook.py hook_db_lockdown_check JSON parse",
    ),
    (
        "yadgar/tests/core/test_loop_heartbeats.py",
        "StopAsyncIteration, TimeoutError",
        "tests/test_loop_heartbeats.py SSE generator advance",
    ),
]


# ---------------------------------------------------------------------------
# T1 — per-site parenthesised form present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path,exception_types,description",
    [(s[0], s[1], s[2]) for s in SITES],
    ids=[s[2] for s in SITES],
)
def test_site_uses_parenthesised_except(
    rel_path: str, exception_types: str, description: str
) -> None:
    """Each audited file must contain the parenthesised multi-catch form.

    We search the whole file for ``except (<types>):`` containing the
    exception types from the audit.  This is robust to line-number drift.
    """
    path = REPO_ROOT / rel_path
    assert path.exists(), f"Source file missing: {path}"

    content = path.read_text(encoding="utf-8")

    # Build a pattern matching ``except (<X>, <Y>):`` for the specific types
    # (order-insensitive search via simple string check in paren-form matches)
    bare_match = _BARE_FORM.search(content)

    # Check the parenthesised form exists for these specific types
    # We look for: except (A, B): or except (B, A): — types may be in either order
    types = [t.strip() for t in exception_types.split(",")]
    _PAREN_FORM.findall(content)

    # Build a broader search: find lines with ``except (...)`` containing both types
    paren_line_re = re.compile(r"except\s*\([^)]*\)\s*:")
    paren_lines = paren_line_re.findall(content)

    # At least one paren-form line must contain all the expected exception types
    found = any(all(t in line for t in types) for line in paren_lines)

    assert found, (
        f"{description}\n"
        f"  File: {rel_path}\n"
        f"  Expected parenthesised form containing: {exception_types}\n"
        f"  Parenthesised except lines found: {paren_lines}\n"
        f"  (Bare-tuple form still present: {bool(bare_match)})"
    )


# ---------------------------------------------------------------------------
# T2 — project-wide: zero bare-tuple forms remain
# ---------------------------------------------------------------------------


def test_no_bare_except_tuple_anywhere() -> None:
    """After sweep: zero ``except X, Y:`` occurrences remain in yadgar/*.py."""
    violations: list[str] = []
    for py_file in YADGAR_ROOT.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            # Skip comment lines
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _BARE_FORM.search(line):
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert violations == [], (
        f"Found {len(violations)} bare except-tuple occurrence(s) — must be zero:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# T3 — embed_service.py critical site: Exception now caught
# ---------------------------------------------------------------------------


def test_embed_service_exception_is_caught_in_shutdown_handler() -> None:
    """embed_service.py shutdown handler must catch both CancelledError AND Exception.

    Pre-fix: ``except asyncio.CancelledError, Exception:`` catches only
    CancelledError — ``Exception`` was a local name binding shadowing the builtin.
    Post-fix: ``except (asyncio.CancelledError, Exception):`` catches both.

    Verified statically: the parenthesised form must appear in embed_service.py
    and must include both ``asyncio.CancelledError`` and ``Exception``.
    """
    # T2 Car D: embed_service packaged under backend/embed_service/ (ADR-0084).
    path = YADGAR_ROOT / "backend" / "embed_service" / "embed_service.py"
    content = path.read_text(encoding="utf-8")

    # Find all parenthesised except clauses
    paren_re = re.compile(r"except\s*\(([^)]+)\)\s*:")
    matches = paren_re.findall(content)

    found = any("asyncio.CancelledError" in m and "Exception" in m for m in matches)

    assert found, (
        "embed_service.py shutdown handler does not use parenthesised form "
        "catching both asyncio.CancelledError and Exception.\n"
        f"Parenthesised except contents found: {matches}"
    )
