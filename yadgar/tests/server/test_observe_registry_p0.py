"""P-SB P0 — the observe↔metrics↔config circular-import fix (leaf registry).

Before this car, ``yadgar/_shared/observability/observe.py`` imported ``_registry``
from ``metrics.py`` at module load, and ``metrics.py`` imported ``resolve_knob``
from ``config.py``, whose ``@observe``-decorated ``YamlConfigSource._load`` imported
``observe`` back — a cycle. The re-entrant import hit a partially initialized module,
raised ``ImportError``, and a bare ``except Exception`` at observe.py silently set
``_PROM_AVAILABLE=False`` for the process lifetime. Every ``@observe`` boundary/stage
metric was silently dead.

The fix extracts the registry into a genuine leaf module
``yadgar/_shared/observability/registry.py`` (zero yadgar imports) that both
``metrics.py`` (re-export, identity-preserving) and ``observe.py`` (direct import in
an UNGUARDED ``else`` block) bind to.

These tests are the P0 regression guard:

* **Import-order (subprocess):** in a FRESH interpreter, importing ``observe`` first
  and importing ``metrics`` first must BOTH leave ``_PROM_AVAILABLE is True`` and the
  four ``yadgar_observe_*`` families registered on the leaf registry. Subprocess (not
  ``importlib.reload``) because the cold cycle only reproduces on a first-time import.
* **Identity:** the leaf ``registry._registry`` IS (object identity) the object
  ``metrics`` re-exports, the object ``observe`` registers on, and the object core
  ``metrics_handler`` renders. One shared ``CollectorRegistry`` across the chain.
* **Emission (no monkeypatch):** calling a real ``@observe(tier="stage")`` fn
  increments the stage histogram sample count in the SHARED registry — the existing
  decorator suite monkeypatches the families and so never caught the dead arm.
"""

from __future__ import annotations

import subprocess
import sys

_OBSERVE_FAMILIES = (
    "yadgar_observe_requests_total",
    "yadgar_observe_request_duration_seconds",
    "yadgar_observe_stage_duration_seconds",
    "yadgar_observe_stage_errors_total",
)


def _run_import_order_probe(first: str) -> str:
    """Import `first` module first in a fresh interpreter; print PROM + family flags.

    Returns the child's stdout (a single line: 'PROM=<bool> FAMS=<bool>').
    """
    other = (
        "yadgar._shared.observability.metrics"
        if first == "yadgar._shared.observability.observe"
        else "yadgar._shared.observability.observe"
    )
    code = f"""
import importlib
first = importlib.import_module({first!r})
other = importlib.import_module({other!r})
import yadgar._shared.observability.observe as obs
import yadgar._shared.observability.registry as reg
fams = {list(_OBSERVE_FAMILIES)!r}
names = set(reg._registry._names_to_collectors)
prom_ok = obs._PROM_AVAILABLE is True
fams_ok = all(f in names for f in fams)
print(f"PROM={{prom_ok}} FAMS={{fams_ok}}")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"probe (first={first}) exited {proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return proc.stdout.strip()


def test_import_order_observe_first_prom_available_and_families_registered():
    out = _run_import_order_probe("yadgar._shared.observability.observe")
    assert out == "PROM=True FAMS=True", out


def test_import_order_metrics_first_prom_available_and_families_registered():
    out = _run_import_order_probe("yadgar._shared.observability.metrics")
    assert out == "PROM=True FAMS=True", out


def test_registry_object_identity_across_leaf_metrics_observe():
    """The leaf registry IS the object metrics re-exports and observe registers on."""
    from yadgar._shared.observability import metrics as metrics_mod
    from yadgar._shared.observability import observe as observe_mod
    from yadgar._shared.observability import registry as registry_mod

    leaf = registry_mod._registry
    assert metrics_mod._registry is leaf, "metrics._registry must re-export the leaf object"
    assert observe_mod._yadgar_registry is leaf, "observe must register on the leaf object"


def test_all_four_observe_families_present_on_leaf_registry():
    from yadgar._shared.observability import registry as registry_mod

    names = set(registry_mod._registry._names_to_collectors)
    missing = [f for f in _OBSERVE_FAMILIES if f not in names]
    assert not missing, f"observe families missing from leaf registry: {missing}"


def test_stage_emission_increments_shared_registry_no_monkeypatch():
    """A real @observe(tier='stage') fn increments the stage histogram in the SHARED
    registry — with NO monkeypatch. This is the arm the dead-registry bug silenced.
    """
    from yadgar._shared.observability import observe as observe_mod
    from yadgar._shared.observability import registry as registry_mod

    assert observe_mod._PROM_AVAILABLE is True, "metric arm must be live post-fix"

    stage_label = "test.psb.p0.stage_emission_probe"

    @observe_mod.observe(tier="stage", metric=stage_label)
    def _probe(x: int) -> int:
        return x + 1

    def _count() -> float:
        val = registry_mod._registry.get_sample_value(
            "yadgar_observe_stage_duration_seconds_count", {"stage": stage_label}
        )
        return val or 0.0

    before = _count()
    assert _probe(1) == 2
    after = _count()
    assert after == before + 1.0, (
        f"stage histogram count did not increment: before={before} after={after}"
    )
