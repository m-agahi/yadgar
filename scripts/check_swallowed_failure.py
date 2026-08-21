#!/usr/bin/env python3
"""ADR-0420 — swallowed-failure lint: failure rendered as well-formed success.

AST-walks in-scope Python and flags two shapes from the ADR-0420 class, where an
operation fails and the caller receives a success-shaped or empty-shaped value
with the reason unlogged or logged below production level.

  R3  non-2xx status paired with a success-shaped body.
      A ``return`` of a response constructor (``JSONResponse``/``Response``/…)
      carrying ``status_code=<const >= 400>`` whose body is an empty literal
      (``[]``/``{}``/``()``) or a dict asserting ``ok``/``stored``/``committed``
      as a literal ``True``. A caller cannot tell HTTP 500 + ``[]`` apart from
      "no matches" without reading the status, and every frontend that parses
      the body first gets a clean empty result.

  R1  swallow-and-return-empty.
      An ``except`` handler that catches BROADLY (bare / ``Exception`` /
      ``BaseException``), never re-raises, returns an empty container or a
      success-shaped dict, and logs at ``debug``/``info`` or not at all.

R1 is BASELINE-RATCHETED, R3 is enforced at zero. See ``.swallow-baseline.json``.

────────────────────────────────────────────────────────────────────────────────
HONEST COVERAGE LIMIT (ADR-0420 requires this be stated, not implied — claiming
fuller coverage than the detector has would itself be an instance of the class):

PROVENANCE OF THE NUMBERS BELOW. Only two figures were measured by THIS script
in the car that shipped it: ``R3 = 0`` and ``R1 = 30`` (run it and see). Every
other count is quoted from the read-only ADR-0420 audit of 2026-08-20 and was
NOT re-verified here — each is marked ``[audit, not re-verified]``. That audit's
R3 count was independently shown WRONG by this implementation (see the ADR
correction), so treat its other figures as indicative, not authoritative.
Restating a number you did not take, in the detector whose whole subject is
measurement honesty, is the class.

  * RETRO-COVERAGE IS 1 OF 14, NOT "roughly half" ``[audit, not re-verified]``.
    ADR-0420's Consequences section claims the detector "catches roughly HALF
    the class". The audit checked that against the ADR's own 14 named instances
    (278, 280, 281, 282, 13, 94, 283, 173, 303, 176, 80, 294, 285, 271) and
    found it retro-catches exactly ONE — 285 — plus a now-fixed half of 13.
    303 was a conditional drop path, not an except handler; 80 and 294 were
    failures rendered AS failures, with the wrong type. The ADR has been amended
    with this correction. THE DETECTOR'S VALUE IS FORWARD-LOOKING: it stops the
    shape from being re-introduced. It is not a retro-audit of the class.

  * The ``None`` / bare-``return`` / ``""`` / ``0`` return shapes are EXCLUDED —
    the recall traded away to buy precision. The audit put that at 197 of the
    250 non-test hits of the naive rule ``[audit, not re-verified]``. Three live
    examples sit in ``yadgar/_shared/wiki/store.py``: ``_collect_wiki_fts_scores``,
    ``_collect_wiki_vector_scores`` and ``_collect_wiki_vector_scores_tagged``
    each do ``except Exception: logger.debug(...)`` with NO return at all, and
    fall through implicitly. Those three are arguably worse than what R1 does
    catch — they are why the real exception behind ledger task 285 sat three
    frames away from the handler that reported it.

  * NON-LITERAL fallbacks are invisible. ``except: return _fallback()`` and
    ``except: return self._empty`` do not match: the rule reads literals only,
    because resolving a call's return shape needs type inference this does not do.

  * ``contextlib.suppress`` is a BYPASS. A ``with suppress(Exception):`` block
    has no ``ExceptHandler`` node, so R1 cannot see it. The audit found one
    non-test use ``[audit, not re-verified]``, but it is a real hole: rewriting
    a handler into ``suppress`` would silently exit the rule.

  * THE LOG-LEVEL PROBE IS NAME-BASED AND OVER-EXEMPTS. ``_handler_log_level``
    matches ANY attribute call whose name is in ``_LOG_LEVELS``, so
    ``parser.error(...)`` (argparse) and ``warnings.warn(...)`` both read as
    >= WARNING and quietly exempt their handler. Resolving the receiver to a
    real ``logging.Logger`` needs type inference this does not do. The 30
    baselined sites are therefore plausibly an UNDERCOUNT, in the direction
    that weakens the ratchet.

  * R2 (literal ``ok: True`` beside an unread failure counter) and R4 (``cmd_*``
    returning 0 after building a counts structure it never branches on) from
    ADR-0420's decision list are DELIBERATELY NOT BUILT. The audit measured R2
    at 8 sites of which 5 are test fixtures — and missing its OWN motivating
    example (``project_seed``) — and R4 at 1 ``[audit, not re-verified]``.
    Neither earns a ratchet.
────────────────────────────────────────────────────────────────────────────────

Baseline governance (modeled on ``.observe-allowlist.json``, deliberately NOT on
``.complexity-allowlist`` HARD entries — those are unconditional passes, which is
itself an ADR-0420 instance the ADR cites). Integrity is ALWAYS hard, including
under ``--warn``:

  * every entry must RE-RESOLVE to a live AST site each run — a stale entry
    exits 1;
  * the recorded ``shape`` and ``log_level`` are RE-COMPARED every run, not
    merely stored. A site whose logging improves makes the baseline TIGHTEN
    (the run fails until the file is regenerated) and it can never loosen;
  * ``category`` must be in the enum and ``rationale`` >= 40 chars.

SCANS THE WORKING TREE, NOT THE GIT INDEX. Under pre-commit that is the same
thing — pre-commit stashes unstaged changes, so the on-disk content during the
hook IS the staged content. A MANUAL run on a dirty tree is therefore reporting
on what you have, not on what you are about to commit; a green manual run is not
evidence the commit will pass. (``scripts/check_observe_coverage.py`` solves this
by reading the git index; that is the closest model for this ratchet's governance
and worth reading. It is not adopted here because this hook is ``always_run`` on
a full scan, where the two agree.)

Usage::

    python scripts/check_swallowed_failure.py
    python scripts/check_swallowed_failure.py --warn
    python scripts/check_swallowed_failure.py --list-all
    python scripts/check_swallowed_failure.py --write-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASELINE = _REPO_ROOT / ".swallow-baseline.json"

# ── rule vocabulary ──────────────────────────────────────────────────────────

# Response constructors whose first positional (or `content=`) is the body.
_RESPONSE_CTORS = frozenset(
    {
        "JSONResponse",
        "Response",
        "PlainTextResponse",
        "HTMLResponse",
        "ORJSONResponse",
        "UJSONResponse",
    }
)

# Dict keys that assert success. A literal True under any of these is the
# "success-shaped body" half of R3 and the "success-shaped literal" half of R1.
_SUCCESS_KEYS = frozenset({"ok", "stored", "committed"})

# Broad catches. A narrow `except ValueError:` states which failure it expects
# and is not the class; a broad one absorbs everything including the failure
# nobody predicted, which is exactly how task 285 hid an UnresolvedProjectError.
_BROAD_EXC_NAMES = frozenset({"Exception", "BaseException"})

# Logger methods, ranked. R1 fires only BELOW warning.
_LOG_LEVELS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "exception": 40,
    "critical": 50,
}
_PRODUCTION_LEVEL = 30  # WARNING — ADR-0420's stated floor for a swallow.
_LEVEL_NONE = "none"

_ALLOWED_CATEGORIES = frozenset(
    {
        "pre-existing",  # frozen day-one debt; NOT a vetted exemption
        "best-effort",  # the operation is genuinely advisory
        "hot-loop",  # logging here would flood
        "shutdown",  # teardown path, nothing left to tell
        "third-party",  # the failure belongs to a library we only observe
    }
)

_MIN_RATIONALE_CHARS = 40


# ── findings ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Site:
    """One matched AST site, keyed so it survives line moves."""

    rule: str  # "R1" | "R3"
    relpath: str
    qualname: str
    ordinal: int  # index among same-rule sites in the same qualname
    lineno: int
    shape: str  # "list" | "dict" | "tuple" | "set" | "success-dict" | "status-<n>"
    log_level: str  # "none" | "debug" | "info" (R1); "n/a" (R3)

    @property
    def key(self) -> str:
        return f"{self.relpath}:{self.qualname}:{self.rule}#{self.ordinal}"


@dataclass
class ScanResult:
    sites: list[Site] = field(default_factory=list)
    # qualnames seen this run — lets a stale entry say WHY it went stale.
    qualnames: set[str] = field(default_factory=set)


# ── shape classification ─────────────────────────────────────────────────────


def _empty_literal_shape(node: ast.expr | None) -> str | None:
    """Return the shape name when *node* is an empty/success-shaped literal."""
    if isinstance(node, ast.List) and not node.elts:
        return "list"
    if isinstance(node, ast.Tuple) and not node.elts:
        return "tuple"
    if isinstance(node, ast.Set) and not node.elts:
        return "set"
    if isinstance(node, ast.Dict):
        if not node.keys:
            return "dict"
        for k, v in zip(node.keys, node.values, strict=False):
            if (
                isinstance(k, ast.Constant)
                and k.value in _SUCCESS_KEYS
                and isinstance(v, ast.Constant)
                and v.value is True
            ):
                return "success-dict"
    return None


def _call_name(node: ast.expr) -> str | None:
    func = node.func if isinstance(node, ast.Call) else node
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _response_body(call: ast.Call) -> ast.expr | None:
    """The body argument of a response constructor: first positional or `content=`."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "content":
            return kw.value
    return None


def _status_code(call: ast.Call) -> int | None:
    for kw in call.keywords:
        if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
            v = kw.value.value
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return None


# ── R3: non-2xx status paired with a success-shaped body ─────────────────────


def _r3_shape(ret: ast.Return) -> str | None:
    call = ret.value
    if not isinstance(call, ast.Call):
        return None
    if _call_name(call) not in _RESPONSE_CTORS:
        return None
    status = _status_code(call)
    if status is None or status < 400:
        return None
    body_shape = _empty_literal_shape(_response_body(call))
    if body_shape is None:
        return None
    return f"status-{status}/{body_shape}"


# ── R1: swallow-and-return-empty ─────────────────────────────────────────────


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare `except:`
        return True
    names: list[ast.expr] = (
        list(handler.type.elts) if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return any(isinstance(n, ast.Name) and n.id in _BROAD_EXC_NAMES for n in names)


def _handler_log_level(handler: ast.ExceptHandler) -> str:
    """Highest logger level called anywhere in the handler body, or "none"."""
    best = 0
    best_name = _LEVEL_NONE
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        level = _LOG_LEVELS.get(node.func.attr)
        if level is not None and level > best:
            best, best_name = level, node.func.attr
    return best_name


def _handler_has_raise(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _handler_returns(handler: ast.ExceptHandler) -> list[ast.Return]:
    """`return` statements OWNED by this handler.

    Excludes returns nested in a function defined inside the handler — those
    belong to that function's control flow, not to the swallow.
    """
    out: list[ast.Return] = []
    stack: list[ast.AST] = list(handler.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Return):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _r1_match(handler: ast.ExceptHandler) -> tuple[str, str, int] | None:
    """Return ``(shape, log_level, lineno)`` when *handler* is a swallow."""
    if not _is_broad_handler(handler):
        return None
    if _handler_has_raise(handler):
        return None
    level_name = _handler_log_level(handler)
    if _LOG_LEVELS.get(level_name, 0) >= _PRODUCTION_LEVEL:
        return None
    for ret in _handler_returns(handler):
        shape = _empty_literal_shape(ret.value)
        if shape is not None:
            return shape, level_name, ret.lineno
    return None


# ── scanning ─────────────────────────────────────────────────────────────────


def _iter_py_files(root: Path, repo_root: Path) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*.py")):
        rel = _rel(p, repo_root)
        if "/tests/" in rel or "/test/" in rel or p.name.startswith("test_"):
            continue
        files.append(p)
    return files


def scan_file(path: Path, repo_root: Path) -> ScanResult:
    res = ScanResult()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):  # fmt: skip
        # A file the checker cannot parse is reported, never silently skipped —
        # a swallow inside the swallow-detector would be the class itself.
        rel = _rel(path, repo_root)
        res.sites.append(
            Site("PARSE", rel, "<file>", 0, 0, "unparseable", _LEVEL_NONE),
        )
        return res

    rel = _rel(path, repo_root)
    counters: dict[tuple[str, str], int] = {}

    def _emit(rule: str, qualname: str, lineno: int, shape: str, level: str) -> None:
        n = counters.get((qualname, rule), 0)
        counters[(qualname, rule)] = n + 1
        res.sites.append(Site(rule, rel, qualname, n, lineno, shape, level))

    def _walk(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                child_qual = f"{qual}.{child.name}" if qual else child.name
                res.qualnames.add(child_qual)
                _scan_body(child, child_qual)
                _walk(child, child_qual)
            else:
                _walk(child, qual)

    def _scan_body(fn: ast.AST, qual: str) -> None:
        """R1 + R3 sites lexically inside *fn* but not inside a nested def."""
        stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
        found: list[tuple[str, int, str, str]] = []
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue  # owned by the nested def's own qualname
            if isinstance(node, ast.ExceptHandler):
                m = _r1_match(node)
                if m is not None:
                    shape, level, lineno = m
                    found.append(("R1", lineno, shape, level))
            if isinstance(node, ast.Return):
                shape3 = _r3_shape(node)
                if shape3 is not None:
                    found.append(("R3", node.lineno, shape3, "n/a"))
            stack.extend(ast.iter_child_nodes(node))
        for rule, lineno, shape, level in sorted(found, key=lambda t: (t[0], t[1])):
            _emit(rule, qual, lineno, shape, level)

    _walk(tree, "")
    return res


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def run(root: Path, repo_root: Path) -> ScanResult:
    merged = ScanResult()
    for path in _iter_py_files(root, repo_root):
        r = scan_file(path, repo_root)
        merged.sites.extend(r.sites)
        merged.qualnames.update(f"{_rel(path, repo_root)}:{q}" for q in r.qualnames)
    return merged


# ── baseline governance (always hard) ────────────────────────────────────────


def validate_entry(key: str, entry: object) -> list[str]:
    errs: list[str] = []
    if not isinstance(entry, dict):
        return [f"{key}: entry must be an object"]
    cat = entry.get("category")
    if cat not in _ALLOWED_CATEGORIES:
        errs.append(f"{key}: invalid category {cat!r} (allowed: {sorted(_ALLOWED_CATEGORIES)})")
    rationale = entry.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE_CHARS:
        errs.append(f"{key}: rationale must be >= {_MIN_RATIONALE_CHARS} chars")
    if not isinstance(entry.get("shape"), str):
        errs.append(f"{key}: missing 'shape' — the ratchet compares it every run")
    if not isinstance(entry.get("log_level"), str):
        errs.append(f"{key}: missing 'log_level' — the ratchet compares it every run")
    return errs


def check_baseline(sites: list[Site], baseline: dict, qualnames: set[str]) -> list[str]:
    """Re-resolve + re-compare every baseline entry. All failures are HARD."""
    errs: list[str] = []
    by_key = {s.key: s for s in sites}

    for key, entry in baseline.items():
        if key.startswith("_"):
            continue  # metadata (_comment, _header, …)
        errs.extend(validate_entry(key, entry))
        site = by_key.get(key)
        if site is None:
            relpath, _, rest = key.partition(":")
            qualname = rest.rsplit(":", 1)[0]
            if f"{relpath}:{qualname}" in qualnames:
                errs.append(
                    f"{key}: NO LONGER A VIOLATION — the function still exists but no "
                    f"longer matches the rule. THIS IS THE RATCHET TIGHTENING, not a bug: "
                    f"re-run `python scripts/check_swallowed_failure.py --write-baseline` "
                    f"to drop the entry. The baseline can never loosen."
                )
            else:
                errs.append(f"{key}: STALE baseline entry — no such function in scope")
            continue

        if not isinstance(entry, dict):
            continue
        recorded_shape = entry.get("shape")
        if isinstance(recorded_shape, str) and site.shape != recorded_shape:
            errs.append(
                f"{key}: SHAPE CHANGED {recorded_shape!r} -> {site.shape!r} "
                f"({site.relpath}:{site.lineno}). A baselined site may not change shape "
                f"in place — fix it, or re-run --write-baseline if the new shape is "
                f"genuinely no worse."
            )
        recorded_level = entry.get("log_level")
        if isinstance(recorded_level, str) and recorded_level != site.log_level:
            rec = _LOG_LEVELS.get(recorded_level, 0)
            obs = _LOG_LEVELS.get(site.log_level, 0)
            if obs > rec:
                errs.append(
                    f"{key}: LOG LEVEL IMPROVED {recorded_level!r} -> {site.log_level!r} "
                    f"({site.relpath}:{site.lineno}). THIS IS THE RATCHET TIGHTENING, not "
                    f"a bug: re-run --write-baseline so the improvement is locked in. It "
                    f"can never be given back."
                )
            else:
                errs.append(
                    f"{key}: LOG LEVEL REGRESSED {recorded_level!r} -> {site.log_level!r} "
                    f"({site.relpath}:{site.lineno}). The baseline only tightens."
                )
    return errs


# ── baseline writing ─────────────────────────────────────────────────────────

_BASELINE_HEADER = [
    "AUTO-GENERATED BASELINE — ADR-0420 swallowed-failure lint.",
    "",
    "THESE ENTRIES ARE UNREVIEWED DEBT FROZEN IN PLACE. THEY ARE NOT VETTED",
    "EXEMPTIONS. Nobody has read most of them and decided the swallow is correct;",
    "they are the state of the tree on the day the rule landed, recorded so the",
    "rule could be enforced at zero NEW violations. Do not cite an entry's presence",
    "here as evidence that its swallow was reviewed and approved — the audit that",
    "produced this list read 8 of the R1 sites, and no precision rate should be",
    "extrapolated from that sample. Treating a frozen baseline as a reviewed",
    "allowlist would be the ADR-0420 class again, one level up.",
    "",
    "The ratchet only tightens: each entry re-resolves to a live AST site every",
    "run, and its recorded shape + log_level are RE-COMPARED, not merely stored.",
    "Improve a site's logging and the run FAILS until this file is regenerated.",
    "Delete entries as sites are fixed; never add one to silence a new violation.",
    "",
    "Regenerate: python scripts/check_swallowed_failure.py --write-baseline",
]


def write_baseline(sites: list[Site], path: Path, previous: dict) -> int:
    """Write the R1 baseline, carrying forward each entry's category/rationale."""
    out: dict = {"_header": _BASELINE_HEADER}
    for site in sorted((s for s in sites if s.rule == "R1"), key=lambda s: s.key):
        prev = previous.get(site.key)
        prev = prev if isinstance(prev, dict) else {}
        out[site.key] = {
            "category": prev.get("category", "pre-existing"),
            "rationale": prev.get(
                "rationale",
                "Frozen day-one debt from the ADR-0420 detector's first run — "
                "UNREVIEWED, not a vetted exemption. See the file header.",
            ),
            "shape": site.shape,
            "log_level": site.log_level,
            "lineno_at_baseline": site.lineno,
        }
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return len(out) - 1


# ── entrypoint ───────────────────────────────────────────────────────────────


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="yadgar", help="Directory to scan (default: yadgar).")
    ap.add_argument("--baseline-file", default=str(_DEFAULT_BASELINE))
    ap.add_argument("--warn", action="store_true", help="Report violations but exit 0.")
    ap.add_argument("--list-all", action="store_true", help="Print every matched site.")
    ap.add_argument("--write-baseline", action="store_true", help="Regenerate the R1 baseline.")
    args = ap.parse_args(argv)

    repo_root = _REPO_ROOT
    root = Path(args.root)
    if not root.is_absolute():
        root = repo_root / root
    if not root.resolve().is_relative_to(repo_root):
        # An out-of-tree --root (self-tests, ad-hoc scans): key paths on the root
        # itself so `relative_to` has an answer and site keys stay stable.
        repo_root = root.resolve()

    result = run(root, repo_root)
    baseline_path = Path(args.baseline_file)
    baseline = _load_baseline(baseline_path)

    if args.write_baseline:
        n = write_baseline(result.sites, baseline_path, baseline)
        print(f"wrote {n} R1 baseline entries to {baseline_path}")
        return 0

    if args.list_all:
        for s in sorted(result.sites, key=lambda s: (s.rule, s.relpath, s.lineno)):
            print(f"{s.rule}  {s.relpath}:{s.lineno}  {s.qualname}  {s.shape}  log={s.log_level}")

    parse_errors = [s for s in result.sites if s.rule == "PARSE"]
    r3 = [s for s in result.sites if s.rule == "R3"]
    r1_new = [s for s in result.sites if s.rule == "R1" and s.key not in baseline]

    # Baseline integrity is ALWAYS hard — --warn does not soften it.
    integrity = check_baseline(result.sites, baseline, result.qualnames)

    if integrity:
        print("swallow-baseline integrity FAILURES:", file=sys.stderr)
        for e in integrity:
            print(f"  {e}", file=sys.stderr)

    for s in parse_errors:
        print(f"UNPARSEABLE: {s.relpath}", file=sys.stderr)

    if r3:
        print(
            f"\nR3 — non-2xx status with a success-shaped body ({len(r3)}):",
            file=sys.stderr,
        )
        for s in r3:
            print(f"  {s.relpath}:{s.lineno}  {s.qualname}  {s.shape}", file=sys.stderr)
        print(
            "  R3 is enforced at ZERO and has NO baseline: name the failure in the "
            "body instead of returning an empty/success-shaped one.",
            file=sys.stderr,
        )

    if r1_new:
        print(f"\nR1 — new swallow-and-return-empty ({len(r1_new)}):", file=sys.stderr)
        for s in r1_new:
            print(
                f"  {s.relpath}:{s.lineno}  {s.qualname}  returns {s.shape}, logs at {s.log_level}",
                file=sys.stderr,
            )
        print(
            "  Log at >= WARNING with exc_info, or return a value that names the "
            "failure. Do NOT add a baseline entry — the baseline only shrinks.",
            file=sys.stderr,
        )

    hard = bool(integrity or parse_errors)
    soft = bool(r3 or r1_new)

    if not hard and not soft:
        print(
            f"swallowed-failure lint OK — R3=0, R1 new=0 "
            f"({len(baseline) - 1 if baseline else 0} baselined R1 sites)"
        )
        return 0
    if hard:
        return 1
    return 0 if args.warn else 1


if __name__ == "__main__":
    raise SystemExit(main())
