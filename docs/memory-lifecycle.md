# Memory Lifecycle

How a memory is born, lives, decays, and eventually disappears.

## Memory Types

| Type | How created | Initial heat | Decay floor | Prune? |
|---|---|---|---|---|
| **Normal memory** | `memorize()` call | 1.0 | `COLD_THRESHOLD` (0.02) | Never deleted |
| **Anchor** | `anchor()` call | 1.0 | Never decays | Never deleted |
| **Protected** | `is_protected=True` | 1.0 | Never decays | Never deleted |
| **Action stream** | Auto-captured tool action | 0.4 | `ACTION_STREAM_COLD_THRESHOLD` (0.1) | Deleted when heat < 0.01, confidence = 0, access_count = 0 |
| **Wiki page** | `wiki_add()` or auto-proposed | — | Never decays | Via `wiki_delete()` only |
| **Episode** | Raw tool-event buffer | — | — | After entity extraction |

## Heat Decay Formula

Every consolidation cycle, each non-protected memory's heat is updated:

```
# Step 1 — emotional-valence modifier (applied first)
emotional_modifier = 1.0 + abs(emotional_valence) * EMOTIONAL_DECAY_RESISTANCE
effective_factor = 1.0 - (1.0 - DECAY_FACTOR) * (1.0 / emotional_modifier)

# Step 2 — confidence modifier (applied to already-modified factor)
confidence_modifier = 1.0 + confidence * 0.1
effective_factor = 1.0 - (1.0 - effective_factor) * (1.0 / confidence_modifier)

new_heat = current_heat * (effective_factor ^ hours_since_last_access)
```

With defaults (`DECAY_FACTOR=0.9995`):
- A memory accessed once per day: heat ≈ 0.988 per day, persists for years
- A normal memory (born at heat=1.0) not accessed: hits `COLD_THRESHOLD` (0.02) in ~**11 months**
- An action-stream memory (born at heat=0.4) not accessed: hits `ACTION_STREAM_COLD_THRESHOLD` (0.1) in ~**2–3 weeks**

Action-stream memories are archived dramatically faster than normal memories — weeks vs months — because they start lower (0.4 vs 1.0) and have a higher floor (0.1 vs 0.02). Before 4.2.0, `COLD_THRESHOLD=0.0` meant nothing was ever archived; action-stream memories effectively lived forever.

### Modifiers

The effective decay rate is slowed by:

- **Confidence** — high-confidence memories decay more slowly (`1.0 + confidence * 0.1` in denominator)
- **Importance** — memories tagged important use `IMPORTANCE_DECAY_FACTOR` (0.9999) instead of base factor
- **Emotional valence** — non-neutral valence applies `EMOTIONAL_DECAY_RESISTANCE` as an additional brake
- **Surprise boost** — novel memories receive `SURPRISE_BOOST` (0.3) added to heat at storage time
- **Session coherence** — memories from the current session get `SESSION_COHERENCE_BONUS` (0.2) added

## Archiving vs Deletion

**Archived**: heat set to 0.0, record remains in DB. Memory is invisible to retrieval but can be recovered.

**Deleted**: record permanently removed from DB. Cannot be recovered.

### When archiving happens

During each consolidation cycle, `_apply_decay` checks every non-protected memory:

```python
effective_cold = ACTION_STREAM_COLD_THRESHOLD if "_action_stream" in tags else COLD_THRESHOLD
if new_heat < effective_cold:
    new_heat = 0.0  # archived
```

- Normal memories: archived when heat < 0.02 (~6 months of no access at default decay)
- Action-stream memories: archived when heat < 0.1 (~2–3 weeks of no access)

### When deletion happens (`_memify_prune`)

Action-stream memories are permanently deleted when all three conditions hold:

1. `heat < 0.01` (fully cold)
2. `confidence < 0.3` (auto-captured, not hand-stored)
3. `access_count == 0` (never recalled)

Normal memories are never automatically deleted. Use `forget(memory_id)` for manual deletion.

## Write Gate

`memorize()` computes a novelty score before inserting. If the content is too similar to already-stored memories, the write is rejected:

- `WRITE_GATE_THRESHOLD = 0.0` means store everything (default)
- Higher values (e.g. 0.3) reject content that overlaps ≥ 30% with existing memories
- `WRITE_GATE_CONTINUITY_DISCOUNT` reduces the effective threshold for content that continues an in-progress task (avoids over-filtering during sustained work)

## Access and Reinforcement

When a memory is returned by `recall()` or `get_project_context()`:

1. `last_accessed` timestamp updated
2. `access_count` incremented
3. `heat` boosted slightly (keeps hot memories hot)
4. If heat is in the reconsolidation window (`RECONSOLIDATION_LOW_THRESHOLD`–`RECONSOLIDATION_HIGH_THRESHOLD`), the memory may be updated with new context from the current query

## Checkpoints and Anchors

**`checkpoint(directory, ...)`** — snapshots current working state (open files, key decisions, next steps). Survives context compaction.

**`anchor(content, context, reason)`** — stores a memory with maximum heat and `is_protected=True`. Never decays, never archived, never pruned. Use for facts that must persist indefinitely (architectural decisions, critical constraints).

**`restore(directory)`** — reconstructs working context from the latest checkpoint, anchors, and hottest recent memories.

## Action Stream

When `ACTION_STREAM_ENABLED=True` (default), every tool call is captured into the sensory buffer and flushed into episode records. During consolidation:

1. Episodes grouped into 30-minute windows per directory
2. Summarised into action-stream memories (tagged `_action_stream`, `_auto`)
3. Entity extraction runs on episode content
4. Knowledge graph edges created for co-occurring entities

Action-stream memories are intentionally ephemeral — they capture *what happened* during a session, not *what matters*. They fade within weeks unless you explicitly `memorize()` something from them.

## Curation (Duplicate Merging)

During consolidation, pairs of memories with similarity ≥ `CURATION_SIMILARITY_THRESHOLD` (0.95) are merged:

- The higher-heat memory survives
- The lower-heat memory is deleted
- Only near-exact duplicates are merged (0.95 is intentionally high to avoid false merges)

## Complementary Learning Systems (CLS)

The CLS stage promotes episodic patterns to semantic memory. When the same entities, concepts, or facts appear repeatedly across multiple sessions, they may be abstracted into a more general memory with higher base heat. This mirrors the hippocampus → neocortex consolidation process in biological memory.

## Configuration Summary

Key settings that control the lifecycle:

| Setting | Default | Effect |
|---|---|---|
| `DECAY_FACTOR` | 0.9995 | Per-hour heat decay multiplier |
| `COLD_THRESHOLD` | 0.02 | Archive floor for all memories |
| `ACTION_STREAM_COLD_THRESHOLD` | 0.1 | Archive floor for action-stream memories |
| `WRITE_GATE_THRESHOLD` | 0.0 | Novelty floor for incoming memories |
| `SESSION_COHERENCE_BONUS` | 0.2 | Heat bonus for current-session memories |
| `CURATION_SIMILARITY_THRESHOLD` | 0.95 | Similarity floor for duplicate merging |
| `COMPRESSION_GIST_AGE_HOURS` | 168 | Hours before gist-compressing old memories |
| `DECISION_AUTO_PROTECT` | true | Auto-protect detected decision statements |
| `ACTION_STREAM_ENABLED` | true | Enable auto-capture of tool actions |

See `docs/configuration.md` for the full reference.

## Project State Memories (v5.0)

Two per-directory memory patterns surface project-level context that doesn't fit episodic or semantic stores:

**`_project_init`** — single memory per directory tagged `_project_init`. Markdown table of contents pointing at wiki slugs, key memory IDs, conventions, lookup tips. Created via `bootstrap_project(directory, content)` (MCP tool, `power=True`). Server-side hard cap `PROJECT_INIT_CAP_CHARS = 2000` — overflow raises `ValueError`, no silent truncation. Idempotent replacement: existing entry is deleted before insert.

**`_active_work`** — single memory per directory tagged `_active_work`. No char cap. In-flight state Claude can read on session resume. Created via `update_active_work(directory, content)` (MCP tool, `power=True`). Atomic delete-then-insert in a single transaction; returns `previous_content` (or `None`) alongside the new memory dict.

Both are `is_protected=True` (never decay) and intentionally leave the `branch` column unset — project-level state is not branch-scoped.

`project_brief(directory, mode)` surfaces these on session start. In `catalog` mode (default, ~500 tokens), only their presence is reported. In `full` mode (~1050 tokens), full content is inlined alongside top anchors, hot memories, and key wiki pages.

`seed_project(directory)` drafts a starter `_project_init` from the project's README and top-level docs after a successful seed.
