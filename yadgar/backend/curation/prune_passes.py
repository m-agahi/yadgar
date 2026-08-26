"""Prune passes for the memify self-improvement cycle."""

import json
import logging
from datetime import UTC, datetime, timedelta

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine
from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

logger = logging.getLogger(__name__)


@observe(tier="stage")
def _parse_tags(mem: dict) -> list:
    tags = mem.get("tags", [])
    if isinstance(tags, str):
        tags = json.loads(tags)
    return tags


@observe(tier="stage")
def _prune_action_stream_cold(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 1: delete cold, unaccessed, low-confidence action-stream summaries."""
    for mem in candidates:
        tags = _parse_tags(mem)
        if "_action_stream" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (
            (mem.get("heat") or 0.0) < 0.01
            and (mem.get("confidence") or 1.0) < 0.3
            and (mem.get("access_count") or 0) == 0
        ):
            storage.delete_memory(mem["id"])
            stats["pruned"] += 1


@observe(tier="stage")
def _prune_auto_generated_old(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 2: delete cold, stale, old auto-generated memories.

    v5.66 replaced "ever-accessed = immortal" with a ``last_accessed``
    recency reprieve.  Ledger task 386 deletes the reprieve: HEAT is this
    pass's recency signal and it already covers the case, so the second
    check was dead at defaults and a live self-renewing loop away from them.

    THE ARITHMETIC (measured 2026-08-26, default settings)
    -----------------------------------------------------
    ``boost_memories_access`` — the ONE live writer of ``last_accessed``
    (``update_memory_last_accessed`` has no production caller) — sets
    ``heat = min(heat + 0.1, 1.0)`` and ``last_accessed`` in the SAME
    statement.  So a recalled row leaves recall at heat >= 0.1, and at
    ``DECAY_FACTOR=0.9995`` it needs ~134 days to decay under
    ``COLD_THRESHOLD=0.02``.  Any row cold enough to clear the heat gate
    above has therefore not been recalled in ~134 days — more than 4x the
    30-day window the reprieve tested.  The branch could not be reached.

    Away from defaults it was not merely dead, it was pass 3's bug: set
    ``AUTO_GENERATED_MEMORY_MAX_AGE_DAYS`` above ~134 and a cold row read
    inside its own cap spares itself, indefinitely, because ``recall()`` is
    what wrote the timestamp it is judged by.

    Heat is kept as the gate because heat is a DECAYING quantity — a single
    recall buys ~134 days and then expires on its own.  A raw timestamp
    comparison buys the full cap and renews in full on the next hit, which
    is the difference between a reprieve and immortality.
    """
    max_age_days = settings.AUTO_GENERATED_MEMORY_MAX_AGE_DAYS
    cold_threshold = settings.COLD_THRESHOLD
    age_cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "auto-generated" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (mem.get("heat") or 0.0) >= cold_threshold:
            continue
        created_at = mem.get("created_at") or ""
        if created_at > age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


@observe(tier="stage")
def _prune_auto_abstracted_old(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 3: hard cap CLS-promoted auto-abstracted semantics by AGE alone.

    Action-stream noise and file co-occurrence patterns start at heat=0.8
    and decay ~0.9995/hour — reaching COLD_THRESHOLD takes 300+ days, so
    the 30-day age cap would never fire with a heat gate.  Age is therefore
    the ONLY cap these rows have; no heat check.

    THE AGE CAP IS AN AGE CAP (ledger task 386)
    -------------------------------------------
    v5.66 added a ``last_accessed`` reprieve here on the premise that
    ``last_accessed`` is "a reliable recency signal" for value.  It is not,
    and the reprieve inverted the pass into a self-reinforcing loop:
    ``recall()`` bumps ``last_accessed`` on EVERY hit, so an auto-abstracted
    "Recurring pattern…" row that keeps matching queries kept sparing itself
    — and the more useless-but-matchy it was, the more it surfaced, and the
    longer it lived.  Retrieval frequency is evidence that a row's embedding
    is generic, not that a machine-generated abstraction is worth keeping;
    the ROW never earned its reprieve, the matcher granted it.

    Pass 4 (``_prune_dream_insights``) already draws this line the right way
    for the same class of machine-generated row: "one accidental recall must
    not let a dream insight escape the age cap forever."  Pass 3 now matches
    it.  An auto-abstracted row past ``AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS``
    is deleted regardless of when it was last read.  A row that genuinely
    must outlive the cap has one sanctioned escape — ``is_protected`` — which
    is checked below and is a human decision rather than a matcher artefact.

    The memory:1110 zombie (38d old, heat=0, access_count=2, last_accessed
    32d ago) is still purged; so now is its twin that was read yesterday.
    """
    auto_abstracted_max_age = settings.AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS
    if auto_abstracted_max_age <= 0:
        return
    aa_age_cutoff = (datetime.now(UTC) - timedelta(days=auto_abstracted_max_age)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "auto-abstracted" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        created_at = mem.get("created_at") or ""
        if created_at > aa_age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


@observe(tier="stage")
def _prune_dream_insights(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 4: hard cap dream insights by age regardless of heat or access_count.

    Dream insights start at heat=0.5 and decay to ~0.1 within days, but
    COLD_THRESHOLD=0.02 means they linger for weeks.  Hard cap by age
    regardless of heat or access_count — one accidental recall must not
    let a dream insight escape the age cap forever.
    """
    dream_max_age = settings.DREAM_INSIGHT_MAX_AGE_DAYS
    if dream_max_age <= 0:
        return
    dream_age_cutoff = (datetime.now(UTC) - timedelta(days=dream_max_age)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "dream" not in tags or "auto-generated" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        created_at = mem.get("created_at") or ""
        if created_at > dream_age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


@observe(tier="stage")
def _prune_action_stream_aged(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 5: prune _action_stream episodics beyond ACTION_STREAM_MAX_AGE_DAYS.

    Action log summaries (tool-call batches) are tagged _action_stream and
    created at heat=0.4.  Pass 1 prunes at heat<0.01 — that takes ~300 days.
    One access (access_count=1) blocked Pass 5 forever under the old guard,
    so stale summaries piled up indefinitely.

    v5.66 replaced "ever-accessed = immortal" with a ``last_accessed``
    recency reprieve.  Ledger task 386 deletes it: this pass has NO heat
    gate — the paragraph above says so, pass 1's gate needs ~300 days — so
    the age cap is the ONLY cap an ``_action_stream`` row has, and a
    reprieve on the only cap does not defer the deletion, it cancels it.

    That is pass 3's defect verbatim, and it ran the same way: ``recall()``
    writes ``last_accessed``, so a summary that kept matching queries kept
    pushing its own cutoff forward and no age could ever reach it.  Unlike
    pass 2 there was no heat gate behind it to make the branch unreachable,
    so this one was live at the shipped defaults.

    An action-stream summary past ``ACTION_STREAM_MAX_AGE_DAYS`` is now
    deleted regardless of when it was last read.  ``is_protected`` remains
    the one escape, as in passes 3 and 4.
    """
    action_stream_max_age = settings.ACTION_STREAM_MAX_AGE_DAYS
    if action_stream_max_age <= 0:
        return
    as_age_cutoff = (datetime.now(UTC) - timedelta(days=action_stream_max_age)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "_action_stream" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        created_at = mem.get("created_at") or ""
        if created_at > as_age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


@observe(tier="stage")
def _prune_degenerate_auto_abstracted(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 6: delete degenerate auto-abstracted semantics unconditionally.

    CLS consolidation_cycle can emit "Recurring pattern across N observations:
    frequently modified together" when clusters are built from _memify_derive
    placeholders.  These memories have no subject and are pure noise.
    Delete unconditionally (regardless of age / heat / access_count) — they
    were never meaningful, so access_count>0 offers no protection here.

    v5.66: removed access_count guard.  Degenerate content is structurally
    invalid (no subject, no signal); an accidental recall should not grant
    immortality.  is_protected is still always honoured.
    """
    for mem in candidates:
        tags = _parse_tags(mem)
        if "auto-abstracted" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        content = mem.get("content") or ""
        if _is_degenerate_auto_abstracted(content):
            logger.info(
                "Pruning degenerate auto-abstracted memory %d: %r",
                mem["id"],
                content[:80],
            )
            storage.delete_memory(mem["id"])
            stats["pruned"] += 1


@observe(tier="stage")
def _memify_prune(
    storage: StorageEngine,
    settings: Settings,
    stats: dict,
) -> None:
    """Delete cold, unaccessed, stale auto-generated memories.

    Pass 1 (action-stream): summaries tagged _action_stream that are cold
    (heat<0.01), low-confidence (<0.3), and never accessed.

    Pass 2 (auto-generated): memories tagged "auto-generated" (derived facts,
    dream insights, CLS semantic promotions) that are cold (heat<COLD_THRESHOLD),
    never accessed (access_count==0 or NONE), older than
    AUTO_GENERATED_MEMORY_MAX_AGE_DAYS, and not protected.

    User-created memories are never touched by either pass.
    """
    candidates = storage.get_memories_by_heat(min_heat=0.0, limit=10000)

    _prune_action_stream_cold(candidates, stats, storage, settings)

    if settings.AUTO_GENERATED_MEMORY_MAX_AGE_DAYS <= 0:
        return  # disabled — gates passes 2-6

    _prune_auto_generated_old(candidates, stats, storage, settings)
    _prune_auto_abstracted_old(candidates, stats, storage, settings)
    _prune_dream_insights(candidates, stats, storage, settings)
    _prune_action_stream_aged(candidates, stats, storage, settings)
    _prune_degenerate_auto_abstracted(candidates, stats, storage, settings)
