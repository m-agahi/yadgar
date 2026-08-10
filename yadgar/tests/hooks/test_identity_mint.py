"""Car C2 of the 0047 PR-40 remediation train — the host-side project_id mint.

ADR-0227: identity is minted by a host-side script at SessionStart, core and
backend derive nothing, and there is NO fallback. A missing or unresolved
project_id FAILS LOUD with a structured error — it is never defaulted, never
inferred, never silently substituted.

These tests pin three things:

1. ``mint_project_id`` RAISES ``UnresolvableProjectError`` when nothing can be
   resolved. The specific regression guarded here is the ``local/<basename>``
   fallback that ``yadgar.core.identity._local_fallback`` still returns — the
   mint must not inherit it.
2. The mint resolves the two legitimate sources: ``.yadgar/project-id`` (walked
   UP from cwd) and the git ``origin`` remote (insteadOf-resolved, host
   stripped, ``.git`` stripped, lowercased).
3. The **package boundary**. ``yadgar.core.hooks._identity_mint`` lives under
   ``core/hooks/`` precisely so the boundary is structural rather than a
   call-graph argument: only the hook entry points may import it. This is
   C15's residue lint in embryo — asserted here so C2 cannot regress before
   C15 exists.
"""

from __future__ import annotations

import ast
import subprocess
import unittest.mock
from pathlib import Path

import pytest

import yadgar.core.hooks._identity_mint as mint_mod
import yadgar.core.identity as identity
from yadgar._shared.server_helpers.server_helpers import _resolve_project_root

_REPO_ROOT = Path(mint_mod.__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clear_identity_caches():
    """Drop the process-scoped lru_caches so tests cannot leak into each other.

    ``_insteadof_rules`` and ``_resolve_project_root`` are both ``lru_cache``d.
    Without a clear, a test that mocks subprocess can prime a cache that a
    LATER test then reads without its own mock — a vacuous pass in either
    direction depending on execution order.
    """
    identity._insteadof_rules.cache_clear()
    _resolve_project_root.cache_clear()
    yield
    identity._insteadof_rules.cache_clear()
    _resolve_project_root.cache_clear()


# ── 1. FAIL LOUD — no fallback, ever ──────────────────────────────────────


def test_mint_raises_when_git_is_absent(tmp_path):
    """No ``git`` binary at all → raise. MUST NOT return ``local/<basename>``.

    This is the container case ADR-0227 was written for: neither image installs
    git, so every subprocess call dies with ``FileNotFoundError``. The old
    ``derive_project_id`` swallowed that and manufactured ``local/<basename>``,
    a well-formed key that passes every type check and writes rows into a
    namespace nobody chose. The mint must refuse instead.
    """
    with unittest.mock.patch("subprocess.check_output", side_effect=FileNotFoundError("git")):
        with pytest.raises(mint_mod.UnresolvableProjectError):
            mint_mod.mint_project_id(str(tmp_path))


def test_mint_raises_for_directory_with_no_remote(tmp_path):
    """A real directory that is simply not a git checkout → raise."""
    with unittest.mock.patch(
        "subprocess.check_output",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        with pytest.raises(mint_mod.UnresolvableProjectError):
            mint_mod.mint_project_id(str(tmp_path))


def test_mint_error_names_the_directory_and_both_remedies(tmp_path):
    """The raise is *structured*: it names the path and how to fix it.

    A fail-loud error that does not say which directory failed, or what the
    operator should do about it, is only marginally better than the silent
    fallback it replaced.
    """
    with unittest.mock.patch("subprocess.check_output", side_effect=FileNotFoundError("git")):
        with pytest.raises(mint_mod.UnresolvableProjectError) as excinfo:
            mint_mod.mint_project_id(str(tmp_path))

    msg = str(excinfo.value)
    assert str(tmp_path) in msg, f"error must name the directory: {msg!r}"
    assert ".yadgar/project-id" in msg, f"error must name the override file: {msg!r}"
    assert "origin" in msg, f"error must name the remote it looked for: {msg!r}"


def test_mint_never_emits_a_local_slash_key(tmp_path):
    """No input shape may produce a ``local/...`` key out of the mint.

    Belt-and-braces over the two raises above: if a future edit reintroduces
    the fallback, this fails regardless of which arm produced it.
    """
    with unittest.mock.patch("subprocess.check_output", side_effect=FileNotFoundError("git")):
        try:
            got = mint_mod.mint_project_id(str(tmp_path))
        except mint_mod.UnresolvableProjectError:
            return  # the contract
    pytest.fail(f"mint returned {got!r} instead of raising — the fallback is back")


def test_mint_module_source_has_no_local_fallback_literal():
    """``_local_fallback`` must not be re-introduced INTO the mint module.

    Source-level so it fails even when no test input happens to reach the arm.
    """
    src = Path(mint_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("local/")
    ]
    assert not offenders, f"mint module must carry no local/ fallback literal: {offenders}"


# ── 2. The two legitimate resolution sources ──────────────────────────────


def test_mint_uses_project_id_file_override(tmp_path):
    """``.yadgar/project-id`` at the cwd short-circuits remote derivation."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("monorepo/sub\n")

    assert mint_mod.mint_project_id(str(tmp_path)) == "monorepo/sub"


def test_mint_walks_up_for_the_project_id_file(tmp_path):
    """The override is found from any descendant directory."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("monorepo/sub\n")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)

    assert mint_mod.mint_project_id(str(child)) == "monorepo/sub"


def test_mint_derives_owner_repo_from_origin_remote(tmp_path):
    """origin → insteadOf-resolved → host stripped → ``.git`` stripped → lower."""
    config_output = "url.codeberg-agent:.insteadof git@codeberg.org:\n"
    remote_origin = "git@codeberg.org:M-Agahi/Yadgar.git"

    def fake_check_output(args, **kwargs):
        joined = " ".join(args)
        if "--get-regexp" in joined:
            return config_output.encode()
        if "remote.origin.url" in joined:
            return remote_origin.encode()
        if "rev-parse" in joined:
            return f"{tmp_path}\n".encode()
        raise subprocess.CalledProcessError(1, "git")

    with unittest.mock.patch("subprocess.check_output", fake_check_output):
        assert mint_mod.mint_project_id(str(tmp_path)) == "m-agahi/yadgar"


def test_mint_returns_a_bare_string_not_a_tuple(tmp_path):
    """``mint_project_id(cwd) -> str`` — not the old ``(id, remote)`` tuple."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("owner/repo\n")

    got = mint_mod.mint_project_id(str(tmp_path))
    assert isinstance(got, str), f"expected str, got {type(got).__name__}"


# ── 3. The emitted banner + the failure text ──────────────────────────────


def test_resolve_session_project_returns_id_and_greppable_banner(tmp_path):
    """Success → (project_id, banner) where the banner is machine-greppable."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("m-agahi/yadgar\n")

    project_id, text = mint_mod.resolve_session_project(str(tmp_path))

    assert project_id == "m-agahi/yadgar"
    assert "yadgar: project_id=m-agahi/yadgar" in text
    assert 'project="m-agahi/yadgar"' in text, (
        "the banner must tell the agent what to pass, not merely what the id is"
    )


def test_resolve_session_project_failure_emits_no_guess(tmp_path):
    """Failure → (None, loud error text). No guessed value anywhere in it."""
    with unittest.mock.patch("subprocess.check_output", side_effect=FileNotFoundError("git")):
        project_id, text = mint_mod.resolve_session_project(str(tmp_path))

    assert project_id is None
    assert "project_id=" not in text, (
        f"the failure text must not carry a project_id= line an agent would copy: {text!r}"
    )
    assert "local/" not in text, f"failure text must not suggest a fallback key: {text!r}"
    assert "ERROR" in text, f"failure must be loud: {text!r}"
    assert ".yadgar/project-id" in text, f"failure must be actionable: {text!r}"


# ── 4. The package boundary (C15's residue lint, in embryo) ───────────────

# The mint is reachable ONLY from the two hook entry points. `core/hooks/` is
# the Claude Code hook surface; `core/cli/hook.py` is the same handler set for
# the opencode/CLI transport (the opencode plugin shells out to `yadgar hook`).
# Anything else — core/server, backend, _shared — importing this module means
# a container-side process is deriving identity again, which is exactly what
# ADR-0227 removed.
_ALLOWED_IMPORTERS = frozenset(
    {
        "yadgar/core/hooks/session-start-context.py",
        "yadgar/core/cli/hook.py",
    }
)


def _importers_of_mint() -> set[str]:
    """Return repo-relative paths of every non-test file naming the mint module."""
    found: set[str] = set()
    for path in (_REPO_ROOT / "yadgar").rglob("*.py"):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith("yadgar/tests/"):
            continue
        if path.resolve() == Path(mint_mod.__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "_identity_mint" in text:
            found.add(rel)
    return found


def test_mint_module_has_only_hook_entry_point_importers():
    """Structural boundary: nothing outside the hook entry points may import it.

    Kept as a set-difference (not a count) deliberately: a count of one is
    satisfiable by a facade module that re-exports the mint to core-server,
    which would re-open exactly the reachability ADR-0227 closed.
    """
    unexpected = _importers_of_mint() - _ALLOWED_IMPORTERS
    assert not unexpected, (
        "ADR-0227: yadgar.core.hooks._identity_mint may be imported ONLY by the "
        f"host-side hook entry points {sorted(_ALLOWED_IMPORTERS)}; found: {sorted(unexpected)}"
    )


def test_mint_is_not_reachable_from_core_server_or_backend():
    """The negative half, named by layer so a violation reads as a layer breach."""
    offenders = sorted(
        rel
        for rel in _importers_of_mint()
        if rel.startswith(("yadgar/core/server/", "yadgar/backend/", "yadgar/_shared/"))
    )
    assert not offenders, (
        f"core-server / backend / _shared must never mint identity (ADR-0227): {offenders}"
    )
