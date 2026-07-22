"""Repo-wiki scanner — extract module/function structure from Python files.

Shares the directory walker from yadgar.seed._scan (SKIP_DIRS, binary exclusions)
and layers Python AST extraction on top to produce structured module records with:
- module docstring
- list of functions/classes with signatures + docstrings
- module-level import names (for lightweight call-ref context)

Option B (tree-sitter + leidenalg community detection) is a follow-on; not built here.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from yadgar._shared.observability.observe import observe

# Re-use the directory-skip logic from seed._scan to avoid duplication.
from yadgar.core.seed._scan import _should_skip_dir

logger = logging.getLogger(__name__)

# Max file size for AST parsing (256KB — larger than seed's 64KB since we need full AST)
_MAX_PARSE_SIZE = 256 * 1024

# repo_wiki-local extra ignores, LAYERED on top of seed._should_skip_dir.
# NOT added to seed._SKIP_DIRS (that frozenset is shared with the seed scanner).
_EXTRA_SKIP_DIRS = frozenset({"migrations"})
# Path suffixes (relative-path fragments) to skip.
_EXTRA_SKIP_PATH_SUFFIXES = ("alembic/versions",)
# Filename globs to skip (generated code, stubs).
_EXTRA_SKIP_FILE_GLOBS = ("*_pb2.py", "*_pb2_grpc.py", "*.pyi")


@dataclass
class FunctionRecord:
    """Extracted function or method definition."""

    name: str
    qualname: str  # e.g. "MyClass.my_method"
    signature: str  # e.g. "def foo(x: int, y: str = 'hi') -> bool"
    docstring: str | None
    is_method: bool
    is_classmethod: bool
    is_staticmethod: bool
    is_async: bool
    lineno: int


@dataclass
class ClassRecord:
    """Extracted class definition."""

    name: str
    bases: list[str]  # base class names as strings
    docstring: str | None
    methods: list[FunctionRecord] = field(default_factory=list)
    lineno: int = 0


@dataclass
class ModuleRecord:
    """All extracted structure for one Python module."""

    module_path: str  # repo-relative path, e.g. "yadgar/retrieval/core.py"
    module_name: str  # dotted name, e.g. "yadgar.retrieval.core"
    docstring: str | None
    functions: list[FunctionRecord] = field(default_factory=list)
    classes: list[ClassRecord] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # top-level import names
    all_exports: list[str] = field(default_factory=list)  # __all__ entries (API re-exports)
    parse_error: str | None = None
    # False only when a successfully-parsed module has NO functions, NO classes,
    # NO docstring, and NO __all__ (a content-less stub → no page).  Parse-error
    # and too-large records keep the default True; their inclusion is governed by
    # skip_parse_errors, not by emptiness.
    has_content: bool = True


@observe(tier="stage")
def _arg_str(arg: ast.arg, default: ast.expr | None = None) -> str:
    """Render one argument node as a string, with optional annotation + default."""
    part = arg.arg
    if arg.annotation:
        part += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        part += f" = {ast.unparse(default)}"
    return part


@observe(tier="stage")
def _build_positional_parts(args: ast.arguments) -> list[str]:
    """Build arg-part list for positional-only and regular args (before *args)."""
    parts: list[str] = []
    posonlyargs = getattr(args, "posonlyargs", [])
    for arg in posonlyargs:
        parts.append(_arg_str(arg))
    if posonlyargs:
        parts.append("/")
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        default_idx = i - defaults_offset
        default = args.defaults[default_idx] if default_idx >= 0 else None
        parts.append(_arg_str(arg, default))
    return parts


@observe(tier="stage")
def _build_kwonly_parts(args: ast.arguments) -> list[str]:
    """Build arg-part list for *args / keyword-only args / **kwargs."""
    parts: list[str] = []
    if args.vararg:
        part = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            part += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(part)
    elif args.kwonlyargs:
        parts.append("*")
    for i, arg in enumerate(args.kwonlyargs):
        kw_default = args.kw_defaults[i]
        parts.append(_arg_str(arg, kw_default))
    if args.kwarg:
        part = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            part += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(part)
    return parts


@observe(tier="stage")
def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str) -> str:
    """Reconstruct a readable function signature from AST (no source read needed)."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    arg_parts = _build_positional_parts(node.args) + _build_kwonly_parts(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {qualname}({', '.join(arg_parts)}){ret}"


@observe(tier="stage")
def _extract_docstring(node: ast.AST) -> str | None:
    """Return the first string literal in a function/class/module body, or None."""
    body = getattr(node, "body", [])
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr):
        return None
    val = first.value
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        return val.value.strip()
    return None


@observe(tier="stage")
def _decorator_name_from(dec: ast.expr) -> str | None:
    """Extract the simple name from a single decorator node, or None."""
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Call):
        return _decorator_name_from(dec.func)
    return None


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return simple decorator names (ignoring arguments)."""
    result = {_decorator_name_from(dec) for dec in node.decorator_list}
    return result - {None}  # type: ignore[return-value]


@observe(tier="stage")
def _extract_imports(tree: ast.Module) -> list[str]:
    """Extract top-level imported module/name strings."""
    names: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                names.append(alias.asname or f"{module}.{alias.name}".lstrip("."))
    return names


@observe(tier="stage")
def _assign_targets(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """Return (targets, value) for an Assign/AnnAssign node, else ([], None)."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


@observe(tier="stage")
def _string_literals(value: ast.expr | None) -> list[str]:
    """Return string-constant entries from a list/tuple literal, else []."""
    if not isinstance(value, (ast.List, ast.Tuple)):
        return []
    return [
        elt.value
        for elt in value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


@observe(tier="stage")
def _extract_all_exports(tree: ast.Module) -> list[str]:
    """Extract string entries from a top-level ``__all__ = [...]`` assignment.

    Handles list/tuple literals of string constants.  Non-literal or dynamically
    built ``__all__`` (e.g. concatenation) yields an empty list — acceptable, the
    only consumer is the emptiness check (a re-export module still has functions
    or the literal here in the common case).
    """
    for node in ast.iter_child_nodes(tree):
        targets, value = _assign_targets(node)
        is_all = any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)
        if is_all:
            return _string_literals(value)
    return []


@observe(tier="stage")
def _visit_class(cls_node: ast.ClassDef) -> ClassRecord:
    """Extract a ClassRecord from a ClassDef AST node."""
    bases = []
    for base in cls_node.bases:
        try:
            bases.append(ast.unparse(base))
        except Exception:
            bases.append("?")

    cls_rec = ClassRecord(
        name=cls_node.name,
        bases=bases,
        docstring=_extract_docstring(cls_node),
        lineno=cls_node.lineno,
    )
    for item in cls_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decs = _decorator_names(item)
        qualname = f"{cls_node.name}.{item.name}"
        sig = _build_signature(item, qualname)
        method = FunctionRecord(
            name=item.name,
            qualname=qualname,
            signature=sig,
            docstring=_extract_docstring(item),
            is_method=True,
            is_classmethod="classmethod" in decs,
            is_staticmethod="staticmethod" in decs,
            is_async=isinstance(item, ast.AsyncFunctionDef),
            lineno=item.lineno,
        )
        cls_rec.methods.append(method)
    return cls_rec


@observe(tier="stage")
def scan_python_module(path: Path, repo_root: Path) -> ModuleRecord:
    """Parse one Python file and return a ModuleRecord.

    path: absolute path to the .py file.
    repo_root: absolute path to the repository root (for repo-relative path).
    """
    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    module_name = rel_path.replace(os.sep, ".").removesuffix(".py")
    if module_name.endswith(".__init__"):
        module_name = module_name.removesuffix(".__init__")

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    if size > _MAX_PARSE_SIZE:
        return ModuleRecord(
            module_path=rel_path,
            module_name=module_name,
            docstring=None,
            parse_error=f"file too large ({size} bytes > {_MAX_PARSE_SIZE})",
        )

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return ModuleRecord(
            module_path=rel_path,
            module_name=module_name,
            docstring=None,
            parse_error=f"SyntaxError: {exc}",
        )
    except Exception as exc:
        return ModuleRecord(
            module_path=rel_path,
            module_name=module_name,
            docstring=None,
            parse_error=f"ParseError: {exc}",
        )

    functions: list[FunctionRecord] = []
    classes: list[ClassRecord] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _build_signature(node, node.name)
            fn = FunctionRecord(
                name=node.name,
                qualname=node.name,
                signature=sig,
                docstring=_extract_docstring(node),
                is_method=False,
                is_classmethod=False,
                is_staticmethod=False,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                lineno=node.lineno,
            )
            functions.append(fn)
        elif isinstance(node, ast.ClassDef):
            classes.append(_visit_class(node))

    module_docstring = _extract_docstring(tree)
    all_exports = _extract_all_exports(tree)
    has_content = bool(functions or classes or module_docstring or all_exports)

    return ModuleRecord(
        module_path=rel_path,
        module_name=module_name,
        docstring=module_docstring,
        functions=functions,
        classes=classes,
        imports=_extract_imports(tree),
        all_exports=all_exports,
        parse_error=None,
        has_content=has_content,
    )


# Extension → extractor REGISTRY (multi-language seam).  ModuleRecord is
# language-neutral; adding a language later = one dict entry + one function.
# Only .py is wired now (Go/TS deferred — #101).
_EXTRACTOR_REGISTRY: dict[str, Callable[[Path, Path], ModuleRecord]] = {
    ".py": scan_python_module,
}


@observe(tier="stage")
def _skip_by_extra_dir(rel_dir: str) -> bool:
    """True if rel_dir lies under a repo_wiki-local extra-skip dir or path suffix."""
    norm = rel_dir.replace("\\", "/")
    parts = norm.split("/") if norm else []
    if any(p in _EXTRA_SKIP_DIRS for p in parts):
        return True
    return any(suffix in norm for suffix in _EXTRA_SKIP_PATH_SUFFIXES)


@observe(tier="stage")
def _is_test_dir(rel_dir: str) -> bool:
    """True if any path segment of rel_dir looks like a test directory."""
    parts = rel_dir.replace("\\", "/").split("/") if rel_dir else []
    return any(p.startswith("test") or p == "tests" for p in parts)


@observe(tier="stage")
def _is_scannable_file(fname: str) -> bool:
    """True if fname has a registered extractor, is not glob-skipped, and is importable."""
    suffix = Path(fname).suffix
    if suffix not in _EXTRACTOR_REGISTRY:
        return False
    if any(fnmatch.fnmatch(fname, pat) for pat in _EXTRA_SKIP_FILE_GLOBS):
        return False
    # Only page IMPORTABLE files — a non-identifier stem is a hook SCRIPT
    # (hyphenated), never importable; paging it collides with its underscore
    # twin's slug (data loss on wiki_add).
    stem = fname[: -len(suffix)] if suffix else fname
    return stem.isidentifier()


@observe(tier="stage")
def _collect_candidates(root: Path, include_tests: bool) -> list[Path]:
    """Walk root, applying dir/name/identifier/registry filters, return candidate files."""
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if not _should_skip_dir(d) and d not in _EXTRA_SKIP_DIRS
        )
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        if (not include_tests and _is_test_dir(rel_dir)) or _skip_by_extra_dir(rel_dir):
            dirnames[:] = []
            continue
        for fname in sorted(filenames):
            if _is_scannable_file(fname):
                candidates.append(Path(dirpath) / fname)
    return candidates


@observe(tier="stage")
def _gitignored_paths(root: Path, candidates: list[Path]) -> set[str]:
    """Return the subset of candidate paths that git considers ignored.

    Batched via a single ``git check-ignore --stdin`` call.  Outside a git repo
    (exit 128) or on any error, returns an empty set (no filtering) — scanning a
    non-git directory must still work.
    """
    if not candidates:
        return set()
    try:
        stdin = "\n".join(str(p) for p in candidates)
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin"],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError) as exc:
        logger.debug("scan_repo: git check-ignore unavailable (%s) — no gitignore filter", exc)
        return set()
    # Exit 0 = some paths ignored; exit 1 = none ignored (normal); 128 = not a git repo.
    if proc.returncode not in (0, 1):
        return set()
    return {line for line in proc.stdout.splitlines() if line}


@observe(tier="stage")
def _extract_record(filepath: Path, root: Path) -> ModuleRecord | None:
    """Run the registered extractor for filepath; drop content-less stubs."""
    extractor = _EXTRACTOR_REGISTRY[filepath.suffix]
    try:
        rec = extractor(filepath, root)
    except Exception as exc:
        logger.warning("scan_repo: failed to scan %s: %s", filepath, exc)
        return None
    # Empty-page skip: content-less stub (successful parse, nothing to show).
    if not rec.has_content:
        return None
    return rec


@observe(tier="boundary")
def scan_repo(repo_root: str | Path, include_tests: bool = False) -> list[ModuleRecord]:
    """Walk a repository and return ModuleRecords for every scannable source file.

    Reuses _should_skip_dir from yadgar.seed._scan to stay consistent with
    the existing project scanner, then layers repo_wiki-local ignores on top:
      - extra skip dirs (migrations/) + path suffixes (alembic/versions) + file
        globs (*_pb2.py, *.pyi);
      - gitignore-aware exclusion (batched ``git check-ignore --stdin``);
      - importable-only: files whose stem is not a valid Python identifier
        (hyphenated hook SCRIPTS) are skipped — kills slug collisions at source;
      - empty-page skip: a successfully-parsed module with no functions, classes,
        docstring, or __all__ emits no record (has_content=False).

    Dispatch is per-suffix through _EXTRACTOR_REGISTRY (multi-language seam); a
    file whose suffix has no registered extractor yields no record.

    Test directories are excluded unless include_tests=True.
    Returns records sorted by module_path (deterministic, diffable).
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {repo_root}")

    candidates = _collect_candidates(root, include_tests)
    ignored = _gitignored_paths(root, candidates)

    records: list[ModuleRecord] = []
    for filepath in candidates:
        if str(filepath) in ignored:
            continue
        rec = _extract_record(filepath, root)
        if rec is not None:
            records.append(rec)

    records.sort(key=lambda r: r.module_path)
    return records
