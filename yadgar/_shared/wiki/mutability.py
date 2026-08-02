# SPDX-License-Identifier: Apache-2.0
"""Wiki mutability resolver — Car J.

Per-type policy + per-page override. The resolver picks the override
when set, else the policy default.

D26:
  adr / adr_superseded → locked
  task → free
  agent_prompt → free
  rollups → derived
"""

from __future__ import annotations

from typing import Literal

Mutability = Literal["free", "locked", "derived"]

VALID_MUTABILITY: frozenset[str] = frozenset({"free", "locked", "derived"})


def effective_mutability(
    *, page_type: str | None, override: str | None
) -> str:
    """Return the effective mutability for a page.

    Override beats policy. Invalid override values fall back to the
    policy default (logged).
    """
    from yadgar._shared.wiki.policy import get_policy

    if override is not None and override in VALID_MUTABILITY:
        return override
    return get_policy(page_type).mutability


def can_edit(*, page_type: str | None, override: str | None) -> bool:
    """Return True if agent/tool edits are permitted on this page.

    ``locked`` blocks edits; ``free`` and ``derived`` allow them
    (derived pages regenerate on write but aren't rejected here —
    the enforcement layer regenerates instead of editing).
    """
    return effective_mutability(page_type=page_type, override=override) != "locked"
