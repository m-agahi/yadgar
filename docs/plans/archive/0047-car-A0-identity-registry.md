# Car A0 — identity derivation + project registry

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: shipped (Car A0 of 0047 spine train — code on `car/A0-identity-registry`)
> Depends on: — (root of the project-identity sub-chain)
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Car A0 is the root of the project-identity sub-chain (§16.11). It ships four things:

1. **`yadgar/core/identity.py`** (NEW) — `derive_project_id()` and helpers: resolve `insteadOf` rewrites, walk UP from cwd for `.yadgar/project-id`, derive `owner/repo` from the git remote with host excluded (§16.1, §16.4), and fall back to `local/<basename>` when no remote exists (§16.2). Core-side, NOT `yadgar/_shared/` (decision 2026-08-08). Composes with the existing `_resolve_project_root` / `_worktree_canonical_root` in `_shared/server_helpers/server_helpers.py` — does not duplicate them.
2. **`.yadgar/` global gitignore entry** — added to `~/.config/git/ignore` (verified absent today); the agent gitconfig (`~/.config/git/agent`) must include the excludes file so subagent sessions ignore `.yadgar/` too. Never committed (§16.3, D32 ②).
3. **`project` registry table + FK** — alembic revision `003_project_registry` in `yadgar/_shared/storage/sql/migrations/versions/` (the real versions dir; NOT the empty stub at `yadgar/_shared/storage/alembic/`). Creates `project` (key PK, display_name, kind ENUM(git|local), remote_url, created_at per §16.5) and adds FKs `task.project_id`/`adr.project_id` → `project.key`. The `project_id` columns themselves ship in Car A's `002_ledger_tables` (NO FK there); `003` only creates the `project` table and adds the FKs.
4. **`_ensure_project_exists(project_id)`** (backend) — registry check on write. FAIL LOUD (decision 2026-08-08, amends ADR-0202): REJECT an unknown `project_id` with a structured error. NOT `INSERT OR IGNORE` — auto-creating the row would manufacture phantom namespaces, the exact failure ADR-0202 says the registry exists to prevent (§16.5).

Tables carrying `project_id`: `task`, `adr` ONLY. `agent_pattern`/`agent_discipline`/`agent_pattern_composes` are reach-global (D3) — no `project_id`. (`agent_prompt_usage`, named in the folded 0095 plan, was fabricated — it does not exist.)

ADR-0202 amendment (wiki page slug `yadgar-adr-0202`): records enforcement = fail-loud. This is a **build-time wiki write**, NOT a repo file shipped in this car's commit — Car A0 ships the code; the ADR wiki amendment lands via `wiki_append_section`/`wiki_update` on `yadgar-adr-0202` when A0 completes. [VERIFY: cite the ADR's current text via `wiki_read("yadgar-adr-0202")` before writing the amendment — do not fabricate it.]

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/core/identity.py` | NEW — `derive_project_id()`, `_resolve_insteadof()`, `.yadgar/project-id` upward walk, `local/<basename>` fallback | `yadgar/core/` exists; `identity.py` absent at HEAD (confirmed via `ls`) |
| `~/.config/git/ignore` | ADD `.yadgar/` entry (plain form, NOT `.yadgar/*` + `!project-id`) | file exists (nix symlink); `.yadgar/` absent from current contents (only `.mcp.json`, `**/.claude/settings.local.json`) |
| `~/.config/git/agent` | ensure `core.excludesFile` points at the global ignore so subagent sessions ignore `.yadgar/` (§16.3 agent note) | file exists (nix symlink); `core.excludesFile` NOT set today (`git config --file ... --get core.excludesFile` → exit 1) — [VERIFY: this is nix-managed; the excludes wiring is a home-manager edit, NOT a repo edit — hand to user via MIGRATION_NOTES, do not edit nix files directly unless asked] |
| `yadgar/_shared/storage/sql/migrations/versions/003_project_registry.py` | NEW alembic revision — `revision="003_project_registry"`, `down_revision="002_ledger_tables"` (Car A's, not yet at HEAD); `upgrade()` creates `project` table + adds FKs on `task.project_id`/`adr.project_id`; `downgrade()` drops FKs + table | versions dir confirmed at `yadgar/_shared/storage/sql/migrations/versions/` (has `0001_config_table.py`); `003*` absent; `002*` absent (Car A deliverable) |
| `yadgar/_shared/storage/alembic/` | DO NOT USE — empty stub from PR #32 (only `__pycache__` + empty `versions/`) | confirmed empty `versions/` subdir |
| `yadgar/backend/admin_exec/project_registry.py` | NEW — `_ensure_project_exists(project_id)` backend guard (FAIL LOUD) | [VERIFY: exact filename is a build-time decision; existing `yadgar/backend/admin_exec/project.py` holds project-scoped write ops (`bootstrap_project_store` at `project.py:24`) — `_ensure_project_exists` is a registry guard, not a write op; a dedicated `project_registry.py` is proposed, confirm at build time] |
| `yadgar/backend/admin_exec/ledger.py` | NOT touched by A0 — Car A creates `_LedgerMixin` here; Car A wires `_ensure_project_exists` into the write path | confirmed absent at HEAD (Car A deliverable) |

## 3. Functions / symbols

### `yadgar/core/identity.py` (NEW — core)

```python
def derive_project_id(cwd: str | None = None) -> tuple[str, str]:
    """Resolve the project_id for the given directory (default: os.getcwd()).

    Returns (project_id, remote_url) where remote_url is the provenance URL
    (or "" for the local fallback).

    Resolution order (§16.2):
      1. .yadgar/project-id found by walking UP from cwd to the first hit
         → read its content (trim whitespace), that IS the project_id.
      2. else owner/repo from the git remote, normalised (§16.4):
         resolve insteadOf rewrites → strip scheme+host → strip trailing .git
         → lowercase. Host EXCLUDED. Use `origin`, documented overridable
         via .yadgar/project-id.
      3. else local/<basename>.
    """
```

- `def _resolve_insteadof(url: str) -> str` — resolve `git config --get-regexp '^url\..*\.insteadof$'` rewrites before parsing (Codeberg remotes rewrite to `codeberg-agent:` ssh alias; naive parse yields `codeberg-agent/<repo>`). [VERIFY: no existing insteadOf parser in `yadgar/` — confirmed via grep; this is genuinely new]
- `def _normalise_remote(url: str) -> str` — strip scheme, host, trailing `.git`, lowercase. Returns `owner/repo` (or `group/sub/.../repo` for nested namespaces — treat key as opaque path, never split on last `/` per §16.9).
- `def _walk_project_id_file(start: str) -> str | None` — walk UP from `start` to first `.yadgar/project-id`; return content or None.
- `def _local_fallback(cwd: str) -> str` — `local/<basename>`.

Composes with `_resolve_project_root` (`yadgar/_shared/server_helpers/server_helpers.py:187`) and `_worktree_canonical_root` (`server_helpers.py:268`) — call these to find the git root before reading the remote; do not re-implement worktree handling.

### `yadgar/backend/admin_exec/project_registry.py` (NEW — backend)

```python
def _ensure_project_exists(project_id: str, *, engine=None) -> None:
    """Registry check on write. FAIL LOUD (decision 2026-08-08, amends ADR-0202).

    SELECT 1 FROM project WHERE key = :project_id. If no row → raise a
    structured error (unknown project_id). NOT INSERT OR IGNORE — auto-creating
    the row would manufacture phantom namespaces (§16.5).

    Called by Car A's _LedgerMixin write path before stamping project_id on a
    task/adr row. A0 ships the function; Car A wires the call site.
    """
```

[VERIFY: exact error type — match the backend's structured-error convention; check `yadgar/backend/admin_exec/` for an existing error/exception pattern at build time]

### alembic `003_project_registry` (NEW)

```python
revision: str = "003_project_registry"
down_revision: str | None = "002_ledger_tables"  # Car A's revision (not yet at HEAD)

def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("key", sa.String(255), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(64), nullable=True),
        sa.Column("kind", sa.Enum("git", "local"), nullable=False),
        sa.Column("remote_url", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_foreign_key("fk_task_project", "task", "project", ["project_id"], ["key"])
    op.create_foreign_key("fk_adr_project", "adr", "project", ["project_id"], ["key"])

def downgrade() -> None:
    op.drop_constraint("fk_task_project", "task", type_="foreignkey")
    op.drop_constraint("fk_adr_project", "adr", type_="foreignkey")
    op.drop_table("project")
```

[VERIFY: `task` and `adr` table names + `project_id` column names must match Car A's `002_ledger_tables` exactly — confirm against Car A's doc when it lands; constraint names follow whatever convention 0001/002 establish]

## 4. Build steps (TDD)

1. **RED** — `tests/core/test_identity.py`: assert `derive_project_id()` returns `("m-agahi/yadgar", ...)` for this repo (origin remote), host excluded; assert `local/<basename>` for a temp dir with no `.git`; assert a `.yadgar/project-id` file at a parent dir overrides derivation; assert insteadOf rewrite is resolved (mock `git config --get-regexp`).
2. **GREEN** — implement `yadgar/core/identity.py`: `_resolve_insteadof`, `_normalise_remote`, `_walk_project_id_file`, `_local_fallback`, `derive_project_id`. Use `_resolve_project_root`/`_worktree_canonical_root` for git-root resolution.
3. **RED** — `tests/backend/test_project_registry.py`: assert `_ensure_project_exists("unknown/proj")` raises structured error when no row; assert no raise when row exists; assert it NEVER inserts (INSERT OR IGNORE is a bug, not a feature).
4. **GREEN** — implement `_ensure_project_exists` in `yadgar/backend/admin_exec/project_registry.py` (or confirmed location).
5. **RED** — alembic: assert `alembic upgrade head` on a fresh DB with 0001+002+003 creates `project` table with the §16.5 columns and the two FKs; assert `downgrade` drops FKs then table.
6. **GREEN** — write `003_project_registry.py` chaining `down_revision="002_ledger_tables"`. (Requires Car A's 002 file to exist for the migration to actually run — coordinate in the same train.)
7. **REFACTOR** — extract insteadOf parsing into a small pure function for testability; ensure `identity.py` imports nothing from `_shared.storage` (core must not touch the DB — §15).

## 5. Acceptance gates

- [ ] `derive_project_id()` returns `m-agahi/yadgar` for this repo (host excluded), verified by a passing test
- [ ] `.yadgar/` is gitignored in `~/.config/git/ignore` AND agent sessions see it ignored (excludes file wired in agent gitconfig) [VERIFY: nix/home-manager wiring — hand to user, do not edit nix directly]
- [ ] `_ensure_project_exists` REJECTS unknown project_id (FAIL LOUD); test proves it never INSERTs
- [ ] `003_project_registry` creates `project` table + FKs; `downgrade` reverses cleanly
- [ ] `identity.py` lives in `yadgar/core/` (NOT `yadgar/_shared/`); imports no storage modules
- [ ] alembic revision file lives in `yadgar/_shared/storage/sql/migrations/versions/` (NOT `yadgar/_shared/storage/alembic/`)
- [ ] backend version bumped per WORKFLOW RULE (new backend code: `_ensure_project_exists`); core version NOT bumped on `identity.py` alone per §16.11 "backend-bump: YES (registry _ensure_project_exists is backend code; identity.py itself is core → NO on its own)" [VERIFY: exact bump mechanism — `pyproject.toml:7` is `5.181.0` today; no `server.json` found at HEAD; the §13.2 "server.json 5.61.0→5.61.1" reference may be stale — confirm the version file at build time]
- [ ] pre-commit green (ruff, import-linter — core must not import `_shared.storage`)
- [ ] tests pass
- [ ] ADR-0202 wiki amendment written (`wiki_append_section`/`wiki_update` on slug `yadgar-adr-0202`) recording enforcement = fail-loud [VERIFY: read current ADR text first]

## 6. Sequencing

- **A0 is the root** — no car must merge before it.
- **Car A depends on A0** — Car A's `_LedgerMixin` write path needs `derive_project_id()` (A0 code) to stamp `project_id`, and needs `_ensure_project_exists` (A0 backend) to guard writes. "A0 precedes A" is a code-availability statement (§16.5).
- **A0's alembic `003` FOLLOWS Car A's `002`** — `down_revision="002_ledger_tables"`. The `project_id` column ships in `002` (Car A, NO FK); `003` (A0) creates the `project` table and adds the FKs. So A0 and A are entangled in the same train: A0's code can merge first, but `003` only runs after `002` exists.
- **Cars L and M depend on A0** — L (memory+wiki backfill) needs `derive_project_id()`; M (cross-project `project=` param) validates against the `project` registry that A0 creates.
- **No car waits on A0's ADR-0202 wiki amendment** — that is a build-time doc write, not a code dependency.

## 7. ADRs / decisions

- **D13** (§16.1) — `project_id` = `<owner>/<repo>` from git remote, host excluded (`m-agahi/yadgar`, not `github.com/m-agahi/yadgar`). Identity survived the Codeberg→GitHub move; a host-qualified key would have orphaned every row.
- **D14** (§16.1/§16.2) — `local/<basename>` fallback for non-git dirs. Weakness doesn't bite: a repo with no remote isn't shared, so portability is moot where the fallback is weak.
- **D32 ②** (§16.3) — `.yadgar/` is NEVER committed; global gitignore, plain `.yadgar/` form (NOT `.yadgar/*` + `!project-id`). A committed identity file is a remote-controlled FK into a private memory store — `git pull` re-keys silently.
- **D32 ③** (§16.9/§16.11) — `body_slug` = `{project_id}_adr-NNNN` with `/`→`_`; config store stays path-keyed (Car L re-slugs; A0 only ships the derivation that makes the slug globally unique).
- **ADR-0199** (wiki) — fixed the identity KEY as owner/repo, host excluded. ADR-0202 amends it (slug + resolution timing). Car A0 implements the key + registry; the fail-loud amendment is recorded in ADR-0202 at build time.
- **ADR-0202** (wiki, slug `yadgar-adr-0202`) — amendment added by this car: enforcement = FAIL LOUD. The registry check on write REJECTS unknown `project_id` with a structured error. NOT `INSERT OR IGNORE`. [VERIFY current ADR text before amending]
- **§16.5 decision (2026-08-08)** — the `project` registry is the typo guard; `task.project_id`/`adr.project_id` become FKs to `project.key`; FAIL LOUD on unknown key.

## 8. Out of scope

- **Car A** — `_LedgerMixin`, `002_ledger_tables` alembic revision, `runtime_config` table, ledger tables, backend `admin_exec/ledger.py`. A0 ships `identity.py` + `_ensure_project_exists` + `003`; Car A wires the call sites and ships `002`.
- **Car L** — memory + wiki `directory_context`→`project_id` backfill + 194-page ADR re-slug + quarantine. A0 ships the derivation; L applies it at scale.
- **Car M** — cross-project `project=` parameter on recall/memorize/wiki/adr/task tools, validated against A0's `project` registry. A0 ships the registry; M consumes it.
- **Car E** — SessionStart hook extension (`yadgar/core/hooks/session-start-context.py`) to expose the derived key + monorepo prompt. A0 ships `derive_project_id()`; E calls it. The monorepo-detection logic reuses `_find_subproject_boundaries` (`yadgar/core/seed/_analysis.py:248`) — NOT A0 scope.
- **The ADR-0202 wiki amendment text** — build-time follow-up write, not a repo file in this car's commit.
- **nix/home-manager edits** for the agent gitconfig excludes-file wiring — hand to user via `MIGRATION_NOTES.md`; do not edit nix files directly.

## 9. Risks / open questions

- [VERIFY: `_validate_project_id` cited at `core/server/tools/task.py:68` in the master plan §16 — that file does NOT exist at HEAD (`yadgar/core/server/tools/task.py` absent; `_validate_project_id` not found via grep in `yadgar/`). The current "no validation" claim in §16 is correct in spirit but the cited coordinate is stale — possibly from the closed PR #32 tree. Confirm at build time whether a validator exists anywhere.]
- [VERIFY: `server.json` version-bump target — the §13.2 review cited `server.json 5.61.0→5.61.1` but no `server.json` exists under `yadgar/` at HEAD. The only version found is `pyproject.toml:7` = `5.181.0`. Confirm the exact backend/core version bump mechanism against WORKFLOW RULE before bumping.]
- [VERIFY: exact home for `_ensure_project_exists` — proposed `yadgar/backend/admin_exec/project_registry.py` (NEW); the existing `yadgar/backend/admin_exec/project.py` holds write ops like `bootstrap_project_store`. A dedicated registry-guard file is cleaner but confirm at build time.]
- [VERIFY: `_resolve_insteadof` must run `git config --get-regexp '^url\..*\.insteadof$'` — confirm the agent gitconfig (`~/.config/git/agent`) chains the real global config so insteadOf rules resolve under agent sessions; §16.3 notes agent sessions use `GIT_CONFIG_GLOBAL=~/.config/git/agent`.]
- [VERIFY: `002_ledger_tables` exact revision id and the `task`/`adr` table+column names — `003`'s `down_revision` and FK column references must match Car A's `002` file exactly. Coordinate within the train.]
- [VERIFY: nix-managed `~/.config/git/ignore` and `~/.config/git/agent` are symlinks into `/nix/store` — editing them directly will be overwritten by home-manager rebuild. The `.yadgar/` ignore entry and excludes-file wiring must be added in the nix config (`modules/home/git.nix` or equivalent), handed to the user via `MIGRATION_NOTES.md`. Do not edit the symlink targets directly.]
- **Sequencing entanglement** — A0's code (identity.py, _ensure_project_exists) must merge before Car A's write path can use it, but A0's `003` alembic revision chains after Car A's `002`. Both ship in the same train; order the commits so identity.py + _ensure_project_exists land before _LedgerMixin's call sites, and the alembic chain (001→002→003) is internally consistent.
