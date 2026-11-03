"""``POST /hooks/project-register`` and ``yadgar project register`` (ADR-1131).

Both are thin surfaces over ``yadgar.core.project_registration.register_project``;
these tests pin the wiring and the response shape, with the registrar faked.
"""

from __future__ import annotations

import argparse
import json

import pytest
from starlette.requests import Request

from yadgar.core import project_registration as pr


def _request(body: bytes) -> Request:
    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/hooks/project-register", "headers": []}
    return Request(scope, _receive)


@pytest.fixture
def fake_register(monkeypatch):
    calls: list[tuple] = []

    def _fake(project_id, display_name=None):
        calls.append((project_id, display_name))
        if project_id == "bad":
            return {"ok": False, "project_id": project_id, "created": False, "error": "nope"}
        return {"ok": True, "project_id": project_id, "created": True}

    monkeypatch.setattr(pr, "register_project", _fake)
    return calls


async def test_route_registers_and_returns_the_result(fake_register):
    from yadgar.core.server.routes.project_register import project_register_handler

    resp = await project_register_handler(_request(b'{"project_id": "quinyx/daneel"}'))

    assert resp.status_code == 200
    assert json.loads(resp.body) == {"ok": True, "project_id": "quinyx/daneel", "created": True}
    assert fake_register == [("quinyx/daneel", None)]


async def test_route_returns_a_refusal_as_200_so_the_hook_sees_the_reason(fake_register):
    from yadgar.core.server.routes.project_register import project_register_handler

    resp = await project_register_handler(_request(b'{"project_id": "bad"}'))

    assert resp.status_code == 200
    assert json.loads(resp.body)["error"] == "nope"


async def test_route_rejects_invalid_json(fake_register):
    from yadgar.core.server.routes.project_register import project_register_handler

    resp = await project_register_handler(_request(b"not json"))

    assert resp.status_code == 400
    assert fake_register == []


def test_cli_register_success(fake_register, capsys):
    from yadgar.core.cli.project import cmd_project_register

    rc = cmd_project_register(argparse.Namespace(project_id="quinyx/daneel", display_name="Daneel"))

    assert rc == 0
    assert fake_register == [("quinyx/daneel", "Daneel")]
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_cli_register_failure_exits_1(fake_register, capsys):
    from yadgar.core.cli.project import cmd_project_register

    rc = cmd_project_register(argparse.Namespace(project_id="bad", display_name=None))

    assert rc == 1
    assert "nope" in capsys.readouterr().err


def test_cli_register_subcommand_is_wired():
    from yadgar.core.cli.project import cmd_project_register, register

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["project", "register", "quinyx/daneel", "--display-name", "D"])

    assert args.func is cmd_project_register
    assert args.project_id == "quinyx/daneel"
    assert args.display_name == "D"
