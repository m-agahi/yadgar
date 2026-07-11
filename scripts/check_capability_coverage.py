#!/usr/bin/env python3
"""I32 — Capability-registry coverage lint (CAPABILITY_REGISTRY domain).

Enforces invariant I32 (catalogue-completeness): every enumerable capability
surface in the codebase has an entry in docs/CAPABILITY_REGISTRY.md. The
registry is the single source of truth for "what features/algorithms/behaviours
exist (wired or not), how they are reached, and their status."

WHAT THIS LINT GUARANTEES — and what it deliberately does NOT
------------------------------------------------------------
GUARANTEES (machine-checkable coverage):
  * every Settings field (config.py)         is referenced by >=1 entry
  * every MCP tool (@_tool decorator)        has an entry
  * every migration (def migration_NNN)      is referenced by >=1 entry
  * every BC-* row (BEHAVIOR_CONTRACT.md)     is referenced by >=1 entry
  * every entry's `status:` is a valid enum value
  * every file path in an entry's `refs:` resolves on disk
  * no entry references a surface item that no longer exists (STALE)

DOES NOT GUARANTEE (needs call-graph / runtime truth — out of scope):
  * that a `status: LIVE` capability is actually reachable at runtime
  * that a `status: DEAD` capability truly has no caller
  A green lint means "fully CATALOGUED", NOT "all statuses VERIFIED". Status
  accuracy is a human/review responsibility; the lint only enforces that a
  status value is present and well-formed.

Three failure classes (mirrors I29 orphan/stale):
  ORPHAN  — surface item (setting / tool / migration / BC) exists in code but
            is referenced by NO registry entry. The catalogue has a hole.
  STALE   — a registry entry references a surface item (setting / tool /
            migration / BC) that no longer exists in code. Dead catalogue row.
  MALFORMED — an entry has an invalid `status:` value, or a `refs:` file path
            that does not resolve on disk.

SYNERGY — dead-config audit (task #41): a Settings field referenced ONLY by
entries whose status is DEAD is dead configuration. `--list-orphans` and
`--audit-dead-config` surface these for the cleanup pass.

Usage:
  python scripts/check_capability_coverage.py             # check, exit 0/1
  python scripts/check_capability_coverage.py --list      # print all 4 sets
  python scripts/check_capability_coverage.py --json      # machine-readable sets
  python scripts/check_capability_coverage.py --repo-root /path

Exit codes:
  0  fully catalogued (no orphans, no stale, no malformed)
  1  one or more violations
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY = _REPO_ROOT / "docs" / "CAPABILITY_REGISTRY.md"
_CONTRACT = _REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md"
_CONFIG = _REPO_ROOT / "yadgar" / "_shared" / "config" / "config.py"
_MIGRATIONS = _REPO_ROOT / "yadgar" / "_shared" / "storage" / "migrations.py"
_TOOLS_DIR = _REPO_ROOT / "yadgar" / "core" / "server" / "tools"

# Valid status enum for a capability entry.
_VALID_STATUS: frozenset[str] = frozenset(
    {
        "LIVE",  # wired and reachable with default config
        "DORMANT",  # reachable in code but disabled by default config (a flag flip turns it on)
        "SHADOW",  # computed/recorded but its result is not acted on (e.g. surprise-gate shadow)
        "DEAD",  # no caller / unreachable — kept for archaeology or pending removal
        "CONFIG-ONLY",  # a knob that exists but whose consumer is dead/absent
    }
)


# ---------------------------------------------------------------------------
# Enumerators — the four authoritative surface sets (AST, no imports).
# These are THE spec: the registry must cover exactly what these return.
# ---------------------------------------------------------------------------
def enumerate_settings(config_file: Path = _CONFIG) -> set[str]:
    """Every field declared on the `Settings(BaseSettings)` class in config.py.

    AST-scan: annotated assignments (``NAME: type = default``) directly inside
    the Settings class body. No import — avoids pydantic side-effects in a lint.
    """
    try:
        tree = ast.parse(config_file.read_text(encoding="utf-8"))
    except (SyntaxError, OSError) as exc:  # pragma: no cover - defensive
        print(f"WARNING: could not parse {config_file}: {exc}", file=sys.stderr)
        return set()
    fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for stmt in node.body:
            # Annotated assignment: FOO: int = 1
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
            # Plain assignment: FOO = 1 (rare for pydantic but be safe)
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                name = stmt.targets[0].id
            else:
                continue
            # Settings fields are UPPER_SNAKE; skip dunders / model_config / private.
            if name.isupper() and not name.startswith("_"):
                fields.add(name)
    return fields


def enumerate_tools(tools_dir: Path = _TOOLS_DIR) -> set[str]:
    """Every MCP tool — a function decorated with ``@_tool`` in server/tools/.

    The tool's public name is the decorated function's name (FastMCP registers
    by function name unless an explicit name= is passed; we capture both).
    """
    tools: set[str] = set()
    for py in sorted(tools_dir.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - defensive: skip unparseable/unreadable file
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                name = _tool_decorator_name(dec)
                if name != "_tool":
                    continue
                # Explicit name= override on @_tool(name="x")?
                explicit = _tool_explicit_name(dec)
                tools.add(explicit or node.name)
    return tools


def _tool_decorator_name(dec: ast.expr) -> str | None:
    """Bare name of a decorator expression (@_tool or @_tool(...))."""
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Call):
        return _tool_decorator_name(dec.func)
    if isinstance(dec, ast.Attribute):
        return dec.attr
    return None


def _tool_explicit_name(dec: ast.expr) -> str | None:
    """Return the literal name= kwarg of @_tool(name="x"), if present."""
    if not isinstance(dec, ast.Call):
        return None
    for kw in dec.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, str):
                return kw.value.value
    return None


def enumerate_migrations(migrations_file: Path = _MIGRATIONS) -> set[str]:
    """Every ``def migration_NNN`` — returned as zero-padded NNN strings."""
    migs: set[str] = set()
    try:
        tree = ast.parse(migrations_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - defensive: skip unparseable/unreadable file
        return migs
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Migration funcs are module-level `_migration_NNN_<desc>` (or the
            # bare `migration_NNN` legacy form).
            m = re.match(r"^_?migration_(\d+)(?:_|$)", node.name)
            if m:
                migs.add(m.group(1))
    return migs


def enumerate_bc(contract_file: Path = _CONTRACT) -> set[str]:
    """Every BC-* identifier in BEHAVIOR_CONTRACT.md (reuse contract parser)."""
    bcs: set[str] = set()
    try:
        text = contract_file.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        return bcs
    # Reuse the same row grammar as check_contract_coverage.parse_rows:
    # list-item `- BC-XXX` or table `| BC-TN |`.
    for line in text.splitlines():
        m = re.match(r"^- (BC-[A-Za-z0-9]+)\b", line)
        if not m:
            m = re.match(r"^\|\s*(BC-T\d+)\s*\|", line)
        if m:
            bcs.add(m.group(1))
    return bcs


# ---------------------------------------------------------------------------
# Registry parser — extract structured fields from each capability entry.
# ---------------------------------------------------------------------------
# An entry begins with a level-3 heading: "### CAP-<DOMAIN>-<NNN> — <Name>".
# Fields are markdown list items: "- **settings:** `A`, `B`" etc.
_ENTRY_RE = re.compile(r"^###\s+(CAP-[A-Z0-9]+-\d+)\b", re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*-\s*\*\*(\w[\w-]*):\*\*\s*(.*)$")
# Backtick-quoted token: `FOO_BAR`
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def parse_registry(text: str) -> list[dict]:
    """Parse the registry into a list of entry dicts.

    Each dict: {id, status, settings:set, tools:set, migrations:set, bc:set,
    refs:list}. Field values are taken from the per-entry markdown list items.
    Backtick-quoted tokens are extracted as the canonical identifiers; bare
    `[]` / `none` / `-` means empty.
    """
    entries: list[dict] = []
    # Split the document into entry-blocks by the heading positions.
    matches = list(_ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        cap_id = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        entry: dict = {
            "id": cap_id,
            "status": None,
            "settings": set(),
            "tools": set(),
            "migrations": set(),
            "bc": set(),
            "refs": [],
        }
        for line in block.splitlines():
            fm = _FIELD_RE.match(line)
            if not fm:
                continue
            key = fm.group(1).lower()
            val = fm.group(2).strip()
            tokens = _BACKTICK_RE.findall(val)
            if key == "status":
                # status is the first bare WORD (may be backticked or plain)
                raw = tokens[0] if tokens else val.split()[0] if val.split() else ""
                entry["status"] = raw.strip().upper()
            elif key == "settings":
                entry["settings"].update(tokens)
            elif key == "tools":
                entry["tools"].update(tokens)
            elif key == "migrations":
                # migrations cited as `022` or bare 022 — normalise to digits
                for t in tokens or re.findall(r"\b(\d{2,4})\b", val):
                    entry["migrations"].add(t.lstrip("0").zfill(3) if t.strip("0") else t)
                    entry["migrations"].add(t)  # keep raw too for lenient match
            elif key == "bc":
                entry["bc"].update(re.findall(r"BC-[A-Za-z0-9]+", val))
            elif key == "refs":
                # refs are file paths (optionally ::node) inside backticks
                for t in tokens:
                    entry["refs"].append(t)
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _normalise_migrations(raw: set[str]) -> set[str]:
    """Normalise a set of migration tokens to canonical 3-digit form."""
    out: set[str] = set()
    for t in raw:
        d = re.sub(r"\D", "", t)
        if d:
            out.add(d.zfill(3))
    return out


def _ref_path_part(ref: str) -> str:
    return ref.split("::", 1)[0]


def _check_malformed(entries: list[dict], repo_root: Path) -> list[str]:
    """MALFORMED: invalid status enum or a `refs:` path that does not resolve."""
    errors: list[str] = []
    for e in entries:
        if e["status"] not in _VALID_STATUS:
            errors.append(
                f"MALFORMED: {e['id']} has status {e['status']!r} — "
                f"must be one of {sorted(_VALID_STATUS)}"
            )
        for ref in e["refs"]:
            path_part = _ref_path_part(ref)
            candidates = [repo_root / path_part, repo_root / "yadgar" / path_part]
            if not any(p.exists() for p in candidates):
                errors.append(f"MALFORMED: {e['id']} refs {ref!r} — file not found: {path_part}")
    return errors


def _check_coverage(covered: set[str], actual: set[str], kind: str, where: str) -> list[str]:
    """ORPHAN (actual not covered) + STALE (covered but not actual) for one surface."""
    errors: list[str] = []
    for x in sorted(actual - covered):
        errors.append(f"ORPHAN {kind}: `{x}` is in {where} but no registry entry references it")
    for x in sorted(covered - actual):
        errors.append(f"STALE {kind} ref: registry cites `{x}` but it's not in {where}")
    return errors


def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    if repo_root is None:
        repo_root = _REPO_ROOT
    registry_file = repo_root / "docs" / "CAPABILITY_REGISTRY.md"
    if not registry_file.is_file():
        return [f"registry not found at {registry_file}"]

    entries = parse_registry(registry_file.read_text(encoding="utf-8"))

    # union of what the registry covers, per surface
    cov: dict[str, set[str]] = {"settings": set(), "tools": set(), "migrations": set(), "bc": set()}
    for e in entries:
        cov["settings"] |= e["settings"]
        cov["tools"] |= e["tools"]
        cov["migrations"] |= _normalise_migrations(e["migrations"])
        cov["bc"] |= e["bc"]

    # the four authoritative surfaces
    settings = enumerate_settings(repo_root / "yadgar" / "_shared" / "config" / "config.py")
    tools = enumerate_tools(repo_root / "yadgar" / "core" / "server" / "tools")
    migrations = _normalise_migrations(
        enumerate_migrations(repo_root / "yadgar" / "_shared" / "storage" / "migrations.py")
    )
    bcs = enumerate_bc(repo_root / "docs" / "BEHAVIOR_CONTRACT.md")

    errors = _check_malformed(entries, repo_root)
    errors += _check_coverage(cov["settings"], settings, "setting", "config.py")
    errors += _check_coverage(cov["tools"], tools, "tool", "the @_tool surface")
    errors += _check_coverage(cov["migrations"], migrations, "migration", "migrations.py")
    errors += _check_coverage(cov["bc"], bcs, "behaviour", "BEHAVIOR_CONTRACT")
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _collect_sets(repo_root: Path) -> dict:
    return {
        "settings": sorted(
            enumerate_settings(repo_root / "yadgar" / "_shared" / "config" / "config.py")
        ),
        "tools": sorted(enumerate_tools(repo_root / "yadgar" / "core" / "server" / "tools")),
        "migrations": sorted(
            _normalise_migrations(
                enumerate_migrations(repo_root / "yadgar" / "_shared" / "storage" / "migrations.py")
            )
        ),
        "bc": sorted(enumerate_bc(repo_root / "docs" / "BEHAVIOR_CONTRACT.md")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="I32 — capability-registry coverage lint")
    parser.add_argument("--list", action="store_true", help="Print the 4 surface sets and counts")
    parser.add_argument("--json", action="store_true", help="Print the 4 surface sets as JSON")
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.json:
        print(json.dumps(_collect_sets(repo_root), indent=2))
        return 0
    if args.list:
        sets = _collect_sets(repo_root)
        for name, items in sets.items():
            print(f"\n=== {name} ({len(items)}) ===")
            for it in items:
                print(it)
        return 0

    errors = check(repo_root)
    if errors:
        print("CAPABILITY_REGISTRY coverage lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("CAPABILITY_REGISTRY coverage lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
