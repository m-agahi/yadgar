"""AST guard: retrieval Settings attribute reads ⊆ real Settings fields (#231).

Locks fix #231 (fix(retrieval): read Settings fields directly so a rename fails
loud). #231 replaced ``getattr(self._settings, "FIELD", default)`` fallbacks with
direct ``self._settings.FIELD`` reads across the retrieval layer, so a future
``Settings`` rename that misses a retrieval call site raises ``AttributeError``
at runtime instead of silently falling back to a default.

This meta-test makes that guarantee permanent: it AST-scans the retrieval
modules #231 converted for DIRECT attribute reads off ``self._settings`` / a bare
``settings`` local, and asserts every accessed UPPER_SNAKE attribute name is a
real field on the ``Settings`` model. If someone renames a ``Settings`` field but
forgets a retrieval call site, THIS test goes RED — the whole point of #231.

Scope precision (no false positives):
  * Only ``self._settings.<ATTR>`` and bare ``settings.<ATTR>`` reads are checked,
    where ``<ATTR>`` is UPPER_SNAKE_CASE — i.e. a config knob, not an arbitrary
    method/attribute (``ctx.profile``, ``b.get`` etc. are never matched).
  * ``getattr(self._settings, "NAME", default)`` reads are intentionally NOT
    flagged — getattr is the sanctioned escape hatch #231 deliberately LEFT in
    place for the phantom (non-Settings) knobs. Those live in _PHANTOM_ALLOWLIST
    below; a phantom knob accessed *directly* (not via getattr) WOULD fail this
    test, which is correct (it should be getattr or a real field).
"""

from __future__ import annotations

import ast

from yadgar.tests._paths import REPO_ROOT as _REPO_ROOT

# Retrieval modules #231 converted from getattr → direct Settings reads.
_RETRIEVAL_DIR = _REPO_ROOT / "yadgar" / "backend" / "retrieval"
_TOUCHED_FILES = (
    "_reranking_cross_encoder.py",
    "_reranking_multi_passage.py",
    "_reranking_nli.py",
    "core.py",
    "fusion.py",
    "providers/fusion.py",
    "recall_pipeline.py",
    "reranking.py",
    "scoring.py",
)

# Phantom knobs #231 deliberately LEFT as getattr(settings, "NAME", default):
# they are NOT real Settings fields, so a direct read would AttributeError at
# runtime. They are read via getattr (with a default) on purpose and are exempt
# from the direct-read subset check. Verified against Settings.model_fields by
# test_phantom_allowlist_is_actually_phantom below (allowlist can't rot silent).
_PHANTOM_ALLOWLIST = frozenset(
    {
        "OPEN_DOMAIN_CANDIDATE_MULTIPLIER",
        "OPEN_DOMAIN_FTS_BOOST",
        "CE_DIVERSITY_INJECT_K",
    }
)


def _is_settings_ref(node: ast.expr) -> bool:
    """True iff *node* is ``self._settings`` or a bare ``settings`` Name."""
    # bare `settings`
    if isinstance(node, ast.Name) and node.id == "settings":
        return True
    # `self._settings`
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "_settings"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return True
    return False


def _direct_settings_attrs() -> dict[str, set[str]]:
    """Map each touched file → set of UPPER_SNAKE attrs read directly off Settings.

    Only DIRECT attribute reads (``self._settings.X`` / ``settings.X``) are
    collected; ``getattr(settings, "X", d)`` calls are ignored (getattr's second
    arg is a plain string, not an ``ast.Attribute``, so it never enters here).
    """
    out: dict[str, set[str]] = {}
    for rel in _TOUCHED_FILES:
        path = _RETRIEVAL_DIR / rel
        assert path.exists(), f"#231 file no longer at expected path: {path}"
        tree = ast.parse(path.read_text(), filename=str(path))
        found: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr.isupper()  # config knobs are UPPER_SNAKE; skips ctx.profile etc.
                and _is_settings_ref(node.value)
            ):
                found.add(node.attr)
        out[rel] = found
    return out


def test_touched_files_present() -> None:
    """Sanity: every #231 file exists and at least one has direct Settings reads."""
    attrs = _direct_settings_attrs()
    assert attrs, "no retrieval files scanned — _TOUCHED_FILES likely broke."
    assert any(v for v in attrs.values()), (
        "no direct self._settings/settings.<ATTR> reads found across the #231 "
        "files — the AST matcher likely broke (or #231 was reverted)."
    )


def test_direct_settings_reads_are_real_fields() -> None:
    """Every DIRECT Settings attribute read in the retrieval layer must be a real
    Settings field. RED when a rename leaves a retrieval call site pointing at a
    field that no longer exists — exactly the failure #231 makes loud.
    """
    from yadgar._shared.config import Settings

    fields = set(Settings.model_fields)
    attrs_by_file = _direct_settings_attrs()

    offenders: list[str] = []
    for rel, attrs in attrs_by_file.items():
        for name in sorted(attrs):
            if name in _PHANTOM_ALLOWLIST:
                # A phantom knob read DIRECTLY (not via getattr) would AttributeError;
                # flag it so the fix is getattr-or-make-it-real, not silent.
                offenders.append(
                    f"{rel}: {name} is a phantom knob read DIRECTLY — use "
                    f"getattr(settings, {name!r}, <default>) or add it to Settings."
                )
                continue
            if name not in fields:
                offenders.append(
                    f"{rel}: self._settings.{name} / settings.{name} is NOT a "
                    f"Settings field — a rename left this retrieval read dangling (#231)."
                )

    assert not offenders, (
        f"{len(offenders)} retrieval Settings-coupling violation(s):\n  "
        + "\n  ".join(offenders)
        + "\n\nFix: rename the retrieval call site to match the Settings field "
        "(or restore the field). Do NOT re-introduce a getattr default to paper "
        "over a real field — #231 made these reads loud on purpose."
    )


def test_phantom_allowlist_is_actually_phantom() -> None:
    """The _PHANTOM_ALLOWLIST must contain ONLY names that are genuinely NOT
    Settings fields. If a phantom knob later becomes a real Settings field, this
    test goes RED so the allowlist entry is removed (allowlist can't silently rot).
    """
    from yadgar._shared.config import Settings

    fields = set(Settings.model_fields)
    leaked = sorted(n for n in _PHANTOM_ALLOWLIST if n in fields)
    assert not leaked, (
        f"{len(leaked)} name(s) in _PHANTOM_ALLOWLIST are now REAL Settings "
        f"fields: {leaked}. Remove them from the allowlist — they should be read "
        "directly (or their getattr fallback dropped)."
    )
