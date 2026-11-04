"""The live SessionStart hook auto-registers the id it minted (ADR-1131).

``_emit_project_id`` (``yadgar.core.cli.hook``) mints, prints the identity
banner, then asks the daemon to create the registry row if absent. A new row
prints one banner line; a duplicate or an unconsultable registry is silent; a
timeout prints a neutral "still pending" line; any other failure prints one
warning line and the session continues.

SAFETY: the root conftest replaces ``_register_minted_project`` with a no-op
for every test, so no test can reach the live daemon. This module keeps a
reference to the REAL function (bound at import, before the fixture runs) and
drives it with ``_post_project_register`` faked, or with ``urlopen`` faked to
test ``_post_project_register`` itself.
"""

from __future__ import annotations

import email.message
import io
import socket
import urllib.error

import pytest

import yadgar.core.cli.hook as hk

_REAL_REGISTER = hk._register_minted_project


@pytest.fixture
def posts(monkeypatch):
    """Fake ``_post_project_register``.

    ``state['outcome']`` / ``state['answer']`` are what the helper returns.
    """
    state: dict = {"calls": [], "outcome": "answered", "answer": None}

    def _fake_post(project_id):
        state["calls"].append(project_id)
        return state["outcome"], state["answer"]

    monkeypatch.setattr(hk, "_post_project_register", _fake_post)
    return state


def test_new_project_prints_the_registered_line(posts):
    posts["answer"] = {"ok": True, "project_id": "quinyx/daneel", "created": True}

    line = _REAL_REGISTER("quinyx/daneel")

    assert line == "yadgar: registered new project quinyx/daneel"
    assert posts["calls"] == ["quinyx/daneel"]


def test_duplicate_is_silent(posts):
    posts["answer"] = {"ok": True, "project_id": "quinyx/daneel", "created": False}

    assert _REAL_REGISTER("quinyx/daneel") is None


def test_daemon_unreachable_warns_and_names_the_fix(posts):
    posts["outcome"], posts["answer"] = "failed", None  # refused, 401, 5xx, bad JSON

    line = _REAL_REGISTER("quinyx/daneel")

    assert line is not None
    assert line.startswith("[yadgar] WARNING")
    assert "quinyx/daneel" in line
    assert 'project_register("quinyx/daneel")' in line


def test_timeout_is_a_neutral_pending_line_not_a_failure(posts):
    """The daemon keeps working after the hook gives up, so the row may land."""
    posts["outcome"], posts["answer"] = "timeout", None

    line = _REAL_REGISTER("quinyx/daneel")

    assert line == (
        "yadgar: project registration for quinyx/daneel still pending; check `yadgar project list`"
    )
    assert "WARNING" not in line
    assert "could not" not in line


def test_registry_unavailable_is_silent(posts):
    """No engine #2: writes are not blocked, so a per-session warning is noise."""
    posts["answer"] = {
        "ok": False,
        "project_id": "quinyx/daneel",
        "created": False,
        "reason": "registry_unavailable",
        "error": "project registry unavailable",
    }

    assert _REAL_REGISTER("quinyx/daneel") is None


def test_backend_create_failure_warns_with_its_reason(posts):
    posts["answer"] = {
        "ok": False,
        "project_id": "quinyx/daneel",
        "created": False,
        "error": "Data too long for column 'key'",
    }

    line = _REAL_REGISTER("quinyx/daneel")

    assert line is not None
    assert "WARNING" in line
    assert "Data too long" in line


def test_hook_sends_the_raw_id_and_lets_the_daemon_guard_it(posts):
    """No hook-side guard: an id already registered must be able to come back
    ``created: False`` even when it would fail the create-path guard."""
    posts["answer"] = {"ok": True, "project_id": "Legacy/Key", "created": False}

    assert _REAL_REGISTER("Legacy/Key") is None
    assert posts["calls"] == ["Legacy/Key"]


def test_daemon_guard_reject_warns_with_its_reason(posts):
    posts["answer"] = {
        "ok": False,
        "project_id": "Quinyx/Daneel",
        "created": False,
        "error": "project_id 'Quinyx/Daneel' must be lowercase",
    }

    line = _REAL_REGISTER("Quinyx/Daneel")

    assert line is not None
    assert "WARNING" in line
    assert "lowercase" in line


# ── _post_project_register: the outcome classification ──────────────────────


class _Resp(io.BytesIO):
    pass


def _fake_urlopen(behaviour, seen: list | None = None):
    def _urlopen(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, req.data, timeout))
        if isinstance(behaviour, BaseException):
            raise behaviour
        return _Resp(behaviour)

    return _urlopen


def test_post_answered_returns_the_body(monkeypatch):
    seen: list = []
    fake = _fake_urlopen(b'{"ok": true, "created": true}', seen)
    monkeypatch.setattr(hk.urllib.request, "urlopen", fake)

    assert hk._post_project_register("quinyx/daneel") == (
        "answered",
        {"ok": True, "created": True},
    )
    url, data, timeout = seen[0]
    assert url.endswith("/hooks/project-register")
    assert data == b'{"project_id": "quinyx/daneel"}'
    assert timeout == hk._REGISTER_TIMEOUT_S <= 3.0


def test_post_response_phase_timeout_is_timeout(monkeypatch):
    # AC1: a bare TimeoutError (response phase) still returns ("timeout", None)
    monkeypatch.setattr(hk.urllib.request, "urlopen", _fake_urlopen(TimeoutError("timed out")))

    assert hk._post_project_register("quinyx/daneel") == ("timeout", None)
    # AC1 triangulation: different message, same classification
    monkeypatch.setattr(hk.urllib.request, "urlopen", _fake_urlopen(TimeoutError("read timed out")))
    assert hk._post_project_register("quinyx/other") == ("timeout", None)


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(TimeoutError("timed out")),
        # AC1 boundary: socket.timeout (an alias of TimeoutError on 3.10+)
        urllib.error.URLError(socket.timeout("timed out")),  # noqa: UP041 - AC1 example is verbatim socket.timeout
        urllib.error.URLError(TimeoutError("connect timed out")),
    ],
    ids=[
        "URLError-TimeoutError",
        "URLError-socket.timeout-AC1-boundary",
        "URLError-TimeoutError-triangulation",
    ],
)
def test_post_connect_phase_timeout_is_failed(monkeypatch, exc):
    # AC1: URLError wrapping a TimeoutError (connect phase) returns ("failed", None)
    monkeypatch.setattr(hk.urllib.request, "urlopen", _fake_urlopen(exc))

    assert hk._post_project_register("quinyx/daneel") == ("failed", None)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("t"),
        urllib.error.URLError(TimeoutError("t")),
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        ConnectionResetError(),
        OSError("boom"),
    ],
)
def test_post_never_raises_for_oserror_family(monkeypatch, exc):
    # AC1 property: never raises for any OSError/URLError/TimeoutError
    monkeypatch.setattr(hk.urllib.request, "urlopen", _fake_urlopen(exc))

    outcome, body = hk._post_project_register("quinyx/daneel")

    assert outcome in {"timeout", "failed"}
    assert body is None


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        urllib.error.HTTPError("u", 401, "unauthorized", email.message.Message(), io.BytesIO(b"")),
        ConnectionResetError(),
    ],
)
def test_post_other_failures_are_classified_as_failed(monkeypatch, exc):
    monkeypatch.setattr(hk.urllib.request, "urlopen", _fake_urlopen(exc))

    assert hk._post_project_register("quinyx/daneel") == ("failed", None)


def test_post_bad_json_is_failed(monkeypatch):
    monkeypatch.setattr(hk.urllib.request, "urlopen", _fake_urlopen(b"not json"))

    assert hk._post_project_register("quinyx/daneel") == ("failed", None)


# ── _emit_project_id wiring ─────────────────────────────────────────────────


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
