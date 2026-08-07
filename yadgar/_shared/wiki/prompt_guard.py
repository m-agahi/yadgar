"""ADR-0208 asymmetric weakening guard — the shared line-delta primitive.

Disciplines (``page_type=agent_discipline``, ADR-0209) are the rule sets that
bind every future dispatch, so an instance able to rewrite one unguarded could
weaken its own constraints. ADR-0208's answer is asymmetric rather than a ban:
ADDITIONS flow freely, a net REMOVAL of an existing rule needs explicit
ratification.

This module holds only the delta computation. It lives in ``_shared`` because
BOTH enforcement points need it and they sit on opposite sides of the
import-linter contract: ``discipline_save``'s front door in
``core/server/tools/agent_prompts.py`` (which re-exports
``_removed_prompt_lines`` for back-compat), and the wiki write chokepoint in
``_shared/wiki/store.py`` that closes task 23's bypass — ``wiki_replace_text``,
``wiki_delete_text``, ``wiki_append_section`` and the positional edit family all
resolved ``agent-discipline-*`` slugs like any other page and could strip rule
lines with zero ratification, because car 8's guard only ever protected its own
front door.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe


@observe(tier="hot", metric="wiki.prompt_guard.removed_prompt_lines")
def removed_prompt_lines(old_body: str, new_body: str) -> list[str]:
    """Return non-empty lines present in old_body but absent (verbatim) from new_body.

    ADR-0208 asymmetric guard, precise definition: an update is additions-only
    when every non-empty existing line survives *somewhere* in the incoming
    body — order and duplication don't matter. This mirrors
    scripts/check_test_weakening.py's delta-counting shape (count what changed,
    don't ban edits outright) rather than a line-position diff: a rule that
    moved to a different spot in the file is not a removal.

    Deduplicated: a repeated identical old line is only reported once.
    """
    new_lines = {ln for ln in new_body.splitlines() if ln.strip()}
    seen: set[str] = set()
    removed: list[str] = []
    for ln in old_body.splitlines():
        if not ln.strip() or ln in seen:
            continue
        seen.add(ln)
        if ln not in new_lines:
            removed.append(ln)
    return removed
