# Project identity implementation — task 0095 (2026-08-08)

**Status:** plan. **Owner:** m-agahi. **Blocks:** 0047 (ledger spine).

Two-phase plan. Phase 1 unblocks the spine immediately — identity derivation
function + `project_id` column on ledger tables, no registry, no migration.
Phase 2 completes the full 0095 scope (registry, memory/wiki migration,
cross-project params, hooks, non-session writers).

All decisions are made (ADR-0199, ADR-0202, spine D13/D14/D32/D33). This plan
is the build order, not a design doc.

## Phase 1 — Unblock spine (1 car, ~150 LOC)

What 0047 actually needs: a `project_id` value on every ledger row. The column
can default to the derived key; the FK to `project.key` and the registry table
ship in Phase 2. Per split-store §8.A1: "the gate is the first `config_set`,
NOT the schema — so a zero-row schema-only pilot may proceed with 0095 open."

### 1.1 Identity derivation

New file: `yadgar/_shared/identity.py`

```python
def derive_project_id(directory: str | Path) -> str:
    """Return owner/repo from git remote, or local/<basename>.

    Resolution order (ADR-0199):
    1. .yadgar/project-id walking up from directory (first hit wins)
    2. Normalised git remote origin URL → owner/repo
    3. local/<basename>

    Host is ALWAYS excluded. GitLab subgroups survive because we
    strip scheme+host+".git" and keep the full path — never split on "/".
    """
```

Key design constraints from ADRs:
- **Host excluded.** `git@github-personal:m-agahi/yadgar.git` → `m-agahi/yadgar`
- **No `/` splitting.** GitLab `gitlab.com/group/subgroup/team/repo.git` → `group/subgroup/team/repo`
- **`insteadOf` resolved before parsing.** Run `git config --get-regexp '^url\.'` in the directory, apply substitutions in order, then parse the resolved URL. This is the fragile part — see §1.4.
- **`.yadgar/project-id` walk.** Start at `directory`, check for `.yadgar/project-id`, ascend to `/`, return first hit. File contains one line: the project_id string. No TTY, no prompt — non-interactive always.
- **`local/<basename>` fallback.** `Path(directory).resolve().name`.
- **Result cached per session.** Caller passes it; the function is the fallback, not the hot path.

### 1.2 `.yadgar/` gitignore

One line in the global gitignore (`~/.config/git/ignore` or equivalent):
```
.yadgar/
```
Not `.yadgar/*` + `!.yadgar/project-id` — the directory is never committed (ADR-0199).

Add to `yadgar/_shared/config/config.py` as a documented path, and verify in
the agent gitconfig (`~/.config/git/agent`) that the global excludes file is
`includes`-ed so subagents also ignore it.

### 1.3 `project_id` column on ledger tables

Alembic migration (new revision in `alembic/versions/`):

```python
# revision: 0003_project_id
# depends_on: 0002_ledger_tables  (or whatever 0047 ships as 0001)

def upgrade():
    for table in ('task', 'adr', 'agent_prompt', 'agent_prompt_usage'):
        op.add_column(table, sa.Column(
            'project_id', sa.String(255), nullable=False,
            server_default='local/unknown'  # placeholder; derivation fills it
        ))
    # Composite index for the common query shape
    op.create_index('ix_task_project_status', 'task', ['project_id', 'status'])
    op.create_index('ix_adr_project_status', 'adr', ['project_id', 'status'])

def downgrade():
    for table in ('task', 'adr', 'agent_prompt', 'agent_prompt_usage'):
        op.drop_column(table, 'project_id')
```

**Column default is `'local/unknown'`** — a sentinel that means "derivation never ran."
The spine's write path calls `derive_project_id()` and stamps the real value.
The sentinel is never written in normal operation; it exists so the migration
doesn't fail on existing rows (there are none — the ledger tables are born in
the same train).

**No FK yet.** The `project` registry table doesn't exist in Phase 1. The FK is
added in Phase 2 when the registry lands. This is safe because the column is
only written by the spine's own `_LedgerMixin`, which always derives a valid key.

### 1.4 `insteadOf` resolution (the hard part)

Git's `insteadOf` can be configured at multiple levels. The resolution:

```python
def _resolve_insteadof(directory: str, url: str) -> str:
    """Apply git insteadOf rewrites to a remote URL."""
    # git config --get-regexp '^url\.' --includes
    # Returns lines like: url.git@github-personal:  git@github.com:
    # Meaning: "when you see the value, substitute the key (minus url. prefix)"
    # Apply longest-prefix-match first, then in config order for same-length
```

Test with the actual yadgar repo's config: `git@github-personal:m-agahi/yadgar.git`
must resolve to `git@github.com:m-agahi/yadgar.git` before parsing.

**Test strategy:** fixture that writes a temporary git config with `insteadOf`
entries, verifies the resolved URL matches expected. Test the codeberg
`codeberg-agent:` alias specifically — it's the live case that would break.

### 1.5 Phase 1 acceptance

- `derive_project_id("/home/max/git/yadgar")` → `"m-agahi/yadgar"`
- `derive_project_id("/tmp/some-random-dir")` → `"local/some-random-dir"`
- `.yadgar/project-id` override takes precedence over git remote
- `insteadOf` rewriting produces correct owner/repo for the codeberg-agent alias
- `project_id` column exists on all 4 ledger tables with the sentinel default
- No TTY prompt in any code path
- Full test suite passes; new tests are byte-pin (not substring) per house pattern

### 1.6 Phase 1 touched files

| File | Change |
|---|---|
| `yadgar/_shared/identity.py` | NEW — `derive_project_id()` + `_resolve_insteadof()` |
| `yadgar/tests/_shared/test_identity.py` | NEW — derivation + insteadOf + override + fallback |
| `alembic/versions/0003_project_id.py` | NEW — add `project_id` column to 4 tables |
| `yadgar/_shared/config/config.py` | `.yadgar/` path constant |
| `~/.config/git/ignore` | `.yadgar/` entry (manual, documented in plan) |

**Phase 1 does NOT touch:** memory, wiki, MCP tools, SurrealDB schema, the
`project` registry table, SessionStart hook, or any existing `directory_context` rows.

## Phase 2 — Full project identity (3 cars, ~800 LOC)

### Car 2A — `project` registry table + FK

**New Alembic revision:**

```python
# revision: 0004_project_registry

def upgrade():
    op.create_table('project',
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=True),
        sa.Column('owner_kind', sa.String(16), nullable=False,
                  server_default='user'),  # user|team|org
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('key'),
    )
    # Add FKs from ledger tables
    for table in ('task', 'adr', 'agent_prompt', 'agent_prompt_usage'):
        op.create_foreign_key(
            f'fk_{table}_project', table, 'project',
            ['project_id'], ['key']
        )
```

**Registry check on write (ADR-0202):** `_LedgerMixin` insert methods call
`_ensure_project_exists(project_id)` which does `INSERT OR IGNORE` into
`project`. This is load-bearing — a typo in `project_id` silently creates a
phantom namespace without it. The registry makes it a loud FK violation instead.

**Acceptance:**
- `INSERT INTO task (project_id) VALUES ('nonexistent/repo')` → FK error
- `_ensure_project_exists('m-agahi/yadgar')` → row exists, subsequent inserts succeed
- `project` table survives Alembic downgrade/upgrade cycle

**Touched files:** `alembic/versions/0004_project_registry.py` (new),
`yadgar/backend/storage/ledger_mixin.py` (add `_ensure_project_exists`),
`yadgar/tests/backend/test_project_registry.py` (new).

### Car 2B — Memory + wiki migration

This is the long pole. ~2,919 memories + ~2,237 wiki pages carry absolute-path
`directory_context`. They must gain a `project_id` column and be backfilled.

**Strategy (per ADR-0199 consequences):**

1. **Survey first.** Query `SELECT DISTINCT directory_context FROM memory`
   and `SELECT DISTINCT directory_context FROM wiki_page`. The distinct count,
   not the row count, is the real size of the job. Run this before writing any
   migration code.
2. **Three cases:**
   - Path exists and is a git repo → derive `project_id` from remote
   - Path exists but is NOT a git repo → `local/<basename>`
   - Path does NOT exist → **QUARANTINE** — set `project_id = 'quarantine/legacy'`
     and record the original path in a `migration_quarantine` table for human review
3. **Wiki re-slug (ADR-0202).** 194 ADR pages + their `[[crossrefs]]` must be
   re-slugged from `adr-NNNN` to `{project_id}_adr-NNNN`. The task-list page is
   deleted (tasks move to SQL). Cross-references in wiki bodies (`[[adr-NNNN]]`)
   must be updated to the new slug.
4. **Verification gate.** After migration: `SELECT COUNT(*) FROM memory WHERE
   project_id IS NULL` → 0. Same for wiki_page. `SELECT COUNT(*) FROM
   migration_quarantine` → recorded but not zero.

**Migration script:** `scripts/migrate_to_project_id.py` (runs ONCE, offline,
with both engines stopped per the backup quiesce protocol). Not an Alembic
migration — this touches SurrealDB rows, not MariaDB schema.

**Acceptance:**
- Dry-run mode reports distinct paths + case classification without mutating
- Live run backfills all rows; verification queries return 0 NULLs
- Quarantined rows are logged with original paths
- Wiki re-slug: `wiki_read("m-agahi_yadgar_adr-0199")` returns the ADR;
  `wiki_read("adr-0199")` returns not-found
- Cross-references in ADR bodies point to new slugs

**Touched files:** `scripts/migrate_to_project_id.py` (new),
`yadgar/tests/scripts/test_migrate_to_project_id.py` (new).

### Car 2C — Cross-project params + SessionStart hook + non-session writers

**Cross-project MCP tool parameters (ADR-0199):**

Add optional `project: str | None = None` parameter to:
- `recall()` — `yadgar/core/server/tools/recall.py`
- `memorize()` — `yadgar/core/server/tools/memorize.py`
- `wiki_add()` / `wiki_read()` / `wiki_query()` — `yadgar/core/server/tools/wiki.py`
- `adr_add()` / `adr_list()` — `yadgar/core/server/tools/adr.py`
- Task tools — `yadgar/core/server/tools/task.py`

Default: derived current project (from SessionStart hook). Override: explicit
`project=` parameter. The override is validated against the `project` registry
(Car 2A) — unknown project → reject with structured error, not silent create.

**SessionStart hook (ADR-0202):**

New hook: `yadgar/core/hooks/session_start.py` (or extend existing).

1. Derive `project_id` via `derive_project_id(cwd)`
2. Call `_ensure_project_exists(project_id)` (registry check)
3. Set `session.project_id` for the session lifetime
4. Monorepo detection: check for workspace markers (`pnpm-workspace.yaml`,
   `go.work`, `Cargo.toml` with `[workspace]`, multiple sibling `pyproject.toml`
   with distinct `[project].name`). If detected, emit a WARNING with the
   derived project_id and the list of detected subprojects — do NOT prompt,
   do NOT block. The user sets `.yadgar/project-id` overrides if needed.
5. **Non-TTY guard:** if `sys.stdin.isatty()` is False, skip any interactive
   step. Never call `input()`, `read`, or any blocking read. This is the
   task-0127 silent-EOF death one layer up.

**Non-session writers (ADR-0202 gap):**

Three paths have no session:
- **Nightly consolidation cycle:** runs as a systemd timer. Derive from
  `YADGAR_PROJECT_ID` env var, falling back to the config store's
  `project.key_override`, falling back to `derive_project_id(STATE_DIR)`.
  The env var is set in the systemd unit file.
- **Queue drainer:** processes memories from multiple projects. Each queued
  item carries its own `directory_context` → derive per-item. No global default.
- **CLI (`yadgar` command):** derive from `cwd`, same as session.

**Acceptance:**
- `recall("test", project="other/repo")` returns scoped results
- `memorize("x", project="nonexistent/repo")` → rejected (registry miss)
- SessionStart hook sets `project_id` without TTY
- Monorepo detection warns but doesn't block
- Nightly cycle reads `YADGAR_PROJECT_ID` env var
- Drainer derives per-item from `directory_context`

**Touched files:**
`yadgar/core/server/tools/recall.py` (add `project` param),
`yadgar/core/server/tools/memorize.py` (add `project` param),
`yadgar/core/server/tools/wiki.py` (add `project` param),
`yadgar/core/server/tools/adr.py` (add `project` param),
`yadgar/core/server/tools/task.py` (add `project` param),
`yadgar/core/hooks/session_start.py` (new or extend),
`yadgar/core/hooks/templates/session_start_prompt.md` (update),
`deploy/systemd/yadgar-nightly.service` (add `YADGAR_PROJECT_ID` env var),
`yadgar/core/queue_drainer.py` (per-item derivation),
`yadgar/cli/main.py` (cwd derivation).

## Sequencing

```
Phase 1 (1 car) ──► 0047 unblocked
                      │
Phase 2A (registry) ──┤
Phase 2B (migration) ─┤── can run in parallel after 2A
Phase 2C (params) ────┘
```

Phase 1 is independent — it touches only new files + Alembic. Phase 2A must
precede 2C (registry must exist before cross-project params validate against it).
Phase 2B is independent of 2A/2C but should run after the survey.

## What this plan deliberately defers

- **Tenancy columns** (`owner_kind`, `owner_id`, `reach`) — spine D17 defers to
  tenancy task; the `project` table has `owner_kind` as a column but the
  scope-filter hook is a no-op
- **`local` namespace collision** — accepted per ADR-0202; revisit if a real
  GitHub owner named `local` appears
- **Repo rename handling** — ADR-0199 accepts the split-identity consequence;
  `.yadgar/project-id` override is the manual fix
- **Slug collision** (`a_b/c` vs `a/b_c`) — accepted per ADR-0202; loud
  unique-index conflict at write time

## Acceptance gates (per car)

- commit hashes
- files/functions changed
- targeted red→green test evidence
- ONE final full-suite result with REAL exit code
- mutation test results for any guard
- backend-bump-needed flag (Phase 1: no — only Alembic migration, no backend
  code change. Phase 2A: no — same. Phase 2B: YES — migration script touches
  SurrealDB. Phase 2C: YES — MCP tool signature changes.)
- `## Yadgar findings` section

## ADR-0081/0082 archival

Per ADR-0081, the completing PR MUST archive this plan:
`git mv docs/plans/project-identity-implementation-2026-08-08.md docs/plans/archive/`.
Per ADR-0082, archival is the FIRST commit of the completing branch.
