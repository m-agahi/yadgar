"""T2 Car C — contract/impl split seam tests (layer-boundary train).

Pins the two seam behaviors introduced by the restoration + wiki package splits:

1. Contract isolation: importing the contract module alone must NOT load the
   impl module (that is the point of the split — backend contract-only
   consumers stop paying for / depending on the impl).
2. PEP-562 shim back-compat: the old flat import paths
   (``yadgar._shared.restoration``, ``yadgar._shared.wiki``) keep resolving the
   public names, and resolve them to the SAME objects as the new canonical
   submodule paths (Car 0 #167 shim precedent).
"""

import subprocess
import sys

import pytest

# ── 1. Contract isolation (subprocess: clean interpreter, no test-session bleed) ──


def _run_isolated(code: str) -> None:
    """Run *code* in a fresh interpreter; raise on non-zero exit."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"isolated import check failed:\n{proc.stderr}"


def test_restoration_contract_import_does_not_load_impl():
    """Importing restoration.contract must not pull in the CheckpointRestore impl."""
    _run_isolated(
        "import sys\n"
        "import yadgar._shared.restoration.contract\n"
        "assert 'yadgar._shared.restoration.checkpoint_restore' not in sys.modules, (\n"
        "    'contract import loaded the impl module')\n"
        "assert 'yadgar._shared.storage' not in sys.modules, (\n"
        "    'contract import loaded storage')\n"
    )


def test_wiki_contract_import_does_not_load_store():
    """Importing wiki.contract must not pull in the WikiStore impl module."""
    _run_isolated(
        "import sys\n"
        "import yadgar._shared.wiki.contract\n"
        "assert 'yadgar._shared.wiki.store' not in sys.modules, (\n"
        "    'contract import loaded the store module')\n"
    )


# ── 2. PEP-562 shim back-compat ───────────────────────────────────────────────


def test_restoration_old_path_resolves_contract_and_impl():
    """Old flat path keeps working and resolves the same objects as canonical paths."""
    import yadgar._shared.restoration as pkg
    from yadgar._shared.restoration.checkpoint_restore import CheckpointRestore
    from yadgar._shared.restoration.contract import CheckpointContext

    assert pkg.CheckpointContext is CheckpointContext
    assert pkg.CheckpointRestore is CheckpointRestore


def test_wiki_old_path_resolves_contract_and_impl():
    """Old flat path keeps working and resolves the same objects as canonical paths."""
    import yadgar._shared.wiki as pkg
    from yadgar._shared.wiki.contract import CATEGORIES, CONFIDENCE_LEVELS, WikiAddOptions
    from yadgar._shared.wiki.store import WikiStore

    assert pkg.WikiAddOptions is WikiAddOptions
    assert pkg.WikiStore is WikiStore
    assert pkg.CATEGORIES is CATEGORIES
    assert pkg.CONFIDENCE_LEVELS is CONFIDENCE_LEVELS


def test_wikistore_class_registries_are_the_contract_registries():
    """WikiStore.CATEGORIES/CONFIDENCE_LEVELS stay as class attrs (test/viz back-compat)
    and are the SAME frozensets as the canonical contract registries."""
    from yadgar._shared.wiki.contract import CATEGORIES, CONFIDENCE_LEVELS
    from yadgar._shared.wiki.store import WikiStore

    assert WikiStore.CATEGORIES is CATEGORIES
    assert WikiStore.CONFIDENCE_LEVELS is CONFIDENCE_LEVELS


@pytest.mark.parametrize(
    "module_name",
    ["yadgar._shared.restoration", "yadgar._shared.wiki"],
)
def test_shim_unknown_attribute_raises(module_name):
    """The PEP-562 shims raise AttributeError for unknown names (not silent None)."""
    import importlib

    mod = importlib.import_module(module_name)
    with pytest.raises(AttributeError):
        _ = mod.definitely_not_a_real_symbol


@pytest.mark.parametrize(
    ("module_name", "expected"),
    [
        ("yadgar._shared.restoration", {"CheckpointContext", "CheckpointRestore"}),
        ("yadgar._shared.wiki", {"WikiAddOptions", "WikiStore", "CATEGORIES"}),
    ],
)
def test_shim_dir_lists_exports(module_name, expected):
    """dir() on the shim packages surfaces the lazily-exported names."""
    import importlib

    mod = importlib.import_module(module_name)
    assert expected <= set(dir(mod))
