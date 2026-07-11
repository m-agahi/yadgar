"""yadgar._shared.rate_limit — token-bucket rate limiter package.

T2 Car D (D1, layer-boundary train): the flat ``rate_limit.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.rate_limit`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.rate_limit.rate_limit``
directly.

  rate_limit.py — TokenBucketRateLimiter — per-caller token-bucket limiter
"""

from typing import Final

_EXPORTS: Final = {
    "OrderedDict": "yadgar._shared.rate_limit.rate_limit",
    "TokenBucketRateLimiter": "yadgar._shared.rate_limit.rate_limit",
    "_MAX_KEYS": "yadgar._shared.rate_limit.rate_limit",
    "annotations": "yadgar._shared.rate_limit.rate_limit",
    "observe": "yadgar._shared.rate_limit.rate_limit",
    "threading": "yadgar._shared.rate_limit.rate_limit",
    "time": "yadgar._shared.rate_limit.rate_limit",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
