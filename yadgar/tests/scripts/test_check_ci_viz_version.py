"""Task 415 — Dockerfile.ci-viz's two version strings must agree, and not lead.

The gate under test (``scripts/check_ci_viz_version.py``) enforces two rules:

  1. ``ARG BASE_TAG`` == ``LABEL org.opencontainers.image.version`` — the file's
     own stated contract, previously unchecked by anything.
  2. ``BASE_TAG`` <= ``pyproject.toml``'s version — a base tag newer than any
     version this repo produced names an image that cannot exist.

It deliberately does NOT require equality with pyproject: yadgar-ci is rebuilt
manually (ADR-0135), so the CI images legitimately lag between rebuilds. The
lag case is pinned as a PASS below so a later "tighten it to equality" edit has
to delete an assertion that says why, rather than silently changing the policy.

The unextractable cases are pinned as FAILURES: a regex that misses and exits 0
is the failure mode this repo just spent a train repairing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "check_ci_viz_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_ci_viz_version", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()

_PYPROJECT = '[project]\nname = "yadgar"\nversion = "5.192.0"\n'


def _dockerfile(base_tag: str = "5.191.0", label: str = "5.191.0") -> str:
    return (
        "# Version: must track yadgar-ci base version. Bump both together.\n"
        f"ARG BASE_TAG={base_tag}\n"
        "FROM docker.io/openfantasy/yadgar-ci:${BASE_TAG}\n"
        '\nLABEL org.opencontainers.image.source="https://github.com/m-agahi/yadgar"\n'
        'LABEL org.opencontainers.image.description="yadgar viz CI runner"\n'
        f'LABEL org.opencontainers.image.version="{label}"\n'
    )


class TestRule1InternalAgreement:
    def test_matching_strings_pass(self):
        ok, msg = mod.check(_dockerfile(), _PYPROJECT)
        assert ok, msg

    def test_disagreeing_strings_fail(self):
        ok, msg = mod.check(_dockerfile(base_tag="5.191.0", label="5.190.0"), _PYPROJECT)
        assert not ok
        assert "disagrees with itself" in msg
        assert "5.191.0" in msg and "5.190.0" in msg

    def test_label_regex_does_not_match_other_label_lines(self):
        """image.source / image.description must not be mistaken for the version."""
        text = (
            "ARG BASE_TAG=5.191.0\n"
            'LABEL org.opencontainers.image.source="https://example/5.000.0"\n'
            'LABEL org.opencontainers.image.description="viz runner 5.000.0"\n'
            'LABEL org.opencontainers.image.version="5.191.0"\n'
        )
        ok, msg = mod.check(text, _PYPROJECT)
        assert ok, msg


class TestRule2OrderingAgainstPyproject:
    def test_lagging_pyproject_is_legal(self):
        """5.191.0 base against a 5.192.0 source is the NORMAL steady state."""
        ok, msg = mod.check(_dockerfile("5.191.0", "5.191.0"), _PYPROJECT)
        assert ok, msg
        assert "lags pyproject 5.192.0" in msg

    def test_equal_to_pyproject_is_legal(self):
        ok, msg = mod.check(_dockerfile("5.192.0", "5.192.0"), _PYPROJECT)
        assert ok, msg

    def test_leading_pyproject_fails(self):
        ok, msg = mod.check(_dockerfile("5.999.0", "5.999.0"), _PYPROJECT)
        assert not ok
        assert "LEADS" in msg
        assert "5.999.0" in msg

    def test_leading_by_a_patch_still_fails(self):
        ok, msg = mod.check(_dockerfile("5.192.1", "5.192.1"), _PYPROJECT)
        assert not ok
        assert "LEADS" in msg


class TestUnextractableIsFailureNotPass:
    @pytest.mark.parametrize(
        ("text", "needle"),
        [
            (
                'LABEL org.opencontainers.image.version="5.191.0"\n',
                "ARG BASE_TAG",
            ),
            ("ARG BASE_TAG=5.191.0\n", "org.opencontainers.image.version"),
            ("# nothing at all\n", "ARG BASE_TAG"),
        ],
    )
    def test_missing_string_fails(self, text, needle):
        ok, msg = mod.check(text, _PYPROJECT)
        assert not ok
        assert needle in msg

    def test_interpolated_label_fails(self):
        """The LABEL is baked verbatim; an uninterpolated value is not a version."""
        ok, msg = mod.check(_dockerfile(label="${BASE_TAG}"), _PYPROJECT)
        assert not ok
        assert "interpolated" in msg

    def test_empty_label_fails(self):
        ok, msg = mod.check(_dockerfile(label=""), _PYPROJECT)
        assert not ok

    def test_unparseable_pyproject_version_fails(self):
        ok, msg = mod.check(_dockerfile(), '[project]\nversion = "not-a-version"\n')
        assert not ok
        assert "pyproject.toml" in msg

    def test_missing_pyproject_version_fails(self):
        ok, msg = mod.check(_dockerfile(), '[project]\nname = "yadgar"\n')
        assert not ok
        assert "pyproject.toml" in msg


class TestRealTree:
    def test_committed_tree_passes(self):
        """The gate must be green on the tree it ships with."""
        ok, msg = mod.check(
            (_REPO_ROOT / "Dockerfile.ci-viz").read_text(),
            (_REPO_ROOT / "pyproject.toml").read_text(),
        )
        assert ok, msg

    def test_main_exits_zero_on_real_tree(self):
        assert mod.main() == 0
