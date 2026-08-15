"""The hook-authored directory -> project_id map (global-config identity tier).

Context: with ONE global ``mcpServers`` entry every MCP request looks identical
on the wire, and the daemon runs ``stateless_http=True`` so there is no
``Mcp-Session-Id`` either. The only per-call signal that varies is the
``directory`` argument. This map lets the daemon USE that argument without
DERIVING anything from it — the SessionStart hook (host-side, where the working
tree exists) registers the pair; the daemon only looks it up.
"""

from __future__ import annotations

import json

import pytest

from yadgar._shared.runtime.session_map import (
    lookup_project_for_directory,
    register_session_project,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
    yield


class TestRegisterAndLookup:
    def test_round_trip(self):
        assert register_session_project("/home/max/git/yadgar", "m-agahi/yadgar")
        assert lookup_project_for_directory("/home/max/git/yadgar") == "m-agahi/yadgar"

    def test_unregistered_directory_is_none_not_a_guess(self):
        """The whole point: an unknown path must NOT become local/<basename>."""
        register_session_project("/home/max/git/yadgar", "m-agahi/yadgar")
        assert lookup_project_for_directory("/home/max/git/some-other-repo") is None

    def test_no_parent_walk(self):
        """A registered parent must not answer for an unregistered child."""
        register_session_project("/home/max/git", "m-agahi/git")
        assert lookup_project_for_directory("/home/max/git/yadgar") is None

    def test_trailing_separator_is_the_same_directory(self):
        register_session_project("/home/max/git/yadgar", "m-agahi/yadgar")
        assert lookup_project_for_directory("/home/max/git/yadgar/") == "m-agahi/yadgar"

    def test_empty_inputs_are_rejected(self):
        assert register_session_project("", "x") is False
        assert register_session_project("/d", "") is False
        assert lookup_project_for_directory("") is None
        assert lookup_project_for_directory(None) is None

    def test_reregistration_updates_the_value(self):
        register_session_project("/d", "owner/old")
        register_session_project("/d", "owner/new")
        assert lookup_project_for_directory("/d") == "owner/new"


class TestFailSoftOnBadState:
    def test_missing_file_is_not_an_error(self):
        assert lookup_project_for_directory("/anything") is None

    def test_corrupt_file_degrades_to_no_identity(self, tmp_path):
        """A half-written map must read as 'nothing registered', never raise."""
        (tmp_path / "session_projects.json").write_text("{not json")
        assert lookup_project_for_directory("/d") is None

    def test_non_dict_payload_degrades(self, tmp_path):
        (tmp_path / "session_projects.json").write_text('["a", "b"]')
        assert lookup_project_for_directory("/d") is None


class TestBounded:
    def test_entries_are_capped_and_oldest_drop_first(self, tmp_path):
        from yadgar._shared.runtime.session_map import _MAX_ENTRIES

        for i in range(_MAX_ENTRIES + 10):
            register_session_project(f"/d/{i}", f"owner/p{i}")
        payload = json.loads((tmp_path / "session_projects.json").read_text())
        assert len(payload) == _MAX_ENTRIES
        assert "/d/0" not in payload
        assert f"/d/{_MAX_ENTRIES + 9}" in payload


class TestResolverUsesTheMap:
    """End-to-end through ``resolve_effective_project``'s tier order.

    The map tier sits BELOW an explicit ``project=`` and below the header, and
    ABOVE the raise. It must never outrank an explicit override, and an
    unregistered directory must still raise rather than resolve.
    """

    def test_registered_directory_resolves(self):
        from yadgar.core.server.tools._project_param import resolve_effective_project

        register_session_project("/home/max/git/yadgar", "m-agahi/yadgar")
        assert (
            resolve_effective_project(
                project=None,
                directory="/home/max/git/yadgar",
                session_project=None,
                tool="wiki_add",
            )
            == "m-agahi/yadgar"
        )

    def test_explicit_project_still_wins(self):
        from yadgar.core.server.tools._project_param import resolve_effective_project

        register_session_project("/home/max/git/yadgar", "m-agahi/yadgar")
        assert (
            resolve_effective_project(
                project="quinyx/flux",
                directory="/home/max/git/yadgar",
                session_project=None,
                tool="wiki_add",
            )
            == "quinyx/flux"
        )

    def test_unregistered_directory_still_raises(self):
        from yadgar.core.server.tools._project_param import (
            UnresolvedProjectError,
            resolve_effective_project,
        )

        with pytest.raises(UnresolvedProjectError):
            resolve_effective_project(
                project=None,
                directory="/home/max/git/never-registered",
                session_project=None,
                tool="wiki_add",
            )
