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
    directory_context: str | None = None
    page_type: str | None = None
    # Car B (#83): explicit-slug + upsert write contract.
    #   slug=None  → store at the title-derived slug (unchanged backward compat).
    #   slug="..." → store at EXACTLY that slug, no title derivation. Required for
    #                structural pages whose crossrefs/stale-diff key on a
    #                caller-computed slug, not the title (originally built for the
    #                repo_wiki generator, since decommissioned — #33/ADR-0162).
    #   upsert=True  → create-or-overwrite at the (explicit or derived) slug.
    #   upsert=False + explicit slug that already exists → reject (slug_exists),
    #                do not overwrite. Only meaningful with an explicit slug; the
    #                legacy title-derived path always upserts by slug regardless.
    slug: str | None = None
    upsert: bool = True
    # Car F (0047 §7): server-side sanctioned insert. _wiki_write_canonical sets
    # ``_sanctioned=True`` on its payload so the storage-layer mutability gate
    # (mutability_gate.enforce_mutability) does not reject first-time inserts of
    # mutability='locked' page_types (e.g. ``adr``). The token is threaded via
    # WikiAddOptions so it survives the canonical-write seam and reaches
    # ``storage.insert_wiki_page`` as the gate's ``sanctioned`` kwarg.
    sanctioned: bool = False
