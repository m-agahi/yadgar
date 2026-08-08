"""Tests for v5.41.4 roadmap_update_lag signal + update_roadmap recommended action.

TDD: these tests are written BEFORE the implementation. Run pytest to see red,
then implement _compute_roadmap_signal and additions to _project_brief_signals.

Signal spec:
- roadmap_update_lag_hours: hours between roadmap wiki updated_at and master HEAD
  commit time (committer date). 0 if roadmap is newer; positive if master moved.
  Sentinel -1 if roadmap wiki slug not found.
- update_roadmap recommended action fires when:
    lag > 0  AND  (pyproject version changed since roadmap updated_at  OR
                   commit message matches ^merge: v\\d+\\.\\d+\\.\\d+  OR
                   commit message contains 'chore: bump version')
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from yadgar.core import server

_TEST_DIR = "/home/max/git/yadgar"

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _git(args: list[str], cwd: str) -> str:
    """Run a git command in cwd and return stdout."""
    return subprocess.check_output(args, cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()


def _make_git_repo(tmp_path: Path) -> Path:
    """Init a bare git repo with git identity configured."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return tmp_path


def _commit(repo: Path, message: str, pyproject_version: str | None = None) -> str:
    """Write pyproject.toml (if version given) and commit. Returns commit hash."""
    if pyproject_version is not None:
        (repo / "pyproject.toml").write_text(f'[project]\nversion = "{pyproject_version}"\n')
    # Touch a file so there's something to commit
    dummy = repo / "dummy.txt"
    dummy.write_text(message)
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return _git(["git", "log", "-1", "--format=%H"], str(repo))


def _get_committer_ts(repo: Path) -> float:
    """Return unix timestamp of HEAD commit (committer date)."""
    return float(_git(["git", "log", "-1", "--format=%ct"], str(repo)))


# ── helper: insert a wiki page with a specific updated_at ─────────────────────


def _insert_roadmap_wiki(updated_at_ts: float) -> None:
    """Insert the roadmap wiki page with updated_at set to updated_at_ts."""
    import datetime

    dt = datetime.datetime.fromtimestamp(updated_at_ts, tz=datetime.UTC)
    updated_at_iso = dt.strftime("%Y-%m-%dT%H:%M:%S")

    # wiki_add derives slug from title: "Yadgar Roadmap & Future Improvements"
    # → "yadgar-roadmap-future-improvements"
    server.wiki_add(
        title="Yadgar Roadmap & Future Improvements",
        content="## Recently shipped\n- placeholder\n",
        category="reference",
        tags=["roadmap"],
        confidence="high",
        source_memory_ids=[],
        wait=True,
        directory=_TEST_DIR,
    )
    # Patch updated_at directly in storage to simulate wiki being refreshed at updated_at_ts
    from yadgar._shared.runtime.lifecycle import _get_storage

    st = _get_storage()
    st._q(
        "UPDATE wiki_page SET updated_at = $ts WHERE slug = $slug",
        {"ts": updated_at_iso, "slug": "yadgar-roadmap-future-improvements"},
    )


# ── test 1: signal present and positive when master is newer ──────────────────


def test_signal_present_when_master_newer(_engines, tmp_path, flush_queue, monkeypatch):
    """roadmap_update_lag_hours is present + positive when master HEAD > roadmap updated_at."""
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.0.0")

    head_ts = _get_committer_ts(repo)
    # Roadmap was updated 2h BEFORE master HEAD
    roadmap_ts = head_ts - 7200.0  # 2 hours earlier

    _insert_roadmap_wiki(roadmap_ts)

    # Patch _get_master_head_info to return our controlled repo's HEAD
    from yadgar.core.server.tools import project as _proj

    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": head_ts,
            "commit_msg": "chore: init",
            "pyproject_version": "5.0.0",
        },
    )
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "5.0.0",
    )

    result = server.project_brief(str(repo), mode="signals")
    assert "roadmap_update_lag_hours" in result
    lag = result["roadmap_update_lag_hours"]
    assert lag > 0, f"Expected positive lag, got {lag}"
    # Should be approximately 2h (within 1s tolerance)
    assert abs(lag - 2.0) < 0.01, f"Expected ~2.0h lag, got {lag}"


# ── test 2: signal zero when roadmap is newer ────────────────────────────────


def test_signal_zero_when_roadmap_newer(_engines, tmp_path, monkeypatch):
    """roadmap_update_lag_hours == 0 when roadmap updated_at > master HEAD commit time."""
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.0.0")

    head_ts = _get_committer_ts(repo)
    # Roadmap updated 1h AFTER master HEAD
    roadmap_ts = head_ts + 3600.0

    _insert_roadmap_wiki(roadmap_ts)

    from yadgar.core.server.tools import project as _proj

    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": head_ts,
            "commit_msg": "chore: init",
            "pyproject_version": "5.0.0",
        },
    )
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "5.0.0",
    )

    result = server.project_brief(str(repo), mode="signals")
    assert result.get("roadmap_update_lag_hours") == 0.0


# ── test 3: update_roadmap action fires on ship commit (merge: vX.Y.Z) ────────


def test_recommended_action_fires_on_ship_commit(_engines, tmp_path, monkeypatch):
    """update_roadmap action fires when HEAD message matches merge: vX.Y.Z pattern."""
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.0.0")
    head_ts = _get_committer_ts(repo)
    roadmap_ts = head_ts - 7200.0  # roadmap 2h stale

    _insert_roadmap_wiki(roadmap_ts)

    from yadgar.core.server.tools import project as _proj

    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": head_ts,
            "commit_msg": "merge: v5.41.3 — hotfix xyz",
            "pyproject_version": "5.41.3",
        },
    )
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "5.41.2",  # version changed → ship confirmed
    )

    result = server.project_brief(str(repo), mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "update_roadmap" in actions, f"Expected update_roadmap in {actions}"

    action = next(a for a in result["recommended_actions"] if a["action"] == "update_roadmap")
    assert "reason" in action
    assert "suggested_call" in action
    assert "wiki_append_section" in action["suggested_call"]


# ── test 4: update_roadmap skipped on non-ship commit ────────────────────────


def test_recommended_action_skips_non_ship_commit(_engines, tmp_path, monkeypatch):
    """update_roadmap action NOT fired for a docs/plan commit that doesn't bump version."""
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.0.0")
    head_ts = _get_committer_ts(repo)
    roadmap_ts = head_ts - 3600.0  # roadmap 1h stale

    _insert_roadmap_wiki(roadmap_ts)

    from yadgar.core.server.tools import project as _proj

    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": head_ts,
            "commit_msg": "docs(plan): add v5.41.4 roadmap plan",
            "pyproject_version": "5.0.0",
        },
    )
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "5.0.0",  # no version change → not a ship
    )

    result = server.project_brief(str(repo), mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "update_roadmap" not in actions, f"Expected no update_roadmap, got {actions}"


# ── test 5: lag computed against master, not current feature branch ───────────


def test_signal_uses_master_not_current_branch(_engines, tmp_path, monkeypatch):
    """Lag is computed against master HEAD regardless of the current branch."""
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.0.0")
    master_head_ts = _get_committer_ts(repo)

    # Checkout a feature branch — master_head_ts stays fixed
    subprocess.run(
        ["git", "checkout", "-b", "feature/test"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    time.sleep(0.01)
    _commit(repo, "feat: something on feature branch", pyproject_version="5.0.0")

    roadmap_ts = master_head_ts - 3600.0

    _insert_roadmap_wiki(roadmap_ts)

    from yadgar.core.server.tools import project as _proj

    # _get_master_head_info is called with resolved path — must return master info
    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": master_head_ts,
            "commit_msg": "chore: init",
            "pyproject_version": "5.0.0",
        },
    )
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "5.0.0",
    )

    result = server.project_brief(str(repo), mode="signals")
    lag = result.get("roadmap_update_lag_hours", None)
    assert lag is not None
    assert lag > 0, f"Expected positive lag from master (not feature branch), got {lag}"


# ── test 6: roadmap wiki not found → sentinel -1 + no action ─────────────────


def test_signal_roadmap_wiki_not_found(_engines, tmp_path, monkeypatch):
    """When roadmap wiki slug is absent, signal returns -1 and action is skipped."""
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.0.0")
    head_ts = _get_committer_ts(repo)

    from yadgar.core.server.tools import project as _proj

    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": head_ts,
            "commit_msg": "merge: v5.0.0",
            "pyproject_version": "5.0.0",
        },
    )
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "4.99.0",
    )

    # No wiki page inserted → slug not found
    result = server.project_brief(str(repo), mode="signals")
    assert result.get("roadmap_update_lag_hours", -1) == -1, (
        f"Expected -1 sentinel (or absent key), got {result.get('roadmap_update_lag_hours')}"
    )
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "update_roadmap" not in actions


# ── test 7: squash-merge case — pyproject diff is PRIMARY ────────────────────


def test_recommended_action_fires_on_squash_merge(_engines, tmp_path, monkeypatch):
    """update_roadmap fires when pyproject version changed, even with no merge: prefix.

    Squash/rebase merges drop the 'merge: vX.Y.Z' subject line. The primary check
    (pyproject version diff) must still detect the ship.
    """
    repo = _make_git_repo(tmp_path / "repo")
    _commit(repo, "chore: init", pyproject_version="5.41.3")
    head_ts = _get_committer_ts(repo)
    roadmap_ts = head_ts - 1800.0  # 30 min stale

    _insert_roadmap_wiki(roadmap_ts)

    from yadgar.core.server.tools import project as _proj

    # Squash-merge commit message — no 'merge:' prefix, no 'chore: bump version'
    monkeypatch.setattr(
        _proj,
        "_get_master_head_info",
        lambda resolved: {
            "commit_ts": head_ts,
            "commit_msg": "feat(memory): add roadmap signal (#412)",  # squash message
            "pyproject_version": "5.41.4",  # current HEAD has bumped version
        },
    )
    # At roadmap_ts, pyproject had the old version
    monkeypatch.setattr(
        _proj,
        "_get_pyproject_version_at_ts",
        lambda resolved, ts: "5.41.3",
    )

    result = server.project_brief(str(repo), mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "update_roadmap" in actions, (
        f"Expected update_roadmap for squash-merge ship (pyproject diff), got {actions}"
    )


# ── _get_master_head_info against a REAL repo (no monkeypatch) ────────────────
#
# Every other test in this file patches _get_master_head_info wholesale, so
# nothing exercised the real function. That gap is why Car 3 (ADR-0215) could
# delete the field the old default-branch helper read, leaving it returning None
# unconditionally — `git log None …` then raised inside the function's broad
# except, _get_master_head_info returned None, and the roadmap-update-lag
# signal was silently dead with the whole suite green. These two tests close it.


def test_get_master_head_info_returns_real_head_for_a_real_repo(tmp_path):
    """The unpatched function returns a populated dict for an actual git repo.

    Discriminating: it goes RED the moment the mainline-ref source returns
    something that cannot be interpolated into `git log <ref>` (the Car 3
    failure mode), because the broad except then swallows it and yields None.
    """
    from yadgar.core.server.tools import project as _proj

    repo = _make_git_repo(tmp_path)
    _commit(repo, "chore: initial", pyproject_version="1.2.3")

    info = _proj._get_master_head_info(str(repo))

    assert info is not None, (
        "_get_master_head_info returned None for a real git repo — the mainline ref "
        "it names in `git log` is unusable (this is exactly the Car 3 regression)"
    )
    assert info["commit_ts"] > 0, f"expected a real committer timestamp, got {info!r}"
    assert "chore: initial" in info["commit_msg"], info
    assert info["pyproject_version"] == "1.2.3", info


def test_origin_head_short_always_returns_a_usable_ref(tmp_path):
    """The mainline-ref helper never returns None — callers interpolate it directly.

    A repo with no `origin/HEAD` (the common case for a fresh local repo) must
    still yield a usable name rather than None, or `git log None` recreates the
    Car 3 failure.
    """
    from yadgar.core.server.tools import project as _proj

    repo = _make_git_repo(tmp_path)
    _commit(repo, "chore: initial")

    ref = _proj._origin_head_short(str(repo))
    assert isinstance(ref, str) and ref, f"expected a non-empty ref name, got {ref!r}"
