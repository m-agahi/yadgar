"""Meta-tests for the I32 prose-liveness guard (gate-blindness class, 2026-07-29).

The I32 coverage lint (``check_capability_coverage.py``) enforces catalogue
completeness over four ENUMERABLE surfaces (settings / tools / migrations / BC).
It never reads an entry's ``explanation:`` or ``wiring:`` prose — which is where
CAP-CODEGRAPH-001's false claim lived, and why that lint ran and PASSED on the
very PR that made the claim false.

``check_registry_prose_liveness.py`` closes the narrow, checkable part of that
gap: a token the registry cites in backticks must still be referenced by
EXECUTABLE code.  Comments and docstrings do not count — measured, all four
surviving ``CODE_GRAPH_ENABLED`` references at the rot commit were prose.

Non-e2e, hermetic: every test builds its own tiny repo under tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):  # type: ignore[return]
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


crpl = _load("check_registry_prose_liveness.py")


# ---------------------------------------------------------------------------
# Fixture builder — a minimal repo with a registry, a source tree, an allowlist
# ---------------------------------------------------------------------------


def _make_repo(
    tmp_path: Path,
    *,
    registry_body: str,
    source: str = "",
    allowlist: dict | None = None,
) -> Path:
    (tmp_path / "docs" / "contracts").mkdir(parents=True)
    (tmp_path / "docs" / "contracts" / "CAPABILITY_REGISTRY.md").write_text(
        registry_body, encoding="utf-8"
    )
    pkg = tmp_path / "yadgar" / "core"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(source, encoding="utf-8")
    if allowlist is not None:
        (tmp_path / ".registry-prose-allowlist.json").write_text(
            json.dumps(allowlist), encoding="utf-8"
        )
    return tmp_path


_ENTRY = textwrap.dedent("""\
    ### CAP-TEST-001 — A capability

    - **status:** LIVE
    - **explanation:** The knob `MY_FEATURE_FLAG` turns it on.
    """)


class TestProseLiveness:
    """A cited token must be referenced by executable code, not just prose."""

    def test_token_only_in_a_docstring_is_flagged(self, tmp_path: Path) -> None:
        """THE defect shape: the doc-fix commit left the name only in a docstring."""
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source='"""Module doc mentioning MY_FEATURE_FLAG for archaeology."""\n\nx = 1\n',
        )
        errors = crpl.check(repo)
        assert errors, "a token surviving only in a docstring must not read as live"
        assert any("MY_FEATURE_FLAG" in e and "DEAD-CLAIM" in e for e in errors), errors

    def test_token_only_in_a_comment_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="x = 1  # MY_FEATURE_FLAG used to be read here\n",
        )
        errors = crpl.check(repo)
        assert any("MY_FEATURE_FLAG" in e for e in errors), errors

    def test_token_in_executable_code_is_live(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="MY_FEATURE_FLAG = True\n",
        )
        assert not crpl.check(repo), "an assigned module constant is live"

    def test_token_as_attribute_access_is_live(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="def f(settings):\n    return settings.MY_FEATURE_FLAG\n",
        )
        assert not crpl.check(repo)

    def test_token_inside_a_non_docstring_string_is_live(self, tmp_path: Path) -> None:
        """os.environ["FOO"] / getenv("FOO") shapes — a real executable reference."""
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source='import os\n\nv = os.environ.get("MY_FEATURE_FLAG")\n',
        )
        assert not crpl.check(repo)

    def test_docstring_exclusion_covers_function_and_class_docstrings(self, tmp_path: Path) -> None:
        """Not just module docstrings — every def/class body's first string too."""
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source=textwrap.dedent("""\
                class C:
                    \"\"\"Class doc naming MY_FEATURE_FLAG.\"\"\"

                    def m(self):
                        \"\"\"Method doc naming MY_FEATURE_FLAG.\"\"\"
                        return 1
                """),
        )
        errors = crpl.check(repo)
        assert any("MY_FEATURE_FLAG" in e for e in errors), errors

    def test_test_only_reference_does_not_count_as_live(self, tmp_path: Path) -> None:
        """A token kept alive only by its own test is still dead production config."""
        repo = _make_repo(tmp_path, registry_body=_ENTRY, source="x = 1\n")
        tests = repo / "yadgar" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_thing.py").write_text("MY_FEATURE_FLAG = True\n", encoding="utf-8")
        errors = crpl.check(repo)
        assert any("MY_FEATURE_FLAG" in e for e in errors), errors

    def test_token_live_in_a_config_file_counts(self, tmp_path: Path) -> None:
        """Env vars legitimately live in flake.nix / compose / Dockerfile."""
        repo = _make_repo(tmp_path, registry_body=_ENTRY, source="x = 1\n")
        (repo / "docker-compose.yml").write_text(
            "services:\n  y:\n    environment:\n      MY_FEATURE_FLAG: 'true'\n", encoding="utf-8"
        )
        assert not crpl.check(repo)


class TestClaimCollection:
    """Which backtick tokens count as identifier claims."""

    def test_status_enum_words_are_not_claims(self) -> None:
        """SHADOW / DORMANT / WRITE are status values, not identifiers.

        The `_`-containing requirement is what drops them; without it the guard
        would demand executable references to English words.
        """
        claims = crpl.collect_claims("- **status:** `DORMANT`\n- x `SHADOW` `WRITE`\n")
        assert claims == set(), claims

    def test_short_tokens_are_not_claims(self) -> None:
        assert crpl.collect_claims("`A_B`\n") == set()

    def test_underscored_upper_token_is_a_claim(self) -> None:
        assert crpl.collect_claims("cites `MY_FEATURE_FLAG` here\n") == {"MY_FEATURE_FLAG"}

    def test_file_paths_in_backticks_are_not_claims(self) -> None:
        assert crpl.collect_claims("- **refs:** `yadgar/core/server/http.py`\n") == set()


class TestAllowlistGovernance:
    """Mirrors I30/.complexity-allowlist governance: rationale floor + stale hard-fail."""

    def test_allowlisted_dead_token_passes(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="x = 1\n",
            allowlist={
                "MY_FEATURE_FLAG": {
                    "rationale": (
                        "Archaeology: the knob was removed in the great purge of 2026 and the "
                        "registry entry deliberately records that it is read nowhere."
                    )
                }
            },
        )
        assert not crpl.check(repo)

    def test_short_rationale_hard_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="x = 1\n",
            allowlist={"MY_FEATURE_FLAG": {"rationale": "too short"}},
        )
        errors = crpl.check(repo)
        assert any("rationale" in e for e in errors), errors

    def test_missing_rationale_hard_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="x = 1\n",
            allowlist={"MY_FEATURE_FLAG": {}},
        )
        assert crpl.check(repo)

    def test_stale_allowlist_entry_hard_fails(self, tmp_path: Path) -> None:
        """A token that came back to life must be de-allowlisted.

        This is what stops the allowlist becoming a write-only dumping ground.
        """
        repo = _make_repo(
            tmp_path,
            registry_body=_ENTRY,
            source="MY_FEATURE_FLAG = True\n",
            allowlist={
                "MY_FEATURE_FLAG": {
                    "rationale": (
                        "Archaeology: the knob was removed in the great purge of 2026 and the "
                        "registry entry deliberately records that it is read nowhere."
                    )
                }
            },
        )
        errors = crpl.check(repo)
        assert any("STALE" in e for e in errors), errors

    def test_allowlist_entry_for_an_uncited_token_is_stale(self, tmp_path: Path) -> None:
        """Registry stopped citing it → the allowlist line is dead weight."""
        repo = _make_repo(
            tmp_path,
            registry_body="### CAP-TEST-001 — A capability\n\n- **status:** LIVE\n",
            source="x = 1\n",
            allowlist={
                "GONE_FROM_REGISTRY": {
                    "rationale": (
                        "Archaeology: the knob was removed in the great purge of 2026 and the "
                        "registry entry deliberately records that it is read nowhere."
                    )
                }
            },
        )
        errors = crpl.check(repo)
        assert any("STALE" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Integration: the real tree
# ---------------------------------------------------------------------------


def test_real_tree_is_clean() -> None:
    """The shipped registry + allowlist exit 0 — the guard's baseline."""
    errors = crpl.check(_REPO_ROOT)
    assert not errors, f"registry prose-liveness must be clean: {errors}"


def test_i32_coverage_lint_is_unaffected() -> None:
    """This is a SEPARATE script; check_capability_coverage's contract is unchanged."""
    cap = _load("check_capability_coverage.py")
    assert not cap.check(_REPO_ROOT)
