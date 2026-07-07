"""Yadgar observability helpers — Prometheus metrics decorators and gauges.

Note: the `@observe` decorator lives in the `yadgar.observability.observe`
submodule. It is intentionally NOT re-exported here — a package-level
``from .observe import observe`` would rebind the package attribute ``observe``
from the submodule to the function, shadowing ``import yadgar.observability.observe``.
Import it explicitly: ``from yadgar.observability.observe import observe``.
"""
