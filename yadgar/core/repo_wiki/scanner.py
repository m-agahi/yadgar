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
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from yadgar._shared.observability.observe import observe

# Re-use the directory-skip logic from seed._scan to avoid duplication.
from yadgar.core.seed._scan import _should_skip_dir

logger = logging.getLogger(__name__)

# Max file size for AST parsing (256KB — larger than seed's 64KB since we need full AST)
_MAX_PARSE_SIZE = 256 * 1024


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
    parse_error: str | None = None


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

    return ModuleRecord(
        module_path=rel_path,
        module_name=module_name,
        docstring=_extract_docstring(tree),
        functions=functions,
        classes=classes,
        imports=_extract_imports(tree),
        parse_error=None,
    )


@observe(tier="boundary")
def scan_repo(repo_root: str | Path, include_tests: bool = False) -> list[ModuleRecord]:
    """Walk a repository and return ModuleRecords for every Python file.

    Reuses _should_skip_dir from yadgar.seed._scan to stay consistent with
    the existing project scanner.  Test directories are excluded unless
    include_tests=True.

    Returns records sorted by module_path (deterministic, diffable).
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {repo_root}")

    records: list[ModuleRecord] = []

    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        # Prune directories in-place so os.walk won't descend into them
        dirnames[:] = sorted(d for d in dirnames if not _should_skip_dir(d))

        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""

        # Optionally skip test directories
        if not include_tests:
            parts = rel_dir.replace("\\", "/").split("/") if rel_dir else []
            if any(p.startswith("test") or p == "tests" for p in parts):
                dirnames[:] = []
                continue

        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            filepath = Path(dirpath) / fname
            try:
                rec = scan_python_module(filepath, root)
                records.append(rec)
            except Exception as exc:
                logger.warning("scan_repo: failed to scan %s: %s", filepath, exc)

    records.sort(key=lambda r: r.module_path)
    return records
