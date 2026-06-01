"""Calibration test — runs with tmp_path fixture to get proper SurrealDB env."""

import math
import struct

import pytest

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "cal.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


_ROADMAP_A = """# Yadgar Roadmap: Future Improvements

## Short-term (next 2 months)
- Implement wiki versioning (v5.41) to track page history
- Add similarity gate to wiki_add to prevent duplicate pages
- Improve embedding model to mpnet for better semantic search

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation
- Automated anchor hygiene with consolidation pass

## Long-term (6+ months)
- LLM-based duplicate resolution and wiki curation
- Retroactive deduplication of existing pages
- Distributed SurrealDB for large-scale deployment

## Architecture principles
Yadgar follows a thin-request-path invariant: all heavy computation deferred
to background consolidation. Wiki operations must complete in <100ms.
"""

_ROADMAP_B = """# Yadgar Future Roadmap

## Near-term (next 2 months)
- Wiki versioning (v5.41) - track page history and enable rollback
- Similarity gate in wiki_add - block near-duplicate page creation
- Better embedding model (mpnet) for semantic search quality

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation across workspaces
- Automated anchor hygiene during consolidation cycles

## Long-term (6+ months)
- LLM-based wiki curation and duplicate resolution
- Retroactive dedup of existing pages (v5.45+)
- Distributed SurrealDB for large deployments

## Core principles
Thin request path: heavy work deferred to consolidation background loop.
All wiki ops target <100ms latency.
"""

_ARCH = """# Yadgar Architecture

## Core components
StorageEngine: SurrealDB wrapper. Mixins: _WikiMixin, _VectorMixin, _MemoryMixin.
WikiStore: hybrid FTS + vector search over wiki_page table.
EmbeddingsService: sentence-transformers, all-MiniLM-L6-v2 default.

## Data flow
memorize() -> WriteGate -> StorageEngine.insert_memory() -> EmbeddingsService.encode_document()
wiki_add() -> WikiStore.add() -> StorageEngine.insert_wiki_page()

## Invariants
I1: request path thin (no ML in handler).
I3: opt-in features short-circuit on disabled.
I25: all knobs registered three-way.
"""

_HOOKS = """# Yadgar Hook System

## Hook types
PreToolUse: fires before every Claude tool call. Captures action_stream.
PostToolUse: fires after tool call completion.
SessionStart: fires on session init. Loads project context.
SessionEnd: fires on shutdown. Captures session summary.

## Hook installation
install_hooks() writes .claude/settings.json hooks block.
Hooks execute as HTTP POSTs to the yadgar daemon.

## Hook data
Each hook receives tool name, input args, output result.
"""

_BENCH = """# Yadgar Benchmark Results v5.26.0

## LongMemEval-s 500 questions
Model: claude-sonnet-4-6
Score: Adopt-1 (headline result)
Methodology: 500 questions from LongMemEval-s benchmark suite.

## Latency metrics
p50 recall: 45ms, p99 recall: 180ms
wiki_add: p50 12ms, p99 45ms
memorize: p50 8ms, p99 30ms
"""

_CONFIG = """# Yadgar Configuration Guide

## Config file location
~/.yadgar/config.yaml - overrides defaults, overridden by env vars.

## Priority order
1. Environment variables (YADGAR_*)
2. ~/.yadgar/config.yaml
3. Built-in defaults in config.py

## Common knobs
YADGAR_EMBEDDING_MODEL: sentence-transformer model name
YADGAR_PORT: HTTP daemon port (default 8765)
YADGAR_DB_PATH: SurrealDB storage directory
"""


def _decode(raw: bytes) -> list[float]:
    n = len(raw) // 4
    return list(struct.unpack(f"{n}f", raw))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _sim(title1, content1, title2, content2):
    emb = server._wiki._embeddings
    e1 = emb.encode_query(f"{title1}\n{content1[:2000]}")
    e2 = emb.encode_query(f"{title2}\n{content2[:2000]}")
    if e1 is None or e2 is None:
        return None
    return _cosine(_decode(e1), _decode(e2))


def test_calibration_print():
    """Compute and print calibration data."""
    print("\n=== NEAR-DUPLICATE PAIRS ===")
    dup1 = _sim(
        "Yadgar Roadmap Future Improvements", _ROADMAP_A, "Yadgar Future Roadmap", _ROADMAP_B
    )
    print(f"Roadmap A vs B: {dup1:.4f}")

    arch2 = _ARCH.replace("Core components", "System components").replace(
        "Data flow", "Processing pipeline"
    )
    dup2 = _sim("Yadgar Architecture", _ARCH, "Yadgar System Architecture", arch2)
    print(f"Arch vs Arch-paraphrase: {dup2:.4f}")

    print("\n=== DISTINCT PAIRS ===")
    dist1 = _sim("Yadgar Architecture", _ARCH, "Yadgar Hook System", _HOOKS)
    print(f"Arch vs Hooks: {dist1:.4f}")
    dist2 = _sim("Yadgar Architecture", _ARCH, "Yadgar Benchmark Results v5.26.0", _BENCH)
    print(f"Arch vs Benchmark: {dist2:.4f}")
    dist3 = _sim("Yadgar Hook System", _HOOKS, "Yadgar Benchmark Results v5.26.0", _BENCH)
    print(f"Hooks vs Benchmark: {dist3:.4f}")
    dist4 = _sim("Yadgar Benchmark Results v5.26.0", _BENCH, "Yadgar Configuration Guide", _CONFIG)
    print(f"Benchmark vs Config: {dist4:.4f}")
    dist5 = _sim("Yadgar Architecture", _ARCH, "Yadgar Configuration Guide", _CONFIG)
    print(f"Arch vs Config: {dist5:.4f}")

    dups = [dup1, dup2]
    dists = [dist1, dist2, dist3, dist4, dist5]
    min_dup = min(d for d in dups if d is not None)
    max_dist = max(d for d in dists if d is not None)
    print(f"\nMin near-dup similarity:  {min_dup:.4f}")
    print(f"Max distinct similarity:  {max_dist:.4f}")
    print(f"Separation margin: {min_dup - max_dist:.4f}")
    print(f"Threshold 0.80 ok? {min_dup > 0.80 and max_dist < 0.80}")

    # Assert clean separation at 0.80
    assert min_dup > 0.80, f"Near-duplicate pair too low ({min_dup:.4f}) — threshold too high"
    assert max_dist < 0.80, f"Distinct pair too high ({max_dist:.4f}) — threshold too low"
