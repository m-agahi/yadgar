"""Build a bootstrap golden evaluation set for the yadgar native eval harness.

# BOOTSTRAP — auto-drafted, REQUIRES HUMAN CURATION

Generates ~50 (query, relevant_memory_ids[]) pairs by sampling stored memories
and deriving paraphrased queries from their content and tags. The output file
is intentionally marked as a bootstrap: it is machine-generated and must be
reviewed and curated by a human before being treated as a trusted benchmark.

Usage:
  # Against the running yadgar daemon (server mode):
  YADGAR_DB_URL=http://127.0.0.1:8000 python benchmarks/build_golden_bootstrap.py

  # Against a local SurrealKV file:
  python benchmarks/build_golden_bootstrap.py --db-path ~/.yadgar/surreal_db

  # Limit sample count:
  python benchmarks/build_golden_bootstrap.py --samples 30

  # Preview without writing:
  python benchmarks/build_golden_bootstrap.py --dry-run

Output:
  benchmarks/golden/golden_set.jsonl

Each JSONL line is a JSON object:
  {
    "query_id": "bs-0001",
    "query": "<paraphrased question derived from memory content>",
    "relevant_memory_ids": [42],
    "memory_content_preview": "<first 120 chars of source memory>",
    "derivation": "paraphrase | tag_lookup | recency_anchor",
    "bootstrap": true,
    "needs_curation": true
  }

bootstrap=true + needs_curation=true are machine sentinels;
human curators should set needs_curation=false after reviewing each pair.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_GOLDEN_PATH = Path(__file__).parent / "golden" / "golden_set.jsonl"

# ── Paraphrase templates ──────────────────────────────────────────────────────
# Applied to memory content to derive a query. These are intentionally diverse
# to cover multiple retrieval scenarios.

_PARAPHRASE_TEMPLATES = [
    "What do I know about {topic}?",
    "Remind me what was decided about {topic}.",
    "What did I note regarding {topic}?",
    "Recall anything related to {topic}.",
    "What have I learned about {topic}?",
    "What was the outcome or conclusion about {topic}?",
    "Find my notes on {topic}.",
    "What do I remember about {topic}?",
    "Tell me what was recorded about {topic}.",
    "What context exists for {topic}?",
]

_TAG_TEMPLATES = [
    "What memories are tagged {tag}?",
    "What do I have stored under the tag {tag}?",
    "Find everything tagged {tag}.",
    "Recall my {tag} notes.",
    "What's in my {tag} category?",
]

_RECENCY_TEMPLATES = [
    "What was the most recent thing I noted about {topic}?",
    "What's the latest I recorded on {topic}?",
    "What did I last note about {topic}?",
]


def _extract_topic(content: str) -> str:
    """Extract a short topic string from memory content.

    Heuristic: take first sentence or first 60 chars, strip noise.
    """
    # Strip leading noise patterns (e.g. "Decision:", "Note:", timestamps)
    content = re.sub(
        r"^(Decision|Note|TODO|FYI|Summary|Update|Action|Result):\s*",
        "",
        content,
        flags=re.IGNORECASE,
    )
    # Take first sentence
    first = re.split(r"[.!?\n]", content.strip())[0].strip()
    # Truncate and clean
    if len(first) > 80:
        first = first[:77] + "..."
    return first or content[:60]


def _paraphrase(content: str, style: str = "paraphrase") -> str:
    """Generate a paraphrased query from memory content."""
    topic = _extract_topic(content)
    if style == "paraphrase":
        tpl = random.choice(_PARAPHRASE_TEMPLATES)
        return tpl.format(topic=topic)
    elif style == "recency":
        tpl = random.choice(_RECENCY_TEMPLATES)
        return tpl.format(topic=topic)
    return f"What do I know about {topic}?"


def _tag_query(tag: str) -> str:
    tpl = random.choice(_TAG_TEMPLATES)
    return tpl.format(tag=tag)


def build_bootstrap(
    db_path: str | None = None,
    samples: int = 50,
    seed: int = 42,
    dry_run: bool = False,
) -> list[dict]:
    """Sample memories and derive golden pairs.

    Returns list of golden pair dicts (also written to golden_set.jsonl unless dry_run).
    """
    random.seed(seed)

    # ── Load memories from storage ──────────────────────────────────────────
    from yadgar._shared.config import Settings
    from yadgar._shared.storage.memory import MemoryStorage

    settings = Settings()
    actual_db_path = db_path or settings.DB_PATH
    storage = MemoryStorage(actual_db_path)

    print(f"Connecting to DB: {actual_db_path}")

    try:
        # Fetch a pool of active, non-stale memories with embeddings
        # (larger pool to sample from)
        pool_sql = (
            "SELECT id, content, tags, created_at, directory_context "
            "FROM memory "
            "WHERE is_stale = false "
            "AND embedding IS NOT NONE "
            "AND string::length(content) > 50 "
            "ORDER BY heat DESC "
            "LIMIT 500"
        )
        rows = storage._q(pool_sql)
        memories = storage._rows_to_dicts(rows) if rows else []
    # `_q` has two backends with disjoint error surfaces (httpx raises
    # HTTPError/ValueError/RuntimeError; the embedded path raises surrealdb-SDK
    # classes not importable here). Printed with the cause, then sys.exit(1).
    except Exception as exc:
        print(f"ERROR: could not query memories: {exc}", file=sys.stderr)
        print("Is the DB running? Use YADGAR_DB_URL or --db-path.", file=sys.stderr)
        sys.exit(1)

    if not memories:
        print("WARNING: no eligible memories found in DB. Cannot build bootstrap.")
        print("Tip: run `yadgar remember` to store some memories first.")
        return []

    print(f"Memory pool: {len(memories)} eligible records")

    # Sample memories (up to `samples`)
    selected = random.sample(memories, min(samples, len(memories)))

    pairs: list[dict] = []
    used_ids: set[int] = set()

    # ── Derivation 1: paraphrase (majority) ─────────────────────────────────
    n_paraphrase = int(len(selected) * 0.60)
    for mem in selected[:n_paraphrase]:
        mid = int(mem["id"])
        if mid in used_ids:
            continue
        used_ids.add(mid)
        pairs.append(
            {
                "query_id": f"bs-{len(pairs) + 1:04d}",
                "query": _paraphrase(mem["content"], style="paraphrase"),
                "relevant_memory_ids": [mid],
                "memory_content_preview": mem["content"][:120],
                "derivation": "paraphrase",
                "bootstrap": True,
                "needs_curation": True,
            }
        )

    # ── Derivation 2: tag-lookup ─────────────────────────────────────────────
    n_tag = int(len(selected) * 0.25)
    for mem in selected[n_paraphrase : n_paraphrase + n_tag]:
        mid = int(mem["id"])
        tags = mem.get("tags") or []
        if not tags or mid in used_ids:
            continue
        used_ids.add(mid)
        tag = random.choice(tags)
        pairs.append(
            {
                "query_id": f"bs-{len(pairs) + 1:04d}",
                "query": _tag_query(tag),
                "relevant_memory_ids": [mid],
                "memory_content_preview": mem["content"][:120],
                "derivation": "tag_lookup",
                "bootstrap": True,
                "needs_curation": True,
            }
        )

    # ── Derivation 3: recency anchor ─────────────────────────────────────────
    for mem in selected[n_paraphrase + n_tag :]:
        mid = int(mem["id"])
        if mid in used_ids:
            continue
        used_ids.add(mid)
        pairs.append(
            {
                "query_id": f"bs-{len(pairs) + 1:04d}",
                "query": _paraphrase(mem["content"], style="recency"),
                "relevant_memory_ids": [mid],
                "memory_content_preview": mem["content"][:120],
                "derivation": "recency_anchor",
                "bootstrap": True,
                "needs_curation": True,
            }
        )

    print(
        f"Generated {len(pairs)} bootstrap pairs ({n_paraphrase} paraphrase, {n_tag} tag_lookup, {len(pairs) - n_paraphrase - n_tag} recency_anchor)"
    )

    if dry_run:
        print("Dry-run: not writing to disk.")
        for p in pairs[:3]:
            print(json.dumps(p, indent=2))
        return pairs

    # ── Write golden_set.jsonl ───────────────────────────────────────────────
    _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_GOLDEN_PATH, "w", encoding="utf-8") as f:
        # Header comment embedded as a sentinel JSON line (query_id=HEADER)
        header = {
            "query_id": "HEADER",
            "note": "BOOTSTRAP — auto-drafted, REQUIRES HUMAN CURATION",
            "generator": "benchmarks/build_golden_bootstrap.py",
            "seed": seed,
            "bootstrap": True,
            "needs_curation": True,
        }
        f.write(json.dumps(header) + "\n")
        for pair in pairs:
            f.write(json.dumps(pair) + "\n")

    print(f"Written {len(pairs)} pairs to {_GOLDEN_PATH}")
    print()
    print("IMPORTANT: this file is MACHINE-GENERATED and REQUIRES HUMAN CURATION.")
    print("Review each pair, verify the query actually retrieves the listed memory,")
    print("set needs_curation=false after validation, and add cross-memory pairs.")

    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build bootstrap golden evaluation set from stored memories."
    )
    parser.add_argument(
        "--db-path", type=str, default=None, help="SurrealKV DB path (embedded mode)"
    )
    parser.add_argument(
        "--samples", type=int, default=50, help="Number of memory pairs to generate (default: 50)"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed for reproducibility (default: 42)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview first 3 pairs without writing"
    )
    args = parser.parse_args()

    build_bootstrap(
        db_path=args.db_path,
        samples=args.samples,
        seed=args.seed,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
