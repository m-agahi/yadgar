#!/usr/bin/env python3
"""Model-id liveness lint — config default ↔ image bake ↔ code literal (task 0121).

WHY THIS EXISTS (the incident, 2026-08-01)
------------------------------------------
Asked what cross-encoder reranker yadgar runs, the answer twice came back
``cross-encoder/ms-marco-MiniLM-*``.  The user was right and the answer was
wrong, and nobody misread the code: ``config.py`` declares

    CROSS_ENCODER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    CROSS_ENCODER_ENABLED: bool = True  # FlashRank ONNX is fast enough for CPU

in the first place a reader looks, while the model actually loaded on the hot
path is ``GTE_RERANKER_MODEL`` (``cross-encoder/ettin-reranker-32m-v1``,
ADR-0104), a hundred lines further down.  ``ms-marco`` is real, live, and
reachable — but only as the DEGRADED second-tier fallback, and it is
deliberately not baked into ``Dockerfile.backend``, so inside the offline
backend container it scores zeros.

Nothing in the repo said so.  ``check_capability_coverage.py`` (I32) enumerates
settings but reads no defaults; ``check_registry_prose_liveness.py`` measures
IDENTIFIER death, and every identifier here is alive.  Both were green.

THE RULES
---------
**Rule 1 — every ``*_MODEL`` Settings default is baked, or allowlisted with a
rationale.**  Left side: AST-scan ``config.py``'s ``Settings`` body for fields
whose name ends in ``_MODEL`` with a string-constant default.  Right side: the
quoted ids inside ``Dockerfile.backend``'s ``RUN python -c`` bake blocks.
Compared on the bare id (after the last ``/``) so ``all-MiniLM-L6-v2`` matches
``sentence-transformers/all-MiniLM-L6-v2``.  A field that is neither baked nor
allowlisted FAILS — and the allowlist rationale is where the sentence whose
absence caused the incident now lives, under a stale-entry rule that stops it
rotting.

**Rule 2 — no orphan model-id literal in the ML-loading modules.**  Any string
literal shaped ``<org>/<name>`` (exactly one slash, no spaces) inside the scan
set must appear in the vocabulary rule 1 assembles (Settings defaults ∪ bake
list ∪ allowlist).  This is what catches a hardcoded inline fallback that has
drifted from the field it is meant to mirror.

THE SCAN SET IS THE DESIGN
--------------------------
Filtering by model-org prefix (``cross-encoder/``, ``BAAI/``, …) is a SILENT
coverage hole, not an FP source — a missed prefix never shows up when the guard
runs clean.  Measured: a four-prefix list would have missed ``nomic-ai/`` and
``mismayil/``, both present in this repo.  So the filter is by MODULE:

    scan set = { files under yadgar/** (excluding yadgar/tests/**) that
                 reference sentence_transformers or transformers }
             ∪ yadgar/backend/ml_client/**
             ∪ yadgar/backend/embed_service/**
             −  yadgar/_shared/config/config.py     # this file IS the vocabulary

Measured FP rate on the real tree: zero.  No MIME type (``application/json``),
no git ref (``origin/master``), no ``React/TSX`` reaches it, because none of
those live in an ML-loading module.  The naive whole-repo variant of rule 2's
regex picks up ~50 such hits and would need a MIME exclusion list; the module
narrowing removes the need for one.

═══════════════════════════════════════════════════════════════════════════════
THE CEILING — read this before trusting a green run
═══════════════════════════════════════════════════════════════════════════════
  * **Prose is not gated.**  Neither the FlashRank ``FIELD_META`` descs nor
    ``entrypoint-backend.sh``'s systemd claim — the two other halves of the same
    car — are visible here.  They were fixed by hand and nothing holds them.
  * **Bare-name model ids** (flashrank's ``ms-marco-MiniLM-L-12-v2``) escape
    rule 2: its pattern requires a slash by construction, and loosening it to
    bare names leaves no closed vocabulary to test against.
  * **Hardcoded ids outside the scan set** are invisible.  The
    ``embed_service/**`` clause exists specifically to keep
    ``embed_service_config.py``'s hand-synced Ettin fallback in scope.
  * **A field renamed away from the ``_MODEL`` suffix** escapes rule 1 entirely.
  * **Host stdio/daemon mode**, where baking is irrelevant — "not baked" is not
    "not usable", which is exactly why the escape hatch is an
    allowlist-with-rationale rather than a hard ban.
  * **A wrong claim about a correctly-baked model** — the same ceiling
    ``check_registry_prose_liveness.py`` documents for itself.
  * **A rationale that has gone false.**  The stale rule fires when a row's
    FIELD changes state (now baked, or deleted).  It does not re-check the
    rationale's premise: ``NLI_MODEL`` and ``COMET_MODEL`` are allowlisted on
    the grounds that their features are default-OFF, and nothing re-fires if
    ``NLI_RERANKING_ENABLED`` or ``COMET_ENRICHMENT_ENABLED`` later flips True
    while the weights stay unbaked.

COMPOSITION
-----------
This is ONE AXIS — *config default ↔ image bake ↔ code literal* — of the general
drift ratchet designed in ``docs/plans/drift-axis-sweep-2026-06-30.md`` (task
#0005), not a competing mechanism.  It is a standalone ``scripts/check_*.py`` in
the established idiom precisely so #0005 can absorb it as an axis implementation
rather than reconcile a second framework.

Allowlist: ``.model-id-allowlist.json`` — ``{"fields": {...}, "literals": {...}}``,
rationale >= 40 chars, STALE entry is a HARD ERROR.  Governance mirrors
``.registry-prose-allowlist.json`` (I32) and ``.complexity-allowlist.json`` (I30).

Usage:
  python scripts/check_model_id_liveness.py                 # check, exit 0/1
  python scripts/check_model_id_liveness.py --list-unbaked   # ignore the allowlist
  python scripts/check_model_id_liveness.py --repo-root /path

Exit codes:
  0  every *_MODEL default is baked or governed, and no orphan literal remains
  1  one or more UNBAKED-MODEL / ORPHAN-LITERAL / STALE / MALFORMED violations
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWLIST_NAME = ".model-id-allowlist.json"
_MIN_RATIONALE = 40

# A HuggingFace-shaped id: exactly one slash, no whitespace, conservative charset.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# Quoted literals inside Dockerfile.backend's `RUN python -c` bake blocks.
_QUOTED_RE = re.compile(r"'([^'\n]+)'|\"([^\"\n]+)\"")

# The two package clauses of the scan set, plus the one subtraction.
_ALWAYS_SCAN = ("yadgar/backend/ml_client", "yadgar/backend/embed_service")
_NEVER_SCAN = ("yadgar/_shared/config/config.py",)

# The module tokens whose presence makes a file an ML-loading module.
_ML_REF_RE = re.compile(r"sentence[-_]transformers|(?<![-\w])transformers\b")


def bare(model_id: str) -> str:
    """The id after the last ``/`` — ``org/name`` and ``name`` compare equal."""
    return model_id.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Rule 1 left side — the *_MODEL Settings defaults
# ---------------------------------------------------------------------------
def enumerate_model_settings(config_file: Path) -> dict[str, str]:
    """``{FIELD_NAME: default}`` for every ``*_MODEL`` field with a str default.

    Empty-string defaults are SKIPPED, not returned: ``IMPLICIT_EMBEDDING_MODEL
    = ""`` is a sentinel for an unimplemented feature (config.py's "CONFIG-ONLY
    pending future DualCSE implementation"), not a model id.  Without this the
    guard fires spuriously on day one.

    AST walk, no import — mirrors ``check_capability_coverage.enumerate_settings``.
    """
    try:
        tree = ast.parse(config_file.read_text(encoding="utf-8"))
    except (SyntaxError, OSError) as exc:  # pragma: no cover - defensive
        print(f"WARNING: could not parse {config_file}: {exc}", file=sys.stderr)
        return {}
    fields: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                name = stmt.targets[0].id
            else:
                continue
            if not (name.isupper() and name.endswith("_MODEL")):
                continue
            default = stmt.value
            if not (isinstance(default, ast.Constant) and isinstance(default.value, str)):
                continue
            if not default.value.strip():
                continue  # sentinel, not a model id
            fields[name] = default.value
    return fields


# ---------------------------------------------------------------------------
# Rule 1 right side — what the image bakes
# ---------------------------------------------------------------------------
def enumerate_baked_ids(dockerfile: Path) -> set[str]:
    """Quoted, slash-or-model-shaped literals inside the Dockerfile's bake blocks.

    Deliberately permissive on the left of the slash: the bake lines call
    ``SentenceTransformer('all-MiniLM-L6-v2')`` (bare) alongside
    ``CrossEncoder('cross-encoder/ettin-reranker-32m-v1')`` (org-qualified), so
    the extractor keeps both shapes and ``bare()`` normalises at compare time.
    """
    if not dockerfile.is_file():
        return set()
    baked: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not ("SentenceTransformer" in stripped or "CrossEncoder" in stripped):
            if "from_pretrained" not in stripped:
                continue
        for m in _QUOTED_RE.finditer(stripped):
            val = m.group(1) or m.group(2) or ""
            if not val or " " in val:
                continue
            if _MODEL_ID_RE.match(val) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", val):
                baked.add(bare(val))
    return baked


# ---------------------------------------------------------------------------
# Rule 2 — the scan set and its literals
# ---------------------------------------------------------------------------
def build_scan_set(repo_root: Path) -> list[Path]:
    """The ML-loading modules — see the module docstring for why it is by-module."""
    pkg = repo_root / "yadgar"
    if not pkg.is_dir():
        return []
    never = {repo_root / p for p in _NEVER_SCAN}
    always_dirs = [repo_root / p for p in _ALWAYS_SCAN]

    selected: set[Path] = set()
    for py in pkg.rglob("*.py"):
        rel = py.relative_to(repo_root)
        if "tests" in rel.parts:
            continue
        if py in never:
            continue
        if any(d in py.parents for d in always_dirs):
            selected.add(py)
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # fmt: skip  # pragma: no cover
            continue
        if _ML_REF_RE.search(src):
            selected.add(py)
    return sorted(selected)


def literals_in_source(src: str) -> set[str]:
    """Every string constant that is ENTIRELY a model-id-shaped token.

    Whole-literal, not substring/word match, and that is load-bearing.  Measured
    on the real tree: splitting literals on whitespace and testing each token
    turns ordinary prose into ~25 false positives — ``stdio/daemon``,
    ``gauge/counter``, ``intra/inter-op``, ``request/response`` all satisfy
    "one slash, no spaces" as WORDS.  Only a literal whose entire value is the
    id is a model reference; anything else is English.

    Docstrings are NOT excluded: a model id named as a whole docstring literal
    would still be drift, and excluding them buys nothing once the match is
    whole-literal (a docstring is never entirely a bare id).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover - defensive
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _MODEL_ID_RE.match(node.value.strip()):
                found.add(node.value.strip())
    return found


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def load_allowlist(path: Path) -> dict[str, dict]:
    """Load ``{"fields": {...}, "literals": {...}}``; absent file = empty."""
    if not path.is_file():
        return {"fields": {}, "literals": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f'{path.name} must be a JSON object with "fields" and "literals"')
    out: dict[str, dict] = {}
    for section in ("fields", "literals"):
        raw = data.get(section, {})
        if not isinstance(raw, dict):
            raise ValueError(f'{path.name}: "{section}" must be an object')
        out[section] = {k: v for k, v in raw.items() if not k.startswith("_")}
    return out


def _rationale_errors(section: str, entries: dict) -> list[str]:
    errors: list[str] = []
    for key, meta in sorted(entries.items()):
        rationale = (meta or {}).get("rationale", "") if isinstance(meta, dict) else ""
        if len(rationale.strip()) < _MIN_RATIONALE:
            errors.append(
                f"MALFORMED allowlist entry {section}.`{key}`: rationale must be >= "
                f"{_MIN_RATIONALE} chars (got {len(rationale.strip())}) — say WHY this "
                "model id legitimately ships unbaked"
            )
    return errors


def _rule1_errors(settings: dict[str, str], baked: set[str], allowed: dict) -> list[str]:
    """Rule 1 — every ``*_MODEL`` default is baked, or governed by an allowlist row."""
    errors: list[str] = []
    unbaked = {name for name, mid in settings.items() if bare(mid) not in baked}

    for name in sorted(allowed):
        if name not in settings:
            errors.append(
                f"STALE allowlist entry fields.`{name}`: no such `*_MODEL` Settings field "
                f"(renamed or deleted) — remove it from {ALLOWLIST_NAME}"
            )
        elif name not in unbaked:
            errors.append(
                f"STALE allowlist entry fields.`{name}`: the model is baked into "
                f"Dockerfile.backend now — remove it from {ALLOWLIST_NAME}"
            )

    for name in sorted(unbaked - set(allowed)):
        errors.append(
            f"UNBAKED-MODEL: `{name}` defaults to '{settings[name]}', which "
            "Dockerfile.backend does not bake. In the offline backend container that "
            "model cannot load. Either bake it, or add a governed entry to "
            f"{ALLOWLIST_NAME} stating what the field really is and which field holds "
            "the live model."
        )
    return errors


def _collect_scan_set_literals(repo_root: Path) -> dict[str, str]:
    """``{literal: first file that contains it}`` across the scan set."""
    seen: dict[str, str] = {}
    for path in build_scan_set(repo_root):
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # fmt: skip  # pragma: no cover
            continue
        for lit in literals_in_source(src):
            seen.setdefault(lit, str(path.relative_to(repo_root)))
    return seen


def _rule2_errors(seen: dict[str, str], vocabulary: set[str], allowed: dict) -> list[str]:
    """Rule 2 — no model-id literal in the scan set outside the known vocabulary."""
    errors: list[str] = []
    for lit in sorted(allowed):
        if lit not in seen:
            errors.append(
                f"STALE allowlist entry literals.`{lit}`: no ML-loading module contains "
                f"that literal any more — remove it from {ALLOWLIST_NAME}"
            )

    for lit, where in sorted(seen.items()):
        if lit in allowed or bare(lit) in vocabulary:
            continue
        errors.append(
            f"ORPHAN-LITERAL: {where} hardcodes '{lit}', which is neither a `*_MODEL` "
            "Settings default nor baked into Dockerfile.backend. A hand-synced copy of a "
            "model id drifts silently — point it at the Settings field, or add a governed "
            f"entry to {ALLOWLIST_NAME}."
        )

    return errors


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------
def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    if repo_root is None:
        repo_root = _REPO_ROOT

    config_file = repo_root / "yadgar" / "_shared" / "config" / "config.py"
    if not config_file.is_file():
        return [f"config.py not found at {config_file}"]

    settings = enumerate_model_settings(config_file)
    baked = enumerate_baked_ids(repo_root / "Dockerfile.backend")

    try:
        allowlist = load_allowlist(repo_root / ALLOWLIST_NAME)
    except ValueError as exc:
        return [f"MALFORMED allowlist: {exc}"]

    # Allowlist integrity FIRST — a governance failure is hard regardless of the
    # liveness verdict (same posture as I30's and I32's allowlist checks).
    errors: list[str] = []
    errors += _rationale_errors("fields", allowlist["fields"])
    errors += _rationale_errors("literals", allowlist["literals"])

    errors += _rule1_errors(settings, baked, allowlist["fields"])

    vocabulary = {bare(m) for m in settings.values()} | baked
    vocabulary |= {bare(m) for m in allowlist["literals"]}
    errors += _rule2_errors(
        _collect_scan_set_literals(repo_root), vocabulary, allowlist["literals"]
    )

    return errors


# ---------------------------------------------------------------------------
# Report helpers — used by the tests to assert on SETS, not on message text
# ---------------------------------------------------------------------------
def unbaked_fields(errors: list[str]) -> set[str]:
    """Field names named by UNBAKED-MODEL violations."""
    return {m.group(1) for e in errors if (m := re.search(r"UNBAKED-MODEL: `([^`]+)`", e))}


def orphan_literals(errors: list[str]) -> set[str]:
    """Literals named by ORPHAN-LITERAL violations."""
    return {
        m.group(1)
        for e in errors
        if (m := re.search(r"ORPHAN-LITERAL: \S+ hardcodes '([^']+)'", e))
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model-id liveness lint (task 0121)")
    parser.add_argument(
        "--list-unbaked",
        action="store_true",
        help="Print every unbaked *_MODEL field and orphan literal (ignores the allowlist)",
    )
    parser.add_argument("--repo-root", default=None, help="Override repo root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.list_unbaked:
        settings = enumerate_model_settings(
            repo_root / "yadgar" / "_shared" / "config" / "config.py"
        )
        baked = enumerate_baked_ids(repo_root / "Dockerfile.backend")
        vocabulary = {bare(m) for m in settings.values()} | baked
        print(f"=== baked ids ({len(baked)}) ===")
        for b in sorted(baked):
            print(f"  {b}")
        print(f"=== *_MODEL fields ({len(settings)}) ===")
        for name, mid in sorted(settings.items()):
            print(f"  {'BAKED  ' if bare(mid) in baked else 'UNBAKED'}  {name} = {mid}")
        print("=== model-id literals in the scan set ===")
        for path in build_scan_set(repo_root):
            for lit in sorted(literals_in_source(path.read_text(encoding="utf-8"))):
                mark = "in-vocab" if bare(lit) in vocabulary else "ORPHAN  "
                print(f"  {mark}  {path.relative_to(repo_root)}: {lit}")
        return 0

    errors = check(repo_root)
    if errors:
        print("model-id liveness lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("model-id liveness lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
