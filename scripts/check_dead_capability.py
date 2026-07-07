#!/usr/bin/env python3
"""I29 — Edge dead-capability lint (EDGE_CONTRACT domain).

Enforces invariant I29 (leverage-completeness): every graph edge type produced
or registered in code has a declared role in docs/EDGE_CONTRACT.md, and no
edge type marked `drop` is still produced.

Three failure modes:
  ORPHAN  — produced in code or in EDGE_TYPES registry, but absent from
             EDGE_CONTRACT (no declared role → reviewer reject).
  DROP    — declared `drop` in EDGE_CONTRACT but still produced in code
             (dead capability that should have been GC'd in 5.54.4+).
  STALE   — in EDGE_CONTRACT but no longer produced in code or registry
             (contract row refers to a type that no longer exists).

Scope: EDGES ONLY (EDGE_CONTRACT domain). Node types (memory/wiki/entity)
are NOT edge types and are explicitly excluded.

How produced types are collected:
  1. AST scan of graph_api.py: walk ast.Dict nodes whose keys include BOTH
     "source" and "target" (edge shape). Extract "type" value when it is a
     Constant str. This handles all literal edge-type strings.
  2. EDGE_TYPES registry keys from yadgar/viz_meta.py (imported at runtime
     OR parsed statically). This captures entity typed-relations emitted
     dynamically via `rel_type` variable (line 335 of graph_api.py) — those
     have no string literal but ARE registry keys.
  The union of (1) and (2) = the full produced/registered set.

How contract roles are collected:
  Parse docs/EDGE_CONTRACT.md. The table MUST have one row per type with the
  type as the first cell (backtick-quoted or plain, inside **..** or not) and
  the role ("retrieval", "display", or "drop") somewhere in the row.
  Each type maps to exactly one role.

Usage:
  python scripts/check_dead_capability.py            # check, exit 0/1
  python scripts/check_dead_capability.py --list-all # list all + status
  python scripts/check_dead_capability.py --repo-root /path/to/repo

Exit codes:
  0  no orphans, no drop-still-produced, no stale rows
  1  one or more violations found

"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent

# Node types — explicitly excluded from edge-type checks.
# These appear as `"type": "memory"` etc. in NODE dicts, not EDGE dicts.
_NODE_TYPES: frozenset[str] = frozenset({"memory", "wiki", "entity"})

# Roles recognised in EDGE_CONTRACT
_VALID_ROLES: frozenset[str] = frozenset({"retrieval", "display", "drop"})

# ---------------------------------------------------------------------------
# Collect produced edge types via AST scan of graph_api.py
# ---------------------------------------------------------------------------


def _ast_edge_types_from_file(src_file: Path) -> set[str]:
    """AST-walk src_file; return edge-type literals from edge-shaped dicts.

    Edge shape discriminator: dict must contain BOTH "source" and "target" keys.
    Only extracts "type" when its value is a Constant str — dynamic vars are
    covered by the EDGE_TYPES registry (see collect_produced_types).
    """
    try:
        tree = ast.parse(src_file.read_text(encoding="utf-8"))
    except (SyntaxError, OSError) as exc:
        print(f"WARNING: could not parse {src_file}: {exc}", file=sys.stderr)
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        # Extract string-keyed pairs
        pairs: dict[str, ast.expr] = {}
        for k, v in zip(node.keys, node.values, strict=False):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                pairs[k.value] = v
        # Must have both "source" and "target" to be an edge dict
        if "source" not in pairs or "target" not in pairs:
            continue
        type_val = pairs.get("type")
        if type_val is None:
            continue
        if isinstance(type_val, ast.Constant) and isinstance(type_val.value, str):
            t = type_val.value
            if t not in _NODE_TYPES:
                found.add(t)
    return found


def collect_produced_types(repo_root: Path) -> set[str]:
    """Collect all produced/registered edge types.

    Union of:
      (a) Literal "type" strings in edge-shaped dicts in graph_api.py
      (b) EDGE_TYPES registry keys from viz_meta.py (static parse)
    """
    graph_api = repo_root / "yadgar" / "core" / "graph_api.py"
    viz_meta = repo_root / "yadgar" / "core" / "viz_meta.py"

    literal_types = _ast_edge_types_from_file(graph_api)
    if viz_meta.exists():
        literal_types |= _ast_edge_types_from_file(viz_meta)

    registry_types = _parse_edge_types_registry(viz_meta)

    return literal_types | registry_types


def _edge_types_dict_value(node: ast.AST) -> ast.Dict | None:
    """Return the dict value of an EDGE_TYPES assignment node, or None.

    Handles both plain assignment (ast.Assign) and annotated assignment
    (ast.AnnAssign), which is what viz_meta.py uses:
      EDGE_TYPES: dict[str, dict] = { ... }
    """
    if isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "EDGE_TYPES" and isinstance(node.value, ast.Dict):
                return node.value
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.target.id == "EDGE_TYPES":
            if isinstance(node.value, ast.Dict):
                return node.value
    return None


def _parse_edge_types_registry(viz_meta: Path) -> set[str]:
    """Parse EDGE_TYPES dict keys from viz_meta.py via AST."""
    if not viz_meta.exists():
        print(f"WARNING: viz_meta.py not found: {viz_meta}", file=sys.stderr)
        return set()
    try:
        tree = ast.parse(viz_meta.read_text(encoding="utf-8"))
    except (SyntaxError, OSError) as exc:
        print(f"WARNING: could not parse {viz_meta}: {exc}", file=sys.stderr)
        return set()

    for node in ast.walk(tree):
        d = _edge_types_dict_value(node)
        if d is None:
            continue
        return {k.value for k in d.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


# ---------------------------------------------------------------------------
# Parse EDGE_CONTRACT.md
# ---------------------------------------------------------------------------

# Matches a markdown table row: | ... | ... | ... |
# The first cell is the edge type (may be wrapped in `backticks`, **bold**,
# or **`both`**). The row must contain a role keyword.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")

# Strip markdown formatting: backticks, bold, inline code
_STRIP_MD = re.compile(r"[`*_]")


def _extract_types_from_cell(cell: str) -> list[str]:
    """Extract one or more edge type identifiers from a table first-cell string.

    Handles single-type cells:
      `co_occurrence`
      **transition**
      **`transition`**
      transition

    Handles multi-type cells (entity typed-relations row):
      **entity typed-relations** (`co_occurrence`, `imports`, `calls`, ...)

    Returns empty list for header separators or non-type cells.
    Returns a list with one or more type strings.
    """
    cell = cell.strip()
    # Skip separator rows (e.g. |---|---|)
    if re.match(r"^[-:| ]+$", cell):
        return []
    # Skip header rows
    if re.search(r"edge\s*/\s*relation|edge type", cell, re.IGNORECASE):
        return []

    # Collect ALL backtick-quoted identifiers that look like edge types
    # (lowercase letters and underscores only — filters out file paths etc.)
    bt_types = re.findall(r"`([a-z][a-z_]*)`", cell)
    # Filter out known non-types (file paths, method names with dots etc.)
    bt_types = [t for t in bt_types if re.match(r"^[a-z][a-z_]*$", t) and t not in _NODE_TYPES]
    if bt_types:
        return bt_types

    # Bold single identifier: **transition**
    bold_match = re.search(r"\*\*([a-z][a-z_]*)\*\*", cell)
    if bold_match:
        return [bold_match.group(1)]

    # Plain identifier (no spaces, only lowercase + underscores)
    plain = _STRIP_MD.sub("", cell).strip()
    if re.match(r"^[a-z][a-z_]*$", plain) and plain not in _NODE_TYPES:
        return [plain]

    return []


def _extract_role_from_row(cells: list[str]) -> str | None:
    """Extract role from the 5th cell (index 4) — the TARGET-role column.

    Table schema (EDGE_CONTRACT.md):
      col 0: Edge / relation
      col 1: Producer
      col 2: Stored?
      col 3: Consumer TODAY
      col 4: TARGET role   ← we want this
      col 5: Action

    Scanning the full row text fails because the Action/Consumer columns
    mention role words in negations ("not fed to retrieval", "do NOT wire to
    retrieval") that should not determine the declared role.
    Falls back to full-row scan when the row has fewer than 5 cells.
    """
    # Prefer the dedicated role column (index 4)
    if len(cells) >= 5:
        role_cell = cells[4].lower()
        for role in _VALID_ROLES:
            if role in role_cell:
                return role
    # Fallback: scan first cell only (some rows are abbreviated)
    first_cell = cells[0].lower() if cells else ""
    for role in _VALID_ROLES:
        if role in first_cell:
            return role
    return None


def parse_contract(contract_file: Path) -> dict[str, str]:
    """Parse docs/EDGE_CONTRACT.md; return {edge_type: role}.

    Requirement: one row per type. The type is in the first cell; the role
    keyword appears somewhere in the row.
    """
    if not contract_file.exists():
        print(f"ERROR: EDGE_CONTRACT.md not found: {contract_file}", file=sys.stderr)
        return {}

    contract: dict[str, str] = {}
    for lineno, line in enumerate(contract_file.read_text(encoding="utf-8").splitlines(), 1):
        row_m = _TABLE_ROW_RE.match(line)
        if not row_m:
            continue
        # We're in a table row
        raw_cells = [c.strip() for c in row_m.group(1).split("|")]

        if not raw_cells:
            continue

        first_cell = raw_cells[0]

        # Detect and skip separator rows
        if re.match(r"^[-:| ]+$", first_cell):
            continue

        edge_types = _extract_types_from_cell(first_cell)
        if not edge_types:
            continue

        role = _extract_role_from_row(raw_cells)
        if role is None:
            print(
                f"WARNING: EDGE_CONTRACT.md line {lineno}: "
                f"type(s) {edge_types!r} have no recognisable role in row — skipping",
                file=sys.stderr,
            )
            continue

        for edge_type in edge_types:
            if edge_type in contract and contract[edge_type] != role:
                print(
                    f"WARNING: EDGE_CONTRACT.md: duplicate row for '{edge_type}' "
                    f"with conflicting roles ({contract[edge_type]!r} vs {role!r}) — using first",
                    file=sys.stderr,
                )
                continue
            contract[edge_type] = role

    return contract


# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------


def check(
    repo_root: Path | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Run the I29 edge dead-capability check.

    Returns (orphans, drop_still_produced, stale_contract):
      orphans            — produced/registered but absent from contract
      drop_still_produced — contract says `drop` but still produced
      stale_contract     — in contract but not produced or registered
    """
    if repo_root is None:
        repo_root = _REPO_ROOT

    produced = collect_produced_types(repo_root)
    contract = parse_contract(repo_root / "docs" / "EDGE_CONTRACT.md")

    contracted = set(contract.keys())

    orphans = sorted(produced - contracted)
    stale = sorted(contracted - produced)
    drop_still = sorted(t for t in produced if contract.get(t) == "drop")

    return orphans, drop_still, stale


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "I29 — edge dead-capability lint: every produced/registered edge type "
            "must have a declared role in EDGE_CONTRACT.md; drop types must be GC'd."
        ),
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="Print all produced types and their contract status, then exit 0.",
    )
    parser.add_argument(
        "--repo-root",
        metavar="DIR",
        default=None,
        help="Override repository root (default: auto-detect from script location).",
    )
    return parser


def _type_status(t: str, in_prod: bool, role: str | None) -> str:
    """Return a human-readable status string for a type in --list-all output."""
    if role is None:
        return "ORPHAN (not in contract)"
    if not in_prod:
        return f"STALE (role={role!r}; not produced)"
    if role == "drop":
        return f"DROP-STILL-PRODUCED (role={role!r})"
    return f"OK (role={role!r})"


def _print_list_all(produced: set[str], contract: dict[str, str]) -> None:
    """Print every type with its contract status (--list-all mode)."""
    contracted = set(contract.keys())
    for t in sorted(produced | contracted):
        in_prod = t in produced
        status = _type_status(t, in_prod, contract.get(t))
        marker = "[produced]" if in_prod else "[contract-only]"
        print(f"{t} {marker}: {status}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    produced = collect_produced_types(repo_root)
    contract = parse_contract(repo_root / "docs" / "EDGE_CONTRACT.md")

    if args.list_all:
        _print_list_all(produced, contract)
        return 0

    orphans, drop_still, stale = check(repo_root)

    failed = False

    for t in orphans:
        print(
            f"ORPHAN: edge type '{t}' is produced/registered in code "
            f"but has no row in docs/EDGE_CONTRACT.md (no declared role). "
            f"Add a row with role=retrieval|display|drop."
        )
        failed = True

    for t in drop_still:
        print(
            f"DROP-STILL-PRODUCED: edge type '{t}' is marked `drop` in "
            f"docs/EDGE_CONTRACT.md but is still produced in code. "
            f"Remove the compute path (GC it) or change the role."
        )
        failed = True

    for t in stale:
        print(
            f"STALE: edge type '{t}' has a row in docs/EDGE_CONTRACT.md "
            f"but is no longer produced in code or registered in EDGE_TYPES. "
            f"Remove the stale contract row."
        )
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
