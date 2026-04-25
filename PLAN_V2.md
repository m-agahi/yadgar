# Yadgar v2 — Architecture Completion Plan

## Context

Yadgar is a biologically-inspired persistent memory engine for Claude Code. After Phase 1-3 (core migration, visualization, wiki), the system works but carries significant dead weight from experiments that didn't survive contact with reality. This plan finishes the design by cutting what failed, strengthening what survived, and making the system usable by others.

### Goals
1. **1000+ memories, 100+ wiki pages** — scale for heavy multi-project use
2. **Other users can install and use it** — portable, documented, approachable
3. **Wiki + memory linked properly** — bidirectional, relevance-gated, auto-curated
4. **Secret/sensitive data protection** — write-path and read-path policy enforcement
5. **Viz as product feature** — convincing, functional, not just a debug tool

### Design Principles (Carried Forward)
- Zero thresholds — all memories always accessible
- No reconsolidation — recall is read-only
- No compression — content preserved verbatim
- Slow decay — heat factor 0.9995
- Protected memories always win

---

## ✅ Phase 0: Finish the Design (Prune Dead Code) — COMPLETE (commits aeffcfb, 41d7644)

**Goal**: Remove experiments that didn't work. Every remaining module earns its place.

### 0.1 Delete Disabled Modules

| Module | Lines | Why delete |
|---|---|---|
| `reconsolidation.py` | 340 | Disabled — silently corrupted memory content on retrieval |
| `compression.py` | 427 | Disabled — destroyed specific details during summarization |
| `crdt_sync.py` | 333 | Dead — single-daemon architecture made multi-process sync obsolete |

**Total: ~1,100 lines removed. Zero risk — all disabled/dead.**

### 0.2 Audit & Likely Delete

| Module | Lines | Audit criteria |
|---|---|---|
| `sleep_compute.py` | 508 | Is anything in server.py or consolidation.py calling it? If not, delete. |
| `cls_store.py` | 558 | Dual-store complementary learning. Called from server.py? If not, delete. |
| `metacognition.py` | 568 | Called from retrieval.py via `set_metacognition`. Check if the metacognition signal is used in WRRF. If only used by `assess_coverage`/`detect_gaps` (tools being removed), delete. |
| `profiles.py` | 401 | Profile search in retrieval.py. Check usage — if profile table is empty, delete. |
| `narrative.py` | 233 | `get_project_story` tool is being removed. If nothing else calls it, delete. |
| `hopfield.py` | 271 | Retrieval signal being removed (Phase 1). Delete after Phase 1. |
| `hdc_encoder.py` | 194 | Retrieval signal being removed (Phase 1). Delete after Phase 1. |
| `fractal.py` | 528 | Retrieval signal being removed (Phase 1). Delete after Phase 1. |
| `causal_discovery.py` | 545 | `get_causal_chain` tool being removed. If nothing else calls it, delete. |

**Potential: ~3,800 additional lines. Audit each — only delete confirmed unused.**

### 0.3 Remove Imports & References

For every deleted module:
- Remove import from `server.py`
- Remove global variable and init/shutdown code
- Remove `_get_X()` helper
- Remove from `init_engines()` and `shutdown()`
- Remove any retrieval.py integration (`set_X()` calls)

### 0.4 Rename Tier-2 Bio Metaphors

Keep the biological branding externally ("biologically-inspired memory engine"). Rename internally for readability:

| Current | Renamed | Why |
|---|---|---|
| `AstrocyteEngine` | `ConsolidationScheduler` | "Astrocyte" tells a new contributor nothing |
| `HippocampalReplay` | `CheckpointRestore` | It's checkpoint/restore. Call it that. |
| `SensoryBuffer` | `ActionLogger` | It logs tool actions. |
| `HippoRetriever` | `Retriever` | It retrieves. |
| `PredictiveCodingGate` | `WriteGate` | It gates writes. "Predictive coding" is the mechanism, not the purpose. |
| `astrocyte_pool.py` | `consolidation.py` (merge into existing) | One consolidation module, not two. |
| `sensory_buffer.py` | `action_logger.py` | |

**Keep as-is** (names map to real behavior):
- `heat` — intuitive for recency/relevance
- `engram` — session clustering (unusual name but distinctive, part of brand)
- `thermodynamics.py` — heat decay math. Name is fine.
- `write gate` — clear purpose
- `cognitive_map.py` — SR navigation (if it survives Phase 1 audit)

### 0.5 Verification

- All existing tests pass after deletions
- `yadgar start` boots without errors
- `remember()`, `recall()`, `get_project_context()` work end-to-end
- Viz loads and displays graph

---

## ✅ Phase 1: Simplify Retrieval — COMPLETE (commits aeffcfb, 41d7644)

**Goal**: Cut from 8 signals to 3-4. Re-enable FTS. Break apart retrieval.py (2,592 lines).

### 1.1 Re-enable FTS

Current `WRRF_FTS_WEIGHT: 0.0` — full-text search is OFF. This is the cheapest, most reliable signal and it's disabled while exotic signals are enabled.

Set `WRRF_FTS_WEIGHT: 0.5` (or tune via benchmark).

### 1.2 Benchmark & Cut Signals

**Benchmark protocol:**
1. Create 20 test queries with expected top-3 results (ground truth from your actual memories)
2. Run recall with: baseline (vector + FTS + heat), current (8 signals, FTS off), full (8 + FTS)
3. Measure precision@5 for each configuration
4. If baseline matches or beats current, cut the exotic signals

**Signals to likely remove:**

| Signal | Weight | Why likely cut |
|---|---|---|
| Hopfield | 0.2 | Associative memory pattern completion — vector similarity already does this |
| HDC | 0.3 | Hyperdimensional computing encoder — experimental, unclear marginal value |
| Fractal | 0.2 | Fractal memory tree hierarchy — `recall_hierarchical` tool being removed |
| SR (successor representation) | 0.3 | Cognitive map navigation — `navigate_memory` tool being removed |

**Signals to keep:**

| Signal | Weight | Why keep |
|---|---|---|
| Vector | 1.0 | Core semantic similarity — foundation of all retrieval |
| FTS (BM25) | 0.5 | Keyword matching — catches what embeddings miss (exact names, paths) |
| PPR (Personalized PageRank) | 0.3 | KG graph walk — genuine value for entity-connected memories |
| Spreading activation | 0.3 | Review: does this add value beyond PPR? If not, cut too. |

**Target: 3-4 signals + cross-encoder reranking.** Cross-encoder (weight 0.6) is likely doing 80% of the quality work post-fusion anyway.

### 1.3 Break Apart retrieval.py

Current: one 2,592-line file doing everything. Split into:

```
yadgar/retrieval/
├── __init__.py          # Re-exports Retriever class
├── core.py              # Retriever class (renamed from HippoRetriever)
├── signals.py           # Individual signal implementations (vector, FTS, PPR)
├── fusion.py            # WRRF fusion logic
├── reranking.py         # Cross-encoder + NLI reranking
├── temporal.py          # Temporal expression parsing
└── entities.py          # Query entity extraction
```

### 1.4 Configurable Retrieval Profiles

For other users — don't force them to understand 8 signals:

```yaml
# ~/.yadgar/config.yaml
retrieval_profile: balanced  # fast | balanced | full

# fast: vector + FTS + heat (no reranking, lowest latency)
# balanced: vector + FTS + PPR + cross-encoder (default)
# full: all signals + cross-encoder + NLI (maximum quality, higher latency)
```

### 1.5 Verification

- Benchmark results documented
- recall() quality maintained or improved
- Query latency measured before/after
- All retrieval tests pass

---

## ✅ Phase 2: Rules Engine as Policy Layer + Secret Protection — COMPLETE (commit 2ad0987)

**Goal**: Rules engine governs both write path (remember, wiki_add) and read path (recall). Built-in secret detection prevents sensitive data from being stored.

### 2.1 Extend Rules to Write Path

Current rules engine only applies at retrieval (read path, line 1642 of server.py). Extend to write path:

```python
# In remember(), BEFORE write gate surprisal check:
if _rules_engine is not None:
    blocked, reason = _rules_engine.check_write_policy(content, context, tags)
    if blocked:
        return {"stored": False, "reason": f"blocked_by_policy: {reason}"}
```

Same for `wiki_add()` and `wiki_ingest()`.

New rule types:
- `"write_block"` — prevent storage if condition matches (hard block)
- `"write_redact"` — allow storage but strip matched patterns (soft protection)

### 2.2 Built-in Secret Detection (Always On, Not Configurable)

Hardcoded patterns that fire BEFORE user rules. These cannot be disabled:

```python
# yadgar/secrets.py (~100 lines)
SECRET_PATTERNS = [
    # AWS keys
    (r'AKIA[0-9A-Z]{16}', "AWS access key"),
    # Private keys
    (r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', "Private key"),
    # JWT tokens
    (r'eyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+', "JWT token"),
    # Generic high-entropy strings (API keys, tokens)
    (r'(?:api[_-]?key|token|secret|password|passwd|credentials?)\s*[=:]\s*["\']?[A-Za-z0-9+/=_-]{20,}', "Credential pattern"),
    # Connection strings with passwords
    (r'(?:mysql|postgres|mongodb|redis)://\w+:[^@\s]+@', "Database connection string"),
    # GitHub/GitLab tokens
    (r'(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}', "GitHub token"),
    (r'glpat-[A-Za-z0-9_-]{20,}', "GitLab token"),
]
```

On match:
- Memory/wiki is NOT stored
- Return includes `{"stored": false, "reason": "secret_detected: AWS access key", "pattern_matched": "AKIA..."}`
- Log the block event (but NOT the content) for audit

### 2.3 User-Configurable Write Rules

Via MCP tool or config:

```python
# Block all memories from a sensitive directory
add_rule("write_block", "directory", "directory_context matches /work/classified/*", "filter")

# Block content containing specific keywords
add_rule("write_block", "global", "content contains internal-api-key", "filter")

# Redact patterns instead of blocking
add_rule("write_redact", "global", "content matches *password=*", "redact:password=[A-Za-z0-9]+:password=***")
```

### 2.4 Read-Path Rules (Existing, Refined)

Current behavior stays — rules apply post-retrieval to filter/boost/penalize. But now this is explicitly the "read policy" complement to "write policy":

- `"hard"` read rules → filter memories from results
- `"soft"` read rules → boost/penalty on retrieval scores
- Wiki blending boost becomes a default soft rule (user can override)

### 2.5 Default Rules (Shipped with Yadgar)

Pre-loaded on first install:

```python
DEFAULT_RULES = [
    # Action stream memories get deprioritized in recall (they're noisy)
    ("soft", "global", "tag contains _action_stream", "penalty:0.3"),
    # Wiki results get a small curated boost (replaces hardcoded +0.15)
    ("soft", "global", "source == wiki", "boost:0.1"),
]
```

Users can modify or delete these. The wiki boost is no longer hardcoded in server.py.

### 2.6 Crash Recovery for Rules

Since user lost rules in a crash: rules should be exportable/importable.

```bash
yadgar rules export > my_rules.yaml
yadgar rules import my_rules.yaml
```

Also: rules should be stored with `is_protected`-equivalent semantics — they survive consolidation, they're backed up with the DB.

### 2.7 Verification

- Secret patterns detected and blocked (unit tests with known patterns)
- Write rules prevent storage correctly
- Read rules filter/boost correctly
- Rules survive daemon restart
- Export/import round-trips correctly

---

## ✅ Phase 3: Fix Wiki Architecture — COMPLETE (commit 1f5b73c)

**Goal**: Wiki blending is relevance-gated, bidirectional linking works, auto-curation via LLM proposals.

### 3.1 Relevance-Gated Blending

Replace hardcoded interleaving (server.py lines 964-992) with:

```python
# Wiki blending governed by rules engine (Phase 2 default soft rule)
# Only include wiki results that score above a minimum threshold
wiki_results = _wiki.query(query, max_results=3)
qualifying = [wr for wr in wiki_results if wr.get("_retrieval_score", 0) > 0.3]

if qualifying:
    for wr in qualifying:
        wr["_source"] = "wiki"
    # Merge by score (not fixed positions) — rules engine adjusts scores
    merged = sorted(merged + qualifying, key=lambda x: x.get("_retrieval_score", 0), reverse=True)
    merged = merged[:max_results]
```

The +0.15 boost is now a default read rule (Phase 2.5), not hardcoded.

### 3.2 Episodic Query Detection

Skip wiki blending when query is clearly temporal:

```python
_TEMPORAL_INDICATORS = {"yesterday", "today", "last week", "this morning", "just now", "earlier", "ago"}

def _is_episodic_query(query: str) -> bool:
    q_lower = query.lower()
    return any(indicator in q_lower for indicator in _TEMPORAL_INDICATORS)
```

If episodic → skip wiki query entirely. Saves latency + prevents irrelevant wiki results.

### 3.3 Bidirectional Memory↔Wiki Linking

**Direction 1** (existing): `wiki_page.source_memory_ids` → memories that contributed to this page.

**Direction 2** (new): When a wiki page cites a memory ID, update that memory record:

```python
# In wiki_add() / wiki_ingest():
if source_memory_ids:
    for mid in source_memory_ids:
        storage.add_wiki_ref_to_memory(mid, slug)
```

Memory record gains: `wiki_refs: ["surreal-bm25-patterns", "deployment-guide"]`

In recall results, this surfaces as: `"This memory contributed to wiki: [[surreal-bm25-patterns]]"`

### 3.4 Auto-Curation Proposal Workflow (LLM-Drafted)

During consolidation, detect memory clusters suitable for wiki synthesis:

**Detection criteria:**
- 3+ memories sharing 2+ tags in the same directory
- No existing wiki page covers this cluster (check via wiki_query)
- Memories are not `_action_stream` (noise)

**Proposal generation:**
1. Consolidation detects cluster
2. Call LLM (local ollama or Claude API — configurable) with prompt:
   ```
   Synthesize these N memories into a wiki article.
   Title, category, tags, content with [[wikilinks]].
   Preserve all specific details — names, paths, versions, commands.
   Do not generalize or summarize away specifics.
   ```
3. Store result in `wiki_draft` table (NOT published)
4. Surface in `get_project_context`:
   ```
   📝 Wiki draft: "SurrealDB BM25 Patterns" (from 4 memories)
      Approve: wiki_approve("surreal-bm25-patterns")
      Edit: wiki_edit("surreal-bm25-patterns")
      Discard: wiki_discard("surreal-bm25-patterns")
   ```
5. On approve: move from `wiki_draft` to `wiki_page`, create bidirectional links

**LLM config:**
```yaml
# ~/.yadgar/config.yaml
wiki_autocuration:
  enabled: true
  llm_provider: ollama  # ollama | anthropic | openai
  llm_model: llama3.2   # or claude-sonnet-4-20250514
  min_cluster_size: 3
  max_drafts_per_cycle: 3
```

### 3.5 New Tools

| Tool | Purpose |
|---|---|
| `wiki_approve(slug)` | Publish a draft to wiki |
| `wiki_discard(slug)` | Delete a draft |
| `wiki_drafts()` | List pending drafts |

### 3.6 Verification

- Wiki results only appear when relevant (not on temporal queries)
- Bidirectional links created and visible in recall results
- Auto-curation generates sensible drafts
- Approve/discard workflow works end-to-end
- Viz shows wiki hexagons with proper edges

---

## Phase 4: Tool Audit & Tiering

**Goal**: Reduce from 31 tools to ~10 core (always loaded) + power tier (on demand).

### 4.1 Core Tier (Always Loaded)

| # | Tool | Purpose |
|---|---|---|
| 1 | `remember` | Write memories |
| 2 | `recall` | Read memories |
| 3 | `get_project_context` | Session context |
| 4 | `forget` | Delete memories |
| 5 | `checkpoint` | Save working state |
| 6 | `restore` | Reload working state |
| 7 | `anchor` | Protect critical facts |
| 8 | `wiki_query` | Search wiki + memories |
| 9 | `wiki_add` | Create wiki pages |
| 10 | `memory_stats` | System health |
| 11 | `add_rule` | Create policy rules |
| 12 | `get_rules` | View active rules |

### 4.2 Power Tier (Loaded on Demand)

Loaded via `load_tools("wiki")` or `load_tools("admin")` or auto-loaded when a power tool is first called:

**Wiki tools**: `wiki_read`, `wiki_list`, `wiki_delete`, `wiki_ingest`, `wiki_lint`, `wiki_approve`, `wiki_discard`, `wiki_drafts`

**Admin tools**: `consolidate_now`, `reembed_all`, `validate_memory`, `seed_project`, `install_hooks`, `sync_instructions`

### 4.3 Remove Entirely

| Tool | Reason |
|---|---|
| `rate_memory` | Unused |
| `recall_hierarchical` | Fractal hierarchy removed (Phase 1) |
| `drill_down` | Fractal hierarchy removed (Phase 1) |
| `navigate_memory` | SR cognitive map removed (Phase 1) |
| `get_causal_chain` | Causal discovery removed (Phase 0) |
| `assess_coverage` | Metacognition removed (Phase 0) |
| `detect_gaps` | Metacognition removed (Phase 0) |
| `get_project_story` | Narrative engine removed (Phase 0) |
| `create_trigger` | Prospective memory — unused |

**9 tools removed. 31 → 22 → split into 12 core + 10 power.**

### 4.4 MCP Resources Audit

Current 5 resources: `memory://stats`, `memory://hot`, `memory://stale`, `memory://processes`, `memory://narrative/{directory}`.

Remove `memory://narrative/{directory}` (narrative engine deleted). Keep others — they're lightweight.

### 4.5 Verification

- Core tools load on startup
- Power tools load on demand without errors
- Removed tools are fully gone (no dangling references)
- Tool schema token overhead measured before/after

---

## Phase 5: Hook Overhead Reduction

**Goal**: Reduce per-session token overhead from hooks without losing useful auto-capture.

### 5.1 Post-Tool Capture Optimization

Current: logs EVERY tool call as `_action_stream` memory. These get pruned during consolidation anyway.

Fix:
- **Only capture state-modifying actions**: Write, Edit, Bash (commands that change things). Skip Read, Glob, Grep.
- **Batch**: Accumulate 5 actions before writing one combined memory
- **Skip self-referential**: Don't log MCP calls to Yadgar itself (circular)

### 5.2 Prompt-Recall Throttling

Current: fires on every user message, running a full recall.

Fix:
- Skip if `get_project_context` ran < 3 minutes ago (session-start already loaded context)
- Rate-limit to max 1 recall per 2 minutes for the same directory
- Make opt-in for new users (off by default, power users enable it)

### 5.3 Session-Start Context Capping

Current: returns ALL hot memories (with slow decay, almost everything is "hot" at scale).

Fix:
- Cap at top-15 by heat
- Include count: "Showing 15 of 847 memories. Use recall() for specific queries."
- Separate: top-10 memories + top-5 wiki pages (not interleaved)

### 5.4 Verification

- Measure token overhead per session before/after
- Auto-capture still records meaningful state changes
- No loss of cross-session continuity

---

## Phase 6: Portability & Packaging

**Goal**: `pip install yadgar && yadgar start` works on macOS, Linux, any Python ≥3.11.

### 6.1 Remove Platform Assumptions

- Verify no hardcoded paths (confirmed: none found in Python code)
- Daemon start: support foreground (default), background via platform detection:
  - Linux with systemd → `systemctl --user start yadgar`
  - macOS → launchd plist OR `nohup` fallback
  - Other → `nohup yadgar start &` with PID file at `~/.yadgar/yadgar.pid`
- Data directory: `~/.yadgar/` on all platforms (XDG_DATA_HOME optional)

### 6.2 First-Run Setup

`yadgar setup` (interactive):
1. Create `~/.yadgar/` and `~/.yadgar/config.yaml`
2. Download embedding model (all-MiniLM-L6-v2, ~80MB)
3. Initialize SurrealDB
4. Generate Claude Code MCP config snippet (copy-paste ready)
5. Install hooks (optional, prompted)
6. Load default rules (secret detection, wiki boost)

### 6.3 Docker Image

```dockerfile
FROM python:3.12-slim
RUN pip install yadgar
EXPOSE 8765 42069
VOLUME /data
ENV YADGAR_DATA_DIR=/data
CMD ["yadgar", "start", "--host", "0.0.0.0"]
```

### 6.4 PyPI Publish

- Clean pyproject.toml (already well-structured)
- Add `py.typed` marker
- Test install in clean venv on Python 3.11, 3.12, 3.13
- CI: GitHub Actions for test + publish on tag

### 6.5 README + Documentation

- What it is (3 sentences)
- Install (2 commands)
- Claude Code configuration (MCP settings JSON)
- First use: remember → recall → get_project_context
- Architecture diagram (from existing wiki)
- Configuration reference
- Rule examples (including secret protection)
- FAQ: "How is this different from just using markdown files?" (the Karpathy comparison)

### 6.6 Verification

- Clean `pip install` on macOS and Linux
- `yadgar setup` completes without errors
- Docker container starts and accepts MCP connections
- README quickstart works end-to-end

---

## Phase 7: Viz as Product Feature

**Goal**: Visualization convinces users that something real and useful is happening.

### 7.1 Fix Force Layout

- Remove pin-on-settle (makes graph feel dead)
- Use alpha decay + velocity decay for natural settling
- Dragging a node should attract/repel connected nodes naturally
- Strong charge repulsion (-30) with link distance based on edge weight

### 7.2 Full Content View

- Click a memory node → side panel shows full content (not truncated at 400 chars)
- Add "Expand" button or scrollable panel
- Wiki nodes: render markdown in side panel

### 7.3 Wiki Integration

- Wiki hexagons visible after Phase 3
- Memory→wiki edges (orange, directed)
- Wiki cross-reference edges (purple, dashed)
- Click wiki node → show full page content + linked memories

### 7.4 Onboarding

- "What am I looking at?" overlay for new users
- Legend: node shapes/colors, edge types
- Tooltip: hover any node for quick summary

### 7.5 Default Landing Page

`yadgar start` opens browser to `http://localhost:42069` with:
- Graph view (main)
- Stats panel (heat histogram, consolidation timeline)
- Quick actions: search bar (runs recall), "Add memory" button

### 7.6 Verification

- Graph loads with 100+ nodes without performance issues
- All node types and edge types visible and interactive
- Side panel shows full content
- New user can understand the viz without documentation

---

## Execution Order & Dependencies

```
Phase 0 (Prune)           ← No dependencies. Do first. ~1-2 sessions.
    ↓
Phase 1 (Retrieval)       ← Depends on Phase 0 (less code). ~2-3 sessions.
    ↓
Phase 2 (Rules + Secrets) ← Independent of Phase 1, but cleaner after. ~2 sessions.
    ↓
Phase 3 (Wiki)            ← Depends on Phase 2 (rules govern wiki blending). ~2-3 sessions.
    ↓
Phase 4 (Tools)           ← Depends on Phase 0-3 (know what survives). ~1 session.
    ↓
Phase 5 (Hooks)           ← Can parallel with Phase 4. ~1 session.
    ↓
Phase 6 (Portability)     ← Blocked by Phases 0-5 (package what's clean). ~2-3 sessions.
    ↓
Phase 7 (Viz)             ← After Phase 3 (wiki nodes). ~2-3 sessions.
```

**Estimated total: ~15-20 focused sessions.**

Phases 0-3 are the critical path. Phases 4-5 are optimization. Phase 6-7 are polish for external users.

---

## What Gets Deleted (Summary)

### Modules (~5,000-7,000 lines)
- `reconsolidation.py` (340)
- `compression.py` (427)
- `crdt_sync.py` (333)
- `hopfield.py` (271) — after Phase 1 benchmark
- `hdc_encoder.py` (194) — after Phase 1 benchmark
- `fractal.py` (528) — after Phase 1 benchmark
- `sleep_compute.py` (508) — if unused
- `cls_store.py` (558) — if unused
- `metacognition.py` (568) — if unused
- `causal_discovery.py` (545) — if unused
- `narrative.py` (233) — if unused
- `profiles.py` (401) — if unused

### MCP Tools (9 removed)
- `rate_memory`, `recall_hierarchical`, `drill_down`, `navigate_memory`
- `get_causal_chain`, `assess_coverage`, `detect_gaps`
- `get_project_story`, `create_trigger`

### What Gets Added
- `yadgar/secrets.py` — built-in secret detection patterns (~100 lines)
- Write-path policy enforcement in rules engine (~50 lines)
- Relevance-gated wiki blending (~30 lines)
- Bidirectional memory↔wiki linking (~40 lines)
- Wiki auto-curation proposal workflow (~200 lines)
- 3 new wiki tools: `wiki_approve`, `wiki_discard`, `wiki_drafts`
- `yadgar rules export/import` CLI commands
- Retrieval profiles (fast/balanced/full)
- Docker + packaging + README

### Net Effect
- **Before**: 22,137 lines, 38 modules, 31 tools, 8 retrieval signals
- **After (est)**: ~14,000-16,000 lines, ~25 modules, 22 tools (12 core + 10 power), 3-4 retrieval signals
- Simpler. Faster. Safer. Installable by others.
