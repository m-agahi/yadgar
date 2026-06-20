"""stats subcommand — detailed memory statistics."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Shared utilities ───────────────────────────────────────────────────────────


def _one(results, key, default=0):
    """Extract a single aggregate value from a GROUP ALL result."""
    try:
        return results[0][0][key] if results and results[0] else default
    except IndexError, KeyError, TypeError:
        return default


def _count(results):
    return _one(results, "count", 0)


def _q(db, project, sql, sql_proj):
    """Run sql_proj when project is set (binding {p: project}), else sql."""
    if project:
        return db.query(sql_proj, {"p": project})
    return db.query(sql)


# ── Stats data container ───────────────────────────────────────────────────────


@dataclass
class StatsData:
    # Core counts
    total: int = 0
    active: int = 0
    stale: int = 0
    archived: int = 0
    protected: int = 0
    # Types
    episodic: int = 0
    semantic: int = 0
    # Compression
    comp_0: int = 0
    comp_1: int = 0
    comp_2: int = 0
    # Heat
    heat_min: float = 0.0
    heat_avg: float = 0.0
    heat_max: float = 0.0
    heat_buckets: list = field(default_factory=list)
    # Access
    total_accesses: int = 0
    avg_accesses: float = 0.0
    max_accesses: int = 0
    total_useful: int = 0
    never_accessed: int = 0
    # Temporal
    oldest: Any = None
    newest: Any = None
    last_accessed: Any = None
    age_days: Any = None
    # Projects breakdown
    project_rows: list = field(default_factory=list)
    # Consolidation
    total_consolidations: int = 0
    last_consol: Any = None
    avg_duration: float = 0.0
    # Knowledge graph
    entity_count: int = 0
    rel_count: int = 0
    causal_edges: int = 0
    # Action log
    action_total: int = 0
    action_unprocessed: int = 0
    # Subsystems
    cluster_count: int = 0
    narrative_count: int = 0
    triggers_active: int = 0
    triggers_fired: int = 0
    # Tags
    top_tags: list = field(default_factory=list)
    # v6 Phase 0.2 — Data-quality metrics
    dq_null_embedding_count: int = 0
    dq_embedding_valid_ratio: float = 0.0
    dq_duplicate_rate: float = 0.0
    dq_zombie_rate: float = 0.0
    dq_domain_coverage: float = 0.0
    dq_surprise_p50: float | None = None
    dq_surprise_p95: float | None = None


# ── HTTP path ─────────────────────────────────────────────────────────────────


def _print_http_summary(data, project):
    """Print brief summary from daemon HTTP response."""
    header = f"=== Yadgar Stats{f' — {project}' if project else ''} ==="
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


def _try_http_path(args):
    """Try the daemon HTTP endpoint. Return True if handled, False to fall through."""
    import json
    import os as _os
    import urllib.parse
    import urllib.request

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
            _print_http_summary(data, args.project)
        return True
    except Exception:
        return False  # daemon not running or unreachable — fall back to direct DB


# ── DB query helpers ───────────────────────────────────────────────────────────


def _query_core_counts(db, project, sd):
    """Populate sd with total, active, stale, archived, protected."""
    total_res = _q(
        db,
        project,
        "SELECT count() FROM memory GROUP ALL",
        "SELECT count() FROM memory WHERE directory_context = $p GROUP ALL",
    )
    sd.total = _count(total_res)

    sd.active = _count(
        _q(
            db,
            project,
            "SELECT count() FROM memory WHERE is_stale = false AND heat >= 0.05 GROUP ALL",
            "SELECT count() FROM memory WHERE directory_context = $p "
            "AND is_stale = false AND heat >= 0.05 GROUP ALL",
        )
    )
    sd.stale = _count(
        _q(
            db,
            project,
            "SELECT count() FROM memory WHERE is_stale = true GROUP ALL",
            "SELECT count() FROM memory WHERE directory_context = $p AND is_stale = true GROUP ALL",
        )
    )
    sd.archived = _count(
        _q(
            db,
            project,
            "SELECT count() FROM memory WHERE heat < 0.05 GROUP ALL",
            "SELECT count() FROM memory WHERE directory_context = $p AND heat < 0.05 GROUP ALL",
        )
    )
    sd.protected = _count(
        _q(
            db,
            project,
            "SELECT count() FROM memory WHERE is_protected = true GROUP ALL",
            "SELECT count() FROM memory WHERE directory_context = $p "
            "AND is_protected = true GROUP ALL",
        )
    )


def _query_type_breakdown(db, project, sd):
    """Populate sd with episodic, semantic."""
    sd.episodic = _count(
        _q(
            db,
            project,
            "SELECT count() FROM memory WHERE store_type = 'episodic' GROUP ALL",
            "SELECT count() FROM memory WHERE directory_context = $p "
            "AND store_type = 'episodic' GROUP ALL",
        )
    )
    sd.semantic = _count(
        _q(
            db,
            project,
            "SELECT count() FROM memory WHERE store_type = 'semantic' GROUP ALL",
            "SELECT count() FROM memory WHERE directory_context = $p "
            "AND store_type = 'semantic' GROUP ALL",
        )
    )


def _query_compression_levels(db, project, sd):
    """Populate sd with comp_0, comp_1, comp_2."""
    for attr, lvl in [("comp_0", 0), ("comp_1", 1), ("comp_2", 2)]:
        res = _q(
            db,
            project,
            f"SELECT count() FROM memory WHERE compression_level = {lvl} GROUP ALL",
            f"SELECT count() FROM memory WHERE directory_context = $p "
            f"AND compression_level = {lvl} GROUP ALL",
        )
        setattr(sd, attr, _count(res))


def _query_heat_stats(db, project, sd):
    """Populate sd with heat_min, heat_avg, heat_max, heat_buckets."""
    heat_res = _q(
        db,
        project,
        "SELECT math::min(heat) AS min_h, math::mean(heat) AS avg_h, "
        "math::max(heat) AS max_h FROM memory GROUP ALL",
        "SELECT math::min(heat) AS min_h, math::mean(heat) AS avg_h, "
        "math::max(heat) AS max_h FROM memory "
        "WHERE directory_context = $p GROUP ALL",
    )
    sd.heat_min = _one(heat_res, "min_h", 0) or 0
    sd.heat_avg = _one(heat_res, "avg_h", 0) or 0
    sd.heat_max = _one(heat_res, "max_h", 0) or 0

    buckets = []
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
        buckets.append((label, _count(br)))
    sd.heat_buckets = buckets


def _query_access_stats(db, project, sd):
    """Populate sd with access stats."""
    access_res = _q(
        db,
        project,
        "SELECT math::sum(access_count) AS total_ac, "
        "math::mean(access_count) AS avg_ac, "
        "math::max(access_count) AS max_ac FROM memory GROUP ALL",
        "SELECT math::sum(access_count) AS total_ac, "
        "math::mean(access_count) AS avg_ac, "
        "math::max(access_count) AS max_ac FROM memory "
        "WHERE directory_context = $p GROUP ALL",
    )
    useful_res = _q(
        db,
        project,
        "SELECT math::sum(useful_count) AS total_uc FROM memory GROUP ALL",
        "SELECT math::sum(useful_count) AS total_uc FROM memory "
        "WHERE directory_context = $p GROUP ALL",
    )
    never_res = _q(
        db,
        project,
        "SELECT count() FROM memory WHERE access_count = 0 GROUP ALL",
        "SELECT count() FROM memory WHERE directory_context = $p AND access_count = 0 GROUP ALL",
    )
    sd.total_accesses = _one(access_res, "total_ac", 0) or 0
    sd.avg_accesses = _one(access_res, "avg_ac", 0) or 0
    sd.max_accesses = _one(access_res, "max_ac", 0) or 0
    sd.total_useful = _one(useful_res, "total_uc", 0) or 0
    sd.never_accessed = _count(never_res)


def _query_temporal_stats(db, project, sd):
    """Populate sd with temporal stats."""
    from datetime import UTC, datetime

    temporal_res = _q(
        db,
        project,
        "SELECT math::min(created_at) AS oldest, math::max(created_at) AS newest, "
        "math::max(last_accessed) AS last_acc FROM memory GROUP ALL",
        "SELECT math::min(created_at) AS oldest, math::max(created_at) AS newest, "
        "math::max(last_accessed) AS last_acc FROM memory "
        "WHERE directory_context = $p GROUP ALL",
    )
    sd.oldest = _one(temporal_res, "oldest", None)
    sd.newest = _one(temporal_res, "newest", None)
    sd.last_accessed = _one(temporal_res, "last_acc", None)

    now = datetime.now(UTC)
    if sd.oldest:
        try:
            oldest_str = str(sd.oldest)
            oldest_dt = datetime.fromisoformat(oldest_str.replace("Z", "+00:00"))
            sd.age_days = (now - oldest_dt).days
        except Exception:
            pass


def _query_project_breakdown(db, sd):
    """Populate sd.project_rows (only when no project filter)."""
    proj_res = db.query(
        "SELECT directory_context, count() AS cnt, math::mean(heat) AS avg_h, "
        "math::max(created_at) AS last_created FROM memory "
        "WHERE directory_context != '' "
        "GROUP BY directory_context ORDER BY cnt DESC LIMIT 15"
    )
    sd.project_rows = proj_res[0] if proj_res else []


def _query_consolidation(db, sd):
    """Populate sd with consolidation stats."""
    sd.total_consolidations = _count(db.query("SELECT count() FROM consolidation_log GROUP ALL"))
    last_consol_res = db.query(
        "SELECT timestamp, duration_ms, memories_added, memories_archived "
        "FROM consolidation_log ORDER BY id DESC LIMIT 1"
    )
    sd.last_consol = last_consol_res[0][0] if last_consol_res and last_consol_res[0] else None
    avg_dur_res = db.query(
        "SELECT math::mean(duration_ms) AS avg_d FROM consolidation_log GROUP ALL"
    )
    sd.avg_duration = _one(avg_dur_res, "avg_d", 0) or 0


def _query_knowledge_graph(db, sd):
    """Populate sd with knowledge graph stats."""
    try:
        sd.entity_count = _count(db.query("SELECT count() FROM entity GROUP ALL"))
        sd.rel_count = _count(db.query("SELECT count() FROM relationship GROUP ALL"))
    except Exception:
        sd.entity_count = sd.rel_count = 0
    try:
        sd.causal_edges = _count(db.query("SELECT count() FROM causal_dag_edge GROUP ALL"))
    except Exception:
        sd.causal_edges = 0


def _query_action_log(db, sd):
    """Populate sd with action log stats."""
    try:
        sd.action_total = _count(db.query("SELECT count() FROM action_log GROUP ALL"))
        sd.action_unprocessed = _count(
            db.query("SELECT count() FROM action_log WHERE processed = false GROUP ALL")
        )
    except Exception:
        sd.action_total = sd.action_unprocessed = 0


def _query_subsystems(db, sd):
    """Populate sd with subsystem stats (clusters, narratives, triggers)."""
    try:
        sd.cluster_count = _count(db.query("SELECT count() FROM memory_cluster GROUP ALL"))
    except Exception:
        sd.cluster_count = 0
    try:
        sd.narrative_count = _count(db.query("SELECT count() FROM narrative_entry GROUP ALL"))
    except Exception:
        sd.narrative_count = 0
    try:
        sd.triggers_active = _count(
            db.query("SELECT count() FROM prospective_memory WHERE is_active = true GROUP ALL")
        )
        trig_fired_res = db.query(
            "SELECT math::sum(triggered_count) AS total_fired FROM prospective_memory GROUP ALL"
        )
        sd.triggers_fired = _one(trig_fired_res, "total_fired", 0) or 0
    except Exception:
        sd.triggers_active = sd.triggers_fired = 0


def _query_top_tags(db, project, sd):
    """Populate sd.top_tags."""
    if project:
        tags_res = db.query(
            "SELECT tags FROM memory WHERE directory_context = $p",
            {"p": project},
        )
    else:
        tags_res = db.query("SELECT tags FROM memory")
    tag_counts: dict[str, int] = {}
    for row in tags_res[0] if tags_res else []:
        try:
            for tag in row.get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        except Exception:
            pass
    sd.top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:10]


# ── v6 Phase 0.2 — Data-quality query ────────────────────────────────────────


def _parse_surprise_scores(rows) -> list[float]:
    """Extract finite surprise_score floats from a SELECT result (best-effort)."""
    scores: list[float] = []
    if not (rows and rows[0]):
        return scores
    for row in rows[0]:
        sv = row.get("surprise_score")
        if sv is None:
            continue
        try:
            scores.append(float(sv))
        except Exception:  # noqa: BLE001 - skip non-numeric
            pass
    return scores


def _dq_null_embedding(db, sd, total):
    try:
        null_count = _count(
            db.query(
                "SELECT count() FROM memory WHERE is_stale = false AND embedding IS NONE GROUP ALL"
            )
        )
        sd.dq_null_embedding_count = null_count
        sd.dq_embedding_valid_ratio = (total - null_count) / total
    except Exception:  # noqa: BLE001 - best-effort telemetry
        pass


def _dq_duplicate(db, sd, total):
    try:
        sim_links = _count(db.query("SELECT count() FROM memory_similarity_link GROUP ALL"))
        sd.dq_duplicate_rate = sim_links / total
    except Exception:  # noqa: BLE001 - best-effort telemetry
        pass


def _dq_zombie(sd, total):
    total_with_stale = total + sd.stale
    if total_with_stale > 0:
        sd.dq_zombie_rate = sd.stale / total_with_stale


def _dq_domain(db, sd, total):
    try:
        domain_count = _count(
            db.query(
                "SELECT count() FROM memory WHERE is_stale = false AND domain IS NOT NONE GROUP ALL"
            )
        )
        sd.dq_domain_coverage = domain_count / total
    except Exception:  # noqa: BLE001 - best-effort telemetry
        pass


def _dq_surprise(db, sd):
    import statistics as _stats  # noqa: PLC0415

    try:
        surp_rows = db.query(
            "SELECT surprise_score FROM memory "
            "WHERE is_stale = false AND surprise_score IS NOT NONE "
            "AND surprise_score > 0 LIMIT 5000"
        )
    except Exception:  # noqa: BLE001 - best-effort telemetry
        return
    scores = _parse_surprise_scores(surp_rows)
    if not scores:
        return
    sd.dq_surprise_p50 = _stats.median(scores)
    sd.dq_surprise_p95 = _stats.quantiles(scores, n=20)[18] if len(scores) >= 20 else max(scores)


def _query_data_quality(db, sd):
    """Populate sd with Phase-0.2 data-quality fields (best-effort telemetry)."""
    total = sd.total  # already computed by _query_core_counts
    if total == 0:
        return
    _dq_null_embedding(db, sd, total)
    _dq_duplicate(db, sd, total)
    _dq_zombie(sd, total)
    _dq_domain(db, sd, total)
    _dq_surprise(db, sd)


# ── Output renderers ───────────────────────────────────────────────────────────


def _build_json_output(sd):
    """Build the JSON output dict from StatsData."""
    data = {
        "total": sd.total,
        "active": sd.active,
        "stale": sd.stale,
        "archived": sd.archived,
        "protected": sd.protected,
        "episodic": sd.episodic,
        "semantic": sd.semantic,
        "compression": {"raw": sd.comp_0, "gist": sd.comp_1, "tag": sd.comp_2},
        "heat": {
            "min": sd.heat_min,
            "avg": sd.heat_avg,
            "max": sd.heat_max,
            "buckets": {b[0]: b[1] for b in sd.heat_buckets},
        },
        "access": {
            "total": sd.total_accesses,
            "avg": sd.avg_accesses,
            "max": sd.max_accesses,
            "useful": sd.total_useful,
            "never_accessed": sd.never_accessed,
        },
        "temporal": {
            "oldest": sd.oldest,
            "newest": sd.newest,
            "last_accessed": sd.last_accessed,
            "age_days": sd.age_days,
        },
        "consolidation": {
            "total": sd.total_consolidations,
            "avg_duration_ms": sd.avg_duration,
        },
        "knowledge_graph": {
            "entities": sd.entity_count,
            "relationships": sd.rel_count,
            "causal_edges": sd.causal_edges,
        },
        "action_log": {"total": sd.action_total, "unprocessed": sd.action_unprocessed},
        "clusters": sd.cluster_count,
        "narratives": sd.narrative_count,
        "triggers": {"active": sd.triggers_active, "fired": sd.triggers_fired},
        "top_tags": dict(sd.top_tags),
        "data_quality": {
            "null_embedding_count": sd.dq_null_embedding_count,
            "embedding_valid_ratio": round(sd.dq_embedding_valid_ratio, 4),
            "duplicate_rate": round(sd.dq_duplicate_rate, 4),
            "zombie_rate": round(sd.dq_zombie_rate, 4),
            "domain_coverage": round(sd.dq_domain_coverage, 4),
            "surprise_p50": round(sd.dq_surprise_p50, 4)
            if sd.dq_surprise_p50 is not None
            else None,
            "surprise_p95": round(sd.dq_surprise_p95, 4)
            if sd.dq_surprise_p95 is not None
            else None,
        },
    }
    if sd.project_rows:
        data["projects"] = [
            {
                "directory": r["directory_context"],
                "count": r["cnt"],
                "avg_heat": round(r["avg_h"] or 0, 4),
                "last_created": r["last_created"],
            }
            for r in sd.project_rows
        ]
    return data


def _print_memories_section(sd):
    print("MEMORIES")
    print(f"  Total:     {sd.total}")
    print(f"  Active:    {sd.active}")
    print(f"  Stale:     {sd.stale}")
    print(f"  Archived:  {sd.archived}")
    print(f"  Protected: {sd.protected}")
    print()


def _print_types_section(sd):
    print("TYPES")
    print(f"  Episodic:  {sd.episodic}")
    print(f"  Semantic:  {sd.semantic}")
    print(f"  Raw:       {sd.comp_0}  |  Gist: {sd.comp_1}  |  Tag: {sd.comp_2}")
    print()


def _print_heat_section(sd):
    print("HEAT")
    print(f"  Min: {sd.heat_min:.4f}  |  Avg: {sd.heat_avg:.4f}  |  Max: {sd.heat_max:.4f}")
    for label, count in sd.heat_buckets:
        bar = "#" * min(count, 40)
        print(f"  {label:20s} {count:5d}  {bar}")
    print()


def _print_access_section(sd):
    print("ACCESS")
    print(f"  Total recalls:   {sd.total_accesses}")
    print(f"  Avg per memory:  {sd.avg_accesses:.1f}")
    print(f"  Max on a single: {sd.max_accesses}")
    print(f"  Rated useful:    {sd.total_useful}")
    print(f"  Never accessed:  {sd.never_accessed}")
    print()


def _print_temporal_section(sd):
    print("TEMPORAL")
    if sd.age_days is not None:
        print(f"  Memory span:     {sd.age_days} days")
    print(f"  Oldest:          {sd.oldest or 'n/a'}")
    print(f"  Newest:          {sd.newest or 'n/a'}")
    print(f"  Last accessed:   {sd.last_accessed or 'n/a'}")
    print()


def _print_consolidation_section(sd):
    print("CONSOLIDATION")
    print(f"  Total cycles:    {sd.total_consolidations}")
    print(f"  Avg duration:    {sd.avg_duration:.0f}ms")
    if sd.last_consol:
        print(f"  Last run:        {sd.last_consol['timestamp']}")
        print(
            f"    Added: {sd.last_consol['memories_added']}  "
            f"Archived: {sd.last_consol['memories_archived']}  "
            f"Duration: {sd.last_consol['duration_ms']}ms"
        )
    print()


def _print_table_output(sd, project):
    """Render human-readable table output."""
    header = f"=== Yadgar Stats{f' — {project}' if project else ''} ==="
    print(header)
    print()

    _print_memories_section(sd)
    _print_types_section(sd)
    _print_heat_section(sd)
    _print_access_section(sd)
    _print_temporal_section(sd)

    if sd.project_rows:
        print("PROJECTS (top 15)")
        for r in sd.project_rows:
            print(f"  {r['cnt']:5d} memories  heat={r['avg_h'] or 0:.4f}  {r['directory_context']}")
        print()

    _print_consolidation_section(sd)

    print("KNOWLEDGE GRAPH")
    print(f"  Entities:        {sd.entity_count}")
    print(f"  Relationships:   {sd.rel_count}")
    print(f"  Causal edges:    {sd.causal_edges}")
    print()

    print("SUBSYSTEMS")
    print(f"  Clusters:        {sd.cluster_count}")
    print(f"  Narratives:      {sd.narrative_count}")
    print(f"  Active triggers: {sd.triggers_active}  (fired {sd.triggers_fired} times)")
    print(f"  Action log:      {sd.action_total} total, {sd.action_unprocessed} unprocessed")
    print()

    if sd.top_tags:
        print("TOP TAGS")
        for tag, count in sd.top_tags:
            print(f"  {count:5d}  {tag}")
        print()

    # v6 Phase 0.2 — Data quality section
    print("DATA QUALITY (v6 Phase 0.2)")
    print(
        f"  Null embeddings:  {sd.dq_null_embedding_count}  (valid ratio: {sd.dq_embedding_valid_ratio:.1%})"
    )
    print(f"  Duplicate rate:   {sd.dq_duplicate_rate:.4f}  (sim-links / active memories)")
    print(f"  Zombie rate:      {sd.dq_zombie_rate:.1%}  (stale / total)")
    print(f"  Domain coverage:  {sd.dq_domain_coverage:.1%}  (memories with domain assigned)")
    if sd.dq_surprise_p50 is not None:
        print(f"  Surprise p50:     {sd.dq_surprise_p50:.4f}")
        print(
            f"  Surprise p95:     {sd.dq_surprise_p95:.4f}"
            if sd.dq_surprise_p95 is not None
            else ""
        )
    else:
        print("  Surprise scores:  n/a (no non-zero scores found)")
    print()


# ── DB direct-access path ──────────────────────────────────────────────────────


def _run_db_path(args):
    """Run the direct DB access path. Exits on error."""
    import json as _json

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

    sd = StatsData()

    try:
        db = Surreal(f"surrealkv://{db_path}")
        db.use("yadgar", "main")

        _query_core_counts(db, project, sd)

        if sd.total == 0:
            label = f"project {project}" if project else "database"
            print(f"No memories in {label}.", file=sys.stderr)
            sys.exit(0)

        _query_type_breakdown(db, project, sd)
        _query_compression_levels(db, project, sd)
        _query_heat_stats(db, project, sd)
        _query_access_stats(db, project, sd)
        _query_temporal_stats(db, project, sd)
        if not project:
            _query_project_breakdown(db, sd)
        _query_consolidation(db, sd)
        _query_knowledge_graph(db, sd)
        _query_action_log(db, sd)
        _query_subsystems(db, sd)
        _query_top_tags(db, project, sd)
        _query_data_quality(db, sd)

    except Exception as e:
        print(f"Failed to query database: {e}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(_json.dumps(_build_json_output(sd), indent=2))
        return

    _print_table_output(sd, project)


# ── Public entry point ─────────────────────────────────────────────────────────


def cmd_stats(args):
    """Show detailed memory statistics.

    Tries the running daemon's HTTP endpoint first (works when server is in Docker).
    Falls back to direct DB access when no daemon is reachable.
    """
    if _try_http_path(args):
        return
    _run_db_path(args)


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
