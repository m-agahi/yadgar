#!/usr/bin/env python3
"""I33 — Observe-coverage lint (tri-signal observability ratchet).

AST-walks in-scope Python functions and classifies each as SATISFIED (has a span
source), auto-exempt (dunder / property / trivial), EXEMPT via
`.observe-allowlist.json`, or MISSING (a non-exempt function with no span source).

Modeled 1:1 on `scripts/check_trace_spans.py` (I24) + `.complexity-allowlist.json`
governance (I30). Stdlib-only.

Modes:
  --warn                 : print MISSING, exit 0 (baseline mode; P0 default).
  (no --warn)            : hard-fail on MISSING (exit 1). Per-area rollout flips.
  --area <name>          : restrict scan to files whose path contains <name>.

Allowlist integrity is ALWAYS hard (even in --warn mode): a stale entry (maps to
no current function), a rationale < 40 chars, or an invalid category → exit 1.

Usage:
  python scripts/check_observe_coverage.py --warn
  python scripts/check_observe_coverage.py --warn --root yadgar --allowlist-file .observe-allowlist.json
  python scripts/check_observe_coverage.py --list-all --warn
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Decorators that count as a span source (satisfy coverage).
_SPAN_DECORATORS = {"trace_span", "observe", "_tool"}

# Exemption categories permitted in .observe-allowlist.json.
_ALLOWED_CATEGORIES = {
    "hot-loop",
    "generated",
    "trivial",
    "property",
    "dunder",
    "framework-instrumented",
    "test",
    "pre-existing",
}

_MIN_RATIONALE_CHARS = 40

# Conservative I/O-sink names — presence disqualifies a fn from "trivial".
_IO_SINK_HINTS = {
    "post",
    "get",
    "request",
    "execute",
    "query",
    "open",
    "write",
    "read",
    "run",
    "check_output",
    "call",
    "encode",
    "insert",
    "connect",
    "commit",
    "info",
    "debug",
    "warning",
    "error",
    "critical",
    "exception",
}


@dataclass
class Finding:
    qualname: str
    lineno: int
    # SATISFIED | EXEMPT_DUNDER | EXEMPT_PROPERTY | EXEMPT_TRIVIAL | EXEMPT_ALLOWLIST
    # | EXEMPT_GLOB | EXEMPT_OBSERVE | MISSING
    status: str
    # Integrity errors raised at classification time (e.g. short @observe(exempt=...)
    # reason). Threaded up to the always-hard integrity channel by run().
    errors: list[str] = field(default_factory=list)
    # For EXEMPT_GLOB findings only: the owning file module stem + the glob that
    # exempted the fn — feeds the ADR-0040 option-C glob-exempt audit report.
    module: str | None = None
    glob: str | None = None


# ── classification helpers ───────────────────────────────────────────────────


def _decorator_name(dec: ast.expr) -> str | None:
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _observe_exempt_reason(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return the string literal passed to `@observe(exempt=...)`, else None.

    Distinguishes a categorized no-op (`@observe(exempt="reason")`) from a real
    span source (`@observe(tier=...)`). A non-literal exempt value returns "" so
    governance flags it (a reason must be a checkable literal, not a runtime expr).
    """
    for d in node.decorator_list:
        if not isinstance(d, ast.Call) or _decorator_name(d) != "observe":
            continue
        for kw in d.keywords:
            if kw.arg == "exempt":
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
                return ""  # exempt present but not a string literal → flag it
    return None


def _has_span_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff the fn carries a real span source.

    `@observe(exempt=...)` is a categorized no-op, NOT a span source — it is handled
    separately so its reason can be governed (closes the P5 hole where an empty
    exempt reason silently counted as SATISFIED).
    """
    for d in node.decorator_list:
        name = _decorator_name(d)
        if name not in _SPAN_DECORATORS:
            continue
        if name == "observe" and _observe_exempt_reason(node) is not None:
            continue  # exempt no-op, not a span
        return True
    return False


def _is_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for d in node.decorator_list:
        n = _decorator_name(d)
        if n in {"property", "cached_property", "setter", "getter", "deleter"}:
            return True
    return False


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_trivial(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """≤3 statements, no control-flow, no raise/await, no I/O-sink call.

    Deliberately strict — favours REQUIRING @observe over silently exempting.
    """
    body = [
        s for s in node.body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)
    ]
    # (drop a leading docstring)
    if len(body) > 3:
        return False
    for sub in ast.walk(node):
        if isinstance(
            sub,
            (ast.If, ast.For, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Await),
        ):
            return False
        if isinstance(sub, ast.Call):
            fn_name = (
                _decorator_name(sub.func)
                if isinstance(sub.func, (ast.Name, ast.Attribute))
                else None
            )
            if fn_name in _IO_SINK_HINTS:
                return False
    return True


def _qualname(stack: list[str], name: str) -> str:
    return ".".join([*stack, name])


# ── file scan ────────────────────────────────────────────────────────────────


def _rel_posix(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX path (e.g. 'yadgar/seed/_generate.py') — the glob key form."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:  # path outside repo_root — fall back to the raw posix form
        return path.as_posix()


def _matches_glob(rel: str, glob: str) -> bool:
    """True if repo-relative POSIX path `rel` matches `glob`.

    `**` is honoured recursively: a trailing `dir/**` matches everything beneath
    `dir/`; `fnmatch`'s `*` does not cross `/`, so we normalise `**` to a
    cross-segment wildcard first.
    """
    if fnmatch.fnmatchcase(rel, glob):
        return True
    # fnmatch treats ** like * (no /-crossing). Emulate recursive ** ourselves.
    if "**" in glob:
        pattern = glob.replace("**", "*")
        # collapse any '*/*' that a 'dir/**' -> 'dir/*' could leave, then match on
        # the whole path with `/` allowed inside the wildcard via a manual check.
        prefix = glob.split("**", 1)[0]
        if rel.startswith(prefix):
            return True
        if fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def scan_file(
    path: Path,
    allowlist: dict,
    exempt_globs: dict | None = None,
    repo_root: Path | None = None,
) -> list[Finding]:
    """Classify every function in a file. `allowlist` maps 'module:qualname' -> entry.

    `exempt_globs` maps a repo-relative POSIX glob (e.g. 'yadgar/seed/**') -> entry;
    a function whose file path matches an exempt glob is EXEMPT_GLOB.
    """
    exempt_globs = exempt_globs or {}
    repo_root = repo_root or _REPO_ROOT
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError) as exc:  # pragma: no cover
        print(f"WARNING: could not parse {path}: {exc}", file=sys.stderr)
        return []

    module = path.stem
    rel = _rel_posix(path, repo_root)
    file_glob = next((g for g in exempt_globs if _matches_glob(rel, g)), None)
    findings: list[Finding] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = _qualname(stack, child.name)
                fq = f"{module}:{qn}"
                status, errs = _classify(child, fq, allowlist, file_glob)
                findings.append(
                    Finding(qualname=qn, lineno=child.lineno, status=status, errors=errs)
                )
                visit(child, [*stack, child.name])
            elif isinstance(child, ast.ClassDef):
                visit(child, [*stack, child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return findings


def _classify(node, fq: str, allowlist: dict, file_glob: str | None) -> tuple[str, list[str]]:
    """Return (status, integrity_errors). Errors are threaded to the always-hard channel."""
    if _is_dunder(node.name):
        return "EXEMPT_DUNDER", []
    if _is_property(node):
        return "EXEMPT_PROPERTY", []
    # @observe(exempt="reason") — categorized no-op; reason is GOVERNED (P5 hole).
    exempt_reason = _observe_exempt_reason(node)
    if exempt_reason is not None:
        if len(exempt_reason.strip()) < _MIN_RATIONALE_CHARS:
            return "EXEMPT_OBSERVE", [
                f"{fq}: @observe(exempt=...) reason must be a string literal >= "
                f"{_MIN_RATIONALE_CHARS} chars"
            ]
        return "EXEMPT_OBSERVE", []
    if _has_span_decorator(node):
        return "SATISFIED", []
    if fq in allowlist:
        return "EXEMPT_ALLOWLIST", []
    if file_glob is not None:
        return "EXEMPT_GLOB", []
    if _is_trivial(node):
        return "EXEMPT_TRIVIAL", []
    return "MISSING", []


# ── allowlist governance (always hard) ───────────────────────────────────────


def validate_allowlist_entry(fq: str, entry: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(entry, dict):
        return [f"{fq}: entry must be an object"]
    cat = entry.get("category")
    if cat not in _ALLOWED_CATEGORIES:
        errs.append(f"{fq}: invalid category {cat!r} (allowed: {sorted(_ALLOWED_CATEGORIES)})")
    rationale = entry.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE_CHARS:
        errs.append(f"{fq}: rationale must be >= {_MIN_RATIONALE_CHARS} chars")
    return errs


def validate_glob_entry(glob: str, entry: dict) -> list[str]:
    """Same governance as a per-fn allowlist entry, keyed on a path glob."""
    errs: list[str] = []
    if not isinstance(entry, dict):
        return [f"glob {glob}: entry must be an object"]
    cat = entry.get("category")
    if cat not in _ALLOWED_CATEGORIES:
        errs.append(
            f"glob {glob}: invalid category {cat!r} (allowed: {sorted(_ALLOWED_CATEGORIES)})"
        )
    rationale = entry.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE_CHARS:
        errs.append(f"glob {glob}: rationale must be >= {_MIN_RATIONALE_CHARS} chars")
    return errs


def _iter_py_files(root: Path, area: str | None) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*.py")):
        rel = str(p)
        if "/tests/" in rel or rel.endswith("_test.py") or p.name.startswith("test_"):
            continue
        if area and area not in rel:
            continue
        files.append(p)
    return files


def run(
    root: Path,
    allowlist: dict,
    area: str | None,
    exempt_globs: dict | None = None,
    repo_root: Path | None = None,
) -> tuple[list[Finding], set[str], list[str]]:
    """Return (all_findings, seen_fqs, integrity_errors).

    Side-channel: each EXEMPT_GLOB finding is tagged (`Finding.module` / `.glob`)
    with the file module stem and the repo-relative glob that exempted it, so the
    ADR-0040 option-C audit report can enumerate every glob-hidden function (a
    drift-visibility safeguard: a whole-dir glob otherwise makes a new/modified fn
    beneath it silently invisible).
    """
    exempt_globs = exempt_globs or {}
    repo_root = repo_root or _REPO_ROOT
    all_findings: list[Finding] = []
    seen_fqs: set[str] = set()
    glob_hits: set[str] = set()  # globs that matched >=1 in-scope function file
    for path in _iter_py_files(root, area):
        module = path.stem
        rel = _rel_posix(path, repo_root)
        file_glob = next((g for g in exempt_globs if _matches_glob(rel, g)), None)
        for g in exempt_globs:
            if _matches_glob(rel, g):
                glob_hits.add(g)
        for f in scan_file(path, allowlist, exempt_globs, repo_root):
            if f.status == "EXEMPT_GLOB":
                f.module = module
                f.glob = file_glob
            all_findings.append(f)
            seen_fqs.add(f"{module}:{f.qualname}")

    integrity: list[str] = []
    # classification-time errors (e.g. short @observe(exempt=...) reason) are hard.
    for f in all_findings:
        integrity.extend(f.errors)
    for fq, entry in allowlist.items():
        if fq.startswith("_"):
            continue  # metadata keys (e.g. "_comment", "_exempt_globs") handled elsewhere
        integrity.extend(validate_allowlist_entry(fq, entry))
        if fq not in seen_fqs:
            integrity.append(f"{fq}: STALE allowlist entry — no such function in scope")
    for glob, entry in exempt_globs.items():
        integrity.extend(validate_glob_entry(glob, entry))
        if glob not in glob_hits:
            integrity.append(f"glob {glob}: STALE — matches no in-scope function file")
    return all_findings, seen_fqs, integrity


# ── glob-exempt audit report (ADR-0040 option C — non-failing) ───────────────


def print_glob_exempt_report(findings: list[Finding]) -> None:
    """Print the count + enumerated list of glob-exempted functions to STDOUT.

    A `_exempt_globs` entry exempts an ENTIRE directory/file forever, so a new or
    modified function beneath it becomes auto-invisible to the I33 ratchet (the
    glob blind-spot ADR-0040 addresses). This report makes that hidden set
    auditable in CI output on every run. It is PURELY informational — it prints to
    stdout and NEVER affects the exit code (option C, the safeguard layer that
    complements option B's glob narrowing).
    """
    glob_findings = [f for f in findings if f.status == "EXEMPT_GLOB"]
    print(f"I33 glob-exempt audit — {len(glob_findings)} function(s) hidden by _exempt_globs:")
    by_glob: dict[str, list[Finding]] = {}
    for f in glob_findings:
        by_glob.setdefault(f.glob or "(unknown glob)", []).append(f)
    for glob in sorted(by_glob):
        fns = by_glob[glob]
        print(f"  {glob}  ({len(fns)} fn):")
        for f in sorted(fns, key=lambda x: (x.module or "", x.qualname)):
            print(f"    {f.module}:{f.qualname}:{f.lineno}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="I33 — observe-coverage lint (tri-signal ratchet).")
    p.add_argument("--warn", action="store_true", help="Warn-mode: MISSING never blocks (exit 0).")
    p.add_argument("--area", default=None, help="Restrict scan to paths containing this substring.")
    p.add_argument("--root", default=str(_REPO_ROOT / "yadgar"), help="Root dir to scan.")
    p.add_argument(
        "--allowlist-file",
        default=str(_REPO_ROOT / ".observe-allowlist.json"),
        help="Path to .observe-allowlist.json.",
    )
    p.add_argument("--list-all", action="store_true", help="Print every function + status, exit 0.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)

    allowlist: dict = {}
    alf = Path(args.allowlist_file)
    if alf.exists():
        try:
            allowlist = json.loads(alf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"ERROR: {alf} is not valid JSON: {exc}", file=sys.stderr)
            return 1

    exempt_globs = allowlist.get("_exempt_globs", {})
    if not isinstance(exempt_globs, dict):
        print("ERROR: _exempt_globs must be an object mapping glob -> entry", file=sys.stderr)
        return 1

    # Globs are keyed repo-relative (e.g. 'yadgar/seed/**'); derive the repo root
    # from the scan root so both live-repo runs and tmp-dir test runs resolve the
    # same POSIX form. Scan root is '.../yadgar' → repo root is its parent.
    repo_root = root.resolve().parent if root.name == "yadgar" else _REPO_ROOT

    findings, _seen, integrity = run(root, allowlist, args.area, exempt_globs, repo_root)

    if args.list_all:
        for f in findings:
            print(f"{f.qualname}:{f.lineno} [{f.status}]")

    # ADR-0040 option C: always surface the glob-exempted set to stdout for CI
    # drift-auditing. Informational only — never touches the exit code below.
    print_glob_exempt_report(findings)

    missing = [f for f in findings if f.status == "MISSING"]

    # Allowlist integrity is ALWAYS hard.
    if integrity:
        print("I33 allowlist integrity FAILURES:", file=sys.stderr)
        for e in integrity:
            print(f"  {e}", file=sys.stderr)
        return 1

    if missing:
        mode = "WARN" if args.warn else "FAIL"
        print(
            f"I33 [{mode}] — {len(missing)} function(s) missing tri-signal coverage "
            f"(no span source, not exempt, not allowlisted):",
            file=sys.stderr,
        )
        for f in missing[:50]:
            print(f"  {f.qualname}:{f.lineno}", file=sys.stderr)
        if len(missing) > 50:
            print(f"  ... and {len(missing) - 50} more", file=sys.stderr)
        if not args.warn:
            return 1

    if not missing and not integrity:
        print("I33 OK — all in-scope functions classified (SATISFIED or exempt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
