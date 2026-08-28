"""LongMemEval benchmark for Yadgar.

Evaluates long-term interactive memory across 500 questions covering:
  - Information Extraction (single-session user/assistant/preference)
  - Multi-Session Reasoning
  - Temporal Reasoning
  - Knowledge Updates
  - Abstention (false-premise detection)

Dataset: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
Paper: Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term
       Interactive Memory" (ICLR 2025, arXiv:2410.10813)

Usage:
  # Phase 1: Retrieval-only (no LLM, fast, free)
  python benchmarks/run_longmemeval.py --retrieval-only

  # Phase 2: Full QA with Claude as reader + judge
  python benchmarks/run_longmemeval.py

  # Subset for quick testing
  python benchmarks/run_longmemeval.py --max-questions 20 --retrieval-only

  # Specific question types only
  python benchmarks/run_longmemeval.py --types temporal-reasoning,knowledge-update

License: LongMemEval dataset is MIT licensed.
  Citation: Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term
  Interactive Memory", ICLR 2025. arXiv:2410.10813.
  Dataset: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
"""

import argparse
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.retrieval import Retriever
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar.backend.curation import MemoryCurator
from yadgar.core._surreal_runner import (
    allocate_port_with_retry,
    spawn_surreal,
    teardown_surreal_proc,
)

# Directory the haystack is ingested under; recall must scope to the same value
# when routed through the unified MCP path (directory-scoped fan-out).
BENCHMARK_DIRECTORY = "/benchmark/longmemeval"

# Car 8 task 293: identity-train regression. C5 (ADR-0227) made project_id a
# required field on insert_memory; the directory-only fall path was deleted. The
# benchmark corpus is itself benchmark-shaped, NOT project-scoped (no project
# owns the haystack — every project compares against the same fixed dataset).
# A stable, known identity string is the right answer: cross-run comparability
# depends on it being identical every invocation; a fresh identity per run
# would scatter the corpus and break per-question memory-id continuity. The
# ``benchmark/`` prefix keeps it out of the project namespace (mirrors the
# ``global`` tag pattern task 50 owns).
BENCHMARK_PROJECT_ID = "benchmark/longmemeval"

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Surreal-server lifecycle ───────────────────────────────────────────

# All data tables defined in _init_schema that must be wiped between questions.
# Excludes: schema_version (migration state), wiki_*, checkpoint (not used in benchmark).
_BENCHMARK_WIPE_TABLES = [
    "memory",
    "episode",
    "entity",
    "relationship",
    "consolidation_log",
    "file_hash",
    "memory_cluster",
    "prospective_memory",
    "narrative_entry",
    "astrocyte_process",
    "memory_rule",
    "memory_archive",
    "memory_transition",
    "causal_dag_edge",
    "engram_slot",
    "action_log",
    "user_profile",
    "derived_belief",
    "counter",
    "memory_similarity_link",
]


def wipe_benchmark_tables(storage: StorageEngine) -> None:
    """DELETE all rows from benchmark data tables to isolate per-question state.

    In server mode, StorageEngine ignores db_path and shares the yadgar/main
    namespace across all calls.  Between questions the benchmark creates a new
    StorageEngine — but in server mode that still points at the same DB.  This
    function wipes all data tables so each question starts with a clean slate.

    Indexes and schema (DEFINE TABLE / DEFINE INDEX / DEFINE ANALYZER) are not
    dropped — they are NOT recreated per question, so wiping data alone is
    sufficient and much faster than REMOVE TABLE + _init_schema().

    Args:
        storage: An open StorageEngine instance in server mode.
    """
    for table in _BENCHMARK_WIPE_TABLES:
        try:
            storage._q(f"DELETE {table};")
        # `_q` has two backends with disjoint error surfaces (httpx raises
        # HTTPError/ValueError/RuntimeError; the embedded path raises
        # surrealdb-SDK classes not importable here). Reported, not swallowed.
        except Exception as exc:  # noqa: BLE001 — `_q` error surface is backend-dependent
            logger.warning("wipe_benchmark_tables: DELETE %s failed: %s", table, exc)


def _wait_for_health(port: int, timeout: float = 30.0) -> None:
    """Poll SurrealDB /health until it responds or timeout expires."""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except OSError:
            # URLError / HTTPError / socket.timeout while the server boots.
            time.sleep(0.1)
    raise RuntimeError(f"SurrealDB did not start on port {port} within {timeout}s")


def spawn_surreal_for_benchmark(data_dir: str) -> tuple[subprocess.Popen, int]:
    """Spawn a SurrealDB server process for the benchmark run.

    Allocates a free port, starts surreal, waits for health-check.
    Caller must call teardown_surreal_proc(proc) on exit.

    Args:
        data_dir: Path to a writable directory for SurrealKV storage.

    Returns:
        (proc, port) — the Popen instance and the bound port.

    Raises:
        FileNotFoundError: If the `surreal` binary is not on PATH.
        RuntimeError: If the server does not start within 30s.
    """
    port = allocate_port_with_retry(n=99)  # n=99 → outside xdist range (gw0–gw3 use 0–3)
    proc = spawn_surreal(port=port, data_dir=data_dir)
    _wait_for_health(port)
    return proc, port


# ── Constants ─────────────────────────────────────────────────────────

DATASET_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"
DATASET_DIR = Path(__file__).parent / "data" / "longmemeval"

QUESTION_TYPES = [
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
]

# Map question types to the 5 core abilities for reporting
ABILITY_MAP = {
    "single-session-user": "Information Extraction",
    "single-session-assistant": "Information Extraction",
    "single-session-preference": "Information Extraction",
    "multi-session": "Multi-Session Reasoning",
    "temporal-reasoning": "Temporal Reasoning",
    "knowledge-update": "Knowledge Updates",
}

# SHA-256 pin for longmemeval_s_cleaned.json (xiaowu0162/longmemeval-cleaned, 2026-05-31).
# If the dataset is updated upstream and the hash changes, a warning is printed (not a hard abort).
# Update this pin after verifying the upstream change is intentional.
LONGMEMEVAL_S_SHA256: str = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"


# ── Reproducibility helpers ───────────────────────────────────────────


def compute_dataset_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file (streaming, large-file safe)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_yadgar_commit() -> str | None:
    """Return current git HEAD SHA (40 hex chars), or None on failure."""
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(Path(__file__).parent.parent),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    # Binary absent -> FileNotFoundError (OSError); non-zero exit ->
    # CalledProcessError; non-UTF-8 output -> UnicodeDecodeError.
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):  # fmt: skip
        return None


def get_claude_version() -> str | None:
    """Return `claude --version` output string, or None if binary absent."""
    try:
        return (
            subprocess.check_output(
                ["claude", "--version"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    # Binary absent -> FileNotFoundError (OSError); non-zero exit ->
    # CalledProcessError; non-UTF-8 output -> UnicodeDecodeError.
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):  # fmt: skip
        return None


def get_surreal_version() -> str | None:
    """Return `surreal version` output string, or None if binary absent."""
    try:
        return (
            subprocess.check_output(
                ["surreal", "version"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    # Binary absent -> FileNotFoundError (OSError); non-zero exit ->
    # CalledProcessError; non-UTF-8 output -> UnicodeDecodeError.
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):  # fmt: skip
        return None


def build_reproducibility_dict(dataset_path: Path, settings) -> dict:
    """Build the reproducibility metadata dict for a benchmark run.

    Fields populated at Phase 1 (retrieval-only):
      yadgar_commit, dataset_sha256, embedding_model, surreal_version,
      python_version, run_date_utc.
    Phase 2 fills in reader_llm + judge_llm from ANTHROPIC_MODEL env var;
    both use the same model (reader + judge are the same Claude invocation pattern).
    """
    llm_model = os.environ.get("ANTHROPIC_MODEL") or None  # None if not set
    return {
        "yadgar_commit": get_yadgar_commit(),
        "dataset_sha256": compute_dataset_sha256(dataset_path),
        "embedding_model": settings.EMBEDDING_MODEL,
        "surreal_version": get_surreal_version(),
        "reader_llm": llm_model,
        "judge_llm": llm_model,
        "python_version": sys.version,
        "run_date_utc": datetime.now(UTC).isoformat(),
    }


# ── Dataset Download ──────────────────────────────────────────────────


def download_dataset(variant: str = "s") -> Path:
    """Download LongMemEval dataset from HuggingFace if not cached."""
    filename_map = {
        "oracle": "longmemeval_oracle.json",
        "s": "longmemeval_s_cleaned.json",
        "m": "longmemeval_m_cleaned.json",
    }
    filename = filename_map[variant]
    local_path = DATASET_DIR / filename

    if local_path.exists():
        print(f"Dataset cached: {local_path}")
        return local_path

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{DATASET_URL}/{filename}"
    print(f"Downloading {url} ...")

    import urllib.request

    urllib.request.urlretrieve(url, local_path)
    print(f"Saved to {local_path} ({local_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # Verify sha256 pin if set (variant s only; m/oracle pins TBD)
    if variant == "s" and LONGMEMEVAL_S_SHA256:
        actual = compute_dataset_sha256(local_path)
        if actual != LONGMEMEVAL_S_SHA256:
            print(
                f"WARNING: dataset sha256 mismatch!\n"
                f"  expected: {LONGMEMEVAL_S_SHA256}\n"
                f"  actual:   {actual}\n"
                "  Dataset may have been updated upstream. "
                "Update LONGMEMEVAL_S_SHA256 after verifying the change is intentional."
            )
        else:
            print(f"Dataset sha256 verified: {actual[:16]}...")

    return local_path


def load_dataset(path: Path) -> list[dict]:
    """Load and validate LongMemEval JSON."""
    with open(path) as f:
        data = json.load(f)

    # Normalize answer field to string (32 questions have int answers)
    for q in data:
        q["answer"] = str(q["answer"])

    print(f"Loaded {len(data)} questions")

    # Print distribution
    type_counts = {}
    abs_count = 0
    for q in data:
        qtype = q["question_type"]
        type_counts[qtype] = type_counts.get(qtype, 0) + 1
        if q["question_id"].endswith("_abs"):
            abs_count += 1

    for qtype, count in sorted(type_counts.items()):
        print(f"  {qtype}: {count}")
    print(f"  abstention: {abs_count}")

    return data


# ── Yadgar Engine Factory ──────────────────────────────────────────


def _parse_settings_overrides(pairs: list[str]) -> dict:
    """Parse repeated KEY=VALUE CLI pairs into a settings-override dict.

    Values are coerced to bool/int/float when they parse cleanly, else kept as
    str; pydantic performs the final field-typed coercion. Used by the v5.98
    rerank-lever quality gate to A/B CROSS_ENCODER_TOP_K / CROSS_ENCODER_BACKEND
    without editing the hardcoded benchmark defaults.
    """
    out: dict = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise ValueError(f"--settings-override expects KEY=VALUE, got: {raw!r}")
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip()
        low = val.lower()
        if low in ("true", "false"):
            out[key] = low == "true"
        else:
            try:
                out[key] = int(val)
            except ValueError:
                try:
                    out[key] = float(val)
                except ValueError:
                    out[key] = val
    return out


def make_benchmark_settings(**overrides) -> Settings:
    """Create Yadgar settings optimized for LongMemEval retrieval."""
    defaults = {
        # Write gate: disable for benchmark (we want all memories stored)
        "WRITE_GATE_THRESHOLD": 0.0,
        # Retrieval signals
        "CROSS_ENCODER_ENABLED": True,
        "CROSS_ENCODER_TOP_K": 75,
        "CROSS_ENCODER_WEIGHT": 1.0,
        "WRRF_VECTOR_WEIGHT": 1.0,
        "WRRF_FTS_WEIGHT": 0.5,
        # Graph signals: disabled (no real graph in benchmark data)
        "WRRF_PPR_WEIGHT": 0.0,
        "WRRF_SPREADING_WEIGHT": 0.0,
        "WRRF_SR_WEIGHT": 0.0,
        "WRRF_HOPFIELD_WEIGHT": 0.2,
        "WRRF_HDC_WEIGHT": 0.0,
        "WRRF_FRACTAL_WEIGHT": 0.0,
        # Reranking
        "GTE_RERANKER_ENABLED": True,
        "NLI_RERANKING_ENABLED": True,
        "NLI_ONLY_FOR_OPEN_DOMAIN": False,
        "MULTI_PASSAGE_RERANKING_ENABLED": True,
        # Index enrichment
        "INDEX_ENRICHMENT_ENABLED": True,
        "CONCEPTNET_ENRICHMENT_ENABLED": True,
        "LOGIC_ENRICHMENT_ENABLED": True,
        "COMET_ENRICHMENT_ENABLED": False,
        "DOC2QUERY_ENRICHMENT_ENABLED": False,
        # Profiles & beliefs
        "PROFILE_EXTRACTION_ENABLED": True,
        "DERIVED_BELIEFS_ENABLED": True,
        # Comparison routing
        "COMPARISON_DUAL_SEARCH_ENABLED": True,
        # Query expansion
        "QUERY_EXPANSION_ENABLED": True,
        # Temporal retrieval
        "TEMPORAL_RETRIEVAL_ENABLED": True,
        # Disable zero-gap features for benchmark (they're for live use)
        "REINJECTION_ENABLED": False,
        "MICRO_CHECKPOINT_ENABLED": False,
        "ACTION_STREAM_ENABLED": False,
        "DECISION_AUTO_PROTECT": False,
    }
    defaults.update(overrides)

    # Build Settings with env prefix disabled
    os.environ.update({f"YADGAR_{k}": str(v) for k, v in defaults.items()})
    return Settings()


def create_engines(db_path: str, settings: Settings):
    """Create a minimal Yadgar engine set for benchmarking."""
    storage = StorageEngine(db_path)
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
    kg = KnowledgeGraph(storage, settings)
    thermo = MemoryThermodynamics(storage, embeddings, settings)
    retriever = Retriever(storage, embeddings, kg, settings)
    curator = MemoryCurator(storage, embeddings, thermo, settings)

    return storage, embeddings, retriever, curator, thermo


# ── Ingestion ─────────────────────────────────────────────────────────


def ingest_question_haystack(
    question: dict,
    storage: StorageEngine,
    embeddings: EmbeddingEngine,
    curator: MemoryCurator,
    thermo: MemoryThermodynamics,
    settings: Settings,
    enrich: bool = True,
) -> dict[str, list[int]]:
    """Ingest a question's haystack sessions into Yadgar.

    Uses round-level decomposition: each user-assistant turn pair becomes
    a separate memory. This is the optimal granularity per the paper
    (+11.3% recall vs session-level).

    Embeds temporal metadata in content for temporal reasoning queries.

    Returns: mapping of session_id -> list of memory_ids (for retrieval eval)
    """
    session_map: dict[str, list[int]] = {}
    sessions = question["haystack_sessions"]
    session_ids = question["haystack_session_ids"]
    session_dates = question["haystack_dates"]

    for _idx, (session, session_id, session_date) in enumerate(
        zip(sessions, session_ids, session_dates, strict=False)
    ):
        memory_ids = []

        # Decompose session into rounds (user-assistant turn pairs)
        rounds = []
        i = 0
        while i < len(session):
            user_msg = None
            asst_msg = None

            if session[i]["role"] == "user":
                user_msg = session[i]["content"]
                if i + 1 < len(session) and session[i + 1]["role"] == "assistant":
                    asst_msg = session[i + 1]["content"]
                    i += 2
                else:
                    i += 1
            elif session[i]["role"] == "assistant":
                asst_msg = session[i]["content"]
                i += 1
            else:
                i += 1
                continue

            rounds.append((user_msg, asst_msg))

        # Store each round as a memory with temporal metadata
        for round_idx, (user_text, asst_text) in enumerate(rounds):
            # Build content with temporal context embedded
            parts = [f"[Date: {session_date}]"]
            if user_text:
                parts.append(f"User: {user_text}")
            if asst_text:
                parts.append(f"Assistant: {asst_text}")
            content = "\n".join(parts)

            # Tags for retrieval evaluation mapping
            tags = [
                f"session:{session_id}",
                f"date:{session_date}",
                f"round:{round_idx}",
            ]

            # Embed and store
            embedding = embeddings.encode(content)
            if embedding is None:
                continue

            memory_id = storage.insert_memory(
                {
                    "content": content,
                    "embedding": embedding,
                    "tags": tags,
                    "directory_context": BENCHMARK_DIRECTORY,
                    # Car 8 task 293: project_id is REQUIRED since C5/ADR-0227
                    # deleted the directory-context fallback. Without this,
                    # _resolve_project_id_for_write raises UnresolvedProjectError
                    # and every haystack ingest fails — ``make longmemeval``
                    # then prints 0.000 on every metric, and absence of data is
                    # read as a real score. The benchmark corpus is global
                    # (not project-scoped), so a fixed identity string is the
                    # correct shape (mirrors the ``global`` tag pattern).
                    "project_id": BENCHMARK_PROJECT_ID,
                    "heat": 1.0,
                    "is_stale": False,
                    "file_hash": None,
                    "embedding_model": embeddings.get_model_name(),
                },
                # Fidelity (v5.82): when enrich=True, forward settings+embeddings so
                # the index-time enrichment pipeline (COMET / ConceptNet / Logic /
                # Doc2Query / FPA) runs during eval ingest — matching production
                # memorize(). Omitting them makes the enrichment guard in
                # storage/memory.py silently no-op, so the eval would measure
                # retrieval over a RAW (un-enriched) corpus, unfaithful to prod.
                embeddings_engine=embeddings if enrich else None,
                settings=settings if enrich else None,
            )

            # Set importance and surprise scores
            importance = thermo.compute_importance(content, tags)
            storage.update_memory_scores(
                memory_id,
                surprise_score=0.5,
                importance=importance,
                emotional_valence=0.0,
            )

            memory_ids.append(memory_id)

        session_map[session_id] = memory_ids

    return session_map


# ── Retrieval Evaluation ──────────────────────────────────────────────


def compute_ndcg(retrieved_session_ids: list[str], gold_session_ids: set[str], k: int) -> float:
    """Compute NDCG@k with binary relevance."""
    dcg = 0.0
    for i, sid in enumerate(retrieved_session_ids[:k]):
        if sid in gold_session_ids:
            dcg += 1.0 / math.log2(i + 2)  # i+2 because log2(1) = 0

    # Ideal DCG: all relevant items at top
    ideal_count = min(len(gold_session_ids), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_count))

    if idcg == 0:
        return 0.0
    return dcg / idcg


def compute_recall(retrieved_session_ids: list[str], gold_session_ids: set[str], k: int) -> float:
    """Compute Recall@k: fraction of gold sessions found in top-k."""
    if not gold_session_ids:
        return 0.0
    found = sum(1 for sid in retrieved_session_ids[:k] if sid in gold_session_ids)
    return found / len(gold_session_ids)


def _unified_recall(
    query: str, max_results: int, directory: str, mode: str | None = None
) -> list[dict]:
    """Retrieve via the MCP recall tool (the unified fan-out path).

    Mirrors run_eval.py's Step-0 fix: route through the SAME entry point MCP
    callers use (yadgar.server.tools.recall.recall) instead of Retriever.recall(),
    so v5.80's fan-out + directory-scoping + single-provider bypass are actually
    measured. type="memory" because LongMemEval haystacks are memory-only (no
    wiki); the flag is forced ON so the measurement is unambiguous regardless of
    the isolated DB's config defaults. Returns the same list[dict] (each carrying
    'id') as Retriever.recall, so the caller's id→session mapping is unchanged.

    mode: optional recall mode (e.g. "landscape" → consensus_retrieve, #67). When
    set, it is passed through to the recall tool; mode dispatch takes precedence
    over type. Lets the benchmark measure an alternate retrieval mode (landscape)
    vs the default arm against the same frozen corpus.
    """
    import sys as _sys

    rm = _sys.modules.get("yadgar.core.server.tools.recall")
    if rm is None:
        import yadgar.core.server.tools.recall as rm  # type: ignore[no-redef]

    # UNIFIED_RECALL_ENABLED was removed from Settings when unified recall became
    # always-on; assigning a non-existent field raises on pydantic models and killed
    # every retrieval ("Settings" object has no field ...). Guard for both eras.
    if hasattr(rm.settings, "UNIFIED_RECALL_ENABLED"):
        rm.settings.UNIFIED_RECALL_ENABLED = True
    recall_fn = rm.recall
    kwargs: dict = {
        "max_results": max_results,
        "min_heat": 0.0,
        "directory": directory,
        "type": "memory",
    }
    if mode:
        kwargs["mode"] = mode
    try:
        return recall_fn(query, **kwargs)
    except TypeError:
        # Older signature without type=/mode= — fall back to the minimal call.
        return recall_fn(query, max_results=max_results, min_heat=0.0, directory=directory)


def evaluate_retrieval(
    question: dict,
    retriever: Retriever,
    session_map: dict[str, list[int]],
    max_results: int = 50,
    unified: bool = False,
    directory: str = BENCHMARK_DIRECTORY,
    mode: str | None = None,
) -> dict:
    """Run retrieval and compute session-level metrics.

    Returns dict with recall@k and ndcg@k for k in {5, 10, 50}.

    unified: when True, route through the MCP recall tool (fan-out / unified
    path, v5.80) instead of Retriever.recall() (legacy memory-only path).
    mode: optional recall mode (e.g. "landscape", #67) forwarded to the unified
    recall tool. Only effective with unified=True. Lets the benchmark measure an
    alternate mode against the same frozen corpus.
    """
    query = question["question"]
    gold_session_ids = set(question["answer_session_ids"])
    is_abstention = question["question_id"].endswith("_abs")

    # Skip retrieval metrics for abstention questions (no ground truth location)
    if is_abstention:
        return {"skipped": True, "reason": "abstention"}

    # Run retrieval (catch FTS5 syntax errors from apostrophes etc.)
    try:
        if unified:
            results = _unified_recall(query, max_results, directory, mode=mode)
        else:
            results = retriever.recall(query, max_results=max_results, min_heat=0.0)
    # `recall` fans out over embeddings, storage and the HTTP backend; the
    # comment above records FTS5 syntax errors as one observed cause, but the
    # set is open. Logged with the question id, not swallowed.
    except Exception as e:  # noqa: BLE001 — recall spans the whole retrieval stack
        logger.warning("Retrieval failed for question %s: %s", question["question_id"], e)
        results = []

    # Build reverse map: memory_id -> session_id
    mid_to_session: dict[int, str] = {}
    for session_id, memory_ids in session_map.items():
        for mid in memory_ids:
            mid_to_session[mid] = session_id

    # Map retrieved memories to session IDs (deduplicated, preserving order)
    retrieved_sessions = []
    seen = set()
    for mem in results:
        sid = mid_to_session.get(mem["id"])
        if sid and sid not in seen:
            retrieved_sessions.append(sid)
            seen.add(sid)

    # Compute metrics at multiple k values
    metrics = {}
    for k in [5, 10, 50]:
        metrics[f"recall@{k}"] = compute_recall(retrieved_sessions, gold_session_ids, k)
        metrics[f"ndcg@{k}"] = compute_ndcg(retrieved_sessions, gold_session_ids, k)

    # MRR: reciprocal rank of first relevant session
    mrr = 0.0
    for i, sid in enumerate(retrieved_sessions):
        if sid in gold_session_ids:
            mrr = 1.0 / (i + 1)
            break
    metrics["mrr"] = mrr

    # Hit rank
    hit_rank = None
    for i, sid in enumerate(retrieved_sessions):
        if sid in gold_session_ids:
            hit_rank = i + 1
            break
    metrics["hit_rank"] = hit_rank

    metrics["retrieved_sessions"] = len(retrieved_sessions)
    metrics["gold_sessions"] = len(gold_session_ids)

    return metrics


# ── Answer Generation (claude -p) ────────────────────────────────────

READER_SYSTEM_PROMPT = """You are answering questions about a user's conversation history.
You will be given relevant excerpts from past conversations and a question.
Answer the question based ONLY on the provided context.
If the context does not contain enough information to answer, say "I don't have enough information to answer this question."
Be concise and specific. Give the most direct answer possible."""

READER_PROMPT_TEMPLATE = """## Relevant conversation history:
{context}

## Question (asked on {question_date}):
{question}

## Answer:"""

JUDGE_SYSTEM_PROMPT = """You are evaluating whether a system's answer to a question is correct.
You will be given the question, the gold (correct) answer, and the system's answer.
Determine if the system's answer is correct.

Rules:
- The system answer does NOT need to match the gold answer word-for-word
- It IS correct if it conveys the same core information
- For temporal questions, accept minor date format differences
- For knowledge-update questions, the answer must reflect the LATEST known information
- For preference questions, the answer must capture the user's preference accurately
- If the question is unanswerable and the system correctly identifies this, mark as correct

Respond with ONLY a JSON object: {"correct": true} or {"correct": false}"""

JUDGE_PROMPT_TEMPLATE = """Question: {question}
Gold answer: {gold_answer}
System answer: {hypothesis}"""


def call_claude_pipe(prompt: str, system_prompt: str = "", timeout: int = 120) -> str:
    """Call Claude via `claude -p` pipe mode.

    Model selection: honours ANTHROPIC_MODEL env var (explicit --model flag).
    Falls back to claude CLI default (Sonnet) if env var not set.

    Returns the generated text. Falls back to empty string on error.
    """
    cmd = ["claude", "-p", "--output-format", "json"]
    model = os.environ.get("ANTHROPIC_MODEL")
    if model:
        cmd.extend(["--model", model])
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("claude -p failed: %s", result.stderr[:200])
            return ""

        response = json.loads(result.stdout)
        return response.get("result", "")
    except subprocess.TimeoutExpired:
        logger.warning("claude -p timed out after %ds", timeout)
        return ""
    # `Exception` used to be in this tuple alongside JSONDecodeError, which it
    # subsumes — the pair read as a narrow catch and was a blind one.
    # subprocess.run: OSError (claude not on PATH); json.loads: ValueError
    # (JSONDecodeError is a subclass); .get on a non-dict: AttributeError.
    # TimeoutExpired is handled above.
    except (OSError, ValueError, AttributeError) as e:  # fmt: skip
        logger.warning("claude -p error: %s", e)
        return ""


def generate_answer(
    question: dict,
    retrieved_memories: list[dict],
    top_k_context: int = 10,
) -> str:
    """Generate an answer using Claude as the reader LLM."""
    # Format retrieved context
    context_parts = []
    for i, mem in enumerate(retrieved_memories[:top_k_context]):
        content = mem.get("content", "")
        context_parts.append(f"[{i + 1}] {content}")
    context = "\n\n".join(context_parts)

    prompt = READER_PROMPT_TEMPLATE.format(
        context=context,
        question=question["question"],
        question_date=question["question_date"],
    )

    return call_claude_pipe(prompt, READER_SYSTEM_PROMPT)


def judge_answer(question: dict, hypothesis: str) -> dict:
    """Judge whether the generated answer is correct using Claude."""
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=question["question"],
        gold_answer=question["answer"],
        hypothesis=hypothesis,
    )

    response = call_claude_pipe(prompt, JUDGE_SYSTEM_PROMPT)

    # Parse judge response
    try:
        # Try to extract JSON from response
        if "{" in response:
            json_str = response[response.index("{") : response.rindex("}") + 1]
            result = json.loads(json_str)
            return {"correct": bool(result.get("correct", False)), "raw": response}
    except (json.JSONDecodeError, ValueError):  # fmt: skip
        pass

    # Fallback: look for yes/true/correct in response
    lower = response.lower()
    correct = any(w in lower for w in ["correct", '"correct": true', "yes"])
    return {"correct": correct, "raw": response}


# ── Main Benchmark Pipeline ──────────────────────────────────────────


def _load_processed(hyp_path: str) -> tuple[set[str], list[dict]]:
    """Load already-processed question results from incremental JSONL.

    Returns:
        (processed_ids, prior_results) — set of question_ids already done,
        and their full query_result dicts for aggregation continuity.
    """
    processed_ids: set[str] = set()
    prior_results: list[dict] = []
    if not os.path.exists(hyp_path):
        return processed_ids, prior_results
    with open(hyp_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                qid = entry.get("question_id")
                if qid:
                    processed_ids.add(qid)
                    prior_results.append(entry)
            # A truncated last line raises ValueError (JSONDecodeError); a line
            # that parses to a non-dict raises AttributeError on .get.
            except (ValueError, AttributeError):  # fmt: skip
                pass
    return processed_ids, prior_results


def run_benchmark(
    dataset_path: Path,
    retrieval_only: bool = False,
    max_questions: int = 0,
    question_types: list[str] | None = None,
    max_results: int = 50,
    top_k_context: int = 10,
    settings_overrides: dict | None = None,
    output_path: str | None = None,
    stratify_per_type: bool = False,
    resume: bool = False,
    unified: bool = False,
    mode: str | None = None,
    enrich: bool = True,
) -> dict:
    """Run the full LongMemEval benchmark.

    Phase 1: Retrieval evaluation (always runs)
    Phase 2: Answer generation + judging (unless retrieval_only=True)

    resume: if True, load already-processed question_ids from incremental JSONL
    and skip them. Requires --output to be set explicitly so the path is stable
    across invocations.
    """
    data = load_dataset(dataset_path)

    # Filter by question types if specified
    if question_types:
        data = [q for q in data if q["question_type"] in question_types]
        print(f"Filtered to {len(data)} questions of types: {question_types}")

    # Limit for quick testing
    # - If `stratify_per_type=True` and types are specified: take up to
    #   ceil(max_questions / len(types)) per type, interleaved so all types
    #   appear early in the run (matters when the harness kills mid-run).
    # - Otherwise: simple head-slice (file is type-sorted upstream, so this
    #   reads contiguous blocks per type).
    if max_questions > 0:
        if stratify_per_type and question_types:
            from collections import defaultdict

            buckets: dict[str, list[dict]] = defaultdict(list)
            for q in data:
                buckets[q["question_type"]].append(q)
            per_type = max(1, max_questions // len(question_types))
            # Truncate each bucket, then interleave round-robin.
            truncated = {t: buckets[t][:per_type] for t in question_types if t in buckets}
            interleaved: list[dict] = []
            i = 0
            while len(interleaved) < max_questions:
                added_this_round = False
                for t in question_types:
                    if t in truncated and i < len(truncated[t]):
                        interleaved.append(truncated[t][i])
                        added_this_round = True
                        if len(interleaved) >= max_questions:
                            break
                if not added_this_round:
                    break
                i += 1
            data = interleaved
            print(f"Stratified to {len(data)} questions (~{per_type} per type, interleaved)")
        else:
            data = data[:max_questions]
            print(f"Limited to {max_questions} questions")

    # ── Resolve output path early (needed for incremental JSONL) ─────────
    # Derive output_path now so hyp_path is stable across resume invocations.
    # Callers should pass --output explicitly when using --resume.
    if output_path is None:
        variant_name = dataset_path.stem.replace("longmemeval_", "").replace("_cleaned", "")
        mode = "retrieval" if retrieval_only else "full"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(
            Path(__file__).parent / "results" / f"longmemeval_{variant_name}_{mode}_{ts}.json"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    hyp_path = output_path.replace(".json", "_hypotheses.jsonl")

    # ── Resume: load already-processed results ─────────────────────────
    processed_ids: set[str] = set()
    _prior_results: list[dict] = []
    if resume and not retrieval_only:
        processed_ids, _prior_results = _load_processed(hyp_path)
        if processed_ids:
            print(f"Resume: skipping {len(processed_ids)} already-processed questions")

    settings = make_benchmark_settings(**(settings_overrides or {}))

    # Initialize embedding engine once (shared across all questions)
    print("Loading embedding model...")
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)

    # Real ML client, shared across questions (holds loaded CE/GTE models).
    # Since folder-split Car 2 removed Reranker's lazy LocalMLClient fallback,
    # Retriever(...) without ml_client gets a NullMLClient — every CE score
    # returns None (≡ circuit-open) and the whole rerank chain silently no-ops.
    from yadgar.backend.ml_client import LocalMLClient  # noqa: PLC0415

    ml_client = LocalMLClient(settings)

    results = {
        "benchmark": "LongMemEval",
        "variant": dataset_path.stem,
        "timestamp": datetime.now(UTC).isoformat(),
        "total_questions": len(data),
        "retrieval_only": retrieval_only,
        "unified_recall": unified,
        "recall_mode": mode,
        "max_results": max_results,
        "top_k_context": top_k_context,
        "settings_overrides": settings_overrides or {},
        "per_query": list(_prior_results),  # pre-load resumed results for aggregation
        "aggregated": {},
        "reproducibility": build_reproducibility_dict(dataset_path, settings),
    }

    # ── Surreal-server lifecycle ──────────────────────────────────────
    # If YADGAR_DB_URL is already set (user points at an existing server),
    # skip the spawn entirely and use that server as-is.
    # Otherwise spawn a surreal-server subprocess on a free port so that
    # FULLTEXT ANALYZER SQL syntax works (embedded surrealkv doesn't support it).
    _spawned_proc = None
    _surreal_tmpdir = None
    _server_mode = bool(os.environ.get("YADGAR_DB_URL"))

    # Benchmark dataset contains technical content (Vulkan APIs, code snippets,
    # API-key-shaped strings in user questions) that triggers false positives
    # in the storage-level secret gate. The gate is a defence-in-depth check
    # for real user data — for fixed benchmark corpora it produces noise that
    # kills the run partway through. Disable for the duration of benchmark
    # ingestion. Caller env is restored in the `finally` block below.
    _prev_secret_gate = os.environ.get("YADGAR_SECRET_GATE_DISABLED")
    os.environ["YADGAR_SECRET_GATE_DISABLED"] = "1"

    if not _server_mode:
        import shutil

        if not shutil.which("surreal"):
            print(
                "WARNING: `surreal` binary not on PATH. "
                "Falling back to embedded mode — FULLTEXT retrieval will fail. "
                "Install SurrealDB or set YADGAR_DB_URL to point at a running server."
            )
        else:
            _surreal_tmpdir = tempfile.mkdtemp(prefix="yadgar_bench_surreal_")
            print(f"Starting SurrealDB server (data dir: {_surreal_tmpdir}) ...")
            _spawned_proc, _port = spawn_surreal_for_benchmark(_surreal_tmpdir)
            os.environ["YADGAR_DB_URL"] = f"http://127.0.0.1:{_port}"
            os.environ["YADGAR_ALLOW_ROOT"] = "1"
            _server_mode = True
            print(f"SurrealDB ready on port {_port}")

    # Unified mode: populate the server _state so the MCP recall tool can access
    # storage/retriever/wiki (mirrors run_eval.py). init_engines binds to the same
    # DB via YADGAR_DB_URL (server mode). Done once before the loop. Without this,
    # the MCP recall path raises "StorageEngine not initialized".
    if unified:
        from yadgar.core import server as _srv  # noqa: PLC0415

        print("Unified mode: initializing server engines for MCP recall path ...")
        _srv.init_engines(db_path=os.environ.get("YADGAR_DB_PATH", settings.DB_PATH))

    start_time = time.monotonic()

    try:
        for qi, question in enumerate(data):
            qid = question["question_id"]
            qtype = question["question_type"]
            is_abs = qid.endswith("_abs")

            # Skip already-processed questions on resume
            if qid in processed_ids:
                continue

            print(
                f"\r[{qi + 1}/{len(data)}] {qtype}: {question['question'][:60]}...",
                end="",
                flush=True,
            )

            # In server mode: reuse single server, wipe data between questions.
            # In embedded mode (no surreal binary): tmpdir per question as before.
            # NOTE: FULLTEXT retrieval is broken in embedded mode; warn but proceed.
            _q_tmpdir_path: str | None = None
            storage = None
            try:
                if _server_mode:
                    db_path = ""  # ignored in server mode
                    storage = StorageEngine(db_path)
                    # Wipe all data tables so this question starts with a clean slate.
                    # Must happen AFTER StorageEngine.__init__ (calls _init_schema).
                    if qi > 0:
                        wipe_benchmark_tables(storage)
                else:
                    # mkdtemp (not ctx mgr) so the dir outlives StorageEngine init.
                    # Cleaned up explicitly after storage.close() in finally block.
                    _q_tmpdir_path = tempfile.mkdtemp(prefix="yadgar_bench_q_")
                    db_path = os.path.join(_q_tmpdir_path, "bench.db")
                    storage = StorageEngine(db_path)

                kg = KnowledgeGraph(storage, settings)
                thermo = MemoryThermodynamics(storage, embeddings, settings)
                retriever = Retriever(storage, embeddings, kg, settings, ml_client=ml_client)
                curator = MemoryCurator(storage, embeddings, thermo, settings)

                # Phase 1a: Ingest haystack
                t_ingest = time.monotonic()
                session_map = ingest_question_haystack(
                    question, storage, embeddings, curator, thermo, settings,
                    enrich=enrich,
                )
                ingest_time = time.monotonic() - t_ingest

                total_memories = sum(len(mids) for mids in session_map.values())

                # Phase 1b: Retrieval evaluation
                t_retrieve = time.monotonic()
                retrieval_metrics = evaluate_retrieval(
                    question,
                    retriever,
                    session_map,
                    max_results=max_results,
                    unified=unified,
                    mode=mode,
                )
                retrieve_time = time.monotonic() - t_retrieve

                # Phase 2: Answer generation + judging
                hypothesis = ""
                judge_result = {}
                gen_time = 0.0
                judge_time = 0.0

                if not retrieval_only:
                    # Get retrieved memories for answer generation
                    try:
                        retrieved = retriever.recall(
                            question["question"], max_results=max_results, min_heat=0.0
                        )
                    # Same open surface as the retrieval-phase call above.
                    # NOTE: unlike that one this handler is SILENT — a failed
                    # recall here degrades the generated answer with no record.
                    except Exception:  # noqa: BLE001 — recall spans the whole retrieval stack
                        retrieved = []

                    t_gen = time.monotonic()
                    hypothesis = generate_answer(question, retrieved, top_k_context)
                    gen_time = time.monotonic() - t_gen

                    if hypothesis:
                        t_judge = time.monotonic()
                        judge_result = judge_answer(question, hypothesis)
                        judge_time = time.monotonic() - t_judge

                # Record per-query result
                query_result = {
                    "question_id": qid,
                    "question_type": qtype,
                    "is_abstention": is_abs,
                    "question": question["question"],
                    "gold_answer": question["answer"],
                    "sessions_in_haystack": len(question["haystack_session_ids"]),
                    "memories_ingested": total_memories,
                    "ingest_seconds": round(ingest_time, 2),
                    "retrieve_seconds": round(retrieve_time, 2),
                    **{k: v for k, v in retrieval_metrics.items() if k != "skipped"},
                }

                if not retrieval_only:
                    query_result["hypothesis"] = hypothesis
                    query_result["correct"] = judge_result.get("correct", False)
                    query_result["gen_seconds"] = round(gen_time, 2)
                    query_result["judge_seconds"] = round(judge_time, 2)

                results["per_query"].append(query_result)
                # Incremental save: append full result to JSONL immediately.
                # Ensures progress survives process death mid-run.
                if not retrieval_only:
                    with open(hyp_path, "a") as _hf:
                        _hf.write(json.dumps(query_result) + "\n")
                        _hf.flush()
            except Exception as _qerr:  # noqa: BLE001 — harness boundary over the whole per-question body
                # Don't let one bad question kill the whole run — record it as error
                # so per-type aggregates remain comparable across runs.
                print(f"\n  ERROR on {qid}: {type(_qerr).__name__}: {_qerr}", flush=True)
                results["per_query"].append(
                    {
                        "question_id": qid,
                        "question_type": qtype,
                        "is_abstention": is_abs,
                        "error": f"{type(_qerr).__name__}: {_qerr}",
                    }
                )
            finally:
                if storage is not None:
                    try:
                        storage.close()
                    except OSError:
                        # `close()` already swallows its own httpx / SDK / flock
                        # errors internally; only a raw fd failure escapes.
                        pass
                # Clean up per-question embedded tmpdir (server mode: None, skip).
                if _q_tmpdir_path is not None:
                    import shutil as _q_shutil

                    _q_shutil.rmtree(_q_tmpdir_path, ignore_errors=True)

    finally:
        # Tear down the spawned server and clean up temp data dir.
        if _spawned_proc is not None:
            teardown_surreal_proc(_spawned_proc)
        if _surreal_tmpdir is not None:
            import shutil as _shutil

            _shutil.rmtree(_surreal_tmpdir, ignore_errors=True)
        # Remove env var we injected (don't pollute caller env on function return).
        if _spawned_proc is not None:
            os.environ.pop("YADGAR_DB_URL", None)
            os.environ.pop("YADGAR_ALLOW_ROOT", None)
        # Restore secret-gate env var (we only disabled it for benchmark ingestion).
        if _prev_secret_gate is None:
            os.environ.pop("YADGAR_SECRET_GATE_DISABLED", None)
        else:
            os.environ["YADGAR_SECRET_GATE_DISABLED"] = _prev_secret_gate

    print()  # newline after progress

    # ── Aggregate metrics ─────────────────────────────────────────────

    elapsed = time.monotonic() - start_time
    results["elapsed_seconds"] = round(elapsed, 1)

    # Group by question type
    by_type: dict[str, list[dict]] = {}
    for qr in results["per_query"]:
        qtype = qr["question_type"]
        if qtype not in by_type:
            by_type[qtype] = []
        by_type[qtype].append(qr)

    agg = {}
    for qtype, queries in sorted(by_type.items()):
        # Retrieval metrics (skip abstention)
        retrieval_queries = [q for q in queries if not q.get("is_abstention")]
        type_agg = {"count": len(queries)}

        if retrieval_queries:
            for metric in [
                "recall@5",
                "recall@10",
                "recall@50",
                "ndcg@5",
                "ndcg@10",
                "ndcg@50",
                "mrr",
            ]:
                vals = [q.get(metric, 0) for q in retrieval_queries if metric in q]
                if vals:
                    type_agg[metric] = round(sum(vals) / len(vals), 4)

        # QA accuracy (if available)
        if not retrieval_only:
            correct_count = sum(1 for q in queries if q.get("correct", False))
            type_agg["qa_accuracy"] = round(correct_count / len(queries), 4) if queries else 0
            type_agg["qa_correct"] = correct_count
            type_agg["qa_total"] = len(queries)

        agg[qtype] = type_agg

    # Overall
    all_retrieval = [q for q in results["per_query"] if not q.get("is_abstention") and "mrr" in q]
    overall = {"count": len(results["per_query"])}
    if all_retrieval:
        for metric in ["recall@5", "recall@10", "recall@50", "ndcg@5", "ndcg@10", "ndcg@50", "mrr"]:
            vals = [q.get(metric, 0) for q in all_retrieval]
            if vals:
                overall[metric] = round(sum(vals) / len(vals), 4)

    if not retrieval_only:
        all_correct = sum(1 for q in results["per_query"] if q.get("correct", False))
        overall["qa_accuracy"] = round(all_correct / len(results["per_query"]), 4)
        overall["qa_correct"] = all_correct
        overall["qa_total"] = len(results["per_query"])

    agg["overall"] = overall

    # Abstention accuracy (separate)
    abs_queries = [q for q in results["per_query"] if q.get("is_abstention")]
    if abs_queries and not retrieval_only:
        abs_correct = sum(1 for q in abs_queries if q.get("correct", False))
        agg["abstention"] = {
            "count": len(abs_queries),
            "qa_accuracy": round(abs_correct / len(abs_queries), 4),
            "qa_correct": abs_correct,
        }

    results["aggregated"] = agg

    # ── Output ────────────────────────────────────────────────────────

    # Print summary table
    print("\n" + "=" * 80)
    print("LongMemEval Results")
    print("=" * 80)

    header = f"{'Type':<30} {'Count':>5} {'MRR':>7} {'R@5':>7} {'R@10':>7} {'NDCG@10':>7}"
    if not retrieval_only:
        header += f" {'QA Acc':>7}"
    print(header)
    print("-" * len(header))

    for qtype in QUESTION_TYPES + ["overall"]:
        if qtype not in agg:
            continue
        a = agg[qtype]
        line = f"{qtype:<30} {a['count']:>5} {a.get('mrr', 0):>7.3f} {a.get('recall@5', 0):>7.3f} {a.get('recall@10', 0):>7.3f} {a.get('ndcg@10', 0):>7.3f}"
        if not retrieval_only:
            line += f" {a.get('qa_accuracy', 0):>7.1%}"
        print(line)

    if "abstention" in agg and not retrieval_only:
        a = agg["abstention"]
        print(
            f"{'abstention':<30} {a['count']:>5} {'N/A':>7} {'N/A':>7} {'N/A':>7} {'N/A':>7} {a.get('qa_accuracy', 0):>7.1%}"
        )

    print(f"\nElapsed: {elapsed:.0f}s ({elapsed / 60:.1f}min)")

    # Save aggregated JSON results
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved: {output_path}")

    # Incremental JSONL is already written per-question during the run.
    # Print path for reference; do NOT truncate/overwrite.
    if not retrieval_only:
        print(f"Hypotheses JSONL: {hyp_path}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Run LongMemEval benchmark against Yadgar")
    parser.add_argument(
        "--variant",
        choices=["oracle", "s", "m"],
        default="s",
        help="Dataset variant: oracle (evidence only), s (~40 sessions), m (~500 sessions)",
    )
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Only compute retrieval metrics (no LLM calls, fast)",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=0,
        help="Limit number of questions (0 = all)",
    )
    parser.add_argument(
        "--types",
        type=str,
        default="",
        help="Comma-separated question types to evaluate",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Max memories to retrieve per question",
    )
    parser.add_argument(
        "--top-k-context",
        type=int,
        default=10,
        help="Top-k retrieved memories to include in reader prompt",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON path",
    )
    parser.add_argument(
        "--stratify-per-type",
        action="store_true",
        help="With --max-questions and --types, sample evenly per type and "
        "interleave them so early-killed runs still yield per-type signal.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Claude model to use (sets ANTHROPIC_MODEL env var). "
        "Example: claude-sonnet-4-6. Overrides ANTHROPIC_MODEL if both set.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted run: skip questions already present in the "
        "incremental JSONL. Requires --output to be set explicitly so the path "
        "is stable across invocations.",
    )
    parser.add_argument(
        "--unified",
        action="store_true",
        help="Route retrieval through the unified MCP recall tool (v5.80 fan-out + "
        "directory-scoping) instead of the legacy Retriever.recall() path. Measures "
        "the path real MCP callers use.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        help="Recall mode forwarded to the unified recall tool (e.g. 'landscape' for "
        "consensus_retrieve, #67). Only effective with --unified. Default arm = no mode.",
    )
    parser.add_argument(
        "--enrich",
        choices=["on", "off"],
        default="on",
        help="Run index-time enrichment (COMET/ConceptNet/Logic/Doc2Query/FPA) during "
        "haystack ingest, matching production memorize() (default: on). 'off' ingests "
        "a RAW corpus (faster, but un-faithful to prod — use only for quick smokes). "
        "Enrichment adds per-memory model inference, so full-500 enriched runs are slow.",
    )
    parser.add_argument(
        "--settings-override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a Settings field for this run (repeatable), e.g. "
        "--settings-override CROSS_ENCODER_TOP_K=5 --settings-override "
        "CROSS_ENCODER_BACKEND=onnx-int8. Threaded into make_benchmark_settings so it "
        "wins over the benchmark defaults. Used to A/B rerank levers (v5.98 quality gate).",
    )

    args = parser.parse_args()

    # Set model via env var so call_claude_pipe picks it up
    if args.model:
        os.environ["ANTHROPIC_MODEL"] = args.model

    dataset_path = download_dataset(args.variant)

    question_types = None
    if args.types:
        question_types = [t.strip() for t in args.types.split(",")]

    settings_overrides = _parse_settings_overrides(args.settings_override)

    run_benchmark(
        dataset_path=dataset_path,
        retrieval_only=args.retrieval_only,
        max_questions=args.max_questions,
        question_types=question_types,
        max_results=args.max_results,
        top_k_context=args.top_k_context,
        settings_overrides=settings_overrides,
        output_path=args.output,
        stratify_per_type=args.stratify_per_type,
        resume=args.resume,
        mode=args.mode,
        unified=args.unified,
        enrich=(args.enrich == "on"),
    )


if __name__ == "__main__":
    main()
