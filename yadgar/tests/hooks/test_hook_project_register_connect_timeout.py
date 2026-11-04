"""AC2: a connect-phase timeout prints the WARNING banner, not "still pending".

SAFETY: the root conftest replaces ``_register_minted_project`` with a no-op
for every test. This module binds the REAL function at import (before the
fixture runs) and fakes only ``urlopen``, so no test reaches a live daemon.
"""

from __future__ import annotations

import urllib.error

import pytest

import yadgar.core.cli.hook as hk

_REAL_REGISTER = hk._register_minted_project


def _warning(project_id: str) -> str:
    return (
        f"[yadgar] WARNING: could not auto-register project {project_id} (daemon "
        "unreachable or request refused). If writes fail with unknown project_id, "
        f'call project_register("{project_id}") once the daemon is up.'
    )


def _pending(project_id: str) -> str:
    return (
        f"yadgar: project registration for {project_id} still pending; check `yadgar project list`"
    )


def _raising_urlopen(exc: BaseException):
    def _urlopen(*_args, **_kwargs):
        raise exc

    return _urlopen


def test_connect_phase_timeout_prints_the_warning_line(monkeypatch):
    monkeypatch.setattr(
        hk.urllib.request,
        "urlopen",
        _raising_urlopen(urllib.error.URLError(TimeoutError("timed out"))),
    )

    line = _REAL_REGISTER("quinyx/daneel")

    # AC2 main: URLError(TimeoutError) -> the existing WARNING banner.
    assert line == _warning("quinyx/daneel")
    assert "still pending" not in line


def test_connect_phase_timeout_warning_names_the_given_project(monkeypatch):
    monkeypatch.setattr(
        hk.urllib.request,
        "urlopen",
        _raising_urlopen(urllib.error.URLError(TimeoutError("timed out"))),
    )

    line = _REAL_REGISTER("acme/widgets")

    # AC2 main, triangulation: a hardcoded id fails.
    assert line == _warning("acme/widgets")
    assert "still pending" not in line


def test_response_phase_timeout_still_prints_pending(monkeypatch):
    monkeypatch.setattr(hk.urllib.request, "urlopen", _raising_urlopen(TimeoutError("timed out")))

    # AC2 boundary: bare TimeoutError -> "still pending" line.
    assert _REAL_REGISTER("quinyx/daneel") == _pending("quinyx/daneel")


def test_response_phase_timeout_pending_names_the_given_project(monkeypatch):
    monkeypatch.setattr(hk.urllib.request, "urlopen", _raising_urlopen(TimeoutError("timed out")))

    # AC2 boundary, triangulation.
    assert _REAL_REGISTER("acme/widgets") == _pending("acme/widgets")


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(TimeoutError("timed out")),
        TimeoutError("timed out"),
    ],
)
def test_register_never_raises_for_either_timeout_shape(monkeypatch, exc):
    monkeypatch.setattr(hk.urllib.request, "urlopen", _raising_urlopen(exc))

    # AC2 property: never raises; always returns a banner string.
    assert isinstance(_REAL_REGISTER("quinyx/daneel"), str)
