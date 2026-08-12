#!/usr/bin/env python3
"""ADR-0225 — the ``directory`` residue sweep (repo-wide), C15 of 0047 §5.

ADR-0225 retires ``directory`` as a *scoping and identity* concept and names a
residue sweep as **the** enforcement mechanism — "without it the ADR is a
code-review promise". This script is that sweep. It lands LAST in the 0047
train on purpose: written first it would have been red for twenty-seven cars;
written now it **ratchets what the train actually achieved** and nothing more.

WHAT IS BEING RATCHETED — read this before "finishing the job"
--------------------------------------------------------------
C14 MEASURED that ``directory`` was **not removed**. C5 removed its ability to
*resolve*; the parameter itself still exists on **46 MCP tools**, in two classes
that a sweep must keep apart:

  * ``resolve_effective_project`` class (``recall``, ``memorize``, ``anchor``,
    ``restore``, ``wiki_add``, ``adr_*``, ``agent_prompt_save``,
    ``bootstrap_project``, ``seed_project``) — **raises** without ``project=``.
    ``directory`` survives as an accepted-but-inert argument.
  * ``accept_project_param`` class (``project_brief``, ``audit_anchors``,
    ``block_*``, ``checkpoint``, ``wiki_list``, ``get_rules``) — **still
    directory-keyed**; ``project=`` is validated only.

**A lint that flagged every ``directory=`` would be red on arrival and wrong.**
The residue this script ratchets is a ``directory`` in a scoping position that
can still *resolve* — not the parameter's existence. Deleting the second class's
parameter breaks it; the allowlist says so per entry, and the next reader who
"finishes the job" against these entries will break live scoping.

Amendment 3 of plan 0047 §2 is the binding rule for when a rename IS safe:
the backing table must already carry ``project_id`` **and** every caller must
actually hold an identity. C9c applied condition 1 alone to 13 signatures and
swept 1.

TWO DIRECTIONS (the shape of ``scripts/check_capability_coverage.py``)
----------------------------------------------------------------------
  1. **RESIDUE** — a residue token in a *scoping position* in a scanned file
     that has no allowlist entry. The tree cannot regress.
  2. **STALE ALLOWLIST** — an entry whose subject no longer exists (the path
     matches no file), or whose subject no longer carries any residue (the
     reason now describes nothing). Both are **hard failures**.

A third, smaller class sits under both: a file the matcher **cannot parse** is
reported as ``UNPARSEABLE``, never scored clean. See ``find_unparseable`` — the
first thing this lint found was three files that parse on the venv's Python
3.14 and not on the 3.13 that pre-commit's ``language: system`` hooks run.

DIRECTION-2 POLICY: HARD FAIL, NOT WARN — and why
-------------------------------------------------
``.test-weakening-allowlist.json``'s ``_stale_policy`` is warn-only and that is
correct *there*: its input is a diff against ``merge-base(origin/master, HEAD)``,
which MOVES — an entry goes stale the moment its branch merges, through nobody's
fault, and hard-failing would turn master red for everyone.

**This lint's input is the filesystem.** Nothing moves under it, so a stale
entry is always somebody's edit and always has an owner. Second clause, from
this very train: C15a found two ``.test-weakening-allowlist.json`` entries that
had been warning since the ADR-0215 train merged, with nobody cleaning them up —
warn-only demonstrably leaves entries stale forever and teaches the next reader
to skim the warnings. Hard fail.

THE THREE SIBLING LINTS — promoted, not replaced
------------------------------------------------
Three tree-scoped predecessors exist and are **kept, not retired** (this car
deletes no test):

  * ``yadgar/tests/_shared/test_c9a_directory_residue_shared.py``  — AST walk
    over parameter names, keyed ``<path>::<function>``, both directions.
  * ``yadgar/tests/backend/test_c9b_backend_directory_residue.py`` — per file,
    regex over the identifier-shaped surface, both directions.
  * ``scripts/directory_residue_allowlist.txt`` — C10's classification of every
    remaining ``core/**`` file. **That file is this script's allowlist**; C10
    wrote it for this lint and the CHANGELOG says so.

THE CROSS-CAR STALE-ENTRY CLASS, AND HOW IT IS CLOSED
-----------------------------------------------------
Twice in this train a sweep in tree X emptied a file of residue and thereby
stranded a *sibling's* allowlist entry in tree Y, landing as the next car's red
(C9c had to remove C9b's ``cls_store/clustering.py`` entry). The mechanism was
never carelessness: **the siblings are pytest modules**, so the standing per-car
requirement (``pre-commit run --all-files``) never ran them, and ADR-0218
forbids the full unit suite — a car sweeping ``backend`` was *structurally
unable* to see ``_shared``'s module go red.

Two things close it, both structural rather than procedural:

  * This script is a **pre-commit hook** (``pass_filenames: false``,
    ``always_run: true``) over the **whole repo**, so every car boundary
    evaluates every tree in one run against one allowlist.
  * The sibling dicts are pulled into that same run: ``_ALLOWLIST`` / ``_SWEPT``
    / ``ALLOWLIST`` are parsed out of the two modules by AST (never imported)
    and checked for **subject existence** — a file or ``path::func`` that no
    longer exists is a hard failure here, in pre-commit, at the boundary.

    **Deliberate limit:** subject existence ONLY. This script does not judge
    "the residue is gone" on a sibling's entry, because the three matchers
    differ by design (C9a walks parameters; C9b regexes lines; this one walks
    four AST positions) and cross-judging would emit false stale verdicts.
    Residue-gone on a sibling entry remains that module's own assertion.

WHAT COUNTS AS A "SCOPING POSITION" — and the blind spots (ADR-0080)
--------------------------------------------------------------------
The discriminator is: *does the position express a name that someone outside
this function fills in or reads back?* Four AST positions qualify:

  ``param``   a function parameter name
  ``kwarg``   a call-site keyword-argument name
  ``key``     a string literal used as a dict key or a subscript index
  ``field``   a class-body annotated field (dataclass / pydantic model)

DELIBERATELY NOT MATCHED, each for a stated reason:

  * **Prose.** A raw ``git grep`` returns ~2 400 hits and roughly a third are
    the English word in a comment. The branch train measured the same thing (19
    of ``core/vacuum/``'s hits were the word "branch" in a comment). An AST walk
    cannot see a docstring, so prose cannot fail this lint and a "fix" that
    rewrites comments cannot pass it either.
  * **Attribute reads** (``args.directory``) and **plain local variables**
    (``directory = ...``). Neither expresses a contract with any caller;
    sweeping them is churn. Both siblings excluded them too.
  * **``getattr(args, "project_directory")`` / ``add_argument("directory")``.**
    An attribute read and a CLI positional spelled as strings. Both live in
    ``core/cli/**``, which is carve-out 1 (host-side minting) anyway.
  * **Positional argument VALUES.** ``f(directory)`` binds by position; the
    callee's parameter is matched instead, which is the same fact once.
  * **Markdown and other non-Python files.** Carve-out 4
    (``docs/**/adr-*``, ``docs/plans/archive/**``, ``docs/CHANGELOG.md`` are
    historical and correct as written) is therefore **moot by construction**
    here: an AST lint over ``*.py`` never reaches them. C14 owns the docs
    surface. This is recorded rather than silently omitted because carve-out 4
    is on the reviewer's list.
  * **``yadgar/tests/**``.** 1 890 of the 2 403 raw hits are in tests, and they
    are legitimate: the second tool class is still directory-keyed, so its
    tests must pass ``directory=``, and ``tmp_path`` is carve-out 3 by name.
    Pinning test call sites would freeze the tests against the very migration
    the next PR performs.
  * **The allowlist's globs.** C10's six ``**`` entries cover 49 ``.py`` files
    as a block; a NEW scoping ``directory`` inside e.g. ``core/install/`` passes
    silently. The globs are kept — they are genuine carve-out-3 subtrees and
    enumerating them risks red-on-arrival — but this is a real gap, not
    coverage, and the allowlist header repeats it.

CARVE-OUT 2 IS APPLIED AS A CLASS, NOT PER ENTRY
------------------------------------------------
``directory_context`` in a **string-literal** position (``row["directory_context"]``,
``{"directory_context": ...}``) is the *stored column*, alive until the drop
migration in the NEXT PR because the backfill derives from it. It is stripped
before matching and never allowlisted — enumerating ~100 column reads would
produce an allowlist that asserts nothing (C9b's argument, adopted verbatim).
A **parameter or keyword** named ``directory_context`` still counts: that is a
signature, not a column, and it gets a per-entry reason.

THE ``branch`` ARM IS A RATCHET, NOT A SWEEP
--------------------------------------------
ADR-0215 already removed branch scoping. ``branch`` is in the token set and
currently matches **zero** scoping positions repo-wide; it is here so a
reintroduction fails, not because it is doing work today. Matching is on the
**exact identifier**, never a substring, which is what keeps two deliberate
survivors alive: git's real ``default_branch`` (``core/code_graph/``) and
Alembic's module-level ``branch_labels`` — the latter is a *required* variable
in every ``sql/migrations/versions/*.py``, and sweeping it breaks the migration
chain. ``tests/test_check_directory_residue.py`` pins both.

Usage:
  python scripts/check_directory_residue.py            # check, exit 0/1
  python scripts/check_directory_residue.py --list     # every residue site
  python scripts/check_directory_residue.py --json     # machine-readable
  python scripts/check_directory_residue.py --repo-root /path --allowlist F

Exit codes:
  0  no residue outside the allowlist, and no stale allowlist entry
  1  one or more violations
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from pathlib import Path

#: A PEP 758 unparenthesized ``except A, B:`` STATEMENT. Anchored at line start
#: and required to end the line so prose mentioning the form (this file does,
#: several times) cannot self-match — the repo has already been bitten once by a
#: guard that scanned for its own marker and tripped on the commit message
#: describing it.
_BARE_EXCEPT_TUPLE = re.compile(
    r"^\s*except\s+[A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)+\s*:\s*(?:#.*)?$"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWLIST_FILE = _REPO_ROOT / "scripts" / "directory_residue_allowlist.txt"

#: Identifiers that mean "a project was identified by its directory".
#: Matched EXACTLY — never as a substring. ``default_branch`` and
#: ``branch_labels`` are different identifiers and are therefore untouched.
RESIDUE_TOKENS: frozenset[str] = frozenset(
    (
        "directory",
        "caller_dir",
        "caller_directory",
        "directory_context",
        "project_directory",
        "target_directory",
        "watch_directory",
        "branch",
    )
)

#: Carve-out 2, applied as a class: the stored column, in string-literal
#: positions only. See the module docstring.
STORED_COLUMN = "directory_context"

#: Roots walked for ``*.py``. ``yadgar/tests`` is excluded by rule (docstring).
SCAN_ROOTS: tuple[str, ...] = (
    "yadgar/core",
    "yadgar/_shared",
    "yadgar/backend",
    "scripts",
    "benchmarks",
    "docs",
)

_SKIP_PARTS = frozenset(
    (
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".claude",
    )
)

#: Tags an allowlist entry may carry. The tag states the CLASS of reason; the
#: trailing ``# comment`` states the specific one. Both are required.
VALID_TAGS: frozenset[str] = frozenset(
    (
        "carve-out-1",  # host-side identity minting / install targets
        "carve-out-2",  # a signature named after the stored legacy column
        "carve-out-3",  # a genuine filesystem path
        "tool-surface",  # the 46-tool `directory` parameter C14 measured as surviving
        "wire-contract",  # a key crossing a process boundary
        "legacy-key",  # a real scope key blocked by Amendment 3's two conditions
    )
)

# --- the sibling lints, closed into this run (subject existence only) --------
_SIBLINGS: tuple[tuple[str, str, str], ...] = (
    (
        "yadgar/tests/_shared/test_c9a_directory_residue_shared.py",
        "_ALLOWLIST",
        "yadgar/_shared",
    ),
    (
        "yadgar/tests/_shared/test_c9a_directory_residue_shared.py",
        "_SWEPT",
        "yadgar/_shared",
    ),
    (
        "yadgar/tests/backend/test_c9b_backend_directory_residue.py",
        "ALLOWLIST",
        "yadgar/backend",
    ),
)

#: Anti-vacuity floors (ADR-0080). Measured on the C15 ref against
#: ``9270c7fa``; all three are LOWER bounds. An empty walk, an unparseable
#: sibling, or a matcher that silently returns nothing would otherwise make
#: every assertion here trivially green — the failure mode this train produced
#: four times. If a later PR genuinely finishes the sweep, LOWER these
#: deliberately rather than letting the guard rot.
#: Minimum characters of stated reason per allowlist entry. 40, matching the
#: repo's governed-allowlist family (``.test-weakening-allowlist.json``,
#: ``.health-endpoint-allowlist.json``, ``.urllib-httperror-close-allowlist.json``)
#: and C9a's own ``test_every_entry_states_a_reason``. The lint that supersedes
#: three siblings must not be the weakest of the four on the one field a
#: reviewer actually reads.
MIN_REASON_CHARS = 40

MIN_FILES_SCANNED = 400  # measured 506
MIN_RESIDUE_HITS = 300  # measured 408 across 80 files
MIN_SIBLING_ENTRIES = 60  # measured 80 (C9a _ALLOWLIST 48 + C9a _SWEPT 9 + C9b ALLOWLIST 23)


# ---------------------------------------------------------------------------
# The matcher — four AST scoping positions, no text scan.
# ---------------------------------------------------------------------------
def _class_field_nodes(tree: ast.AST) -> set[int]:
    """``id()`` of every ``AnnAssign`` that is a class-body field."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out.add(id(stmt))
    return out


def _is_residue_literal(node: ast.expr | None) -> str | None:
    """A string literal that is a residue token, carve-out 2 already stripped."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    if node.value not in RESIDUE_TOKENS or node.value == STORED_COLUMN:
        return None
    return node.value


def _param_hits(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str, str]]:
    a = node.args
    every = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    every += [x for x in (a.vararg, a.kwarg) if x is not None]
    return [
        (node.lineno, "param", f"{node.name}({arg.arg})")
        for arg in every
        if arg.arg in RESIDUE_TOKENS
    ]


def _kwarg_hits(node: ast.Call) -> list[tuple[int, str, str]]:
    return [
        (node.lineno, "kwarg", f"{kw.arg}=") for kw in node.keywords if kw.arg in RESIDUE_TOKENS
    ]


def _key_hits(node: ast.Subscript | ast.Dict) -> list[tuple[int, str, str]]:
    if isinstance(node, ast.Subscript):
        tok = _is_residue_literal(node.slice)
        return [(node.lineno, "key", f"[{tok!r}]")] if tok else []
    out: list[tuple[int, str, str]] = []
    for k in node.keys:
        tok = _is_residue_literal(k)
        if tok:
            out.append((node.lineno, "key", f"{tok!r}:"))
    return out


def scan_source(source: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, kind, detail)`` for every scoping-position residue hit."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    fields = _class_field_nodes(tree)
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out += _param_hits(node)
        elif isinstance(node, ast.Call):
            out += _kwarg_hits(node)
        elif isinstance(node, (ast.Subscript, ast.Dict)):
            out += _key_hits(node)
        elif isinstance(node, ast.AnnAssign) and id(node) in fields:
            if isinstance(node.target, ast.Name) and node.target.id in RESIDUE_TOKENS:
                out.append((node.lineno, "field", node.target.id))
    return sorted(out)


def iter_sources(repo_root: Path, scan_roots: tuple[str, ...] = SCAN_ROOTS):
    """Yield every ``*.py`` under ``scan_roots``, tests excluded."""
    for root_name in scan_roots:
        base = repo_root / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(repo_root)
            if any(part in _SKIP_PARTS for part in rel.parts):
                continue
            if "tests" in rel.parts or path.name.startswith("test_"):
                continue
            yield path, rel.as_posix()


def find_unparseable(repo_root: Path, scan_roots: tuple[str, ...] = SCAN_ROOTS) -> list[str]:
    """Files the matcher CANNOT read. A file it cannot read is not a clean file.

    ``scan_source`` returns ``[]`` on a ``SyntaxError`` so the unit-level matcher
    stays total, but at repo level that silence is the exact failure this train
    produced four times — most nearly C15a's ``_parse_iso``, whose swallowed
    ``TypeError`` meant the nightly sweep archived nothing in production with
    every test green. So the repo-level walk reports parse failures as
    violations instead of scoring the file clean.

    This is not hypothetical here. Three files under the scan roots parsed
    on the venv's Python 3.14 (PEP 758 allows an unparenthesized
    ``except A, B:``) and FAILED on the Python 3.13 that pre-commit's own
    ``language: system`` hooks run under. Silently skipping them made two
    allowlist entries look stale — a false Direction-2 verdict, visible only at
    commit time. C15 parenthesised all three (with ``# fmt: skip``, the repo's
    convention against ruff-format re-stripping them) and this check keeps the
    next one from being silent.
    """
    bad: list[str] = []
    for path, rel in iter_sources(repo_root, scan_roots):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            continue
        # Interpreter-INDEPENDENT arm, and the reason it exists: the ast.parse
        # arm below can only fire under a Python that REJECTS PEP 758, so on
        # 3.14 it is structurally incapable of seeing this regression. If a
        # ruff-format run strips one of the `# fmt: skip` parens again, 3.13
        # breaks and every 3.14-side check stays green — the same
        # "silent under the environment that matters" shape. The repo's
        # `test_v5_46_16_except_tuple_sweep.py` covers `yadgar/**` only, which
        # is exactly why `benchmarks/**` and `docs/diagrams/**` rotted.
        for lineno, line in enumerate(source.splitlines(), 1):
            if _BARE_EXCEPT_TUPLE.match(line):
                bad.append(
                    f"UNPARSEABLE: {rel}:{lineno} uses PEP 758's unparenthesized "
                    "`except A, B:`, which is a SyntaxError on every Python before "
                    "3.14 — including the interpreter pre-commit's `language: system` "
                    "hooks run. Write `except (A, B):  # fmt: skip`; the `# fmt: skip` "
                    "is what stops ruff-format stripping the parens back off."
                )
        try:
            ast.parse(source)
        except SyntaxError as exc:
            bad.append(
                f"UNPARSEABLE: {rel}:{exc.lineno} cannot be parsed by this "
                f"interpreter ({sys.version_info.major}.{sys.version_info.minor}) "
                f"— {exc.msg}. The sweep cannot score a file it cannot read, so "
                "this is a violation, not a skip. Fix the source (a version-only "
                "syntax such as PEP 758's unparenthesized `except A, B:` fails "
                "here because pre-commit's `language: system` hooks run a "
                "different Python from the venv)."
            )
    return bad


def find_residue(repo_root: Path, scan_roots: tuple[str, ...] = SCAN_ROOTS):
    """Return ``(residue, files_scanned)`` — ``{relpath: [(lineno, kind, detail)]}``."""
    residue: dict[str, list[tuple[int, str, str]]] = {}
    scanned = 0
    for path, rel in iter_sources(repo_root, scan_roots):
        scanned += 1
        try:
            hits = scan_source(path.read_text(encoding="utf-8"))
        except OSError:  # pragma: no cover - defensive
            continue
        if hits:
            residue[rel] = hits
    return residue, scanned


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def parse_allowlist(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Parse ``<tag>  <path-or-glob>  # reason`` rows.

    Returns ``(entries, errors)`` where an entry is ``(tag, pattern, reason)``.
    Malformed rows are errors, never silently skipped — an unparsed row is an
    entry that silently grants nothing and hides a residue site.
    """
    entries: list[tuple[str, str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        body, _, comment = raw.partition("#")
        body = body.strip()
        if not body:
            continue
        parts = body.split()
        if len(parts) != 2:
            errors.append(
                f"MALFORMED allowlist line {lineno}: expected '<tag> <path>', got {raw!r}"
            )
            continue
        tag, pattern = parts
        if tag not in VALID_TAGS:
            errors.append(
                f"MALFORMED allowlist line {lineno}: unknown tag {tag!r} — "
                f"must be one of {sorted(VALID_TAGS)}"
            )
            continue
        reason = comment.strip()
        if len(reason) < MIN_REASON_CHARS:
            errors.append(
                f"MALFORMED allowlist line {lineno}: entry {pattern!r} has no usable "
                f"stated reason (>= {MIN_REASON_CHARS} chars after '#'). A bare entry "
                "is not reviewable."
            )
            continue
        if pattern in seen:
            errors.append(f"MALFORMED allowlist line {lineno}: duplicate entry {pattern!r}")
            continue
        seen.add(pattern)
        entries.append((tag, pattern, reason))
    return entries, errors


def _matches(pattern: str, rel: str) -> bool:
    if pattern.endswith("/**"):
        return rel.startswith(pattern[:-2])
    return fnmatch.fnmatch(rel, pattern)


def _pattern_files(repo_root: Path, pattern: str, known: list[str] | None = None) -> list[str]:
    """Every scanned ``*.py`` the pattern resolves to (may be empty).

    ``known`` lets the caller hoist the walk: ``check()`` resolves 78 entries,
    and re-walking 506 files per entry made the hook ~7x slower than the
    ~2s neighbours in ``.pre-commit-config.yaml``.
    """
    rels = known if known is not None else [rel for _, rel in iter_sources(repo_root)]
    return [rel for rel in rels if _matches(pattern, rel)]


# ---------------------------------------------------------------------------
# Sibling allowlists — subject existence only (see module docstring).
# ---------------------------------------------------------------------------
def parse_sibling_keys(source: str, name: str) -> list[str] | None:
    """Extract the string keys/items of a module-level dict or tuple literal.

    AST only — the sibling modules are never imported. ``None`` means the
    binding was not found, which is a hard failure (see ``check_siblings``).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return None
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Dict):
            return [
                k.value
                for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return [
                e.value
                for e in value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return None


def _subject_exists(repo_root: Path, tree_root: str, entry: str) -> bool:
    """``<relpath>`` or ``<relpath>::<function>`` still names something real."""
    rel, _, func = entry.partition("::")
    path = repo_root / tree_root / rel
    if not path.is_file():
        return False
    if not func:
        return True
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):  # fmt: skip  # pragma: no cover - defensive
        return False
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
        for n in ast.walk(tree)
    )


def check_siblings(repo_root: Path) -> tuple[list[str], int]:
    """Return ``(errors, entries_parsed)`` for the two sibling test modules."""
    errors: list[str] = []
    parsed = 0
    for module_rel, binding, tree_root in _SIBLINGS:
        module = repo_root / module_rel
        if not module.is_file():
            errors.append(
                f"SIBLING MISSING: {module_rel} is gone. The unified lint checks the "
                "sibling allowlists so a sweep cannot strand one unseen; if the module "
                "was genuinely retired, remove its row from `_SIBLINGS` deliberately."
            )
            continue
        keys = parse_sibling_keys(module.read_text(encoding="utf-8"), binding)
        if keys is None:
            errors.append(
                f"SIBLING UNPARSEABLE: {binding} not found as a module-level literal in "
                f"{module_rel}. This arm would go silently green — repoint it."
            )
            continue
        parsed += len(keys)
        for entry in keys:
            if not _subject_exists(repo_root, tree_root, entry):
                errors.append(
                    f"STALE SIBLING ENTRY: {module_rel}::{binding} names {entry!r} under "
                    f"{tree_root}/, which no longer exists. A sweep in another tree "
                    "stranded it — remove or repoint the entry in the sibling module."
                )
    return errors, parsed


# ---------------------------------------------------------------------------
# The two directions
# ---------------------------------------------------------------------------
def check(
    repo_root: Path | None = None,
    allowlist_file: Path | None = None,
    *,
    scan_roots: tuple[str, ...] = SCAN_ROOTS,
    check_floors: bool = True,
    check_sibling_lints: bool = True,
) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    if repo_root is None:
        repo_root = _REPO_ROOT
    if allowlist_file is None:
        allowlist_file = allowlist_path(repo_root)
    if not allowlist_file.is_file():
        return [f"allowlist not found at {allowlist_file}"]

    entries, errors = parse_allowlist(allowlist_file.read_text(encoding="utf-8"))
    errors += find_unparseable(repo_root, scan_roots)
    residue, scanned = find_residue(repo_root, scan_roots)

    # ── anti-vacuity floors (ADR-0080) ──────────────────────────────────────
    if check_floors:
        if scanned < MIN_FILES_SCANNED:
            errors.append(
                f"VACUOUS: only {scanned} files walked under {list(scan_roots)} "
                f"(floor {MIN_FILES_SCANNED}). The walk did not reach the tree, so "
                "every check below would be trivially green."
            )
        total = sum(len(v) for v in residue.values())
        if total < MIN_RESIDUE_HITS:
            errors.append(
                f"VACUOUS: only {total} residue sites found (floor {MIN_RESIDUE_HITS}). "
                "Either the matcher broke or the sweep genuinely finished; if it "
                "finished, LOWER `MIN_RESIDUE_HITS` deliberately."
            )

    # ── Direction 1 — residue outside the allowlist ─────────────────────────
    for rel in sorted(residue):
        if any(_matches(pat, rel) for _, pat, _ in entries):
            continue
        sites = "; ".join(f"{ln}:{kind} {detail}" for ln, kind, detail in residue[rel][:6])
        more = "" if len(residue[rel]) <= 6 else f" (+{len(residue[rel]) - 6} more)"
        errors.append(
            f"RESIDUE: {rel} has a `directory`-family token in a scoping position "
            f"with no allowlist entry — {sites}{more}. Re-key it onto `project_id` "
            "(plan 0047 §2 Amendment 3: the table must carry `project_id` AND every "
            "caller must hold an identity), or add an allowlist entry WITH a tag and "
            "a stated reason."
        )

    # ── Direction 2 — stale allowlist entries (hard fail; see docstring) ────
    known = [rel for _, rel in iter_sources(repo_root, scan_roots)]
    for tag, pattern, reason in entries:
        resolved = _pattern_files(repo_root, pattern, known)
        if not resolved:
            errors.append(
                f"STALE ENTRY (no subject): `{pattern}` [{tag}] matches no scanned file. "
                f"Its reason ({reason!r}) describes nothing — delete or repoint it."
            )
            continue
        if not any(rel in residue for rel in resolved):
            errors.append(
                f"STALE ENTRY (no residue): `{pattern}` [{tag}] resolves to "
                f"{len(resolved)} file(s), none of which still carries residue. The "
                f"sweep reached it — delete the entry. Reason on file: {reason!r}"
            )

    # ── the sibling arm ─────────────────────────────────────────────────────
    if check_sibling_lints:
        sib_errors, sib_parsed = check_siblings(repo_root)
        errors += sib_errors
        if check_floors and sib_parsed < MIN_SIBLING_ENTRIES:
            errors.append(
                f"VACUOUS: only {sib_parsed} sibling allowlist entries parsed "
                f"(floor {MIN_SIBLING_ENTRIES}). The sibling arm would be silently "
                "green — a renamed binding, not a finished sweep."
            )
    return errors


def allowlist_path(repo_root: Path) -> Path:
    return repo_root / "scripts" / "directory_residue_allowlist.txt"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ADR-0225 — `directory` residue sweep")
    parser.add_argument("--list", action="store_true", help="Print every residue site")
    parser.add_argument("--json", action="store_true", help="Print residue sites as JSON")
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    parser.add_argument("--allowlist", default=None, help="Override the allowlist file")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else _REPO_ROOT
    allow = Path(args.allowlist) if args.allowlist else allowlist_path(repo_root)

    if args.json or args.list:
        residue, scanned = find_residue(repo_root)
        if args.json:
            print(
                json.dumps(
                    {
                        "files_scanned": scanned,
                        "residue": {k: [list(t) for t in v] for k, v in residue.items()},
                    },
                    indent=2,
                )
            )
            return 0
        print(f"=== {scanned} files walked, {len(residue)} with residue ===")
        for rel in sorted(residue):
            print(f"\n{rel}  ({len(residue[rel])})")
            for ln, kind, detail in residue[rel]:
                print(f"  {ln:5d}  {kind:6s}  {detail}")
        return 0

    errors = check(repo_root, allow)
    if errors:
        print("ADR-0225 `directory` residue sweep FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("ADR-0225 `directory` residue sweep OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
