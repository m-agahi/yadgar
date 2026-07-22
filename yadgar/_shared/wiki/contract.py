"""Wiki contract — option dataclass + canonical registries shared across layers.

T2 Car C (layer-boundary train): extracted from the flat wiki.py so
contract-only consumers (backend admin_exec/write_exec wiki impls, core viz
metadata) can import the shapes and registries without loading the WikiStore
impl module.
"""

from dataclasses import dataclass
from typing import Final

# Canonical wiki page category registry. Single source of truth — WikiStore
# validates against it and core viz legend/colors iterate it (no hardcoded
# 8-key literals anywhere else).
CATEGORIES: Final = frozenset(
    {
        "architecture",
        "decision",
        "pattern",
        "debugging",
        "reference",
        "convention",
        "fact",
        "analysis",
    }
)

# Canonical confidence-level registry for wiki pages.
CONFIDENCE_LEVELS: Final = frozenset({"high", "medium", "low"})


@dataclass
class WikiAddOptions:
    """Optional metadata bundle for WikiStore.add().

    Bundles the five least-frequently-passed kwargs so the public add()
    signature stays at 6 params (self + title + content + category + tags + opts)
    — below the params_hard=8 cap (I13).

    v5.55 complexity-debt campaign: extracted from add() params=10 → params=6.
    """

    source_memory_ids: list[int] | None = None
    confidence: str = "medium"
    branch: str | None = None
    directory_context: str | None = None
    page_type: str | None = None
    # Car B0 (#83): SHA256 of the source file bytes + the absolute source path,
    # for repo-wiki module pages (page_type='module'). Persisted so the host-side
    # `--stale-only` check can diff stored hash vs live file without a disk scan.
    hash: str | None = None
    source_file: str | None = None
