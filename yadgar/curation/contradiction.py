"""Contradiction detection for memory curation."""

import logging
import re

from yadgar.observability.observe import observe
from yadgar.storage import StorageEngine

logger = logging.getLogger(__name__)

# Negation patterns for contradiction detection
_NEGATION_RE = re.compile(
    r"\b(not|don't|doesn't|didn't|won't|can't|cannot|isn't|aren't|wasn't|weren't|"
    r"no longer|instead of|rather than|replaced|switched from|stopped using|"
    r"removed|deprecated|dropped|never)\b",
    re.IGNORECASE,
)

# Verb extraction for entity-action comparison
_ACTION_RE = re.compile(
    r"\b(use|using|uses|prefer|prefers|run|runs|running|install|installed|"
    r"deploy|deployed|enable|enabled|disable|disabled|add|added|remove|removed|"
    r"switch|switched|migrate|migrated|choose|chose|set|configured)\b",
    re.IGNORECASE,
)


@observe(tier="stage")
def detect_contradictions(
    storage: StorageEngine,
    similar: list[tuple[int, float]],
    new_content: str,
) -> list[dict]:
    """Find existing memories that may contradict new_content.

    Returns list of dicts: {"memory_id", "content", "similarity", "reason"}.
    """
    contradictions = []

    new_has_negation = bool(_NEGATION_RE.search(new_content))
    new_actions = set(a.lower() for a in _ACTION_RE.findall(new_content))

    for mem_id, sim in similar:
        mem = storage.get_memory(mem_id)
        if mem is None:
            continue

        old_content = mem["content"]
        old_has_negation = bool(_NEGATION_RE.search(old_content))

        # Check 1: one has negation patterns, the other doesn't
        if new_has_negation != old_has_negation:
            contradictions.append(
                {
                    "memory_id": mem_id,
                    "content": old_content,
                    "similarity": sim,
                    "reason": "negation_mismatch",
                }
            )
            # Reduce confidence of old contradicting memory
            old_confidence = mem.get("confidence", 1.0)
            storage.update_memory_fields(mem_id, confidence=max(old_confidence - 0.2, 0.1))
            continue

        # Check 2: same entities but different actions
        old_actions = set(a.lower() for a in _ACTION_RE.findall(old_content))
        if new_actions and old_actions and new_actions != old_actions:
            # Only flag if there's meaningful overlap in subject matter
            # (similarity > 0.7 already ensures topical overlap)
            shared = new_actions & old_actions
            if len(shared) < len(new_actions | old_actions) * 0.5:
                contradictions.append(
                    {
                        "memory_id": mem_id,
                        "content": old_content,
                        "similarity": sim,
                        "reason": "action_divergence",
                    }
                )
                old_confidence = mem.get("confidence", 1.0)
                storage.update_memory_fields(mem_id, confidence=max(old_confidence - 0.1, 0.1))

    return contradictions
