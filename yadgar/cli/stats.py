"""stats subcommand — detailed memory statistics.

# Module size justified: single-responsibility CLI command. All LoC implement
# cmd_stats — one function with two output paths (daemon HTTP fallback → direct
# DB access) and two format modes (table, JSON). The size is driven by the number
# of parallel aggregate queries needed for a comprehensive stats view, not by
# multiple responsibilities. Splitting (e.g. per-section helpers) would scatter
# query logic that shares local variables and a single DB connection.
"""

import sys
from pathlib import Path


def cmd_stats(args):
    """Show detailed memory statistics.

    Tries the running daemon's HTTP endpoint first (works when server is in Docker).
    Falls back to direct DB access when no daemon is reachable.
    """
    import json
    import os as _os
    import urllib.parse
    import urllib.request
    from datetime import UTC, datetime

    port = int(_os.environ.get("YADGAR_PORT", "8765"))
    http_url = f"http://127.0.0.1:{port}/api/stats"
    if args.project:
        http_url += "?" + urllib.parse.urlencode({"project": args.project})
    try:
        # §8: Validate scheme before urlopen to prevent file:// SSRF.
        _parsed = urllib.parse.urlparse(http_url)
        if _parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Disallowed scheme in URL: {_parsed.scheme!r}")
        resp = urllib.request.urlopen(http_url, timeout=2)  # noqa: S310
        data = json.loads(resp.read().decode())
        if args.format == "json":
            print(json.dumps(data, indent=2))
        else:
            # Brief summary — daemon has limited stats; full detail needs direct DB
            header = f"=== Yadgar Stats{f' — {args.project}' if args.project else ''} ==="
            print(header)
            print()
            print(f"  Total:    {data.get('total_memories', '?')}")
            print(f"  Active:   {data.get('active_count', '?')}")
            print(f"  Archived: {data.get('archived_count', '?')}")
            print(f"  Stale:    {data.get('stale_count', '?')}")
            print(f"  Avg heat: {data.get('avg_heat', 0):.4f}")
            print(f"  Last consolidation: {data.get('last_consolidation', 'n/a')}")
            print()
            print("(Daemon running in Docker — for full stats run with direct DB access)")
        return
    except Exception:
        pass  # daemon not running or unreachable — fall back to direct DB access

    try:
        from surrealdb import Surreal
    except ImportError:
        print(
            "surrealdb package not installed and daemon is not reachable.\n"
            "Install it with: pip install surrealdb  or start the daemon: yadgar daemon start",
            file=sys.stderr,
        )
        sys.exit(1)

    from yadgar.config import Settings

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    project = str(Path(args.project).resolve()) if args.project else None

    def _one(results, key, default=0):
        """Extract a single aggregate value from a GROUP ALL result."""
        try:
            return results[0][0][key] if results and results[0] else default
        except (IndexError, KeyError, TypeError) as _e:
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


def register(subparsers):
    p = subparsers.add_parser("stats", help="Show detailed memory statistics")
    p.add_argument(
        "--project", type=str, default=None, help="Filter to a specific project directory"
    )
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    p.set_defaults(func=cmd_stats)
