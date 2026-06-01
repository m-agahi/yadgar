# Competitor Scan — Graphify (2026-05-31)

**Scope:** Targeted single-project audit of [Graphify](https://graphify.net) — a tool that has been positioned in some 2026 coverage as an "AI memory layer" and that surfaced in user-facing search results adjacent to mem0 / Zep / Letta. Goal: determine whether Graphify is a competitor to yadgar, an integration target for reducing yadgar's per-recall token cost, or irrelevant.

**Yadgar version at time of writing:** v5.25.6 live (v5.26.0 Sonnet 500q benchmark rerun in flight on local-only branch). 8-stage retrieval pipeline. Pilot Haiku LongMemEval-s accuracy reported ~61-65% from in-flight Phase 2 work; Sonnet rerun supersedes.

**Bottom line up front:** Graphify is **not** a persistent agent-memory system. It is a static codebase-knowledge-graph indexer that builds a queryable graph from source files. The 71x token-reduction headline is real but measures "graph query vs grep+read-files," not anything comparable to yadgar's recall path. There is no integration angle for yadgar's 8-stage retrieval pipeline. The one genuine functional overlap is wiki-style code-structure documentation — and even there, the borrowable IP is the technique (tree-sitter + Leiden), not the code.

---

## 1. Identity

**Project:** [`safishamsi/graphify`](https://github.com/safishamsi/graphify) on GitHub.
**Tagline:** "AI coding assistant skill (Claude Code, Codex, OpenCode, Cursor, Gemini CLI, and more). Turn any folder of code, SQL schemas, R scripts, shell scripts, docs, papers, images, or videos into a queryable knowledge graph."
**License:** MIT.
**Authors:** Safi Shamsi (LinkedIn). Y Combinator S26 batch. Commercial entity: `graphifylabs.ai`.
**PyPI:** [`graphifyy`](https://pypi.org/project/graphifyy/) (double-y; the `graphify` name was taken).
**Language:** Python (CLI), tree-sitter for code parsing.
**Activity:** Created 2026-04-03. Default branch `v8`. Pushed 2026-05-31. ~57.2k stars / ~6k forks / 278 open issues. Releases on a near-daily cadence: `v0.8.22` (2026-05-27) through `v0.8.26` (2026-05-30). Three commits within an hour of this audit's wallclock. **Highly active, not abandoned, not vapor.**
**Coverage:** Mentioned in [Analytics Vidhya](https://www.analyticsvidhya.com/blog/2026/04/graphify-guide/), [Knightli](https://knightli.com/en/2026/05/21/safishamsi-graphify-ai-code-knowledge-graph/), [OpenClaw guide](https://openclawlaunch.com/guides/openclaw-graphify), [Amit Ray's Antigravity guide](https://amitray.com/antigravity-graphify-10x-faster-ai-coding/). Pitched as "Karpathy's LLM Wiki idea materialized."
**Notably absent from:** the [mem0 graph-memory roundup](https://mem0.ai/blog/graph-memory-solutions-ai-agents) (Mem0 / LangMem / Letta / Zep / Supermemory — not Graphify). Memory-systems benchmarks (LongMemEval, LoCoMo, BEAM) — no Graphify entry. This absence is the strongest single signal that the AI-memory community does not classify Graphify as a memory system.

---

## 2. Architecture

Graphify is a two-pass codebase indexer that emits an on-disk graph artifact, not an agent-memory backend.

**Pass 1 — Deterministic AST extraction.** Tree-sitter parses 33 languages (Python, TS/JS, Go, Rust, Java, C/C++, Ruby, C#, Kotlin, Scala, PHP, Swift, Lua, Zig, SQL, BYOND DM, shell, etc.). Extracts classes, functions, imports, call graphs, docstrings, rationale comments (`# NOTE:` / `# WHY:` / `# HACK:`). **No LLM is involved at this stage.** Output: nodes + edges in an in-memory NetworkX graph.

**Pass 2 — Parallel semantic extraction.** PDFs, markdown, images, videos go through an LLM backend (Claude / Gemini / OpenAI / DeepSeek / Kimi / Ollama / Bedrock / Claude-CLI). Extracts concepts and links them into the same NetworkX graph. Confidence tagged per edge: `EXTRACTED` (1.0), `INFERRED` (variable), `AMBIGUOUS` (flagged for review).

**Clustering.** Leiden community detection on the assembled graph (topology only, no embeddings, no vectors). Identifies "god nodes" (high centrality) and "surprising connections" (low-prior-probability links).

**Output artifacts** under `graphify-out/`:
- `graph.html` — interactive force-directed visualization.
- `GRAPH_REPORT.md` — markdown report of god nodes, communities, surprising connections, suggested questions.
- `graph.json` — full graph for programmatic query.
- `cache/` — SHA256-keyed incremental cache.
- Optional exports: SVG, GraphML (Gephi/yEd), Cypher (Neo4j push), Obsidian vault, markdown wiki.

**Storage model.** Single JSON file on disk. Committed to git alongside the repo. Git merge driver included to union-merge concurrent commits. **Zero database, zero embedding model, zero vector store, zero persistence layer.**

**Retrieval.** CLI subcommands: `graphify query "<question>"`, `graphify path A B` (shortest path), `graphify explain "<node>"`. These walk the in-memory graph loaded from `graph.json`. No vector similarity search. No FTS. No rerank. The retrieval mechanism is BFS/DFS graph traversal.

**MCP server.** Optional install (`pip install "graphifyy[mcp]"`). Tools: `query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `list_prs`, `get_pr_impact`, `triage_prs`. Stdio transport. Designed for AI coding assistants to call into the pre-built graph instead of re-grepping files.

Sources: [README](https://github.com/safishamsi/graphify/blob/v8/README.md), [Analytics Vidhya breakdown](https://www.analyticsvidhya.com/blog/2026/04/graphify-guide/), [OpenClaw guide](https://openclawlaunch.com/guides/openclaw-graphify).

---

## 3. Comparison Table

| Dimension | yadgar | mem0 | Zep / Graphiti | Graphify |
|---|---|---|---|---|
| **Category** | Agent memory + wiki | Agent memory | Agent memory (KG) | Codebase indexer |
| **Persistent across sessions** | Yes (memories + wiki) | Yes (facts) | Yes (KG episodes) | Yes (graph.json file) — but content is code structure, not conversation |
| **Tracks conversation / preferences** | Yes | Yes | Yes | **No** |
| **LongMemEval-s** | ~61-65% Haiku pilot; Sonnet 500q in flight | 94.4% | 63.8% | **Not benchmarked. Wrong category.** |
| **LoCoMo** | not run | 91.6% | not headline | **Not benchmarked. Wrong category.** |
| **Storage** | SurrealDB (embedded) | Qdrant / 24+ backends | Neo4j / FalkorDB / Kuzu / Neptune | Single `graph.json` file on disk |
| **Retrieval** | 8-stage (FTS+KNN+BM25+PPR+HyDE+NLI+CE+MMR) | Vector KNN + BM25 + entity | Graph traversal + vector + BM25 | Graph BFS/DFS only |
| **Vector embeddings** | Yes (HNSW backend) | Yes | Yes | **No** |
| **License** | Apache-2.0 | Apache-2.0 | Apache-2.0 (Graphiti) | MIT |
| **Cost model** | self-host free | OSS / freemium SaaS | OSS / SaaS | OSS free (LLM API costs for doc/PDF/image pass only) |
| **Branch awareness** | Yes (1.5× boost + scope resolution) | No | No | No (but graph rebuilds per-commit via git hook) |
| **Anchor / pinned memory** | Yes (`is_protected`, tiers, scopes) | Yes (custom_categories) | No | N/A — no memory concept |
| **Write-time contradiction** | Yes (v5.17.0) | No (conflict overwrite) | Partial (validity windows) | N/A |
| **Wiki / curated docs** | Yes (~1.9k pages) | No | No | Partial — auto-generates `GRAPH_REPORT.md` + `--wiki` flag |
| **MCP tools** | 32 first-party | ~4 | None first-party | 7 (`query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `list_prs`, `get_pr_impact`, `triage_prs`) |
| **Active 2026** | yes | yes | yes | yes (daily releases) |
| **GitHub stars** | n/a (private codebase) | ~48k | ~12k Graphiti / Zep separate | ~57k |
| **Funding / backing** | personal | $24M raised | acquired-era | Y Combinator S26 |

The dimension where comparison breaks: **Graphify is not in the same product category as the other three rows.** Putting it in a memory-system competitor table is a category error. The reason it appears in adjacent search results is that it ships an MCP server and uses the phrase "knowledge graph for AI agents" — but its persistence target is `src/` files, not conversations.

---

## 4. Benchmark Claims

**LongMemEval / LoCoMo / BEAM:** None. Graphify does not publish, and the LLM-memory benchmark community does not include it. The [Mem0 State-of-Memory 2026 article](https://mem0.ai/blog/state-of-ai-agent-memory-2026), the [Vectorize agent-memory benchmark](https://github.com/vectorize-io/agent-memory-benchmark), and the [Awesome-GraphMemory survey](https://github.com/DEEP-PolyU/Awesome-GraphMemory) do not list it. The [ByteRover 2.0 LoCoMo leaderboard](https://www.byterover.dev/blog/benchmark-ai-agent-memory) does not list it. This is correct — it is not the kind of system those benchmarks evaluate.

**The 71x token-reduction claim (in detail):** "Graphify achieves roughly 1.7k tokens per query vs. ~123k with a naive read-everything approach — a 71x reduction" ([amitray.com Antigravity guide](https://amitray.com/antigravity-graphify-10x-faster-ai-coding/), corroborated by [OpenClaw guide](https://openclawlaunch.com/guides/openclaw-graphify)). The Analytics Vidhya post cites "71.5x fewer tokens per query on a mixed corpus of Karpathy repos, research papers, and images."

What the baseline actually is: the agent reading raw source files cold to answer a code-structure question. What the win actually is: the agent reading a curated graph summary instead. **This is a measurement against `grep + cat`, not against any memory system.** Yadgar's `recall()` returns pre-curated memory chunks; the baseline that Graphify beats by 71x is not part of yadgar's call path at all. The claim is real but does not transfer.

**No independent reproduction.** No third party has reproduced the 71x figure. The marketing materials all cite the same source.

---

## 5. Token-Reduction Angle (Yadgar Integration)

The user asked specifically whether Graphify could plug into yadgar's 8-stage retrieval pipeline to reduce per-recall token cost. Walking the checklist:

| Mechanism | Does Graphify offer it? | Useful for yadgar? |
|---|---|---|
| Context compression of retrieved memories before LLM | **No.** Graphify does not summarize retrieved nodes; it returns subgraph paths. | N/A |
| Graph-walk replaces vector search | **Yes, but only over code AST.** Graphify has no embeddings. | No — yadgar's memories are not code ASTs. The graph Graphify builds is the wrong shape. |
| LRU / sketch-based result cache | **No.** Graphify caches AST parse output (SHA256 on file content), not query results. | No |
| Hybrid structured+text format with fewer tokens than narrative | **Partial.** `GRAPH_REPORT.md` is structured, but it is one artifact per repo, not per query. | Marginal — yadgar already returns ranked chunks, not narrative dumps. |
| Importable compression module | **No.** The compression IS the codebase graph. There is no standalone summarizer. | No |
| Importable Leiden community detection | **Yes, but trivial.** Graphify uses [`leidenalg`](https://github.com/vtraag/leidenalg) — already a public package. | If yadgar ever wanted community detection over its memory graph, just `pip install leidenalg`. No Graphify dependency needed. |

**Quantified verdict on per-recall token reduction: zero direct impact.** The 71x figure measures a workflow yadgar does not perform (cold file-read by an agent). Yadgar's recall path is already memory-structured; the comparable "post-retrieval token spend" is the size of the returned chunk list, which Graphify does not address.

**Indirect angle worth one sentence.** Yadgar's wiki contains ~1.9k pages. A small fraction of those are auto-generatable from AST (function signatures, call graphs, import topology). The technique Graphify uses — tree-sitter + Leiden + confidence-tagged edges — could in principle be borrowed if yadgar wanted to bootstrap a `code-structure` wiki category from a fresh repo. But this is the *idea*, not the *code*. The actual borrowable artifact would be ~50 lines of tree-sitter wrapper around `leidenalg`, written from scratch. No need to vendor Graphify.

---

## 6. License & Integration Feasibility

**Graphify license: MIT.** Compatible with yadgar's Apache-2.0 — MIT-licensed code can be vendored into an Apache-2.0 project with attribution.

**But there is nothing worth vendoring.** The pieces that look reusable on paper:

- Tree-sitter wrapping → trivial; reproducible in a day by writing against `py-tree-sitter` directly.
- Leiden community detection → already in the public `leidenalg` package; Graphify just calls it.
- MCP-over-stdio server → yadgar already has 32 MCP tools via FastMCP.
- Confidence tagging schema (`EXTRACTED` / `INFERRED` / `AMBIGUOUS`) → a tag convention, not code.

The integration cost (read Graphify's source, vendor a module, keep it in sync with their daily releases) exceeds the rewrite cost. The license is permissive, but permission without value is irrelevant.

**One genuine non-architectural concern:** Graphify is an MCP server competing for the same Claude Code tool-call attention surface as yadgar. If a user installs both, prompts and skills push the agent toward the graph-walk path for code-structure questions, while yadgar's `recall()` / `wiki_query()` handle everything else. This is a coexistence-with-handoff problem, not a conflict — but worth noting if user feedback ever surfaces tool-selection confusion.

---

## 7. Verdict

**Irrelevant as a memory-system competitor.** Graphify does not target persistent agent memory across sessions. It does not track conversations, preferences, anchors, or contradictions. It is not benchmarked on LongMemEval / LoCoMo and the memory-systems community does not classify it as one. The marketing language overlaps with memory-systems vocabulary ("knowledge graph for AI agents"), but the persistence target is `src/` files, not conversations.

**Irrelevant as an integration target for token reduction.** The 71x token-reduction headline measures "graph query vs raw file read" — a workflow yadgar's recall path does not perform. Graphify offers no context-compression module, no result cache, no embedding-free retrieval relevant to yadgar's memory chunks. The borrowable techniques (tree-sitter parsing, Leiden clustering) are public packages, not Graphify IP.

**Relevant only as a coexistence consideration.** Both ship MCP servers. A Claude Code user can install both and route code-structure questions to Graphify while routing memory / wiki / project questions to yadgar. No conflict, no integration work needed.

**Recommendation:** Move on. Do not add Graphify to the rolling competitor-tracking table. Revisit only if (a) Graphify pivots to persistent agent memory (their daily release pace makes this watchable cheaply via `gh api repos/safishamsi/graphify/releases`), or (b) yadgar wants to bootstrap a `code-structure` wiki category and decides to write its own tree-sitter+Leiden pipeline (in which case Graphify's choice of techniques is validation, not a dependency).

---

## 8. Open Questions

1. **Wiki-bootstrap from AST — worth a v5.99-class spike?** Yadgar has no auto-populated code-structure wiki category. A 50-line tree-sitter + leidenalg sidecar could emit `code-graph-*` wiki pages on commit. Value gated on whether the existing wiki coverage on code structure is actually thin (no audit run yet). **Decision needed:** ship as a v5.99-class deferred candidate or drop entirely?
2. **MCP tool-selection guidance — does the user run both?** If both yadgar and Graphify are installed in the same Claude Code session, what prompts the agent to prefer one for code questions vs the other? This is a Claude-Code config-level question, not a yadgar code question — but if it surfaces in user feedback, it lives in the integration layer (skills / CLAUDE.md), not yadgar itself.
3. **Confirm Sonnet 500q LongMemEval-s number does not change this verdict.** The in-flight v5.26.0 Sonnet rerun will refresh yadgar's accuracy headline. Even at 80%+ that does not change Graphify's category — they remain in different product spaces. No revisit triggered by yadgar benchmark numbers alone.

---

**Sources:**
[Graphify repo](https://github.com/safishamsi/graphify),
[Graphify website](https://graphify.net),
[Analytics Vidhya breakdown](https://www.analyticsvidhya.com/blog/2026/04/graphify-guide/),
[Knightli review](https://knightli.com/en/2026/05/21/safishamsi-graphify-ai-code-knowledge-graph/),
[OpenClaw + Graphify guide](https://openclawlaunch.com/guides/openclaw-graphify),
[Amit Ray Antigravity guide](https://amitray.com/antigravity-graphify-10x-faster-ai-coding/),
[Mem0 graph-memory roundup](https://mem0.ai/blog/graph-memory-solutions-ai-agents),
[Mem0 state-of-memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026),
[Vectorize agent-memory benchmark](https://github.com/vectorize-io/agent-memory-benchmark),
[Awesome-GraphMemory survey](https://github.com/DEEP-PolyU/Awesome-GraphMemory),
[ByteRover 2.0 LoCoMo leaderboard](https://www.byterover.dev/blog/benchmark-ai-agent-memory),
[Zep arXiv paper](https://arxiv.org/abs/2501.13956).
