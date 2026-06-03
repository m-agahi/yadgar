-- DuckDB analytics views for Yadgar memory corpus.
-- Loaded at export time into the .duckdb file.
-- Each view comes with a COMMENT describing the behavioral question it answers.
-- Re-apply with: duckdb <file.duckdb> < views.sql

-- 1. Heat decay distribution
CREATE OR REPLACE VIEW v_decay_distribution AS
WITH bucketed AS (
    SELECT heat, access_count,
        ntile(10) OVER (ORDER BY heat) AS bucket
    FROM memory
)
SELECT
    bucket,
    count(*) AS count,
    min(heat) AS min_heat,
    max(heat) AS max_heat,
    avg(heat) AS avg_heat,
    avg(access_count) AS avg_access_count
FROM bucketed
GROUP BY bucket
ORDER BY bucket;
COMMENT ON VIEW v_decay_distribution IS
    'Histogram of heat bucketed into 10 quantiles. Answers: what does decay look like?';

-- 2. Recall efficacy by tag
CREATE OR REPLACE VIEW v_recall_efficacy_by_tag AS
SELECT
    t.tag,
    count(DISTINCT al.id) AS recall_count,
    count(DISTINCT al.memory_id) AS distinct_memories_recalled,
    max(al.ts) AS last_recalled_at
FROM memory_tag t
LEFT JOIN action_log al ON al.memory_id = t.memory_id
    AND al.tool = 'recall'
GROUP BY t.tag
ORDER BY recall_count DESC;
COMMENT ON VIEW v_recall_efficacy_by_tag IS
    'Recall event counts by tag. Answers: which tags actually surface in recall?';

-- 3. Anchor usage
CREATE OR REPLACE VIEW v_anchor_usage AS
SELECT
    anchor_id,
    count(*) AS pinned_memories,
    max(heat) AS max_heat,
    avg(heat) AS avg_heat,
    max(last_accessed) AS last_accessed,
    bool_or(is_stale) AS any_stale
FROM memory
WHERE anchor_id IS NOT NULL AND anchor_id != ''
GROUP BY anchor_id
ORDER BY pinned_memories DESC;
COMMENT ON VIEW v_anchor_usage IS
    'Anchor-pinned memories: heat + staleness. Answers: are our anchors earning their pinning?';

-- 4. High-heat memories
CREATE OR REPLACE VIEW v_high_heat_memories AS
SELECT
    id,
    substr(content, 1, 120) AS content_snippet,
    heat,
    access_count,
    useful_count,
    tags,
    directory_context,
    last_accessed
FROM memory
ORDER BY heat DESC
LIMIT 100;
COMMENT ON VIEW v_high_heat_memories IS
    'Top-100 memories by current heat with content snippet. Answers: what is the hot core?';

-- 5. Domain clustering
CREATE OR REPLACE VIEW v_domain_clustering AS
SELECT
    mc.cluster_id,
    mc.centroid_label,
    mc.member_count,
    avg(m.heat) AS avg_heat,
    count(DISTINCT t.tag) AS distinct_tags
FROM memory_cluster mc
LEFT JOIN memory m ON m.cluster_id = CAST(mc.cluster_id AS VARCHAR)
LEFT JOIN memory_tag t ON t.memory_id = m.id
GROUP BY mc.cluster_id, mc.centroid_label, mc.member_count
ORDER BY mc.member_count DESC;
COMMENT ON VIEW v_domain_clustering IS
    'Memory clusters with heat + tag diversity. Answers: what semantic domains dominate?';

-- 6. Consolidation effect
CREATE OR REPLACE VIEW v_consolidation_effect AS
SELECT
    cl.id,
    cl.timestamp,
    cl.memories_added,
    cl.memories_updated,
    cl.memories_archived,
    cl.memories_deleted,
    cl.duration_ms,
    (SELECT avg(heat) FROM memory WHERE last_accessed <= cl.timestamp) AS avg_heat_at_run
FROM consolidation_log cl
ORDER BY cl.timestamp DESC;
COMMENT ON VIEW v_consolidation_effect IS
    'Consolidation log with corpus heat at run time. Answers: do nightly cycles move heat?';

-- 7. Conflict density
CREATE OR REPLACE VIEW v_conflict_density AS
SELECT
    t.tag,
    count(r.id) AS contradiction_count,
    max(r.valid_from) AS last_contradiction
FROM relationship r
JOIN memory m ON m.id = r.source_memory_id
JOIN memory_tag t ON t.memory_id = m.id
WHERE r.rel_type = 'contradicts'
GROUP BY t.tag
ORDER BY contradiction_count DESC;
COMMENT ON VIEW v_conflict_density IS
    'Contradiction relationship count per tag. Answers: where do contradictions cluster?';

-- 8. Wiki coverage
CREATE OR REPLACE VIEW v_wiki_coverage AS
SELECT
    tag_value AS tag,
    count(DISTINCT wp.id) AS page_count,
    max(wp.updated_at) AS last_updated,
    min(wp.created_at) AS oldest_page,
    count(DISTINCT CASE WHEN wp.approved THEN wp.id END) AS approved_count,
    count(DISTINCT CASE WHEN NOT wp.approved OR wp.approved IS NULL THEN wp.id END)
        AS unapproved_count
FROM wiki_page wp
CROSS JOIN UNNEST(CASE
    WHEN wp.tags IS NOT NULL
    THEN json_extract_string(wp.tags::JSON, '$[*]')
    ELSE []
END) AS t(tag_value)
GROUP BY tag_value
ORDER BY page_count DESC;
COMMENT ON VIEW v_wiki_coverage IS
    'Wiki pages grouped by tag with staleness. Answers: what is documented vs stale?';

-- 9. Tool call volume
CREATE OR REPLACE VIEW v_tool_call_volume AS
SELECT
    tool,
    strftime(ts, '%Y-%m-%d') AS day,
    count(*) AS call_count,
    count(DISTINCT directory_context) AS distinct_projects
FROM action_log
WHERE ts IS NOT NULL
GROUP BY tool, strftime(ts, '%Y-%m-%d')
ORDER BY day DESC, call_count DESC;
COMMENT ON VIEW v_tool_call_volume IS
    'MCP tool call volume by tool + day. Answers: which tools are actually used?';

-- 10. Branch distribution
CREATE OR REPLACE VIEW v_branch_distribution AS
SELECT
    coalesce(branch, '(canonical)') AS branch,
    count(*) AS memory_count,
    avg(heat) AS avg_heat,
    min(created_at) AS oldest,
    max(created_at) AS newest
FROM memory
GROUP BY coalesce(branch, 'master')
ORDER BY memory_count DESC;
COMMENT ON VIEW v_branch_distribution IS
    'Memory counts per branch. Answers: are non-master branches diverging?';
