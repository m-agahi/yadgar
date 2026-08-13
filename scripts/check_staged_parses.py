"""Every Python file being committed must PARSE. No exceptions, no silent skips.

WHY THIS EXISTS
---------------
On 2026-08-12 a pre-commit run reformatted a file, ``git add -A`` re-staged the
rewrite, and the commit landed carrying a file that did not parse (``93f3ed95``,
reverted in ``8f826592``). Nothing at commit time looked at the result of the
formatter's own rewrite; CI found it two hours later.

Nearly every AST-based gate in ``scripts/`` reads sources behind a
``except SyntaxError: continue``-shaped arm, because a lint that dies on one bad
file cannot report on the other four hundred. That tolerance is correct *there*
and is why the broken file was skipped by all of them at once and scored clean by
every one. This hook is the counterpart whose ONLY job is the thing they forgive.

``check_directory_residue.find_unparseable`` is the nearest existing relative and
is deliberately NOT duplicated: it walks six fixed scan roots on ``always_run``,
so it never sees ``yadgar/tests/**``, ``hooks/**`` or anything outside them. This
hook is keyed to the STAGED SET instead — whatever is actually being committed,
wherever it lives.

THE INTERPRETER THIS RUNS UNDER — read before "fixing" the version handling
--------------------------------------------------------------------------
``language: system`` hooks do NOT run the project venv's Python. During a real
``git commit`` they run the interpreter baked into ``.git/hooks/pre-commit`` as
``INSTALL_PYTHON``, which on this machine is **3.13**, while ``pyproject.toml``
declares ``requires-python = ">=3.14"``. Both facts are current and measured, not
inferred: a bare PEP 758 ``except A, B:`` staged into a commit is rejected at
commit time with ``SyntaxError: multiple exception types must be parenthesized``,
and that is exactly why the repo's ``# fmt: skip`` except-tuple pins and
``test_v5_46_16_except_tuple_sweep.py`` are load-bearing rather than obsolete.

**This hook therefore does NOT validate against ``requires-python``.** An earlier
draft did, and it hard-failed every commit — the floor said 3.14, the hook ran
3.13. More importantly the comparison was backwards: validating under a grammar
LOOSER than the commit-time one is the dangerous direction, because it accepts
source that every other ``language: system`` gate will then reject. Running under
the same (stricter) interpreter as its sibling gates is the correct behaviour,
not a defect to be corrected here.

KNOWN REPO DEFECT, deliberately not fixed by this hook: ``pre-commit run
--all-files`` (ambient PATH, 3.14 here) and ``git commit`` (pinned 3.13) do not
check the same language, so a file can pass the former and fail the latter. That
split is worth its own change; this hook's contribution is to make the
interpreter it used VISIBLE on every run so the divergence stops being silent.

ANTI-VACUITY — the ways this could report OK while checking nothing
------------------------------------------------------------------
This repo has produced a documented run of guards that passed while asserting
nothing, so each is closed explicitly rather than by hope:

1. **No files.** pre-commit does not invoke a ``pass_filenames: true`` hook at
   all when nothing matches (it prints "no files to check ... Skipped"), so an
   empty argv in normal operation is impossible. It therefore means the hook was
   MIS-WIRED — ``pass_filenames`` flipped to false, or the ``types`` filter
   narrowed to nothing — which would make this hook pass forever while reading
   no source at all. Empty argv is an ERROR.

2. **Unreadable file.** A file that cannot be opened or decoded is NOT a file
   with nothing wrong; it is a file this check could not perform. Reported as a
   violation, never skipped. ``except (OSError, UnicodeDecodeError): continue``
   is precisely the anti-pattern this hook exists to refuse.

3. **Invisible interpreter.** The grammar a parse check used is part of its
   result, so it is printed on EVERY run, pass or fail. A sanity floor rejects an
   interpreter old enough that the check would be measuring a different language
   entirely.

Usage:
    python scripts/check_staged_parses.py FILE [FILE ...]
    # pre-commit passes the staged files; it stashes unstaged changes first,
    # so the on-disk content this reads IS the staged content.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: Below this, the interpreter is old enough that "does it parse" is answering a
#: question about a different language. Deliberately NOT `requires-python` — see
#: the module docstring; the hook legitimately runs under an interpreter older
#: than the project's own floor.
_SANITY_FLOOR = (3, 9)


def running_interpreter() -> tuple[int, int]:
    return (sys.version_info.major, sys.version_info.minor)


def check_interpreter(running: tuple[int, int] | None = None) -> list[str]:
    """Reject an interpreter too old for the verdict to mean anything."""
    running = running_interpreter() if running is None else running
    if running >= _SANITY_FLOOR:
        return []
    return [
        f"INTERPRETER TOO OLD: this hook is running Python "
        f"{running[0]}.{running[1]}, below the {_SANITY_FLOOR[0]}.{_SANITY_FLOOR[1]} "
        "sanity floor. A parse check under a grammar this old reports on a "
        "different language than the one being committed."
    ]


def check_file(path: Path) -> list[str]:
    """Parse one file. Unreadable and unparseable are BOTH violations."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            f"UNREADABLE: {path}: {exc}. This check could not be performed on "
            "this file, which is not the same as the file being fine."
        ]
    except UnicodeDecodeError as exc:
        return [
            f"UNDECODABLE: {path}: not valid UTF-8 ({exc.reason} at byte "
            f"{exc.start}). A source file this check cannot decode is a "
            "violation, not a skip."
        ]

    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno if exc.lineno is not None else "?"
        col = f":{exc.offset}" if exc.offset is not None else ""
        detail = (exc.text or "").rstrip()
        rendered = f"\n      {detail}" if detail else ""
        return [f"SYNTAX ERROR: {path}:{line}{col}: {exc.msg}{rendered}"]
    except ValueError as exc:  # null bytes, absurd nesting depth
        return [f"UNPARSEABLE: {path}: {exc}"]
    return []


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    running = running_interpreter()
    version = f"{running[0]}.{running[1]}"

    errors = check_interpreter(running)
    if errors:
        for err in errors:
            print(f"check-staged-parses: {err}", file=sys.stderr)
        return 1

    if not args:
        print(
            "check-staged-parses: ERROR: invoked with no files. pre-commit skips "
            "a `pass_filenames: true` hook entirely when nothing matches, so an "
            "empty argv means this hook is mis-wired and would pass forever "
            "without reading any source. Check `pass_filenames` / `types` in "
            ".pre-commit-config.yaml.",
            file=sys.stderr,
        )
        return 1

    for name in args:
        errors.extend(check_file(Path(name)))

    if errors:
        print(
            f"check-staged-parses: {len(errors)} file(s) being committed do not "
            f"parse under Python {version} (the interpreter pre-commit's "
            "`language: system` hooks run — NOT the project venv):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        print(
            "\nA commit carrying an unparseable file is how 93f3ed95 shipped. "
            "Fix the source — do NOT skip this hook.",
            file=sys.stderr,
        )
        return 1

    print(f"check-staged-parses: {len(args)} file(s) parse cleanly under Python {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
