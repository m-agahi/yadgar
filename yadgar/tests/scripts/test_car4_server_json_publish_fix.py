"""Car 4 — server.json publish fix guard tests (#66).

D1: stdio transport fully retired. server.json must NOT advertise a
    pypi/stdio package, and MUST declare a streamable-HTTP remote entry.

Refs: ADR-0144 D1; feat/multi-client-framework Car 4.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SERVER_JSON = REPO_ROOT / "server.json"


@pytest.fixture(scope="module")
def server_data() -> dict:
    assert SERVER_JSON.exists(), f"server.json not found at {SERVER_JSON}"
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))


class TestD1NoStdioTransport:
    """server.json must carry zero stdio-transport package entries."""

    def test_no_packages_with_stdio_transport(self, server_data: dict) -> None:
        """D1: no packages[].transport.type == 'stdio'."""
        packages = server_data.get("packages", [])
        bad = [p for p in packages if p.get("transport", {}).get("type") == "stdio"]
        assert not bad, (
            f"server.json still advertises {len(bad)} stdio package(s): {bad}.\n"
            "Fix: remove or replace the stdio package entry (D1 — stdio fully retired)."
        )


class TestD1StreamableHttpRemote:
    """server.json must declare a streamable-HTTP remote entry."""

    def test_remotes_field_present(self, server_data: dict) -> None:
        """D1: server.json must have a non-empty 'remotes' list."""
        remotes = server_data.get("remotes", [])
        assert remotes, (
            "server.json has no 'remotes' array (or it is empty).\n"
            "Fix: add a 'remotes' entry with type='streamable-http' (D1)."
        )

    def test_at_least_one_streamable_http_remote(self, server_data: dict) -> None:
        """D1: at least one remote entry must use streamable-http transport."""
        remotes = server_data.get("remotes", [])
        http_remotes = [r for r in remotes if r.get("type") == "streamable-http"]
        assert http_remotes, (
            f"No streamable-http remote found. Remotes: {remotes}.\n"
            "Fix: add a remote with type='streamable-http' pointing to the MCP endpoint."
        )

    def test_streamable_http_remote_has_url(self, server_data: dict) -> None:
        """D1: the streamable-http remote must carry a non-empty url."""
        remotes = server_data.get("remotes", [])
        http_remotes = [r for r in remotes if r.get("type") == "streamable-http"]
        for remote in http_remotes:
            assert remote.get("url"), (
                f"streamable-http remote has no url: {remote}.\n"
                "Fix: set url to the daemon's MCP endpoint."
            )


class TestVersionConsistency:
    """Drift guard: version fields must survive the packages change."""

    def test_version_field_present(self, server_data: dict) -> None:
        """server.json top-level 'version' field must be present and non-empty."""
        assert server_data.get("version"), (
            "server.json 'version' field missing or empty — breaks check_versions.py."
        )

    def test_backend_version_field_present(self, server_data: dict) -> None:
        """server.json 'backend_version' field must be present and non-empty."""
        assert server_data.get("backend_version"), (
            "server.json 'backend_version' field missing or empty — breaks drift guard."
        )
