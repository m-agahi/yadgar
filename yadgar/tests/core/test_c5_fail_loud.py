"""C5 (0047 PR#40 §5) — the fail-loud flip.

After this car nothing in the system can produce a ``project_id`` it was not
given. ADR-0227:

> ALL fallbacks are deleted: no ``_local_fallback``, no ``local/<basename>``, no
> ``GLOBAL_FALLBACK`` ``"global"`` tier, no directory tier in the resolver. A
> missing or unresolved ``project_id`` FAILS LOUD with a structured error at the
> boundary — it is never defaulted, never inferred, never silently substituted.

Every test here asserts the caller RAISES, and that the raised payload names the
TOOL and the FIX — not that some default came back. A test that merely asserted
"no longer returns ``'global'``" would pass for a function that returned ``None``,
which is the same silent-wrong-answer failure wearing a different value.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from yadgar._shared.errors import (
    UnresolvedPatternError,
    UnresolvedProjectError,
    YadgarError,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PKG_ROOT = _REPO_ROOT / "yadgar"


# ── the structured error itself ────────────────────────────────────────────


class TestUnresolvedProjectError:
    """The payload is the product — a bare raise is only half the requirement."""

    def test_payload_names_tool_and_fix(self):
        exc = UnresolvedProjectError("wiki_add")
        assert exc.payload["error"] == "unresolved_project"
        assert exc.payload["tool"] == "wiki_add"
        # The fix must be actionable: it has to say what to PASS.
        assert 'project="owner/repo"' in exc.payload["fix"]

    def test_message_carries_tool_and_fix_for_the_propagating_path(self):
        # Not every call site returns an envelope; where it propagates, str(exc)
        # is the only thing the agent sees.
        text = str(UnresolvedProjectError("memorize"))
        assert "memorize" in text
        assert 'project="owner/repo"' in text

    def test_is_a_yadgar_error(self):
        assert issubclass(UnresolvedProjectError, YadgarError)
        assert issubclass(UnresolvedPatternError, YadgarError)


# ── tier 3 + tier 4 of the resolver are gone ───────────────────────────────


class TestResolverRaises:
    """``resolve_effective_project`` has exactly two tiers left."""

    def test_no_project_no_session_no_directory_raises(self):
        from yadgar.core.server.tools._project_param import resolve_effective_project

        with pytest.raises(UnresolvedProjectError) as ei:
            resolve_effective_project(
                project=None, directory=None, session_project=None, tool="unit"
            )
        assert ei.value.payload["tool"] == "unit"

    def test_directory_no_longer_derives(self):
        """The RED the plan names: a real directory must NOT yield ``local/<x>``.

        Before C5 this returned ``local/yadgar`` (or the git-derived key) by
        shelling out to git from a container that has no git. Now it raises.
        """
        from yadgar.core.server.tools._project_param import resolve_effective_project

        with pytest.raises(UnresolvedProjectError):
            resolve_effective_project(
                project=None,
                directory="/home/max/git/yadgar",
                session_project=None,
                tool="unit",
            )

    def test_directory_global_sentinel_raises_rather_than_minting(self):
        """``directory="global"`` is a REACH declaration, never an identity.

        §1.4: ``"global"`` is never a project_id; cross-project reach is a
        separate tag. The old tier-4 answered this case with the sentinel.
        """
        from yadgar.core.server.tools._project_param import resolve_effective_project

        with pytest.raises(UnresolvedProjectError):
            resolve_effective_project(
                project=None, directory="global", session_project=None, tool="unit"
            )

    def test_explicit_project_still_wins(self):
        from yadgar.core.server.tools._project_param import resolve_effective_project

        out = resolve_effective_project(
            project="m-agahi/yadgar",
            directory="/tmp/somewhere",
            session_project=None,
            tool="unit",
        )
        assert out == "m-agahi/yadgar"

    def test_session_project_still_resolves(self):
        from yadgar.core.server.tools._project_param import resolve_effective_project

        out = resolve_effective_project(
            project=None, directory=None, session_project="owner/repo", tool="unit"
        )
        assert out == "owner/repo"

    def test_global_fallback_constant_is_gone(self):
        import yadgar.core.server.tools._project_param as mod

        assert not hasattr(mod, "GLOBAL_FALLBACK")
        assert "GLOBAL_FALLBACK" not in mod.__all__


# ── the derivation function itself is deleted ──────────────────────────────


class TestDeriveProjectIdDeleted:
    def test_derive_project_id_symbol_is_gone(self):
        import yadgar.core.identity as identity

        assert not hasattr(identity, "derive_project_id")

    def test_local_fallback_symbol_is_gone(self):
        import yadgar.core.identity as identity

        assert not hasattr(identity, "_local_fallback")

    def test_pure_helpers_survive(self):
        """The mint imports them; deleting them would break the host-side path."""
        import yadgar.core.identity as identity

        for name in (
            "_normalise_remote",
            "_parse_insteadof_map",
            "_insteadof_rules",
            "_walk_project_id_file",
            "_origin_remote",
        ):
            assert hasattr(identity, name), name


# ── the storage chokepoint mints nothing ───────────────────────────────────


class TestStorageChokepointRaises:
    def test_missing_caller_value_raises(self):
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        with pytest.raises(UnresolvedProjectError):
            _resolve_project_id_for_write(caller_value=None, directory_context="/home/max/x")

    def test_sentinel_directory_no_longer_mints_global(self):
        """The single line the old grep guard would NOT have caught.

        ``if not directory_context or directory_context == "global": return
        "global"`` produced exactly the sentinel §1.4 forbids.
        """
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        for dc in (None, "", "global"):
            with pytest.raises(UnresolvedProjectError):
                _resolve_project_id_for_write(caller_value=None, directory_context=dc)

    def test_empty_string_caller_value_raises(self):
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        with pytest.raises(UnresolvedProjectError):
            _resolve_project_id_for_write(caller_value="", directory_context="/x")

    def test_caller_value_is_returned_verbatim(self):
        from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write

        assert _resolve_project_id_for_write(caller_value="a/b", directory_context=None) == "a/b"

    def test_no_lazy_core_identity_import_remains(self):
        import yadgar._shared.storage._project_id_writer as mod

        assert not hasattr(mod, "_CORE_IDENTITY_TARGET")


# ── the prelude's unknown-pattern path ─────────────────────────────────────


class _StubStorage:
    """Storage that resolves nothing — the unknown-slug case."""


class TestDispatchPreludeUnknownPattern:
    def test_unknown_pattern_raises_naming_the_slug(self, monkeypatch):
        import yadgar.core.server.tools.dispatch_helper as dh

        monkeypatch.setattr(dh, "_cached_agent_prompt", lambda pattern, storage: None)
        monkeypatch.setattr(
            dh, "_record_prelude_marker", lambda storage, directory, project=None: None
        )
        monkeypatch.setattr(dh, "_get_contract_text", lambda storage: "## Contract\n\nbody")

        with pytest.raises(UnresolvedPatternError) as ei:
            dh.agent_dispatch_prelude(
                pattern="does-not-exist",
                task_topic="anything",
                storage=_StubStorage(),
            )
        assert ei.value.payload["slug"] == "agent-prompt-does-not-exist"
        assert "does-not-exist" in str(ei.value)

    def test_storage_read_failure_is_not_reported_as_absent(self, monkeypatch):
        """A storage error is not "pattern absent" — it must not be swallowed."""
        import yadgar.core.server.tools.dispatch_helper as dh

        def _boom(pattern, storage):
            raise RuntimeError("surreal is down")

        monkeypatch.setattr(dh, "_cached_agent_prompt", _boom)
        monkeypatch.setattr(
            dh, "_record_prelude_marker", lambda storage, directory, project=None: None
        )
        monkeypatch.setattr(dh, "_get_contract_text", lambda storage: "## Contract\n\nbody")

        with pytest.raises(RuntimeError, match="surreal is down"):
            dh.agent_dispatch_prelude(pattern="pr-review", task_topic="x", storage=_StubStorage())

    def test_empty_pattern_stays_the_documented_skip(self, monkeypatch):
        import yadgar.core.server.tools.dispatch_helper as dh

        def _never(pattern, storage):
            raise AssertionError("pattern='' must not reach the prompt lookup")

        monkeypatch.setattr(dh, "_cached_agent_prompt", _never)
        monkeypatch.setattr(
            dh, "_record_prelude_marker", lambda storage, directory, project=None: None
        )
        monkeypatch.setattr(dh, "_get_contract_text", lambda storage: "## Contract\n\nbody")

        out = dh.agent_dispatch_prelude(pattern="", task_topic="topic", storage=_StubStorage())
        assert "## Contract" in out
        assert "Recall hint" in out

    def test_include_context_over_an_empty_corpus_still_returns_a_prelude(self, monkeypatch):
        """GREEN-unchanged: ``_build_context_block`` is deliberately untouched."""
        import yadgar.core.server.tools.dispatch_helper as dh

        monkeypatch.setattr(
            dh, "_record_prelude_marker", lambda storage, directory, project=None: None
        )
        monkeypatch.setattr(dh, "_get_contract_text", lambda storage: "## Contract\n\nbody")
        monkeypatch.setattr(
            dh,
            "_build_context_block",
            lambda **kw: "",  # empty corpus / recall timeout / suppressed fan-out
        )

        out = dh.agent_dispatch_prelude(
            pattern="",
            task_topic="topic",
            storage=_StubStorage(),
            include_context=True,
        )
        assert "## Contract" in out


# ── the enforcement knob dies here ─────────────────────────────────────────


class TestEnforcementKnobGone:
    def test_settings_field_removed(self):
        from yadgar._shared.config import get_settings

        assert not hasattr(get_settings(), "DIRECTORY_ENFORCEMENT")

    def test_no_live_source_reference_survives(self):
        """No LIVE reference — prose recording the deletion is not a reference.

        Deliberately AST-level rather than a raw-text grep: this car leaves
        comments at each deleted site explaining what went and why, and a
        text-level guard would force those explanations out, which is how a
        deletion loses its rationale one refactor later.
        """
        hits = []
        for _path, tree, rel in _walk_source():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "YADGAR_DIRECTORY_ENFORCEMENT":
                    hits.append(f"{rel}:{node.lineno}")
        assert hits == [], f"YADGAR_DIRECTORY_ENFORCEMENT still read at: {hits}"


# ── the residue guard ──────────────────────────────────────────────────────
#
# The plan's grep guard, with the clause that catches the site the earlier
# draft's guard would have missed: a bare ``return "global"`` / an ``== "global"``
# comparison in a project_id position.


#: Files whose "global" literals are a REACH tag or a directory value, not a
#: project_id. Each entry is a deliberate, reviewed exemption.
_GLOBAL_LITERAL_ALLOWLIST = {
    # directory_context values + the always-eligible read predicate (C7/C11 own
    # the read path; C5 owns identity minting only).
    "yadgar/_shared/storage/directory.py",
    # 'global' as a DIRECTORY argument on library pages (reach, not ownership).
    "yadgar/core/server/tools/agent_prompts.py",
}


#: Files whose "local/" literals are a deterministic carry-by-design
#: classification or mapping, not a request-path identity minting. ADR-0227
#: bans fallback minting in ``resolve_effective_project``; Car A's ``parse_map``
#: uses ``local/`` as a NAMING CONVENTION (the third tier of
#: ``derive_project_id``, deterministic over the operator-supplied corpus) and
#: as a ``kind``-field CLASSIFIER, not as a project_id minted in the auth path.
#: Each entry is a deliberate, reviewed exemption.
_LOCAL_MINTING_ALLOWLIST = {
    # Car A (2026-08-14 train): ``parse_map`` classifies the operator-supplied
    # project_id column 2 as ``git`` or ``local`` by prefix check. The ``local/``
    # literal is a CLASSIFIER, not a minting site.
    "yadgar/core/cli/project.py",
}


def _walk_source():
    """Yield ``(path, ast_tree, rel)`` for every non-test source file."""
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        if "tests" in path.parts:
            continue
        rel = str(path.relative_to(_REPO_ROOT))
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=rel), rel


#: Module-level constants whose whole job is to RECOGNISE a dead sentinel so it
#: can be rejected. A job enqueued by an older client can still carry
#: ``"unresolved"``; the gate has to name the value to refuse it. Recognising is
#: the opposite of minting, and a guard that cannot tell them apart would force
#: the rejection lists out of existence.
_RECOGNISER_NAMES = frozenset(
    {
        "_SENTINEL_PROJECT_IDS",
        "_NON_IDENTIFYING_PROJECT_IDS",
        # Car D (2026-08-14 train): the corpus re-key migration recognises these
        # directory values as non-identifying sentinels and DROPs them in the
        # operator-reviewed map. The literal "unresolved" is one of four dead
        # values ('', 'global', 'unresolved', 'system') in the same frozenset;
        # exempting the whole set from the 'unresolved' literal test is correct
        # because the test's intent is to catch minting, not recognition.
        "_NON_IDENTIFYING_DIRECTORY_VALUES",
    }
)


def _recogniser_constants(tree) -> set[int]:
    """Return ``id()`` of every constant inside a recogniser-set assignment."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if names & _RECOGNISER_NAMES and node.value is not None:
            exempt.update(id(sub) for sub in ast.walk(node.value))
    return exempt


def _mints_local_prefix(node) -> bool:
    """True for ``f"local/{x}"`` or a ``"local/"`` literal used as a key."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("local/")
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.startswith("local/")
        )
    return False


class TestNoResidualFallbacks:
    """AST-level, so the deletion COMMENTS survive while the CODE cannot."""

    def test_global_fallback_identifier_is_unreferenced(self):
        offenders: list[str] = []
        for _path, tree, rel in _walk_source():
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "GLOBAL_FALLBACK":
                    offenders.append(f"{rel}:{node.lineno}: GLOBAL_FALLBACK")
                elif isinstance(node, ast.alias) and node.name == "GLOBAL_FALLBACK":
                    offenders.append(f"{rel}: imports GLOBAL_FALLBACK")
        assert offenders == [], "GLOBAL_FALLBACK still live:\n" + "\n".join(offenders)

    def test_no_local_prefix_or_unresolved_minting(self):
        offenders: list[str] = []
        for _path, tree, rel in _walk_source():
            exempt = _recogniser_constants(tree)
            local_exempt = rel in _LOCAL_MINTING_ALLOWLIST
            for node in ast.walk(tree):
                if _mints_local_prefix(node) and not local_exempt:
                    offenders.append(f"{rel}:{node.lineno}: local/ minting")
                if (
                    isinstance(node, ast.Constant)
                    and node.value == "unresolved"
                    and id(node) not in exempt
                ):
                    offenders.append(f"{rel}:{node.lineno}: 'unresolved' literal")
        assert offenders == [], "residual fallback minting:\n" + "\n".join(offenders)

    def test_no_global_minted_into_a_project_id_position(self):
        """The clause that catches ``_project_id_writer.py``'s deleted branch.

        Walks the AST rather than grepping so ``return "global"`` is only an
        offence inside a function whose job is to produce a project_id, and so a
        ``project_id = "global"`` assignment is caught even when it is spelled
        across a conditional expression.
        """
        offenders: list[str] = []
        for _path, tree, rel in _walk_source():
            if rel in _GLOBAL_LITERAL_ALLOWLIST:
                continue
            for node in ast.walk(tree):
                # project_id = "global"  /  "project_id": "global"
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        name = (
                            tgt.id
                            if isinstance(tgt, ast.Name)
                            else getattr(tgt, "attr", None)
                            if isinstance(tgt, ast.Attribute)
                            else None
                        )
                        if name and "project_id" in name and _is_global_str(node.value):
                            offenders.append(f"{rel}:{node.lineno}: project_id = 'global'")
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values, strict=False):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "project_id"
                            and _is_global_str(v)
                        ):
                            offenders.append(f"{rel}:{k.lineno}: 'project_id': 'global'")
                # A function that produces a project_id must not return "global".
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    "project_id" in node.name
                ):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Return) and _is_global_str(sub.value):
                            offenders.append(f"{rel}:{sub.lineno}: return 'global'")
        assert offenders == [], "'global' minted as a project_id:\n" + "\n".join(offenders)


def _is_global_str(node) -> bool:
    """True for ``"global"`` written literally or through a conditional."""
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return node.value == "global"
    if isinstance(node, ast.IfExp):
        return _is_global_str(node.body) or _is_global_str(node.orelse)
    if isinstance(node, ast.BoolOp):
        return any(_is_global_str(v) for v in node.values)
    return False
