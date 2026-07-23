"""Car F end-to-end / integration smoke for the code_graph train (ADR-0162).

Exercises the REAL host-side path against the REAL ``codebase-memory-mcp``
binary on a tiny throwaway git repo the test creates:

    git init (bare origin) + 2 source files + a commit on the default branch
      → default_branch.refresh_index (default-branch temp-worktree flow)
      → runner.get_architecture / render_digest / build_block_payload
      → assert a bounded non-empty digest + the emit-payload shape.

The whole flow is driven through the production ``code_graph.cmd_code_graph``
``refresh --json`` seam (the exact shape Car D's stop-hook prompt consumes), so
this test verifies the wiring end-to-end, not a re-implemented copy of it.

GUARD (mirrors ``conftest.py`` surreal ``shutil.which`` guard + the existing
``TestLiveSmoke`` in ``test_code_graph_cli.py``): the codebase-memory-mcp static
binary is a ~259 MB host-side external dep installed opt-in via
``yadgar setup --code-graph`` — it is NEVER present in CI. When absent the whole
module skips cleanly (skip_inventory entry ``code-graph-e2e-smoke-01``, ADR-0087).

Hermetic: a temp dir, a local bare repo as ``origin`` (no network), and
``XDG_CACHE_HOME`` pointed at the temp dir so the binary's SQLite lands in the
throwaway tree — never the user's ``~/.cache``. Nothing here reaches the network
beyond invoking the local binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None,
    reason="codebase-memory-mcp binary not installed (Car F live e2e; mirrors conftest.py:491 surreal guard) — 259MB host-side dep, never in CI",
)


def _git(argv: list[str], *, cwd: str | Path) -> None:
    """Run a git command in ``cwd``; raise on non-zero (fixture setup only)."""
    subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo_with_origin(root: Path) -> Path:
    """Build a hermetic work repo whose ``origin`` is a local bare repo.

    Returns the work-tree path.  The bare repo satisfies ``refresh_index``'s HARD
    CONSTRAINT: it resolves ``origin/<default>`` and ``git fetch origin <default>``
    entirely locally (no network).  ``refs/remotes/origin/HEAD`` is populated via
    ``git remote set-head`` so ``resolve_default_branch``'s primary path
    (``symbolic-ref refs/remotes/origin/HEAD``) works and we do not silently lean
    on the version-varying ``remote show origin`` fallback.
    """
    origin = root / "origin.git"
    work = root / "work"
    origin.mkdir()
    work.mkdir()

    # Bare origin on a fixed default branch (init.defaultBranch is host-varying;
    # pin it so the fixture is deterministic).
    _git(["init", "--bare", "--initial-branch=main", "."], cwd=origin)

    _git(["init", "--initial-branch=main", "."], cwd=work)
    _git(["config", "user.email", "e2e@example.invalid"], cwd=work)
    _git(["config", "user.name", "code_graph e2e"], cwd=work)

    # Two tiny source files → a couple of parseable nodes for the indexer.
    (work / "app.py").write_text(
        "def handler(request):\n"
        "    return greet(request)\n\n"
        "def greet(name):\n"
        '    return f"hello {name}"\n'
    )
    (work / "util.py").write_text(
        'def greet(name):\n    return "hi " + name\n\ndef unused():\n    return 0\n'
    )
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-m", "initial"], cwd=work)

    _git(["remote", "add", "origin", str(origin)], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)
    # Populate refs/remotes/origin/HEAD (a bare init + push does NOT set it).
    _git(["remote", "set-head", "origin", "-a"], cwd=work)
    return work


@pytest.fixture
def _cache_in_tmp(tmp_path, monkeypatch):
    """Point the codebase-memory-mcp SQLite cache at the throwaway tree.

    ``config.cache_dir`` keys off ``XDG_CACHE_HOME`` → the real binary's state
    lands under ``tmp_path``, never the user's ``~/.cache``.  ``XDG_CACHE_HOME``
    is set WITHOUT ``clear=True`` so PATH/HOME survive for the real git + binary
    subprocesses.

    ADR-0163: enable is a runtime-config-store read (host client → daemon), so the
    old ``CODE_GRAPH_ENABLED`` env no longer enables the feature. With no daemon in
    this test, the host client fail-opens to disabled; patch ``config.is_enabled``
    True so the real refresh path runs instead of skipping ``opted_out``.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("yadgar.core.code_graph.config.is_enabled", lambda *a, **k: True)
    return tmp_path


class TestCodeGraphEndToEnd:
    """Real binary, real git, real digest — the Car F pilot-gate live path."""

    def test_refresh_emits_bounded_digest_payload(self, tmp_path, capsys, _cache_in_tmp):
        from yadgar.core.cli import code_graph
        from yadgar.core.code_graph import config

        work = _make_repo_with_origin(tmp_path)

        # Drive the PRODUCTION seam: `yadgar code-graph refresh <repo> --json`.
        args = SimpleNamespace(repo=str(work), cg_command="refresh", project=None, json=True)
        code_graph.cmd_code_graph(args)

        out = capsys.readouterr().out.strip()
        assert out, "refresh --json must emit a payload on stdout"
        payload = json.loads(out.splitlines()[-1])

        # A live index of a repo with a remote/default branch must NOT skip.
        assert not payload.get("skipped"), f"unexpected skip: {payload!r}"

        # Emit-payload shape (the C→D seam Car D's hook prompt consumes).
        assert payload["block_name"] == "code_graph"
        assert set(payload) >= {"block_name", "directory", "content", "chars", "skipped"}
        assert payload["skipped"] is False

        # Digest is bounded + non-empty.
        content = payload["content"]
        assert content, "digest content must be non-empty"
        assert payload["chars"] == len(content)
        assert 0 < len(content) <= config.DIGEST_CHAR_BUDGET

        # Digest is keyed to the REAL repo (canonical_root), NOT the temp worktree.
        assert str(tmp_path) not in payload["directory"] or "origin.git" not in payload["directory"]
        assert payload["directory"] == str(work.resolve())

        # The header marker proves render_digest ran over real architecture data.
        assert "code_graph:" in content

    def test_index_then_get_architecture_nonempty(self, tmp_path, _cache_in_tmp):
        """Lower-level path: refresh_index → get_architecture returns real data."""
        from yadgar.core.code_graph import default_branch, runner

        work = _make_repo_with_origin(tmp_path)

        idx = default_branch.refresh_index(str(work))
        assert idx.get("indexed") is True, f"index unexpectedly skipped: {idx!r}"
        assert idx["canonical_root"] == str(work.resolve())
        assert idx["default_branch"] == "main"

        proj = idx.get("project") or Path(work).resolve().name
        arch = runner.get_architecture(proj, allowed_root=str(work.resolve()))
        assert isinstance(arch, dict) and arch, "get_architecture returned no data"
