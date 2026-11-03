"""The live SessionStart hook auto-registers the id it minted (ADR-1131).

``_emit_project_id`` (``yadgar.core.cli.hook``) mints, prints the identity
banner, then asks the daemon to create the registry row if absent. A new row
prints one banner line; a duplicate is silent; any failure prints one warning
line and the session continues.

SAFETY: the root conftest replaces ``_register_minted_project`` with a no-op
for every test, so no test can reach the live daemon. This module keeps a
reference to the REAL function (bound at import, before the fixture runs) and
drives it with ``_http_post`` faked.
"""

from __future__ import annotations

import pytest

import yadgar.core.cli.hook as hk

_REAL_REGISTER = hk._register_minted_project


@pytest.fixture
def posts(monkeypatch):
    """Fake ``_http_post``; ``state['answer']`` is what the daemon returns."""
    state: dict = {"calls": [], "answer": None}

    def _fake_post(path, payload, timeout=1.0):
        state["calls"].append((path, dict(payload), timeout))
        return state["answer"]

    monkeypatch.setattr(hk, "_http_post", _fake_post)
    return state


def test_new_project_prints_the_registered_line(posts):
    posts["answer"] = {"ok": True, "project_id": "quinyx/daneel", "created": True}

    line = _REAL_REGISTER("quinyx/daneel")

    assert line == "yadgar: registered new project quinyx/daneel"
    assert len(posts["calls"]) == 1
    path, payload, timeout = posts["calls"][0]
    assert path == "/hooks/project-register"
    assert payload == {"project_id": "quinyx/daneel"}
    assert timeout <= 3.0


def test_duplicate_is_silent(posts):
    posts["answer"] = {"ok": True, "project_id": "quinyx/daneel", "created": False}

    assert _REAL_REGISTER("quinyx/daneel") is None


def test_daemon_unreachable_warns_and_names_the_fix(posts):
    posts["answer"] = None  # _http_post: connection refused, 401, 5xx, timeout

    line = _REAL_REGISTER("quinyx/daneel")

    assert line is not None
    assert line.startswith("[yadgar] WARNING")
    assert "quinyx/daneel" in line
    assert 'project_register("quinyx/daneel")' in line


def test_backend_refusal_warns_with_its_reason(posts):
    posts["answer"] = {
        "ok": False,
        "project_id": "quinyx/daneel",
        "created": False,
        "error": "engine #2 not composed",
    }

    line = _REAL_REGISTER("quinyx/daneel")

    assert line is not None
    assert "WARNING" in line
    assert "engine #2 not composed" in line


def test_guard_reject_warns_without_a_daemon_call(posts):
    line = _REAL_REGISTER("Quinyx/Daneel")

    assert line is not None
    assert "WARNING" in line
    assert "lowercase" in line
    assert posts["calls"] == []


def test_emit_project_id_prints_the_registration_line(monkeypatch, tmp_path, capsys):
    """Wiring: the live hook path calls the registrar after a successful mint."""
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("quinyx/daneel\n")
    seen: list[str] = []

    def _fake_register(project_id):
        seen.append(project_id)
        return f"yadgar: registered new project {project_id}"

    monkeypatch.setattr(hk, "_register_minted_project", _fake_register)
    monkeypatch.setattr(hk, "_register_directory_identity", lambda cwd, pid: None)

    assert hk._emit_project_id(str(tmp_path)) == "quinyx/daneel"

    out = capsys.readouterr().out
    assert seen == ["quinyx/daneel"]
    assert "yadgar: registered new project quinyx/daneel" in out


def test_emit_project_id_survives_a_registrar_crash(monkeypatch, tmp_path, capsys):
    (tmp_path / ".yadgar").mkdir()
    (tmp_path / ".yadgar" / "project-id").write_text("quinyx/daneel\n")

    def _boom(project_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hk, "_register_minted_project", _boom)
    monkeypatch.setattr(hk, "_register_directory_identity", lambda cwd, pid: None)

    assert hk._emit_project_id(str(tmp_path)) == "quinyx/daneel"
    assert "WARNING" in capsys.readouterr().out


def test_emit_project_id_reports_a_malformed_file(monkeypatch, tmp_path, capsys):
    (tmp_path / ".yadgar").mkdir()
    path = tmp_path / ".yadgar" / "project-id"
    path.write_text("quinyx/daneel\nquinyx/other\n")
    called: list[str] = []
    monkeypatch.setattr(hk, "_register_minted_project", lambda pid: called.append(pid))

    assert hk._emit_project_id(str(tmp_path)) is None

    out = capsys.readouterr().out
    assert str(path) in out
    assert called == []
