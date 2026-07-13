"""Leaf registry module — the single shared Prometheus ``CollectorRegistry``.

P-SB P0: this module exists to break the ``observe → metrics → config → observe``
import cycle. It is a GENUINE LEAF — it imports ONLY ``prometheus_client`` and
NOTHING from ``yadgar``. Both ``metrics.py`` (which re-exports ``_registry`` for
back-compat) and ``observe.py`` (which registers the four ``yadgar_observe_*``
families) bind to this one object, and core :8765 renders it via
``generate_latest(_registry)``.

Do NOT add any ``yadgar.*`` import here. The whole point of the leaf is that
importing it can never re-enter a partially initialized yadgar module.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

# The one shared registry. Isolated (not prometheus_client's default REGISTRY) so
# tests can run without cross-contamination — identical semantics to the object
# metrics.py used to construct, now hoisted here so it is import-cycle-free.
_registry = CollectorRegistry()
