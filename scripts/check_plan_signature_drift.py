#!/usr/bin/env python3
"""Cross-document signature-drift lint for `docs/plans/**` (Car 0047 spine train).

WHY THIS EXISTS (the incident, 2026-08-09)
------------------------------------------------------------------------------
The 0047 spine train is 16 per-car plan docs. Car A defines a function
signature. Car D writes a prohibition about it. Car E violated BOTH:

  Car A (`0047-car-A-ledger-tables.md`) defines
      async def create_task_row(self, *, project_id, title, status="pending",
                                state=None, active_form=None, plan_path=None,
                                body_slug=None) -> dict
  Car D (`0047-car-D-task-tools.md`) says, in prose:
      "No `origin` parameter. §14.1 dropped `origin` as a column ...
       do NOT carry it forward"
  Car E (`0047-car-E-task-seed-session-hooks.md`) nonetheless wrote
      create_task_row(project_id=, origin="yadgar", title=subject,
                      active_form=, state=, plan_path=, body_slug=, directory=)

`origin` and `directory` exist in NEITHER the signature nor the prohibition's
allowance. A human found this by reading one file. Two independent LLM audits
did not — and structurally COULD NOT, because the signature, the prohibition,
and the violation live in three different documents. Single-document review
cannot catch this class; it needs a mechanical cross-reference. That is this
guard.

WHAT THIS GUARD DOES
------------------------------------------------------------------------------
1. Builds a signature map from every fenced ```python / ```py block under
   `docs/plans/**` (archive/ EXCLUDED, see below): for each `def NAME(...)` /
   `async def NAME(...)`, the set of accepted parameter names.
2. Scans the same corpus for CALL SITES of those same names — anywhere in the
   text, fenced or inline-backticked — and extracts the kwarg names passed.
3. FAILS on any kwarg absent from that name's signature, reporting `file:line`,
   the offending kwarg(s), and where the canonical signature lives.

DELIBERATE SCOPE CHOICES (each one is a false-positive control)
------------------------------------------------------------------------------
Plan prose is full of illustrative and abbreviated calls. A gate that cries
wolf gets disabled, which is strictly worse than no gate — so every ambiguous
case resolves toward UNDER-matching:

* `archive/**` is EXCLUDED. Archived plans are history: they legitimately
  reference signatures that have since been retired, and rewriting shipped
  history to satisfy a lint would be the wrong repair. Drift only matters in
  plans that are still being built against.

* ONLY names DEFINED WITHIN THE CORPUS are checked. Plans reference real MCP
  tools, stdlib, and third-party APIs freely; a call to an undefined name is
  not evidence of anything. No resolution against the real codebase — that
  would be a different guard with a different (much noisier) failure mode.

* A signature containing `**kwargs` makes the function UNCHECKABLE — it accepts
  anything by construction — so the name is dropped from the map entirely
  (e.g. `update_task_row(self, task_id, **fields)`).

* Multiple definitions of one name take the UNION of their parameters. Several
  names are legitimately restated in abbreviated form by a consuming car
  (`create_adr_row` in A+F, `list_adr_rows` in A+B+F). Union is the lenient
  reading; flagging the disagreement would fail on correct docs.

* `self` / `cls` are stripped — the signatures are methods.

* Calls whose ENTIRE argument list is empty, `...`, or `…` are skipped:
  `create_task_row()`, `list_task_rows(...)` are illustrative prose, not drift.
  Note this is deliberately narrow — a call is skipped only when the WHOLE arg
  list is a placeholder. A call mixing real and valueless kwargs
  (`f(project_id=, origin="x")`) is EXACTLY the defect shape above and must
  stay checked. Kwarg NAMES are validated even when values are elided, which
  is why `task_list(directory=...)` passes on the merit of `directory` being in
  car D's signature rather than by being skipped.

* Kwargs are extracted at PAREN DEPTH 1 only, with string literals blanked
  first, so `foo(bar=baz(qux=1))` does not attribute `qux` to `foo` and
  `foo(bar="x=y")` does not invent a kwarg `x`.

* Spans preceded by `def ` / `async def ` are not call sites. Without this the
  corpus's own signature blocks would each self-report as a violating call.

* SIGNATURE DECLARATIONS IN PROSE are treated as definitions, not calls. A span
  carrying a `->` return arrow or a depth-1 parameter annotation is a
  declaration: car H's `adr_add(..., tier: str | None = None, subsystem: str |
  None = None) -> dict` is the doc that EXTENDS the signature, so reading it as
  a violating call inverts the truth. Its parameters are folded into the map
  (union) BEFORE call scanning, so car H's own later build-step call
  `adr_add(..., tier="binding", subsystem="storage")` is correctly clean.

* A LEADING `..., ` / `…, ` means "the existing arguments, elided". The author
  is not enumerating the full call, so the span asserts nothing about it and is
  skipped (but, unlike a declaration, contributes no parameters).

* MINIMUM KWARG COUNT (2). A span passing a single kwarg is, in plan prose,
  overwhelmingly a way of NAMING a parameter rather than enumerating a call:
  "if `adr_list(subsystem=...)` becomes hot", "v7 features:
  `recall(synthesize=True)`", ADR-0124's quoted `wiki_add(branch_hint=master)`.
  All three are legitimate prose; none asserts a full argument list. Measured
  on the corpus at introduction: this single rule removed 4 of 9 raw hits, all
  4 confirmed false positives by hand. The cost is stated in THE CEILING.

THE CEILING — read this before trusting a green run
------------------------------------------------------------------------------
This is CROSS-DOCUMENT KWARG-NAME liveness, and nothing more:

* It does not check argument TYPES, ORDER, or positional arity — only that
  every kwarg name passed is a name the signature accepts.
* It cannot see a MISSING required argument. A call omitting `title=` is
  invisible here.
* A drifting call that passes only ONE kwarg is invisible (the minimum-kwarg
  rule above). `create_task_row(origin="yadgar")` alone would pass. This is the
  deliberate price of not firing on the four prose forms named above.
* A doc that declares a WRONG signature (annotated, with `->`) is believed
  rather than challenged — its parameters are absorbed. Deciding which of two
  disagreeing declarations is canonical is a different and much harder problem;
  this guard does not attempt it.
* It cannot check calls to functions defined only in real code, only in
  `archive/`, or nowhere at all.
* It cannot read prose. Car D's `origin` prohibition is enforced here only
  incidentally — because car A's signature happens to omit `origin`. A
  prohibition on a parameter that IS in the signature would not be caught.
* Markdown is matched by regex, not parsed. A pathological fence nesting can
  hide a block; that is the intended trade (prefer under- to over-matching).

No allowlist. Every call this guard checks is meant to resolve against a
signature in the same corpus; if a new shape needs an exception, narrow the
checked-call shape above with a documented reason rather than bolting on an
allowlist file.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_PLANS_REL = "docs/plans"
_ARCHIVE_DIR = "archive"

# Fenced ```python / ```py blocks only. Signature definitions live nowhere else,
# and restricting the def-scan here keeps prose and non-python fences out.
_PY_FENCE_RE = re.compile(r"^```(?:python|py)[^\n]*\n(.*?)^```", re.S | re.M)

# `def NAME(` / `async def NAME(` — the paren is matched separately (balanced).
_DEF_RE = re.compile(r"(?:^|\n)\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")

# A kwarg name at the point of use. `(?!=)` rejects `==`; other comparison
# operators (`>=`, `!=`) cannot match because the char before `=` is not `\w`.
_KWARG_RE = re.compile(r"\b([a-z_]\w*)\s*=(?!=)")

# Argument lists that are pure placeholders — illustrative prose, never drift.
_PLACEHOLDER_ARGS = {"", "...", "…"}

# A leading `..., ` / `…, ` means "the existing arguments, elided" — the author
# is not enumerating the full call, so the span makes no claim about it.
_ELIDED_HEAD_RE = re.compile(r"^\s*(?:\.\.\.|…)\s*,")

# A parameter annotation at depth 1 (`tier: str | None = None`) marks a
# SIGNATURE DECLARATION rather than a call.
_ANNOTATION_RE = re.compile(r"\b[a-z_]\w*\s*:")

# A `-> ret` immediately after the closing paren likewise marks a declaration.
_ARROW_RE = re.compile(r"^\s*->")

# Below this many kwargs a span is a prose mention of a parameter, not an
# enumeration of a call's arguments. See MINIMUM KWARG COUNT in the docstring.
_MIN_KWARGS = 2

# A call span longer than this is assumed to be a mis-balanced match (e.g. an
# unterminated quote swallowing the rest of the document) and is skipped.
_MAX_SPAN = 4000


def _match_paren(text: str, open_idx: int) -> int | None:
    """Index of the `)` matching the `(` at open_idx, or None if unbalanced.

    String-aware, so parens inside literals do not shift the depth. Returns
    None past _MAX_SPAN — an unterminated quote must degrade to a skip
    (under-match), never to a runaway span.
    """
    depth = 0
    quote: str | None = None
    i = open_idx
    end = min(len(text), open_idx + _MAX_SPAN)
    while i < end:
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _depth1_text(span: str) -> str:
    """`span` with nested bracket groups and string literals blanked out.

    Everything at depth 1 of the original call survives verbatim; anything
    deeper (or inside a literal) becomes spaces. This is what makes
    `foo(bar=baz(qux=1))` yield only `bar`, and `foo(bar="x=y")` not invent a
    kwarg `x`.
    """
    kept: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(span):
        c = span[i]
        keep = " "
        if quote is not None:
            if c == "\\":
                kept.append("  ")
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0:
            keep = c
        kept.append(keep)
        i += 1
    return "".join(kept)


def _depth1_kwargs(span: str) -> set[str]:
    """Kwarg names passed at paren depth 1 of a call."""
    return set(_KWARG_RE.findall(_depth1_text(span)))


def _params_from_signature(name: str, params_src: str) -> set[str] | None:
    """Accepted parameter names for `def name(params_src)`.

    Returns None when the signature accepts arbitrary kwargs (`**kwargs`) —
    such a function is unconstrained and must be dropped from the map.
    Raises SyntaxError upward as None-by-caller: an unparseable signature is
    skipped rather than guessed at.

    The corpus's signature blocks are BODILESS (`def f(x) -> dict` with no
    colon), which is a SyntaxError for a bare ast.parse — hence the synthesis.
    """
    try:
        tree = ast.parse(f"def {name}({params_src}): pass")
    except SyntaxError:
        return None
    fn = tree.body[0]
    if not isinstance(fn, ast.FunctionDef):
        return None
    a = fn.args
    if a.kwarg is not None:
        return None
    names = {arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    return names - {"self", "cls"}


def collect_signatures(
    files: list[Path], root: Path
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build {name: accepted params} and {name: defining files} from py fences.

    Multiple definitions of one name take the UNION of their parameters — a
    consuming car legitimately restating an abbreviated signature must not
    fail the fuller call. A name whose signature is unconstrained or
    unparseable ANYWHERE is dropped from the map entirely (under-match).
    """
    params: dict[str, set[str]] = {}
    sources: dict[str, set[str]] = {}
    unconstrained: set[str] = set()

    for path in files:
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        for fence in _PY_FENCE_RE.finditer(text):
            block = fence.group(1)
            for m in _DEF_RE.finditer(block):
                name = m.group(1)
                open_idx = block.index("(", m.end() - 1)
                close_idx = _match_paren(block, open_idx)
                if close_idx is None:
                    unconstrained.add(name)
                    continue
                accepted = _params_from_signature(name, block[open_idx + 1 : close_idx])
                if accepted is None:
                    unconstrained.add(name)
                    continue
                params.setdefault(name, set()).update(accepted)
                sources.setdefault(name, set()).add(rel)

    for name in unconstrained:
        params.pop(name, None)
        sources.pop(name, None)
    return params, sources


def _is_declaration(span: str, after: str) -> bool:
    """True when a `name(...)` span DECLARES a signature rather than calling it.

    Two markers, either sufficient:
      * a `->` return arrow immediately after the closing paren, and/or
      * a parameter annotation at depth 1 (`tier: str | None = None`).

    Car H's `adr_add(..., tier: str | None = None, subsystem: str | None = None)
    -> dict` is the motivating case: it is the doc that EXTENDS the signature,
    so treating it as a violating call inverts the truth.
    """
    return bool(_ARROW_RE.match(after) or _ANNOTATION_RE.search(_depth1_text(span)))


def _iter_call_spans(text: str, call_re: re.Pattern[str]):
    """Yield (name, span, line, after) for each `name(...)` occurrence."""
    for m in call_re.finditer(text):
        # A definition is not a call site. Without this the corpus's own
        # signature blocks would each self-report as a violating call.
        if re.search(r"(?:async\s+)?def\s+$", text[max(0, m.start() - 16) : m.start()]):
            continue
        open_idx = m.end() - 1
        close_idx = _match_paren(text, open_idx)
        if close_idx is None:
            continue
        line = text.count("\n", 0, m.start()) + 1
        yield m.group(1), text[open_idx + 1 : close_idx], line, text[close_idx + 1 : close_idx + 8]


def absorb_declarations(
    files: list[Path], root: Path, params: dict[str, set[str]], sources: dict[str, set[str]]
) -> None:
    """Fold prose signature DECLARATIONS into the signature map, in place.

    A later car that declares an extension (`adr_list(directory, status=None,
    tier="binding", limit=50, offset=0) -> dict`) is the new authority for that
    name; calls elsewhere using the extended parameters are correct, not drift.
    """
    if not params:
        return
    call_re = re.compile(r"\b(" + "|".join(sorted(map(re.escape, params))) + r")\s*\(")
    for path in sorted(files):
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, span, _line, after in _iter_call_spans(text, call_re):
            if not _is_declaration(span, after):
                continue
            declared = _params_from_signature(name, _ELIDED_HEAD_RE.sub("", span))
            if not declared:
                continue
            params[name].update(declared)
            sources[name].add(rel)


def collect_violations(
    files: list[Path], root: Path, params: dict[str, set[str]], sources: dict[str, set[str]]
) -> list[str]:
    """Scan every file for calls to known names and report unknown kwargs."""
    if not params:
        return []
    call_re = re.compile(r"\b(" + "|".join(sorted(map(re.escape, params))) + r")\s*\(")

    violations: list[str] = []
    for path in sorted(files):
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, span, line, after in _iter_call_spans(text, call_re):
            if span.strip() in _PLACEHOLDER_ARGS:
                continue
            if _ELIDED_HEAD_RE.match(span) or _is_declaration(span, after):
                continue
            passed = _depth1_kwargs(span)
            if len(passed) < _MIN_KWARGS:
                continue
            unknown = passed - params[name]
            if not unknown:
                continue
            defined_in = ", ".join(sorted(sources[name]))
            violations.append(
                f"{rel}:{line}: `{name}(...)` passes unknown kwarg(s) "
                f"{', '.join('`' + k + '`' for k in sorted(unknown))} — "
                f"canonical signature in {defined_in} accepts: "
                f"{', '.join(sorted(params[name])) or '(none)'}"
            )
    return violations


def check(root: Path) -> list[str]:
    """Return a list of human-readable violation strings (empty == clean)."""
    plans = root / _PLANS_REL
    if not plans.is_dir():
        return []
    files = sorted(p for p in plans.rglob("*.md") if _ARCHIVE_DIR not in p.relative_to(plans).parts)
    params, sources = collect_signatures(files, root)
    absorb_declarations(files, root, params, sources)
    return collect_violations(files, root, params, sources)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent

    violations = check(root)
    if violations:
        print("Plan signature-drift lint FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            f"\n{len(violations)} cross-document signature violation(s) in "
            f"{_PLANS_REL}/. Fix the call site, or update the canonical "
            "signature if the parameter is genuinely being added.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Plan signature-drift lint OK — every checked call in {_PLANS_REL}/ matches its signature."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
