"""Car B — backend PTC cache factories (config + ledger namespaces).

The PTC is a separate cache surface for config + ledger reads. Each namespace
is a fresh ``Cache(name=..., invalidation=Manual(), key_fn=...)`` whose
``key_fn`` embeds the current scope version, so a ``ScopeVersions.bump`` makes
prior keys unreachable — no explicit ``invalidate`` call.

Naming follows the existing factory convention in ``cache_budgets.py``
(``_make_<name>_cache`` + ``get_<name>_cache`` accessor + weight registered in
``_NAMESPACE_WEIGHTS``).
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from yadgar.backend.cache import cache_budgets
from yadgar.backend.cache.cache_budgets import (
    _make_config_ptc_cache,
    _make_ledger_ptc_cache,
    get_config_ptc_cache,
    get_ledger_ptc_cache,
)
from yadgar.backend.cache.scope_versions import get_scope_versions


@pytest.fixture(autouse=True)
def _clear_registry() -> Generator[None]:
    """Drop any registered config/ledger namespace from prior tests.

    The backend ``Cache`` is overwrite-on-dup, so a stale instance would mask
    a freshly-built factory's behavior.
    """
    from yadgar.backend.cache.cache import _REGISTRY

    _REGISTRY.pop("config_ptc", None)
    _REGISTRY.pop("ledger_ptc", None)
    yield
    _REGISTRY.pop("config_ptc", None)
    _REGISTRY.pop("ledger_ptc", None)


class TestPtcFactoryShape:
    def test_config_ptc_factory_returns_cache(self) -> None:
        cache = _make_config_ptc_cache()
        assert cache.name == "config_ptc"
        # Manual invalidation (no TTL / no ModelCkpt) — writes are Car B's
        # whole-flush-equivalent: bump the scope version, prior keys miss.
        from yadgar.backend.cache.cache import Manual

        assert isinstance(cache._invalidation, Manual)

    def test_ledger_ptc_factory_returns_cache(self) -> None:
        cache = _make_ledger_ptc_cache()
        assert cache.name == "ledger_ptc"
        from yadgar.backend.cache.cache import Manual

        assert isinstance(cache._invalidation, Manual)

    def test_getter_returns_same_instance(self) -> None:
        a = get_config_ptc_cache()
        b = get_config_ptc_cache()
        assert a is b

    def test_ledger_getter_returns_same_instance(self) -> None:
        a = get_ledger_ptc_cache()
        b = get_ledger_ptc_cache()
        assert a is b

    def test_config_and_ledger_distinct(self) -> None:
        assert get_config_ptc_cache() is not get_ledger_ptc_cache()


class TestNamespaceWeightsRegistered:
    def test_config_ptc_weight_present(self) -> None:
        assert "config_ptc" in cache_budgets._NAMESPACE_WEIGHTS

    def test_ledger_ptc_weight_present(self) -> None:
        assert "ledger_ptc" in cache_budgets._NAMESPACE_WEIGHTS


class TestVersionInKeyInvalidation:
    def test_bump_makes_prior_config_key_unreachable(self) -> None:
        cache = get_config_ptc_cache()
        sv = get_scope_versions()

        cache.put(("seq_batch", None), {"value": 1})
        assert cache.get(("seq_batch", None)) == {"value": 1}

        sv.bump("config", "seq_batch")
        assert cache.get(("seq_batch", None)) is None  # key_fn re-derived → miss

    def test_bump_makes_prior_ledger_key_unreachable(self) -> None:
        cache = get_ledger_ptc_cache()
        sv = get_scope_versions()

        cache.put(("m-agahi/yadgar", "task"), [{"id": 1}])
        assert cache.get(("m-agahi/yadgar", "task")) == [{"id": 1}]

        sv.bump("ledger", "m-agahi/yadgar")
        assert cache.get(("m-agahi/yadgar", "task")) is None

    def test_distinct_scopes_do_not_invalidate_each_other(self) -> None:
        cache = get_config_ptc_cache()
        sv = get_scope_versions()

        cache.put(("alpha", None), 1)
        cache.put(("beta", None), 2)

        sv.bump("config", "alpha")
        assert cache.get(("alpha", None)) is None  # bumped — miss
        assert cache.get(("beta", None)) == 2  # untouched — hit


class TestGetExportable:
    def test_config_ptc_getter_exported(self) -> None:
        """The accessor is re-exported from ``yadgar.backend.cache`` (lazy
        via the PEP-562 __init__)."""
        import yadgar.backend.cache as bc  # noqa: PLC0415

        assert bc.get_config_ptc_cache is get_config_ptc_cache

    def test_ledger_ptc_getter_exported(self) -> None:
        import yadgar.backend.cache as bc  # noqa: PLC0415

        assert bc.get_ledger_ptc_cache is get_ledger_ptc_cache
