"""Car A0 of 0047 spine train — ``yadgar.core.identity`` derivation seam.

Resolution order (§16.2 of the master plan), as amended by C5 / ADR-0227:

    1. ``.yadgar/project-id`` walked UP from cwd → use its content.
    2. ``owner/repo`` from the git remote, normalised (§16.4):
       resolve insteadOf rewrites → strip scheme+host → strip trailing
       ``.git`` → lowercase. Host excluded. Reads ``origin``; the
       ``.yadgar/project-id`` file is the documented override.
    3. **Nothing.** The third tier — ``local/<basename>`` — is DELETED, and
       so is the ``derive_project_id`` composition that reached it. An
       unresolvable directory raises ``UnresolvableProjectError``.

``yadgar.core.identity`` keeps only the pure readers; the composition that
decides which sources count moved host-side to
``yadgar.core.hooks._identity_mint.mint_project_id``, which is where the
tests below that used to drive ``derive_project_id`` now point. They were
INVERTED rather than deleted: each one pinned a tier this car removed, so
the same input now has to produce a raise instead of a manufactured key.

The tests pin the surviving sub-functions (``_resolve_insteadof``,
``_normalise_remote``, ``_walk_project_id_file``) separately from the
top-level mint so a regression in either half is diagnosed by name.
"""

from __future__ import annotations

import os
import subprocess
import unittest.mock
from pathlib import Path

import pytest

import yadgar.core.identity as identity
from yadgar._shared.server_helpers.server_helpers import _resolve_project_root
from yadgar.core.hooks._identity_mint import UnresolvableProjectError, mint_project_id


@pytest.fixture(autouse=True)
def _clear_identity_caches():
    """Drop the process-scoped lru_caches so tests cannot leak into each other.

    ``_insteadof_rules`` and ``_resolve_project_root`` are both ``lru_cache``d.
    Without a clear, a test that mocks subprocess can prime a cache that a
    LATER test then reads without its own mock — a vacuous pass in either
    direction depending on execution order.

    NOT a project-supplying autouse fixture: it clears caches only, and no
    test in this file gets a ``project_id`` it did not name itself.
    """
    identity._insteadof_rules.cache_clear()
    _resolve_project_root.cache_clear()
    yield
    identity._insteadof_rules.cache_clear()
    _resolve_project_root.cache_clear()


# ── sub-function: _resolve_insteadof (pure parser) ─────────────────────────


def test_resolve_insteadof_is_identity_when_no_rules():
    """Empty insteadOf map → URL passes through verbatim."""
    out = identity._parse_insteadof_map(
        {"git@github.com:": "https://github.com/"},
        "https://example.com/foo/bar.git",
    )
    assert out == "https://example.com/foo/bar.git"


def test_resolve_insteadof_substitutes_simple_insteadof():
    """The single-rule case: ``git@github.com:`` → ``git@github-personal:``.

    ``git config --get-regexp '^url\\..*\\.insteadof$'`` emits lines shaped
    ``url.<rewrite-target>.insteadof <source-pattern>``. So ``git@github-personal:``
    is the rewrite target and ``git@github.com:`` is the source pattern that
    triggers it.
    """
    out = identity._parse_insteadof_map(
        {
            "git@github-personal:": "git@github.com:",
        },
        "git@github.com:m-agahi/yadgar.git",
    )
    assert out == "git@github-personal:m-agahi/yadgar.git"


def test_resolve_insteadof_iterates_until_fixed_point():
    """Two rules in chain: apply repeatedly until no rule matches.

    Rule A rewrites ``https://github.com/`` → ``git@github.com:``, and rule B
    rewrites ``git@github.com:m-agahi/`` → ``git@github-personal:m-agahi/``.
    A URL that matches BOTH must end up rewritten by both — B is the one
    the live config carries (it's a ``m-agahi/``-scoped chain).
    """
    out = identity._parse_insteadof_map(
        {
            "git@github.com:": "https://github.com/",
            "git@github-personal:m-agahi/": "git@github.com:m-agahi/",
        },
        "https://github.com/m-agahi/yadgar.git",
    )
    assert out == "git@github-personal:m-agahi/yadgar.git"


def test_resolve_insteadof_avoids_infinite_loop():
    """Two rules that map to each other must not loop forever.

    The parser must terminate after a bounded number of iterations when no
    rule applies, rather than spin until interrupted. With both rules
    in play, the resolver rewrites once then stops — neither pattern
    applies to its own rewrite target.
    """
    out = identity._parse_insteadof_map(
        {
            "beta": "alpha",
            "alpha": "beta",
        },
        "alpha/x",
    )
    # Either fixed point is correct: the chain has no canonical direction.
    # The test pins the safety property (no hang), not the answer.
    assert out in {"alpha/x", "beta/x"}


def test_resolve_insteadof_ignores_malformed_keys():
    """A key without the ``.insteadof`` suffix in the upstream form is skipped.

    The live format the parser builds is ``{rewrite_target: source}`` —
    a key that starts with ``url.`` and ends with ``.pushInsteadOf`` is
    a different kind of rule (``pushInsteadOf`` rewrites pushes, not
    fetches) and must be filtered out upstream by ``_insteadof_rules``.
    The ``_parse_insteadof_map`` helper itself trusts its input shape;
    the upstream filter is what guards against malformed entries.
    """
    # Drive ``_insteadof_rules`` with a fake config stream so the
    # malformed line is dropped before reaching the parser.
    out = identity._insteadof_rules_from_lines(
        "url.git@github.com:.pushInsteadOf git@github-personal:\n"
        "url.git@github.com:.insteadof git@github-personal:\n"
    )
    assert out == {"git@github.com:": "git@github-personal:"}


# ── sub-function: _normalise_remote ───────────────────────────────────────


def test_normalise_remote_strips_ssh_scheme_and_dot_git():
    assert identity._normalise_remote("git@github-personal:m-agahi/yadgar.git") == "m-agahi/yadgar"


def test_normalise_remote_strips_https_scheme_and_host():
    assert identity._normalise_remote("https://github.com/m-agahi/yadgar.git") == "m-agahi/yadgar"


def test_normalise_remote_lowercases_owner_and_repo():
    """§16.4: the key is lowercase. Case-different inputs collapse."""
    assert identity._normalise_remote("git@github-personal:M-Agahi/Yadgar.git") == "m-agahi/yadgar"


def test_normalise_remote_preserves_nested_namespaces():
    """§16.9: never split on the LAST ``/`` — a key is an opaque path."""
    assert identity._normalise_remote("git@example.com:group/sub/yadgar.git") == "group/sub/yadgar"


def test_normalise_remote_strips_trailing_dot_git_only():
    """``.git`` is removed only when it is the suffix — never mid-path."""
    assert identity._normalise_remote("git@github.com:m-agahi/yadgar.git") == "m-agahi/yadgar"


def test_normalise_remote_keeps_dot_in_repo_name():
    """A repo named ``yadgar.io`` must NOT have its ``.io`` stripped."""
    assert identity._normalise_remote("git@github.com:m-agahi/yadgar.io") == "m-agahi/yadgar.io"


# ── sub-function: _walk_project_id_file ───────────────────────────────────


def test_walk_project_id_file_returns_content_when_present(tmp_path):
    """A file at the start directory overrides remote-derived identity."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("m-agahi/yadgar\n")

    assert identity._walk_project_id_file(str(tmp_path)) == "m-agahi/yadgar"


def test_walk_project_id_file_walks_up_to_parent(tmp_path):
    """A file in a parent directory overrides — the start is anywhere under it."""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / ".yadgar").mkdir()
    (parent / ".yadgar" / "project-id").write_text("monorepo/sub\n")
    child = parent / "child" / "grandchild"
    child.mkdir(parents=True)

    assert identity._walk_project_id_file(str(child)) == "monorepo/sub"


def test_walk_project_id_file_trims_whitespace(tmp_path):
    """Whitespace and trailing newlines do NOT become part of the key."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("  m-agahi/yadgar  \n\n")

    assert identity._walk_project_id_file(str(tmp_path)) == "m-agahi/yadgar"


def test_walk_project_id_file_returns_none_when_absent(tmp_path):
    """No ``.yadgar/project-id`` anywhere up → caller falls through to remote."""
    child = tmp_path / "a" / "b" / "c"
    child.mkdir(parents=True)

    assert identity._walk_project_id_file(str(child)) is None


# ── the deleted tier: _local_fallback ─────────────────────────────────────
#
# Both tests below pinned ``local/<basename>``. C5 deleted the function that
# produced it, so they are INVERTED in place: the same input now has to fail
# loud. ADR-0227's sentence, applied literally — "a fallback that cannot fail
# is worse than an error, because it manufactures a plausible-looking wrong
# answer"; ``local/scratch`` was exactly such an answer.


def test_bare_basename_is_no_longer_a_project_id(tmp_path):
    """A non-git directory raises — it does NOT get a ``local/<name>`` key."""
    d = tmp_path / "scratch"
    d.mkdir()

    assert not hasattr(identity, "_local_fallback")
    with pytest.raises(UnresolvableProjectError) as ei:
        mint_project_id(str(d))
    assert "local/scratch" not in str(ei.value)


def test_trailing_separator_does_not_resurrect_the_basename_key(tmp_path):
    """The trailing-separator shape the old fallback tolerated raises too.

    Kept as its own case: the deleted helper had a dedicated ``rstrip(os.sep)``
    branch, and a re-introduction would most plausibly arrive with the same
    normalisation, so the input shape stays pinned even though the expected
    outcome inverted.
    """
    d = tmp_path / "scratch"
    d.mkdir()

    with pytest.raises(UnresolvableProjectError):
        mint_project_id(str(d) + os.sep)


# ── top-level composition: mint_project_id (was derive_project_id) ────────


def test_derive_project_id_returns_owner_repo_for_this_repo(monkeypatch):
    """The live origin remote in this checkout carries the owner/repo
    ``m-agahi/yadgar``; after insteadOf resolution + normalisation (scheme+host
    stripped, ``.git`` suffix dropped) the project_id collapses to
    ``m-agahi/yadgar``.

    The exact ``remote_url`` string is intentionally NOT asserted here — it
    tracks the live remote format (SSH alias ``git@github-personal:`` /
    HTTPS-with-token / etc.) and the format depends on how the checkout was
    provisioned. The contract under test is the *normalised* project_id, not
    the diagnostic remote-url echo. See `test_derive_project_id_returns_owner_repo_for_ssh_remote`
    (in the SSH-fixture file) and `test_derive_project_id_returns_owner_repo_for_https_remote`
    for the two canonical formats pinned independently.

    C5: the composition moved to ``mint_project_id``, which returns a bare
    string rather than the old ``(project_id, remote_url)`` tuple. The
    provenance half of the assertion is re-pointed at ``_origin_remote``, the
    surviving reader the mint feeds — so the echo is still pinned, by the
    function that now produces it.
    """
    cwd = os.getcwd()
    project_id = mint_project_id(cwd)

    assert project_id == "m-agahi/yadgar"
    # The remote must be a non-empty string carrying the repo path so the
    # provenance echo never silently disappears.
    remote_url = identity._origin_remote(_resolve_project_root(cwd))
    assert remote_url, f"remote_url must be non-empty for a git checkout, got {remote_url!r}"
    assert "m-agahi/yadgar" in remote_url, (
        f"remote_url must carry the owner/repo path, got {remote_url!r}"
    )


def test_derive_project_id_uses_project_id_file_override(tmp_path, monkeypatch):
    """A ``.yadgar/project-id`` at the cwd MUST override remote derivation.

    Even when ``origin`` would resolve to ``m-agahi/yadgar``, an explicit
    override at the directory short-circuits the resolution chain.

    C5: the old assertion that the echoed ``remote_url`` is ``""`` is
    re-pointed at the short-circuit it was evidence FOR — the mint must not
    consult the remote at all once the override answers, so ``_origin_remote``
    is replaced by a landmine rather than merely observed to return nothing.
    """
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("monorepo/sub\n")

    import yadgar.core.hooks._identity_mint as mint_mod

    def _explode(_root: str) -> str:
        raise AssertionError("the project-id override must short-circuit before the remote")

    monkeypatch.setattr(mint_mod, "_origin_remote", _explode)

    assert mint_project_id(str(tmp_path)) == "monorepo/sub"


def test_derive_project_id_raises_when_there_is_no_remote(tmp_path, monkeypatch):
    """A directory with no ``.git`` and no ``origin`` remote RAISES.

    INVERTED by C5 (this test previously asserted ``local/scratch``). The
    error has to be actionable, so it names the directory and both remedies —
    an agent that reads it must be able to fix the call without guessing, and
    must NOT be able to copy a plausible key out of the message.
    """
    # Patch subprocess so a real ``git config --get-regexp`` is never run;
    # the resolver must accept "no rules" and proceed to the raise.
    monkeypatch.setattr(
        "subprocess.check_output",
        unittest.mock.Mock(side_effect=subprocess.CalledProcessError(1, "git")),
    )

    d = tmp_path / "scratch"
    d.mkdir()

    with pytest.raises(UnresolvableProjectError) as ei:
        mint_project_id(str(d))

    message = str(ei.value)
    assert str(d) in message
    assert ".yadgar/project-id" in message
    assert "local/scratch" not in message


def test_derive_project_id_resolves_insteadof_rewrites(tmp_path, monkeypatch):
    """A codeberg-style insteadOf must be applied before remote parsing.

    Mock ``git config --get-regexp '^url\\..*\\.insteadof$'`` to return one
    rewrite rule: ``git@codeberg.org:`` → ``codeberg-agent:``. A mocked
    remote ``git@codeberg.org:m-agahi/yadgar.git`` must produce the key
    ``m-agahi/yadgar`` (NOT ``codeberg-agent/m-agahi/yadgar``, which is what
    a naive parse would emit).
    """

    # The mocked git config output format: each line is "<key> <value>".
    config_output = "url.codeberg-agent:.insteadof git@codeberg.org:\n"
    remote_origin = "git@codeberg.org:m-agahi/yadgar.git"

    real_check_output = subprocess.check_output

    def fake_check_output(args, **kwargs):
        joined = " ".join(args)
        if "config" in joined and "--get-regexp" in joined:
            return config_output.encode()
        if "config" in joined and "remote.origin.url" in joined:
            return remote_origin.encode()
        return real_check_output(args, **kwargs)

    monkeypatch.setattr("subprocess.check_output", fake_check_output)

    # No project-id file → walks UP looking for one. The top-level test
    # path has no parent project-id, so the resolver falls through to
    # the remote path.
    assert mint_project_id(str(tmp_path)) == "m-agahi/yadgar"


def test_derive_project_id_never_inserts_into_storage(monkeypatch):
    """§15: core must not touch the DB. ``derive_project_id`` is pure.

    If ``identity.py`` ever grew ``from yadgar._shared.storage import ...``,
    a future change could route identity writes through a DB-backed code
    path. This test parses the source AST and fails on any import of the
    storage namespace — the only structural way to keep core off the DB.
    """
    import ast

    src = Path(identity.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "yadgar._shared.storage" or node.module.startswith(
                "yadgar._shared.storage."
            ):
                bad_imports.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "yadgar._shared.storage" or alias.name.startswith(
                    "yadgar._shared.storage."
                ):
                    bad_imports.append(alias.name)
    assert not bad_imports, (
        f"core/identity.py must not import _shared.storage — §15 core-must-not-touch-DB: {bad_imports}"
    )
