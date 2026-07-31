"""I25b: config declared-default vs code-default agreement.

I25 (``test_config_three_way_sync.py``) checks that every Settings field is
PRESENT across config.py / config_yaml.py / config_registry.py — or allowlisted.
It never compares the *values*.  That gap let
``ConfigEntry("YADGAR_BACKEND_VOLUME", "yadgar-backend-data", ...)`` sit in the
registry documenting a volume name no install surface has ever created (the code
default is ``yadgar-db-data``), invisible to review, until task 0103.

This module closes the gap: a DECLARED registry default that disagrees with the
CODE default it documents is a hard failure unless explicitly allowlisted with a
rationale (``yadgar/tests/config_default_mismatch_allowlist.txt``).

Generalises the task:0044 pattern (``test_vacuum_now.py::test_no_load_bearing_
code_default`` pins ONE registry default to ONE module constant) from a single
hand-written assertion to a whole-registry sweep.

Which code default is canonical
-------------------------------
Two classes of registry entry, resolved differently:

* **Settings-backed** — a ``Settings`` field of the same name exists.  Then
  ``Settings.model_fields[F].default`` IS the canonical code default; scattered
  ``os.environ.get`` readers are secondary consumers.  That matches the
  codebase's own env > yaml > default resolution (ADR-0014).
* **Registry-only** — no Settings field.  The code default lives in
  ``os.environ.get(NAME, <default>)`` / ``os.getenv(NAME, <default>)`` call
  sites, found by AST scan.  ``YADGAR_BACKEND_VOLUME`` is this class.

Structural exclusions (NOT allowlist entries — these cannot be compared at all):

* Registry entries whose declared default is a computed expression rather than a
  literal (``str(_paths.DATA_DIR)`` and friends).  There is no static value to
  compare against.
* ``yadgar/_shared/paths/paths.py`` — the derived-constant resolver module.  Its
  ``os.environ.get(X, "").strip()`` calls are override PROBES ("did the operator
  set this?"), not default declarations; the real default is the value computed
  in the else-branch.

Coverage boundary, stated honestly rather than papered over
------------------------------------------------------------
The value check reaches the 239 Settings-backed entries plus the registry-only
entries that have a statically-resolvable env-get default — not all 282
registry entries.  Specifically NOT covered:

* 3 entries whose declared default is a computed expression (excluded above).
* 13 registry-only entries with no ``os.environ.get(NAME, <literal>)`` call
  site at all (as of task:0103 — ``SURREAL_USER``, the ``*_LOG_FILE_PATH`` and
  ``*_LOG_RATE_LIMIT_*`` pairs, ``YADGAR_BACKEND_EMBED_URL``,
  ``YADGAR_BACKEND_METRICS_URL``, ``YADGAR_CONFIG_FILE``, ``YADGAR_RO_PASS``,
  and the two image knobs below).
* 7 call sites whose default is a dynamic expression — notably
  ``DOCKERHUB_IMAGE`` / ``DOCKERHUB_BACKEND_IMAGE``, which are version-pinned at
  import time and therefore have no single static value a registry entry could
  declare.
* The ``os.environ.get(NAME) or "fallback"`` shape, which is not scanned.

These counts are documented, not asserted: pinning them would turn every
unrelated knob addition red.  ``test_env_get_scan_is_not_vacuous`` instead
guards that both buckets stay non-empty, so the check cannot silently degrade
into passing by scanning nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_YADGAR_PKG = _REPO_ROOT / "yadgar"
_REGISTRY_SRC = _YADGAR_PKG / "_shared" / "config" / "config_registry.py"
_ALLOWLIST_PATH = Path(__file__).parent.parent / "config_default_mismatch_allowlist.txt"

#: Approved exception categories.  See the allowlist file header for semantics.
VALID_REASONS = {
    "display-only",
    "container-local",
    "dynamic-default",
    "judgement-pending",
}

#: Modules excluded from the env-get scan (structural, see module docstring).
_EXCLUDED_MODULES = {
    "yadgar/_shared/paths/paths.py",
}

#: Directory names skipped when scanning for env-get call sites.
_SKIP_DIRS = {"tests"}

#: Truthy tokens for a "bool"-kind registry default.
_TRUTHY = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def _parse_allowlist() -> dict[str, tuple[str, str]]:
    """Return ``{env_name: (reason_category, free_text_rationale)}``."""
    out: dict[str, tuple[str, str]] = {}
    if not _ALLOWLIST_PATH.exists():
        return out
    for raw_line in _ALLOWLIST_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        name = parts[0]
        token = parts[1] if len(parts) > 1 else ""
        rationale = parts[2] if len(parts) > 2 else ""
        reason = token[len("reason=") :] if token.startswith("reason=") else ""
        out[name] = (reason, rationale.strip())
    return out


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise(kind: str, value: object) -> object:
    """Render a declared or code default to a kind-aware comparable form.

    Representation differences are NOT mismatches: a ``tuple`` code default and
    the comma-joined string that declares it carry the same value, and ``"1"``
    and ``True`` are the same bool.
    """
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in _TRUTHY
    if kind in ("int", "float"):
        try:
            return float(str(value))
        except TypeError, ValueError:
            return ("unparsed", str(value))
    return str(value)


# ---------------------------------------------------------------------------
# Registry introspection
# ---------------------------------------------------------------------------


def _literal_registry_defaults() -> dict[str, tuple[str, str]]:
    """Return ``{name: (literal_default, kind)}`` for statically-declared entries.

    Entries whose default is a computed expression are omitted — they have no
    static value to compare (see module docstring).
    """
    from yadgar._shared.config.config_registry import list_config

    kinds = {e.name: e.kind for e in list_config()}
    tree = ast.parse(_REGISTRY_SRC.read_text())
    out: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "ConfigEntry" or len(node.args) < 2:
            continue
        name_node, default_node = node.args[0], node.args[1]
        if not isinstance(name_node, ast.Constant) or not isinstance(default_node, ast.Constant):
            continue
        name = name_node.value
        if name in kinds:
            out[name] = (default_node.value, kinds[name])
    return out


# ---------------------------------------------------------------------------
# AST scan of env-get call sites
# ---------------------------------------------------------------------------


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """Module-level ``NAME = <literal>`` assignments."""
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.target, ast.Name)
        ):
            out[node.target.id] = node.value.value
    return out


def _from_imports(tree: ast.Module) -> dict[str, str]:
    """``local_name -> source_module`` for absolute ``from X import name``."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


def _env_get_name(call: ast.Call) -> str | None:
    """Return the env-var name if ``call`` is ``os.getenv``/``os.environ.get``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    is_getenv = func.attr == "getenv" and isinstance(func.value, ast.Name)
    is_environ_get = (
        func.attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
    )
    if not (is_getenv or is_environ_get):
        return None
    if len(call.args) < 2 or not isinstance(call.args[0], ast.Constant):
        return None
    return call.args[0].value if isinstance(call.args[0].value, str) else None


def _scan_env_defaults(names: set[str]) -> tuple[list[tuple], list[tuple]]:
    """Scan ``yadgar/`` for env-get defaults naming any entry in ``names``.

    Returns ``(resolved, unresolved)`` where a resolved item is
    ``(name, value, "path:line")`` and an unresolved item is
    ``(name, source_expr, "path:line")``.
    """
    sources: dict[Path, ast.Module] = {}
    module_consts: dict[str, dict[str, object]] = {}
    for path in sorted(_YADGAR_PKG.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if any(part in _SKIP_DIRS for part in path.parts) or rel in _EXCLUDED_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError, UnicodeDecodeError:  # pragma: no cover - defensive
            continue
        sources[path] = tree
        module_consts[".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)] = (
            _module_constants(tree)
        )

    resolved: list[tuple] = []
    unresolved: list[tuple] = []
    for path, tree in sources.items():
        consts = _module_constants(tree)
        imports = _from_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _env_get_name(node)
            if name is None or name not in names:
                continue
            default_node = node.args[1]
            where = f"{path.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}"
            if isinstance(default_node, ast.Constant):
                resolved.append((name, default_node.value, where))
            elif isinstance(default_node, ast.Name):
                ident = default_node.id
                if ident in consts:
                    resolved.append((name, consts[ident], where))
                elif ident in imports and ident in module_consts.get(imports[ident], {}):
                    resolved.append((name, module_consts[imports[ident]][ident], where))
                else:
                    unresolved.append((name, ast.unparse(default_node), where))
            else:
                unresolved.append((name, ast.unparse(default_node), where))
    return resolved, unresolved


# ---------------------------------------------------------------------------
# Mismatch computation
# ---------------------------------------------------------------------------


def _compute_mismatches() -> dict[str, list[str]]:
    """Return ``{env_name: [human-readable mismatch descriptions]}``.

    Allowlist entries are NOT filtered out here — callers decide.  That is what
    lets ``test_no_stale_exceptions`` verify each exception is still live.
    """
    from yadgar._shared.config import Settings

    declared = _literal_registry_defaults()
    settings_fields = Settings.model_fields

    mismatches: dict[str, list[str]] = {}
    registry_only: set[str] = set()

    for name, (declared_default, kind) in declared.items():
        field = name[len("YADGAR_") :] if name.startswith("YADGAR_") else name
        info = settings_fields.get(field)
        if info is None or field != name.removeprefix("YADGAR_"):
            registry_only.add(name)
            continue
        code_default = info.default
        if code_default is None:
            continue
        if _normalise(kind, declared_default) != _normalise(kind, code_default):
            mismatches.setdefault(name, []).append(
                f"registry declares {declared_default!r} but Settings.{field} "
                f"defaults to {code_default!r} (config.py)"
            )

    resolved, _unresolved = _scan_env_defaults(registry_only)
    for name, code_default, where in resolved:
        declared_default, kind = declared[name]
        if _normalise(kind, declared_default) != _normalise(kind, code_default):
            mismatches.setdefault(name, []).append(
                f"registry declares {declared_default!r} but {where} defaults to {code_default!r}"
            )
    return mismatches


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigDefaultValues:
    """I25b: a declared default must equal the code default it documents."""

    def test_allowlist_file_exists(self) -> None:
        """The exception file must exist for the ratchet to function."""
        assert _ALLOWLIST_PATH.exists(), (
            f"Missing: {_ALLOWLIST_PATH}\n"
            "Create it (header + one 'NAME reason=<category> <rationale>' per "
            "deliberate exception)."
        )

    def test_declared_defaults_match_code_defaults(self) -> None:
        """Every registry default equals its code default, or is allowlisted."""
        allowlist = _parse_allowlist()
        mismatches = _compute_mismatches()
        offenders = {n: d for n, d in mismatches.items() if n not in allowlist}

        lines: list[str] = []
        for name in sorted(offenders):
            for detail in offenders[name]:
                lines.append(f"  {name}: {detail}")

        assert not lines, (
            "I25b declared-vs-code default mismatch(es):\n\n"
            + "\n".join(lines)
            + "\n\nFix options:\n"
            "  1. Correct the ConfigEntry default in config_registry.py to the "
            "value the code actually uses.  Prefer this — the code default is "
            "what installs on disk depend on.\n"
            "  2. If the disagreement is deliberate, add the knob to "
            f"{_ALLOWLIST_PATH.name} with reason=<category> and a rationale."
        )

    def test_no_stale_exceptions(self) -> None:
        """Every allowlist entry must still be a live mismatch.

        Without this the exception file silently accumulates lies: someone fixes
        a value, the exception stays, and the next real drift on that knob is
        pre-excused.
        """
        allowlist = _parse_allowlist()
        mismatches = _compute_mismatches()
        stale = sorted(name for name in allowlist if name not in mismatches)
        assert not stale, (
            "Stale entries in "
            f"{_ALLOWLIST_PATH.name} — these no longer mismatch and must be "
            "deleted:\n  " + "\n  ".join(stale)
        )

    def test_exceptions_have_rationale(self) -> None:
        """Every allowlist entry needs a valid category AND free-text rationale."""
        errors: list[str] = []
        for name, (reason, rationale) in sorted(_parse_allowlist().items()):
            if reason not in VALID_REASONS:
                errors.append(f"{name}: reason {reason!r} is not one of {sorted(VALID_REASONS)}")
            if not rationale:
                errors.append(f"{name}: missing free-text rationale after reason=")
        assert not errors, (
            "I25b allowlist entries with missing/invalid annotation:\n  "
            + "\n  ".join(errors)
            + "\n\nFormat: YADGAR_NAME reason=<category> <free-text rationale>"
        )

    def test_env_get_scan_is_not_vacuous(self) -> None:
        """The registry-only half of the check must actually resolve call sites.

        Anti-vacuity guard.  ``test_declared_defaults_match_code_defaults``
        passes trivially if ``_scan_env_defaults`` silently stops resolving
        anything — an import-shape change, a rename, a stricter ``ast`` node
        type would all present as "no mismatches" rather than as a failure.
        ``YADGAR_BACKEND_VOLUME`` is pinned by name because it is the knob whose
        drift motivated this module (task:0103): its code default lives in a
        module-level constant in ``runtime.py``, imported into ``daemon.py`` and
        used there as the env-get fallback, so resolving it exercises the
        cross-module constant path end to end.

        The coverage boundary is documented in the module docstring rather than
        emitted here: the repo runs pytest with ``filterwarnings = error`` (so a
        ``warnings.warn`` report would fail the suite) under xdist (so a
        ``print`` would be swallowed).  Pinning the uncovered set as an
        assertion is worse still — every unrelated knob addition would turn it
        red for no defect.
        """
        from yadgar._shared.config import Settings

        declared = _literal_registry_defaults()
        registry_only = {
            n for n in declared if n.removeprefix("YADGAR_") not in Settings.model_fields
        }
        resolved, unresolved = _scan_env_defaults(registry_only)
        covered = {name for name, _v, _w in resolved}

        assert "YADGAR_BACKEND_VOLUME" in covered, (
            "_scan_env_defaults no longer resolves the YADGAR_BACKEND_VOLUME "
            "call site (yadgar/core/daemon/daemon.py, default "
            "runtime._BACKEND_VOLUME).  The registry-only half of I25b is "
            "silently vacuous until this resolves again — do NOT relax this "
            "assertion; fix the scanner or re-point it at whichever knob now "
            "exercises the cross-module-constant path."
        )

        # The scanner must also keep distinguishing "resolved to a literal" from
        # "dynamic expression" — collapsing the two would either compare against
        # garbage or silently drop comparable sites.  Both buckets are non-empty
        # in this tree (see the coverage-boundary note in the module docstring).
        assert unresolved, (
            "_scan_env_defaults resolved every env-get default, including the "
            "known-dynamic ones (DOCKERHUB_IMAGE / DOCKERHUB_BACKEND_IMAGE are "
            "computed at import time).  Either the scanner started resolving "
            "expressions it cannot evaluate, or it stopped scanning those "
            "modules — both make the comparison untrustworthy."
        )
