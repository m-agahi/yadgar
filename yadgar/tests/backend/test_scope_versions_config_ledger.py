"""Car B — ``ScopeVersions`` admits ``scope_kind="config"`` and ``scope_kind="ledger"``.

The version-in-key mechanism the data caches (``engram_slot`` / ``graph``) use for
freshness already accepts arbitrary kinds via the ``(scope_kind, scope_id) -> int``
map. Car B pins TWO new conventions as the contract Cars D/F/I will rely on:

  * ``scope_kind="config"`` — ``scope_id`` is a config key, or the sentinel
    ``"__global__"`` for list reads (list_config_rows).
  * ``scope_kind="ledger"`` — ``scope_id`` is a project_id, or the sentinel
    ``"__global__"`` for cross-project reads (list_*_all_projects).

The two kinds share the same map but must not collide on equal ``scope_id``
across kinds — bumping ``ledger("foo")`` must not move ``config("foo")``.
"""

from __future__ import annotations

import pytest

from yadgar.backend.cache.scope_versions import ScopeVersions


@pytest.fixture
def sv() -> ScopeVersions:
    """Fresh ScopeVersions per test (no cross-test bleed)."""
    return ScopeVersions()


class TestConfigScopeKind:
    def test_bump_then_version_round_trip(self, sv: ScopeVersions) -> None:
        sv.bump("config", "seq_batch")
        assert sv.version("config", "seq_batch") == 1

    def test_unbumped_scope_is_zero(self, sv: ScopeVersions) -> None:
        assert sv.version("config", "never_bumped") == 0

    def test_repeated_bumps_monotonic(self, sv: ScopeVersions) -> None:
        a = sv.bump("config", "seq_batch")
        b = sv.bump("config", "seq_batch")
        c = sv.bump("config", "seq_batch")
        assert a < b < c
        assert sv.version("config", "seq_batch") == c

    def test_global_sentinel_works(self, sv: ScopeVersions) -> None:
        """``"__global__"`` sentinel (Car B convention) is a valid scope_id."""
        sv.bump("config", "__global__")
        assert sv.version("config", "__global__") == 1


class TestLedgerScopeKind:
    def test_bump_then_version_round_trip(self, sv: ScopeVersions) -> None:
        sv.bump("ledger", "m-agahi/yadgar")
        assert sv.version("ledger", "m-agahi/yadgar") == 1

    def test_repeated_bumps_monotonic(self, sv: ScopeVersions) -> None:
        a = sv.bump("ledger", "proj-x")
        b = sv.bump("ledger", "proj-x")
        assert a < b
        assert sv.version("ledger", "proj-x") == b

    def test_global_sentinel_works(self, sv: ScopeVersions) -> None:
        """``"__global__"`` sentinel for cross-project reads."""
        sv.bump("ledger", "__global__")
        assert sv.version("ledger", "__global__") == 1


class TestCrossKindIsolation:
    def test_same_id_distinct_kinds_do_not_collide(self, sv: ScopeVersions) -> None:
        """Bumping ``ledger("foo")`` must NOT move ``config("foo")`` — the
        (scope_kind, scope_id) tuple is the key, not scope_id alone."""
        sv.bump("ledger", "foo")
        assert sv.version("config", "foo") == 0
        assert sv.version("ledger", "foo") == 1

        sv.bump("config", "foo")
        assert sv.version("config", "foo") == 1
        assert sv.version("ledger", "foo") == 1  # still 1, did not move

    def test_data_kinds_still_work(self, sv: ScopeVersions) -> None:
        """Pre-existing ``slot`` / ``entity`` kinds are untouched (regression)."""
        sv.bump("slot", "1")
        sv.bump("entity", "e1")
        assert sv.version("slot", "1") == 1
        assert sv.version("entity", "e1") == 1
        assert sv.version("config", "1") == 0
