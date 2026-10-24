"""Ledger 1791 — per-project external task ledger gate (yadgarhq/docs ADR-0992).

A project whose project_id starts with a prefix in the GLOBAL runtime config
key ``tasks.external_ledger_prefixes`` keeps its tasks in yaadgaar. For it:

* SessionStart prints one line naming that ledger, reads no yadgar ledger rows,
  and hands the hook no nudge and no rows (so nothing is seeded);
* ``task_write`` refuses with ``error="external_task_ledger"`` on create AND update;
* ``task_list`` / ``task_get`` still read (history).

Every other project must behave byte-identically to the code before the gate.
Those tests compare a run with the key SET against a run with the key UNSET,
rather than only checking that some text is absent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from yadgar.core import task_ledger_gate as gate

_KEY = "tasks.external_ledger_prefixes"
_EXTERNAL = "yadgarhq/docs"
_INTERNAL = "quinyx/aws-work"


def _resolver_for(value):
    """A resolver that answers ``value`` for the gate key and records each call."""
    calls: list[tuple] = []

    def _resolve(key, directory, default):
        calls.append((key, directory, default))
        return value if key == _KEY else default

    _resolve.calls = calls  # type: ignore[attr-defined]
    return _resolve


@pytest.fixture
def config_value(monkeypatch):
    """Set what the DAEMON resolver returns for the gate key (None = key unset).

    Patches ``_runtime_config.config_get`` — the one daemon-side read path —
    so no forward, cache or live daemon is involved.
    """
    from yadgar.core.server.tools import _runtime_config

    state: dict = {"value": None}

    def _fake_config_get(key, directory=None, default=None, **_kwargs):
        if key == _KEY and state["value"] is not None:
            return state["value"]
        return default

    monkeypatch.setattr(_runtime_config, "config_get", _fake_config_get)
    return state


# ── the predicate ────────────────────────────────────────────────────────────


class TestPredicate:
    def test_reads_the_global_scope_with_a_list_default(self) -> None:
        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(_EXTERNAL, r) is True
        assert r.calls == [(_KEY, None, [])]

    def test_prefix_match_only(self) -> None:
        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(_INTERNAL, r) is False
        assert gate.is_external_ledger_project("xyadgarhq/docs", r) is False
        assert gate.is_external_ledger_project("yadgarhq", r) is False

    @pytest.mark.parametrize("value", [None, [], "yadgarhq/", {"a": 1}, 7, [""], ["  "], [3]])
    def test_unset_or_malformed_matches_nothing(self, value) -> None:
        assert gate.is_external_ledger_project(_EXTERNAL, _resolver_for(value)) is False

    def test_blank_entries_are_dropped_not_honoured(self) -> None:
        r = _resolver_for(["", 5, "yadgarhq/"])
        assert gate.external_ledger_prefixes(r) == ("yadgarhq/",)
        assert gate.is_external_ledger_project(_INTERNAL, r) is False

    def test_resolver_error_matches_nothing(self) -> None:
        def _boom(key, directory, default):
            raise RuntimeError("daemon down")

        assert gate.is_external_ledger_project(_EXTERNAL, _boom) is False

    @pytest.mark.parametrize("project", ["", None])
    def test_no_project_means_no_read(self, project) -> None:
        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(project, r) is False
        assert r.calls == []

    def test_match_ignores_case_on_both_sides(self) -> None:
        assert gate.is_external_ledger_project("YadgarHQ/Docs", _resolver_for(["yadgarhq/"]))
        assert gate.is_external_ledger_project("yadgarhq/docs", _resolver_for(["YADGARHQ/"]))

    @pytest.mark.parametrize("value", ["yadgarhq/", {"a": 1}, 7, ["yadgarhq/", 3]])
    def test_malformed_value_warns_once(self, value, caplog) -> None:
        r = _resolver_for(value)
        with caplog.at_level(logging.WARNING, logger=gate.__name__):
            gate.external_ledger_prefixes(r)
            gate.external_ledger_prefixes(r)
        warnings = [rec for rec in caplog.records if "malformed" in rec.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.parametrize("value", [None, [], ["yadgarhq/"]])
    def test_unset_or_wellformed_value_does_not_warn(self, value, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=gate.__name__):
            gate.external_ledger_prefixes(_resolver_for(value))
        assert not [rec for rec in caplog.records if "malformed" in rec.getMessage()]


@pytest.fixture(autouse=True)
def _reset_warn_once(monkeypatch):
    """Each test starts with the once-per-process malformed warning unspent."""
    monkeypatch.setattr(gate, "_malformed_warned", False, raising=False)


# ── the daemon resolver across a backend blip (PR #81 review, blocking) ──────


@pytest.fixture
def blippy_backend(monkeypatch):
    """The REAL daemon resolver over a fake backend that can be down or up.

    Only ``_forward_admin`` is patched: ``config_get``, its cache and the gate
    run as shipped. ``state["down"]`` makes every forward raise.
    """
    from yadgar.core.server.tools import _runtime_config as rc

    state = {"down": True, "forwards": 0}

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
        state["forwards"] += 1
        if state["down"]:
            raise RuntimeError("backend down")
        if payload["key"] == _KEY and payload["directory"] is None:
            return {"row": {"key": _KEY, "directory": None, "value": ["yadgarhq/"]}}
        return {"row": None}

    rc._get_cache().clear()
    monkeypatch.setattr(rc, "_forward_admin", fake_forward)
    yield state
    rc._get_cache().clear()


class TestDaemonResolverAcrossABackendBlip:
    def test_down_then_up_gate_recovers_and_nothing_is_cached(self, blippy_backend, caplog):
        from yadgar.core.server.tools import _runtime_config as rc

        with caplog.at_level(logging.WARNING, logger=gate.__name__):
            assert gate.is_external_ledger_project(_EXTERNAL, gate.daemon_resolver) is False
        assert rc._get_cache().get(rc._cache_key(_KEY, None)) is None
        assert [r for r in caplog.records if r.name == gate.__name__ and _KEY in r.getMessage()]

        blippy_backend["down"] = False
        assert gate.is_external_ledger_project(_EXTERNAL, gate.daemon_resolver) is True

    def test_a_good_read_is_cached(self, blippy_backend):
        blippy_backend["down"] = False
        assert gate.is_external_ledger_project(_EXTERNAL, gate.daemon_resolver) is True
        assert gate.is_external_ledger_project(_EXTERNAL, gate.daemon_resolver) is True
        assert blippy_backend["forwards"] == 1


# ── task_write refuses, task_list / task_get still read ─────────────────────


@pytest.fixture
def forward_calls():
    """Capture every forward the task tools make."""
    from yadgar.core.server.tools import task as task_mod

    calls: list[tuple[str, dict]] = []

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
        calls.append((op, payload))
        if op == "create_task_row":
            return {"id": 501}
        if op == "update_task_row":
            return {"id": payload.get("id")}
        if op == "list_task_rows":
            return {"rows": [{"id": 1, "title": "old", "status": "pending"}]}
        return {"row": {"id": 1, "title": "old", "status": "completed"}}

    with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
        yield calls


_EXPECTED_REFUSAL = {
    "ok": False,
    "error": "external_task_ledger",
    "project_id": _EXTERNAL,
    "ledger": "yaadgaar",
    "message": (
        f"Tasks for {_EXTERNAL} live in yaadgaar: use mcp__yaadgaar__create_task, "
        "edit_task or transition_task. The yadgar task ledger is frozen for this "
        "project; task_list and task_get still read its history."
    ),
}


class TestTaskWriteRefuses:
    def test_create_is_refused_and_nothing_is_forwarded(self, config_value, forward_calls):
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = ["yadgarhq/"]
        result = task_write(project_id=_EXTERNAL, title="new work")

        assert result == _EXPECTED_REFUSAL
        assert forward_calls == []

    def test_update_is_refused_and_nothing_is_forwarded(self, config_value, forward_calls):
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = ["yadgarhq/"]
        result = task_write(project_id=_EXTERNAL, title="t", id=12, status="completed")

        assert result == _EXPECTED_REFUSAL
        assert forward_calls == []

    def test_the_project_override_is_what_is_gated(self, config_value, forward_calls):
        """``project=`` wins over ``project_id``, so the gate reads the override."""
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = ["yadgarhq/"]
        result = task_write(project_id=_INTERNAL, title="t", project=_EXTERNAL)

        assert result == _EXPECTED_REFUSAL
        assert forward_calls == []

    def test_an_internal_override_is_not_gated(self, config_value, forward_calls):
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = ["yadgarhq/"]
        result = task_write(project_id=_EXTERNAL, title="t", project=_INTERNAL)

        assert result == {"ok": True, "id": 501}
        assert forward_calls[0][1]["project_id"] == _INTERNAL

    def test_reads_stay_open(self, config_value, forward_calls):
        from yadgar.core.server.tools.task import task_get, task_list

        config_value["value"] = ["yadgarhq/"]
        assert task_list(project_id=_EXTERNAL) == [{"id": 1, "title": "old", "status": "pending"}]
        assert task_get(project_id=_EXTERNAL, id=1)["id"] == 1
        assert [op for op, _ in forward_calls] == ["list_task_rows", "get_task_row"]


class TestTaskWriteUnchangedForOtherProjects:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"title": "create me"},
            {"title": "update me", "id": 44, "status": "in_progress"},
        ],
    )
    def test_same_result_and_payload_with_and_without_the_key(
        self, config_value, forward_calls, kwargs
    ):
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = None
        unset = task_write(project_id=_INTERNAL, **kwargs)
        unset_calls = list(forward_calls)
        forward_calls.clear()

        config_value["value"] = ["yadgarhq/"]
        gated = task_write(project_id=_INTERNAL, **kwargs)

        assert gated == unset
        assert forward_calls == unset_calls
        assert unset["ok"] is True


# ── SessionStart ─────────────────────────────────────────────────────────────


def _make_client(token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", token)

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("external_task_ledger_gate")
    from yadgar.core import server

    server.init_engines(db_path=str(tmp_path / "test.db"), embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


_ROWS = [{"id": 7, "title": "open thing", "status": "pending"}]


@pytest.fixture
def session_context(_engines, tmp_path, monkeypatch):
    """GET /hooks/session-context with a fixed brief and a spy ledger nudge."""
    from yadgar.core.server import http as _http

    nudge_calls: list[tuple] = []

    async def _spy_nudge(directory, project=""):
        nudge_calls.append((directory, project))
        return "[yadgar] NUDGE open tasks\n", list(_ROWS)

    def _fixed_brief(directory, mode="catalog"):
        return {"_render": "BRIEF\n"}

    token = "tok-1791"
    client = _make_client(token, monkeypatch)
    srv = sys.modules["yadgar.core.server"]
    monkeypatch.setattr(_http, "_task_list_restore_nudge", _spy_nudge)
    monkeypatch.setattr(srv, "project_brief", _fixed_brief, raising=False)

    def _get(project: str, seed: bool = False) -> dict:
        params = f"directory={tmp_path}&project={project.replace('/', '%2F')}"
        if seed:
            params += "&seed=1"
        resp = client.get(
            f"/hooks/session-context?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    _get.nudge_calls = nudge_calls  # type: ignore[attr-defined]
    return _get


_LINE = (
    f"[yadgar] Tasks for {_EXTERNAL} live in yaadgaar (mcp__yaadgaar__*); "
    "the yadgar task ledger is frozen for this project.\n"
)


class TestSessionStartExternalProject:
    def test_one_ledger_line_leads_and_no_ledger_read(self, config_value, session_context):
        config_value["value"] = ["yadgarhq/"]
        body = session_context(_EXTERNAL)

        assert body["text"].startswith(_LINE)
        assert body["text"].count("live in yaadgaar") == 1
        assert "NUDGE" not in body["text"]
        assert session_context.nudge_calls == []

    def test_seed_capable_hook_gets_no_nudge_and_no_rows(self, config_value, session_context):
        config_value["value"] = ["yadgarhq/"]
        body = session_context(_EXTERNAL, seed=True)

        assert body["text"].startswith(_LINE)
        assert body["task_nudge"] == ""
        assert body["tasks"] == []
        assert session_context.nudge_calls == []


class TestSessionStartUnchangedForOtherProjects:
    @pytest.mark.parametrize("seed", [False, True])
    def test_same_body_with_and_without_the_key(self, config_value, session_context, seed):
        config_value["value"] = None
        unset = session_context(_INTERNAL, seed=seed)

        config_value["value"] = ["yadgarhq/"]
        gated = session_context(_INTERNAL, seed=seed)

        assert gated == unset
        assert "yaadgaar" not in str(unset)
        assert len(session_context.nudge_calls) == 2


def test_session_task_section_passes_through_for_other_projects() -> None:
    async def _nudge():
        return "N", list(_ROWS)

    out = asyncio.run(gate.session_task_section(_INTERNAL, _nudge, _resolver_for(["yadgarhq/"])))
    assert out == ("", "N", _ROWS)


def test_session_task_section_skips_the_read_for_external_projects() -> None:
    async def _nudge():
        raise AssertionError("an external-ledger project must not read the yadgar ledger")

    out = asyncio.run(gate.session_task_section(_EXTERNAL, _nudge, _resolver_for(["yadgarhq/"])))
    assert out == (_LINE, "", [])


# ── agent_dispatch_prelude HANDBACK wording (all projects) ───────────────────


def test_packaged_contract_names_the_projects_task_ledger_not_yadgars() -> None:
    from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS

    content = CONTRACT_GENESIS[2]
    assert "the project's task ledger" in content
    assert "yadgar ledger" not in content.lower()
    assert "yadgar task ledger" not in content.lower()


# ── ledger 1803: the yaadgaar project registry decides, the prefix is fallback ─
#
# yadgarhq/docs ADR-1000: "this project's tasks live in yaadgaar" follows the
# yaadgaar gateway project registry. The yaadgaar client's SessionStart hook
# records the gateway's answer per project (the record contract lives in
# ``core.task_ledger_registry``); yadgar's host hook mirrors it into the yadgar
# data dir, which the daemon container mounts. The prefix key is consulted ONLY
# when no registry answer exists.

_NEW_YADGARHQ = "yadgarhq/newrepo"
_REGISTERED_ELSEWHERE = "acme/widget"


def _write_record(project: str, registered, **extra) -> None:
    """Write a yaadgaar registry record exactly as the client contract specifies."""
    import json as _json

    from yadgar.core import task_ledger_registry as reg

    path = reg.record_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"project": project, "registered": registered, "checked_at": "2026-10-23T10:00:00Z"}
    body.update(extra)
    path.write_text(_json.dumps(body))


def _registry_for(answer):
    calls: list[str] = []

    def _lookup(project_id):
        calls.append(project_id)
        return answer

    _lookup.calls = calls  # type: ignore[attr-defined]
    return _lookup


def _decisions(caplog) -> list[tuple[str, str, bool]]:
    return [
        (r.ledger_project, r.ledger_source, r.external)
        for r in caplog.records
        if r.name == "yadgar.core.task_ledger_gate" and hasattr(r, "ledger_source")
    ]


class TestRegistryDecides:
    def test_a_unregistered_yadgarhq_project_is_not_gated(self, caplog) -> None:
        """(a) A ``yadgarhq/`` project the gateway does not register: yadgar owns its tasks."""
        caplog.set_level(logging.INFO, logger="yadgar.core.task_ledger_gate")
        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(_NEW_YADGARHQ, r, _registry_for(False)) is False
        assert r.calls == []  # a registry answer means no prefix read at all
        assert _decisions(caplog) == [(_NEW_YADGARHQ, "registry", False)]

    def test_b_a_registered_project_outside_yadgarhq_is_gated(self, caplog) -> None:
        """(b) Registered but outside every prefix: its tasks live in yaadgaar."""
        caplog.set_level(logging.INFO, logger="yadgar.core.task_ledger_gate")
        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(_REGISTERED_ELSEWHERE, r, _registry_for(True))
        assert _decisions(caplog) == [(_REGISTERED_ELSEWHERE, "registry", True)]

    def test_c_registry_unavailable_falls_back_to_the_prefix(self, caplog) -> None:
        """(c) No registry answer: the prefix decides, and the log says so."""
        caplog.set_level(logging.INFO, logger="yadgar.core.task_ledger_gate")
        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(_NEW_YADGARHQ, r, _registry_for(None)) is True
        assert (
            gate.is_external_ledger_project(_REGISTERED_ELSEWHERE, r, _registry_for(None)) is False
        )
        assert _decisions(caplog) == [
            (_NEW_YADGARHQ, "fallback-prefix", True),
            (_REGISTERED_ELSEWHERE, "fallback-prefix", False),
        ]

    def test_a_crashing_registry_lookup_falls_back_to_the_prefix(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="yadgar.core.task_ledger_gate")

        def _boom(_project_id):
            raise OSError("unreadable")

        r = _resolver_for(["yadgarhq/"])
        assert gate.is_external_ledger_project(_NEW_YADGARHQ, r, _boom) is True
        assert _decisions(caplog) == [(_NEW_YADGARHQ, "fallback-prefix", True)]

    def test_the_default_registry_is_the_mirror(self) -> None:
        """No ``registry`` argument: the gate reads the mirror the host hook wrote."""
        from yadgar.core import task_ledger_registry as reg

        _write_record(_REGISTERED_ELSEWHERE, True)
        assert reg.mirror_registry_answer(_REGISTERED_ELSEWHERE) is True
        assert gate.is_external_ledger_project(_REGISTERED_ELSEWHERE, _resolver_for([])) is True


class TestYaadgaarRecord:
    """The record contract: what the yaadgaar client writes, what yadgar accepts."""

    def test_path_is_state_dir_registry_escaped_casefolded_key(self, monkeypatch, tmp_path):
        from yadgar.core import task_ledger_registry as reg

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        monkeypatch.delenv("YAADGAAR_STATE_DIR", raising=False)
        assert reg.record_path("Acme/Widget") == (
            tmp_path / "st" / "yaadgaar" / "registry" / "acme%2Fwidget.json"
        )
        monkeypatch.setenv("YAADGAAR_STATE_DIR", str(tmp_path / "override"))
        assert reg.record_path("acme/widget") == (
            tmp_path / "override" / "registry" / "acme%2Fwidget.json"
        )

    @pytest.mark.parametrize("registered", [True, False])
    def test_an_explicit_answer_is_read(self, registered) -> None:
        from yadgar.core import task_ledger_registry as reg

        _write_record(_NEW_YADGARHQ, registered)
        assert reg.read_yaadgaar_record(_NEW_YADGARHQ) is registered

    def test_no_record_is_unavailable(self) -> None:
        from yadgar.core import task_ledger_registry as reg

        assert reg.read_yaadgaar_record(_NEW_YADGARHQ) is None

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            '{"project": "yadgarhq/newrepo"}',
            '{"project": "yadgarhq/newrepo", "registered": "yes"}',
        ],
    )
    def test_a_malformed_record_is_unavailable(self, body) -> None:
        from yadgar.core import task_ledger_registry as reg

        path = reg.record_path(_NEW_YADGARHQ)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        assert reg.read_yaadgaar_record(_NEW_YADGARHQ) is None

    def test_a_record_naming_another_project_is_unavailable(self) -> None:
        import json as _json

        from yadgar.core import task_ledger_registry as reg

        path = reg.record_path(_NEW_YADGARHQ)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"project": "other/repo", "registered": True}))
        assert reg.read_yaadgaar_record(_NEW_YADGARHQ) is None


class TestMirror:
    """Host hook copies the record into the yadgar data dir; the daemon reads it."""

    def test_mirror_carries_the_answer_into_the_data_dir(self) -> None:
        from yadgar.core import task_ledger_registry as reg

        _write_record(_NEW_YADGARHQ, False)
        assert reg.mirror_registry_answer(_NEW_YADGARHQ) is False
        assert reg.read_mirror(_NEW_YADGARHQ) is False
        assert reg.mirror_path().is_file()

    def test_a_vanished_record_clears_the_mirror_entry(self) -> None:
        from yadgar.core import task_ledger_registry as reg

        _write_record(_REGISTERED_ELSEWHERE, True)
        assert reg.mirror_registry_answer(_REGISTERED_ELSEWHERE) is True
        reg.record_path(_REGISTERED_ELSEWHERE).unlink()
        assert reg.mirror_registry_answer(_REGISTERED_ELSEWHERE) is None
        assert reg.read_mirror(_REGISTERED_ELSEWHERE) is None

    def test_mirror_lookup_is_case_insensitive(self) -> None:
        from yadgar.core import task_ledger_registry as reg

        _write_record("acme/widget", True)
        reg.mirror_registry_answer("acme/widget")
        assert reg.read_mirror("Acme/Widget") is True

    def test_no_mirror_file_is_unavailable(self) -> None:
        from yadgar.core import task_ledger_registry as reg

        assert reg.read_mirror(_NEW_YADGARHQ) is None

    def test_the_cli_hook_mirrors_after_minting(self) -> None:
        """The live SessionStart hook (``yadgar hook session-start-context``) mirrors."""
        from yadgar.core import task_ledger_registry as reg
        from yadgar.core.cli import hook as cli_hook

        _write_record(_REGISTERED_ELSEWHERE, True)
        cli_hook._register_directory_identity("/tmp/some-checkout", _REGISTERED_ELSEWHERE)
        assert reg.read_mirror(_REGISTERED_ELSEWHERE) is True


class TestRegistryEndToEnd:
    """Daemon-side callers read the mirror: SessionStart seeding and ``task_write``."""

    def test_b_registered_outside_prefix_session_start_seeds_nothing(
        self, config_value, session_context
    ):
        from yadgar.core import task_ledger_registry as reg

        config_value["value"] = ["yadgarhq/"]
        _write_record(_REGISTERED_ELSEWHERE, True)
        reg.mirror_registry_answer(_REGISTERED_ELSEWHERE)

        body = session_context(_REGISTERED_ELSEWHERE, seed=True)

        assert body["text"].startswith(
            f"[yadgar] Tasks for {_REGISTERED_ELSEWHERE} live in yaadgaar"
        )
        assert body["task_nudge"] == ""
        assert body["tasks"] == []
        assert session_context.nudge_calls == []

    def test_a_unregistered_yadgarhq_session_start_seeds_from_yadgar(
        self, config_value, session_context
    ):
        from yadgar.core import task_ledger_registry as reg

        config_value["value"] = ["yadgarhq/"]
        _write_record(_NEW_YADGARHQ, False)
        reg.mirror_registry_answer(_NEW_YADGARHQ)

        body = session_context(_NEW_YADGARHQ, seed=True)

        assert "live in yaadgaar" not in body["text"]
        assert body["tasks"] == _ROWS
        assert len(session_context.nudge_calls) == 1

    def test_b_registered_outside_prefix_task_write_refuses(self, config_value, forward_calls):
        from yadgar.core import task_ledger_registry as reg
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = ["yadgarhq/"]
        _write_record(_REGISTERED_ELSEWHERE, True)
        reg.mirror_registry_answer(_REGISTERED_ELSEWHERE)

        result = task_write(project_id=_REGISTERED_ELSEWHERE, title="new work")

        assert result["error"] == "external_task_ledger"
        assert forward_calls == []

    def test_a_unregistered_yadgarhq_task_write_is_allowed(self, config_value, forward_calls):
        from yadgar.core import task_ledger_registry as reg
        from yadgar.core.server.tools.task import task_write

        config_value["value"] = ["yadgarhq/"]
        _write_record(_NEW_YADGARHQ, False)
        reg.mirror_registry_answer(_NEW_YADGARHQ)

        assert task_write(project_id=_NEW_YADGARHQ, title="new work") == {"ok": True, "id": 501}


_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def live_hook(config_value, session_context, tmp_path, monkeypatch):
    """Drive the LIVE hook (``cli.hook.hook_session_start_context``) against the endpoint.

    Only the yaadgaar record is written by the test. The hook mints (stubbed),
    mirrors, and calls ``/hooks/session-context`` in-process; the harness seed
    store is pinned under ``tmp_path`` (never the real ``~/.claude/tasks``).
    """
    import io
    import json as _json

    import yadgar.core.cli.hook as hk
    from yadgar.core import task_ledger_registry as reg

    home = tmp_path / "claude-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("YADGAR_HARNESS_SEED_DISABLED", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TASK_LIST_ID", raising=False)
    config_value["value"] = ["yadgarhq/"]

    def _run(project: str) -> Path:
        monkeypatch.setattr(
            "yadgar.core.hooks._identity_mint.resolve_session_project",
            lambda cwd: (project, f"yadgar: project_id={project}"),
        )
        mirrored_before_call: list = []

        def _fake_get(path, params=None, timeout=2.0):
            assert path == "/hooks/session-context"
            # The mirror must already hold this session's answer when the
            # daemon decides whether to hand back rows to seed.
            mirrored_before_call.append(reg.read_mirror(project))
            return session_context(params["project"], seed=params.get("seed") == "1")

        monkeypatch.setattr(hk, "_http_get", _fake_get)
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(_json.dumps({"cwd": str(tmp_path), "session_id": _SID}))
        )
        hk.hook_session_start_context()
        _run.mirrored_before_call = mirrored_before_call  # type: ignore[attr-defined]
        return home / "tasks" / _SID

    return _run


class TestLiveHookSeed:
    """The live hook's harness seed is off for exactly the registry's set."""

    def test_a_registered_project_gets_no_seed(self, live_hook, session_context, capsys):
        _write_record(_REGISTERED_ELSEWHERE, True)

        task_dir = live_hook(_REGISTERED_ELSEWHERE)

        assert live_hook.mirrored_before_call == [True]
        assert not task_dir.exists() or list(task_dir.glob("*.json")) == []
        assert session_context.nudge_calls == []
        out = capsys.readouterr().out
        assert "Task list seeded from the ledger" not in out
        assert f"Tasks for {_REGISTERED_ELSEWHERE} live in yaadgaar" in out

    def test_an_unregistered_yadgarhq_project_is_seeded(self, live_hook, capsys):
        _write_record(_NEW_YADGARHQ, False)

        task_dir = live_hook(_NEW_YADGARHQ)

        assert live_hook.mirrored_before_call == [False]
        assert (task_dir / f"{_ROWS[0]['id']}.json").is_file()
        assert "Task list seeded from the ledger" in capsys.readouterr().out
