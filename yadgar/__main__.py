"""Entry point for python -m yadgar."""

import argparse
import sys
from datetime import UTC
from pathlib import Path

from yadgar import __version__
from yadgar.server import main

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")

STARTUP_BANNER = f"""\
=== Yadgar v{__version__} ===
Biologically-inspired persistent memory engine for Claude Code

Active modules:
  * StorageEngine         (SurrealDB with KV + FTS + vector search)
  * EmbeddingEngine       (sentence-transformers)
  * SensoryBuffer         (episode capture)
  * MemoryThermodynamics  (surprise, importance, valence, decay)
  * KnowledgeGraph        (typed relationships, causal detection)
  * HippoRetriever        (PPR + vector + FTS5 + spreading activation + fractal)
  * MemoryCurator         (merge/link/create, contradiction, memify)
  * AstrocyteEngine       (background consolidation daemon)
  * AstrocytePool         (domain-aware processes: code/decisions/errors/deps)
  * SleepComputeEngine    (dream replay, compression, community detection)
  * FractalMemoryTree     (hierarchical multi-scale retrieval)
  * ProspectiveMemory     (future-oriented triggers)
  * NarrativeEngine       (autobiographical project stories)
  * StalenessDetector     (file-change watchdog)

MCP Tools: remember, recall, forget, validate_memory, get_project_context,
           consolidate_now, memory_stats, rate_memory, recall_hierarchical,
           drill_down, create_trigger, get_project_story, seed_project,
           checkpoint, restore, anchor, navigate_memory, assess_coverage,
           detect_gaps, install_hooks, sync_instructions

MCP Resources: memory://stats, memory://hot, memory://stale,
               memory://processes, memory://narrative/{{directory}}
"""


def _init_replay_lightweight(db_path=None):
    """Initialize only the engines needed for drain/restore (no daemons, no server)."""
    import logging

    # Suppress all library logging — hooks must only output data to stdout
    logging.disable(logging.CRITICAL)

    from yadgar.cognitive_map import CognitiveMap
    from yadgar.config import Settings
    from yadgar.embeddings import EmbeddingEngine
    from yadgar.knowledge_graph import KnowledgeGraph
    from yadgar.metacognition import MetaCognition
    from yadgar.restoration import HippocampalReplay
    from yadgar.retrieval import HippoRetriever
    from yadgar.storage import StorageEngine

    settings = Settings()
    storage = StorageEngine(db_path or settings.DB_PATH)
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
    kg = KnowledgeGraph(storage, settings)
    cognitive_map = CognitiveMap(storage, settings)
    retriever = HippoRetriever(storage, embeddings, kg, settings)
    retriever.set_cognitive_map(cognitive_map)
    metacognition = MetaCognition(storage, embeddings, kg, settings)

    replay = HippocampalReplay(
        storage=storage,
        embeddings=embeddings,
        retriever=retriever,
        cognitive_map=cognitive_map,
        metacognition=metacognition,
        settings=settings,
    )
    return storage, replay


def cmd_drain(args):
    """Pre-compaction drain: save context to DB before Claude compacts."""
    import json

    directory = args.directory
    storage, replay = _init_replay_lightweight(args.db_path)
    try:
        result = replay.pre_compact_drain(directory)
        # Output JSON to stdout so hook can parse it if needed
        print(json.dumps(result))
    finally:
        storage.close()


def cmd_restore(args):
    """Post-compaction restore: reconstruct context and print markdown to stdout."""
    directory = args.directory
    storage, replay = _init_replay_lightweight(args.db_path)
    try:
        result = replay.restore(directory)
        formatted = result.get("formatted", "")
        if formatted:
            print(formatted)
    finally:
        storage.close()


def cmd_capture(args):
    """Lightweight action capture — writes directly to SurrealDB without ML models.

    Used by PostToolCall hooks and manual capture.
    """
    from datetime import datetime

    from surrealdb import Surreal

    from yadgar.config import Settings

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())

    try:
        db = Surreal(f"surrealkv://{db_path}")
        db.use("yadgar", "main")
        db.query(
            "CREATE action_log SET tool_name = $tn, tool_input_summary = $s, "
            "directory = $d, session_id = $sid, timestamp = $ts, processed = false",
            {
                "tn": args.tool_name,
                "s": args.summary or "",
                "d": args.directory or "",
                "sid": args.session or "",
                "ts": datetime.now(UTC).isoformat(),
            },
        )
    except Exception as e:
        print(f"Failed to capture action: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_context(args):
    """Lightweight context query — reads hot memories without loading ML models.

    Used by SessionStart hooks to inject context on every session.
    """
    from surrealdb import Surreal

    from yadgar.config import Settings

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    directory = args.directory

    try:
        db = Surreal(f"surrealkv://{db_path}")
        db.use("yadgar", "main")

        hot_results = db.query(
            "SELECT content, heat FROM memory "
            "WHERE directory_context = $dir AND heat >= 0 "
            "ORDER BY heat DESC LIMIT 6",
            {"dir": directory},
        )
        hot = hot_results[0] if hot_results else []

        anchored_results = db.query(
            "SELECT content FROM memory "
            "WHERE is_protected = true AND heat > 0 AND $anchor IN tags "
            "ORDER BY created_at DESC LIMIT 4",
            {"anchor": "_anchor"},
        )
        anchored = anchored_results[0] if anchored_results else []
    except Exception:
        return

    if not hot and not anchored:
        return

    print("# Yadgar — Session Context\n")
    if anchored:
        print("## Critical Facts")
        for row in anchored:
            print(f"- {row['content'][:200]}")
        print()
    if hot:
        print("## Project Context")
        for row in hot:
            content = row["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"- [{row['heat']:.1f}] {content}")
        print()
    print(f"*Context for: {directory}*")


def cmd_stats(args):
    """Show detailed memory statistics."""
    import json
    from datetime import datetime

    from surrealdb import Surreal

    from yadgar.config import Settings

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    project = str(Path(args.project).resolve()) if args.project else None

    def _one(results, key, default=0):
        """Extract a single aggregate value from a GROUP ALL result."""
        try:
            return results[0][0][key] if results and results[0] else default
        except (IndexError, KeyError, TypeError):
            return default

    def _count(results):
        return _one(results, "count", 0)

    try:
        db = Surreal(f"surrealkv://{db_path}")
        db.use("yadgar", "main")

        # ── Core counts ──
        if project:
            total_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p GROUP ALL",
                {"p": project},
            )
        else:
            total_res = db.query("SELECT count() FROM memory GROUP ALL")
        total = _count(total_res)

        if total == 0:
            label = f"project {project}" if project else "database"
            print(f"No memories in {label}.", file=sys.stderr)
            sys.exit(0)

        if project:
            active_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND is_stale = false AND heat >= 0.05 GROUP ALL",
                {"p": project},
            )
            stale_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND is_stale = true GROUP ALL",
                {"p": project},
            )
            archived_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p AND heat < 0.05 GROUP ALL",
                {"p": project},
            )
            protected_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND is_protected = true GROUP ALL",
                {"p": project},
            )
        else:
            active_res = db.query(
                "SELECT count() FROM memory WHERE is_stale = false AND heat >= 0.05 GROUP ALL"
            )
            stale_res = db.query("SELECT count() FROM memory WHERE is_stale = true GROUP ALL")
            archived_res = db.query("SELECT count() FROM memory WHERE heat < 0.05 GROUP ALL")
            protected_res = db.query(
                "SELECT count() FROM memory WHERE is_protected = true GROUP ALL"
            )

        active = _count(active_res)
        stale = _count(stale_res)
        archived = _count(archived_res)
        protected = _count(protected_res)

        # ── Type breakdown ──
        if project:
            episodic_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND store_type = 'episodic' GROUP ALL",
                {"p": project},
            )
            semantic_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND store_type = 'semantic' GROUP ALL",
                {"p": project},
            )
        else:
            episodic_res = db.query(
                "SELECT count() FROM memory WHERE store_type = 'episodic' GROUP ALL"
            )
            semantic_res = db.query(
                "SELECT count() FROM memory WHERE store_type = 'semantic' GROUP ALL"
            )
        episodic = _count(episodic_res)
        semantic = _count(semantic_res)

        # ── Compression levels ──
        if project:
            comp_0_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND compression_level = 0 GROUP ALL",
                {"p": project},
            )
            comp_1_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND compression_level = 1 GROUP ALL",
                {"p": project},
            )
            comp_2_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND compression_level = 2 GROUP ALL",
                {"p": project},
            )
        else:
            comp_0_res = db.query(
                "SELECT count() FROM memory WHERE compression_level = 0 GROUP ALL"
            )
            comp_1_res = db.query(
                "SELECT count() FROM memory WHERE compression_level = 1 GROUP ALL"
            )
            comp_2_res = db.query(
                "SELECT count() FROM memory WHERE compression_level = 2 GROUP ALL"
            )
        comp_0 = _count(comp_0_res)
        comp_1 = _count(comp_1_res)
        comp_2 = _count(comp_2_res)

        # ── Heat stats ──
        if project:
            heat_res = db.query(
                "SELECT math::min(heat) AS min_h, math::mean(heat) AS avg_h, "
                "math::max(heat) AS max_h FROM memory "
                "WHERE directory_context = $p GROUP ALL",
                {"p": project},
            )
        else:
            heat_res = db.query(
                "SELECT math::min(heat) AS min_h, math::mean(heat) AS avg_h, "
                "math::max(heat) AS max_h FROM memory GROUP ALL"
            )
        heat_min = _one(heat_res, "min_h", 0) or 0
        heat_avg = _one(heat_res, "avg_h", 0) or 0
        heat_max = _one(heat_res, "max_h", 0) or 0

        heat_buckets = []
        for lo, hi, label in [
            (0, 0.01, "cold (<0.01)"),
            (0.01, 0.1, "cool (0.01-0.1)"),
            (0.1, 0.5, "warm (0.1-0.5)"),
            (0.5, 0.9, "hot (0.5-0.9)"),
            (0.9, 999, "burning (0.9+)"),
        ]:
            if project:
                br = db.query(
                    "SELECT count() FROM memory WHERE directory_context = $p "
                    "AND heat >= $lo AND heat < $hi GROUP ALL",
                    {"p": project, "lo": lo, "hi": hi},
                )
            else:
                br = db.query(
                    "SELECT count() FROM memory WHERE heat >= $lo AND heat < $hi GROUP ALL",
                    {"lo": lo, "hi": hi},
                )
            heat_buckets.append((label, _count(br)))

        # ── Access stats ──
        if project:
            access_res = db.query(
                "SELECT math::sum(access_count) AS total_ac, "
                "math::mean(access_count) AS avg_ac, "
                "math::max(access_count) AS max_ac FROM memory "
                "WHERE directory_context = $p GROUP ALL",
                {"p": project},
            )
            useful_res = db.query(
                "SELECT math::sum(useful_count) AS total_uc FROM memory "
                "WHERE directory_context = $p GROUP ALL",
                {"p": project},
            )
            never_res = db.query(
                "SELECT count() FROM memory WHERE directory_context = $p "
                "AND access_count = 0 GROUP ALL",
                {"p": project},
            )
        else:
            access_res = db.query(
                "SELECT math::sum(access_count) AS total_ac, "
                "math::mean(access_count) AS avg_ac, "
                "math::max(access_count) AS max_ac FROM memory GROUP ALL"
            )
            useful_res = db.query(
                "SELECT math::sum(useful_count) AS total_uc FROM memory GROUP ALL"
            )
            never_res = db.query("SELECT count() FROM memory WHERE access_count = 0 GROUP ALL")
        total_accesses = _one(access_res, "total_ac", 0) or 0
        avg_accesses = _one(access_res, "avg_ac", 0) or 0
        max_accesses = _one(access_res, "max_ac", 0) or 0
        total_useful = _one(useful_res, "total_uc", 0) or 0
        never_accessed = _count(never_res)

        # ── Temporal stats ──
        if project:
            temporal_res = db.query(
                "SELECT math::min(created_at) AS oldest, math::max(created_at) AS newest, "
                "math::max(last_accessed) AS last_acc FROM memory "
                "WHERE directory_context = $p GROUP ALL",
                {"p": project},
            )
        else:
            temporal_res = db.query(
                "SELECT math::min(created_at) AS oldest, math::max(created_at) AS newest, "
                "math::max(last_accessed) AS last_acc FROM memory GROUP ALL"
            )
        oldest = _one(temporal_res, "oldest", None)
        newest = _one(temporal_res, "newest", None)
        last_accessed = _one(temporal_res, "last_acc", None)

        now = datetime.now(UTC)
        age_days = None
        if oldest:
            try:
                oldest_str = str(oldest)
                oldest_dt = datetime.fromisoformat(oldest_str.replace("Z", "+00:00"))
                age_days = (now - oldest_dt).days
            except Exception:
                pass

        # ── Per-project breakdown (only when no --project filter) ──
        project_rows = []
        if not project:
            proj_res = db.query(
                "SELECT directory_context, count() AS cnt, math::mean(heat) AS avg_h, "
                "math::max(created_at) AS last_created FROM memory "
                "WHERE directory_context != '' "
                "GROUP BY directory_context ORDER BY cnt DESC LIMIT 15"
            )
            project_rows = proj_res[0] if proj_res else []

        # ── Consolidation history ──
        consol_count_res = db.query("SELECT count() FROM consolidation_log GROUP ALL")
        total_consolidations = _count(consol_count_res)

        last_consol_res = db.query(
            "SELECT timestamp, duration_ms, memories_added, memories_archived "
            "FROM consolidation_log ORDER BY id DESC LIMIT 1"
        )
        last_consol = last_consol_res[0][0] if last_consol_res and last_consol_res[0] else None

        avg_dur_res = db.query(
            "SELECT math::mean(duration_ms) AS avg_d FROM consolidation_log GROUP ALL"
        )
        avg_duration = _one(avg_dur_res, "avg_d", 0) or 0

        # ── Knowledge graph ──
        try:
            entity_res = db.query("SELECT count() FROM entity GROUP ALL")
            entity_count = _count(entity_res)
            rel_res = db.query("SELECT count() FROM relationship GROUP ALL")
            rel_count = _count(rel_res)
        except Exception:
            entity_count = rel_count = 0

        try:
            causal_res = db.query("SELECT count() FROM causal_dag_edge GROUP ALL")
            causal_edges = _count(causal_res)
        except Exception:
            causal_edges = 0

        # ── Action log ──
        try:
            act_total_res = db.query("SELECT count() FROM action_log GROUP ALL")
            action_total = _count(act_total_res)
            act_unproc_res = db.query(
                "SELECT count() FROM action_log WHERE processed = false GROUP ALL"
            )
            action_unprocessed = _count(act_unproc_res)
        except Exception:
            action_total = action_unprocessed = 0

        # ── Clusters ──
        try:
            cluster_res = db.query("SELECT count() FROM memory_cluster GROUP ALL")
            cluster_count = _count(cluster_res)
        except Exception:
            cluster_count = 0

        # ── Narrative entries ──
        try:
            narrative_res = db.query("SELECT count() FROM narrative_entry GROUP ALL")
            narrative_count = _count(narrative_res)
        except Exception:
            narrative_count = 0

        # ── Prospective memories ──
        try:
            trig_active_res = db.query(
                "SELECT count() FROM prospective_memory WHERE is_active = true GROUP ALL"
            )
            triggers_active = _count(trig_active_res)
            trig_fired_res = db.query(
                "SELECT math::sum(triggered_count) AS total_fired FROM prospective_memory GROUP ALL"
            )
            triggers_fired = _one(trig_fired_res, "total_fired", 0) or 0
        except Exception:
            triggers_active = triggers_fired = 0

        # ── Top tags ──
        tag_counts: dict[str, int] = {}
        if project:
            tags_res = db.query(
                "SELECT tags FROM memory WHERE directory_context = $p",
                {"p": project},
            )
        else:
            tags_res = db.query("SELECT tags FROM memory")
        for row in tags_res[0] if tags_res else []:
            try:
                for tag in row.get("tags") or []:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except Exception:
                pass
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:10]

    except Exception as e:
        print(f"Failed to query database: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Output ──
    if args.format == "json":
        data = {
            "total": total,
            "active": active,
            "stale": stale,
            "archived": archived,
            "protected": protected,
            "episodic": episodic,
            "semantic": semantic,
            "compression": {"raw": comp_0, "gist": comp_1, "tag": comp_2},
            "heat": {
                "min": heat_min,
                "avg": heat_avg,
                "max": heat_max,
                "buckets": {b[0]: b[1] for b in heat_buckets},
            },
            "access": {
                "total": total_accesses,
                "avg": avg_accesses,
                "max": max_accesses,
                "useful": total_useful,
                "never_accessed": never_accessed,
            },
            "temporal": {
                "oldest": oldest,
                "newest": newest,
                "last_accessed": last_accessed,
                "age_days": age_days,
            },
            "consolidation": {"total": total_consolidations, "avg_duration_ms": avg_duration},
            "knowledge_graph": {
                "entities": entity_count,
                "relationships": rel_count,
                "causal_edges": causal_edges,
            },
            "action_log": {"total": action_total, "unprocessed": action_unprocessed},
            "clusters": cluster_count,
            "narratives": narrative_count,
            "triggers": {"active": triggers_active, "fired": triggers_fired},
            "top_tags": dict(top_tags),
        }
        if project_rows:
            data["projects"] = [
                {
                    "directory": r["directory_context"],
                    "count": r["cnt"],
                    "avg_heat": round(r["avg_h"] or 0, 4),
                    "last_created": r["last_created"],
                }
                for r in project_rows
            ]
        print(json.dumps(data, indent=2))
        return

    # Human-readable table output
    header = f"=== Yadgar Stats{f' — {project}' if project else ''} ==="
    print(header)
    print()

    print("MEMORIES")
    print(f"  Total:     {total}")
    print(f"  Active:    {active}")
    print(f"  Stale:     {stale}")
    print(f"  Archived:  {archived}")
    print(f"  Protected: {protected}")
    print()

    print("TYPES")
    print(f"  Episodic:  {episodic}")
    print(f"  Semantic:  {semantic}")
    print(f"  Raw:       {comp_0}  |  Gist: {comp_1}  |  Tag: {comp_2}")
    print()

    print("HEAT")
    print(f"  Min: {heat_min:.4f}  |  Avg: {heat_avg:.4f}  |  Max: {heat_max:.4f}")
    for label, count in heat_buckets:
        bar = "#" * min(count, 40)
        print(f"  {label:20s} {count:5d}  {bar}")
    print()

    print("ACCESS")
    print(f"  Total recalls:   {total_accesses}")
    print(f"  Avg per memory:  {avg_accesses:.1f}")
    print(f"  Max on a single: {max_accesses}")
    print(f"  Rated useful:    {total_useful}")
    print(f"  Never accessed:  {never_accessed}")
    print()

    print("TEMPORAL")
    if age_days is not None:
        print(f"  Memory span:     {age_days} days")
    print(f"  Oldest:          {oldest or 'n/a'}")
    print(f"  Newest:          {newest or 'n/a'}")
    print(f"  Last accessed:   {last_accessed or 'n/a'}")
    print()

    if project_rows:
        print("PROJECTS (top 15)")
        for r in project_rows:
            print(f"  {r['cnt']:5d} memories  heat={r['avg_h'] or 0:.4f}  {r['directory_context']}")
        print()

    print("CONSOLIDATION")
    print(f"  Total cycles:    {total_consolidations}")
    print(f"  Avg duration:    {avg_duration:.0f}ms")
    if last_consol:
        print(f"  Last run:        {last_consol['timestamp']}")
        print(
            f"    Added: {last_consol['memories_added']}  Archived: {last_consol['memories_archived']}  Duration: {last_consol['duration_ms']}ms"
        )
    print()

    print("KNOWLEDGE GRAPH")
    print(f"  Entities:        {entity_count}")
    print(f"  Relationships:   {rel_count}")
    print(f"  Causal edges:    {causal_edges}")
    print()

    print("SUBSYSTEMS")
    print(f"  Clusters:        {cluster_count}")
    print(f"  Narratives:      {narrative_count}")
    print(f"  Active triggers: {triggers_active}  (fired {triggers_fired} times)")
    print(f"  Action log:      {action_total} total, {action_unprocessed} unprocessed")
    print()

    if top_tags:
        print("TOP TAGS")
        for tag, count in top_tags:
            print(f"  {count:5d}  {tag}")
        print()


def cmd_vacuum(args):
    """Compact the SurrealKV commit log by export → drop clog → reimport.

    Must be run while the daemon is stopped (it holds an exclusive DB lock).
    """
    import pickle
    import shutil

    from yadgar.config import Settings
    from yadgar.storage import StorageEngine

    settings = Settings()
    db_path_str = str(Path(args.db_path or settings.DB_PATH).expanduser())
    db_path = Path(db_path_str)
    clog_path = db_path / "clog"

    if not clog_path.exists():
        print("No clog directory found — nothing to vacuum.")
        return

    old_size = sum(f.stat().st_size for f in clog_path.rglob("*") if f.is_file())
    print(f"Clog size before: {old_size / 1024 / 1024:.0f} MB")

    # Preserve live data; skip ephemeral tables that get rebuilt automatically
    KEEP_TABLES = [
        "memory",
        "memory_archive",
        "memory_transition",
        "entity",
        "relationship",
        "causal_dag_edge",
        "user_profile",
        "derived_belief",
        "checkpoint",
        "memory_rule",
        "engram_slot",
        "narrative_entry",
        "prospective_memory",
        "counter",
        "episode",
    ]

    print("Opening database (will fail if daemon is running)...")
    try:
        storage = StorageEngine(db_path_str)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(
            "Stop the daemon first:  systemctl --user stop yadgar.service",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Phase 1: Export ──────────────────────────────────────────────────────
    print("Exporting tables...")
    dump: dict = {}
    total = 0
    for table in KEEP_TABLES:
        try:
            rows = storage._db.query(f"SELECT * FROM {table}")
            records = rows[0] if rows else []
            if not isinstance(records, list):
                records = []
            dump[table] = records
            total += len(records)
            print(f"  {table}: {len(records)}")
        except Exception as e:
            print(f"  {table}: skipped — {e}")
            dump[table] = []

    dump_path = db_path.parent / "vacuum_dump.pkl"
    with open(dump_path, "wb") as f:
        pickle.dump(dump, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"\n{total} records saved to {dump_path}")
    storage.close()

    # ── Phase 2: Drop clog ───────────────────────────────────────────────────
    backup_clog = db_path.parent / "surreal_db_clog.bak"
    if backup_clog.exists():
        shutil.rmtree(backup_clog)
    print("Backing up and clearing clog...")
    shutil.copytree(clog_path, backup_clog)
    shutil.rmtree(clog_path)
    clog_path.mkdir()

    # ── Phase 3: Reimport ────────────────────────────────────────────────────
    print("Reimporting into fresh database...")
    storage = StorageEngine(db_path_str)

    for table in KEEP_TABLES:
        records = dump.get(table, [])
        ok = errors = 0
        for rec in records:
            try:
                rid = rec.get("id")
                if rid is None:
                    continue
                if hasattr(rid, "id"):
                    raw_id = rid.id
                else:
                    s = str(rid)
                    raw_id = s.split(":")[-1] if ":" in s else s
                content = {k: v for k, v in rec.items() if k != "id"}
                storage._db.query(
                    f"UPSERT {table}:{raw_id} CONTENT $data",
                    {"data": content},
                )
                ok += 1
            except Exception:
                errors += 1
        msg = f"  {table}: {ok} restored"
        if errors:
            msg += f", {errors} errors"
        print(msg)

    storage.close()

    new_size = sum(f.stat().st_size for f in clog_path.rglob("*") if f.is_file())
    saved = old_size - new_size
    pct = int(100 * saved // old_size) if old_size else 0
    print("\nVacuum complete.")
    print(f"  Before: {old_size / 1024 / 1024:.0f} MB")
    print(f"  After:  {new_size / 1024 / 1024:.0f} MB")
    print(f"  Saved:  {saved / 1024 / 1024:.0f} MB ({pct}%)")
    print(f"\nBackup clog: {backup_clog}")
    print(f"Pickle dump: {dump_path}")


def cmd_seed(args):
    """Bootstrap memory for an existing project by scanning its structure."""
    import json

    from yadgar.seed import seed_project

    directory = str(Path(args.directory).resolve())
    print(f"Seeding project: {directory}", file=sys.stderr)

    result = seed_project(
        directory=directory,
        db_path=args.db_path,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(
            f"\n[DRY RUN] Would create {result['memories_generated']} memories for {result['project']}\n",
            file=sys.stderr,
        )
        for mem in result.get("memories", []):
            tags = ", ".join(mem["tags"])
            print(f"  [{tags}] {mem['content'][:120]}...", file=sys.stderr)
    else:
        replaced_msg = f", replaced {result['replaced']} old" if result.get("replaced") else ""
        print(
            f"\nSeeded {result['project']}: "
            f"{result['created']} created{replaced_msg} "
            f"(from {result['memories_generated']} total)",
            file=sys.stderr,
        )

    print(json.dumps(result))


def cmd_daemon(args):
    """Manage the Yadgar background daemon."""
    import os as _os

    from yadgar.daemon import YadgarDaemon

    port = int(getattr(args, "port", None) or _os.environ.get("YADGAR_PORT", "8765"))
    daemon = YadgarDaemon(port=port, db_path=getattr(args, "db_path", None))

    sub = args.daemon_command
    if sub is None:
        print("Usage: yadgar daemon <start|stop|restart|status|configure-mcp|install-service>")
        return

    if sub == "start":
        result = daemon.start()
        if result["status"] == "started":
            print(f"Yadgar daemon started (PID: {result['pid']}, port: {result['port']})")
            print("  Switch MCP to HTTP:  yadgar daemon configure-mcp")
            print("  Auto-start on login: yadgar daemon install-service")
        elif result["status"] == "already_running":
            print(f"Yadgar daemon already running (PID: {result['pid']}, port: {result['port']})")
        elif result["status"] == "failed":
            print(f"Cannot start daemon: {result['reason']}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Unexpected result: {result}", file=sys.stderr)
            sys.exit(1)

    elif sub == "stop":
        result = daemon.stop()
        if result["status"] == "stopped":
            print(f"Yadgar daemon stopped (was PID: {result['pid']})")
        else:
            print("Yadgar daemon is not running.")

    elif sub == "restart":
        result = daemon.restart()
        start = result["started"]
        if start.get("status") in ("started", "already_running"):
            print(f"Yadgar daemon restarted (PID: {start.get('pid')}, port: {start.get('port')})")
        else:
            print(f"Restart result: {result}", file=sys.stderr)

    elif sub == "status":
        result = daemon.status()
        if result.get("running"):
            print("Yadgar daemon: running")
            print(f"  PID:     {result.get('pid')}")
            print(f"  Port:    {result.get('port')}")
            print(f"  Version: {result.get('version', '?')}")
            print(f"  Uptime:  {result.get('uptime_seconds', '?')}s")
        else:
            print("Yadgar daemon: not running")
            print("  Start with: yadgar daemon start")

    elif sub == "configure-mcp":
        result = daemon.configure_mcp()
        print(f"MCP config updated: {result['updated']}")
        print(f"  Sessions connect to: http://127.0.0.1:{port}/sse")

    elif sub == "install-service":
        result = daemon.install_systemd_service()
        print(f"Systemd service written: {result['service_file']}")
        print(f"  Enable:  {result['enable']}")
        print(f"  Start:   {result['start']}")
        print(f"  Status:  {result['status']}")


def cli():
    parser = argparse.ArgumentParser(description="Yadgar memory engine MCP server")
    subparsers = parser.add_subparsers(dest="command")

    # Default server mode (no subcommand)
    parser.add_argument("--port", type=int, default=None, help="Server port (default: 8742)")
    parser.add_argument("--db-path", type=str, default=None, help="Database path")
    parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=VALID_TRANSPORTS,
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress startup banner",
    )

    # drain subcommand
    drain_parser = subparsers.add_parser("drain", help="Pre-compaction context drain")
    drain_parser.add_argument("directory", help="Project directory")
    drain_parser.add_argument("--db-path", type=str, default=None, help="Database path")

    # restore subcommand
    restore_parser = subparsers.add_parser("restore", help="Post-compaction context restore")
    restore_parser.add_argument("directory", help="Project directory")
    restore_parser.add_argument("--db-path", type=str, default=None, help="Database path")

    # capture subcommand (used by PostToolCall hooks)
    capture_parser = subparsers.add_parser("capture", help="Lightweight action capture")
    capture_parser.add_argument("--tool", dest="tool_name", required=True, help="Tool name")
    capture_parser.add_argument("--summary", type=str, default="", help="Tool input summary")
    capture_parser.add_argument("--directory", type=str, default="", help="Working directory")
    capture_parser.add_argument("--session", type=str, default="", help="Session ID")
    capture_parser.add_argument("--db-path", type=str, default=None, help="Database path")

    # context subcommand (used by SessionStart hooks)
    context_parser = subparsers.add_parser("context", help="Lightweight context query")
    context_parser.add_argument("directory", help="Project directory")
    context_parser.add_argument("--db-path", type=str, default=None, help="Database path")

    # stats subcommand
    stats_parser = subparsers.add_parser("stats", help="Show detailed memory statistics")
    stats_parser.add_argument(
        "--project", type=str, default=None, help="Filter to a specific project directory"
    )
    stats_parser.add_argument("--db-path", type=str, default=None, help="Database path")
    stats_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )

    # vacuum subcommand
    vacuum_parser = subparsers.add_parser(
        "vacuum", help="Compact the SurrealKV commit log (daemon must be stopped)"
    )
    vacuum_parser.add_argument("--db-path", type=str, default=None, help="Database path")

    # seed subcommand
    seed_parser = subparsers.add_parser("seed", help="Bootstrap memory for an existing project")
    seed_parser.add_argument("directory", help="Project directory to scan and seed")
    seed_parser.add_argument("--db-path", type=str, default=None, help="Database path")
    seed_parser.add_argument(
        "--dry-run", action="store_true", help="Scan and show what would be stored without storing"
    )

    # config subcommand
    config_parser = subparsers.add_parser("config", help="Manage Yadgar configuration")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_init_p = config_sub.add_parser(
        "init", help="Write default config.yaml with all settings commented"
    )
    config_init_p.add_argument("--force", action="store_true", help="Overwrite existing config")
    config_list_p = config_sub.add_parser(
        "list", help="List all settings with current values and sources"
    )
    config_list_p.add_argument(
        "--section", type=str, default=None, help="Filter to a section (e.g. daemon)"
    )
    config_get_p = config_sub.add_parser("get", help="Get a single setting value")
    config_get_p.add_argument("key", help="Setting name (e.g. daemon_check_interval)")
    config_set_p = config_sub.add_parser("set", help="Set a setting value in config.yaml")
    config_set_p.add_argument("key", help="Setting name")
    config_set_p.add_argument("value", help="New value")
    config_sub.add_parser("edit", help="Open config.yaml in $EDITOR")

    # daemon subcommand
    daemon_parser = subparsers.add_parser("daemon", help="Manage the Yadgar background daemon")
    daemon_parser.add_argument("--port", type=int, default=None, help="Daemon port (default: 8765)")
    daemon_parser.add_argument("--db-path", type=str, default=None, help="Database path")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_sub.add_parser("start", help="Start the daemon in the background")
    daemon_sub.add_parser("stop", help="Stop the running daemon")
    daemon_sub.add_parser("restart", help="Restart the daemon")
    daemon_sub.add_parser("status", help="Show daemon status")
    daemon_sub.add_parser(
        "configure-mcp", help="Switch ~/.claude.json MCP config to HTTP transport"
    )
    daemon_sub.add_parser(
        "install-service", help="Install systemd user service for auto-start on login"
    )

    args = parser.parse_args()

    if args.command == "config":
        from yadgar.config_yaml import (
            cmd_config_edit,
            cmd_config_get,
            cmd_config_init,
            cmd_config_list,
            cmd_config_set,
        )

        sub = getattr(args, "config_command", None)
        if sub is None:
            config_parser.print_help()
        elif sub == "init":
            cmd_config_init(args)
        elif sub == "list":
            cmd_config_list(args)
        elif sub == "get":
            cmd_config_get(args)
        elif sub == "set":
            cmd_config_set(args)
        elif sub == "edit":
            cmd_config_edit(args)
    elif args.command == "drain":
        cmd_drain(args)
    elif args.command == "restore":
        cmd_restore(args)
    elif args.command == "capture":
        cmd_capture(args)
    elif args.command == "context":
        cmd_context(args)
    elif args.command == "vacuum":
        cmd_vacuum(args)
    elif args.command == "seed":
        cmd_seed(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "daemon":
        cmd_daemon(args)
    else:
        # Default: run MCP server
        if not args.quiet and args.transport != "stdio":
            print(STARTUP_BANNER, file=sys.stderr)
            print(f"Transport: {args.transport}", file=sys.stderr)
            if args.port:
                print(f"Port: {args.port}", file=sys.stderr)
            if args.db_path:
                print(f"Database: {args.db_path}", file=sys.stderr)
            print(file=sys.stderr)

        main(port=args.port, db_path=args.db_path, transport=args.transport)


if __name__ == "__main__":
    cli()
