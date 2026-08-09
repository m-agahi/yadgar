# Car M — cross-project `project=` param on MCP tools

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: A0, D, F
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Car M makes working in one project while filing/reading for another a first-class, explicit operation (§16.6). Today identity is a filesystem fact — `recall`/`memorize`/`wiki_read`/`adr_list` scope by an absolute `directory` path, so a memory written from `/home/max/git/yadgar` cannot be addressed from `/home/max/git/other-repo` without being checked out there. Once identity is a string key (`owner/repo`, shipped by Car A0), `quinyx/aws2slack` is addressable without being checked out.

This car adds an OPTIONAL `project: str | None = None` parameter to the MCP-facing tools:

- `recall()` and `memorize()` — `yadgar/core/server/tools/recall.py`, `memorize.py`
- `wiki_add` / `wiki_read` / `wiki_query` — `yadgar/core/server/tools/wiki.py`
- `adr_add` / `adr_list` (and the read-side `adr_get`) — `yadgar/core/server/tools/adr.py` (re-pointed by Car F)
- task tools — `yadgar/core/server/tools/task.py` (built by Car D; does NOT exist at HEAD today)

Default = the derived current project (from SessionStart — Car E extends `yadgar/core/hooks/session-start-context.py:37 main()`, NOT a new `session_start.py`). When a caller supplies `project=`, the override is VALIDATED against the `project` registry built by Car A0: an unknown project_id is REJECTED fail-loud (§16.5 decision 2026-08-08, amends ADR-0202). NOT a silent auto-create — auto-creating would manufacture phantom namespaces, the exact failure the registry exists to prevent.

**backend-bump: NO.** The MCP tools live in `core/server/tools/`, and `BACKEND_BUILD_DIRS=("backend",)` per `scripts/check_backend_bump.py:44`. The drainer per-item `project_id` derivation (`yadgar/backend/queue_drainer/apply.py:126`, already per-item) is a separate backend concern folded into Cars A0/L, NOT Car M. Car M touches only core.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/core/server/tools/recall.py` | ADD `project: str \| None = None` param to `recall()` at `recall.py:319`; thread it into the directory-scope resolution so a supplied `project` overrides the `directory`-derived scope | `recall.py:319` — signature `def recall(query, max_results=5, min_heat=0.0, profile=None, directory=None, type="all", mode=None, tags=None, max_chars=None)` confirmed via Read |
| `yadgar/core/server/tools/memorize.py` | ADD `project: str \| None = None` param to `memorize()` at `memorize.py:31`; when supplied, stamp it on the memory as `project_id` (replacing the `context`-as-directory derivation path) | `memorize.py:31` — signature `def memorize(content, context, tags, is_protected=False, provenance_agent=None, tier=None, valid_until=None, ttl_days=None, reason="", wait=False)` confirmed via Read; `context` is the directory path today |
| `yadgar/core/server/tools/wiki.py` | ADD `project: str \| None = None` to `wiki_add` (`wiki.py:296`), `wiki_read` (`wiki.py:626`), `wiki_query` (`wiki.py:521`) | signatures confirmed via Read: `wiki_add(title, content, category="reference", tags=None, source_memory_ids=None, confidence="medium", append=False, force=False, replace_slug=None, wait=False, directory=None, page_type=None, slug=None, upsert=True)` at `:296`; `wiki_query(query, tags=None, category=None, max_results=5, directory=None)` at `:521`; `wiki_read(slug, directory=None)` at `:626` |
| `yadgar/core/server/tools/adr.py` | ADD `project: str \| None = None` to `adr_add` (`adr.py:143`), `adr_list` (`adr.py:319`), `adr_get` (`adr.py:295`) | signatures confirmed via Read: `adr_add(directory, title, status, date, context, decision, rationale, alternatives, consequences, revisit_trigger, supersedes)` at `:143`; `adr_get(directory, adr_id)` at `:295`; `adr_list(directory, status=None, limit=50, offset=0)` at `:319`. NOTE: Car F re-points these from wiki-page reads to ledger-backed queries — Car M builds on the post-F shape. [VERIFY: Car F's final signatures when F lands; `directory` may be removed or repurposed by F — coordinate] |
| `yadgar/core/server/tools/task.py` | NEW (Car D deliverable) — ADD `project: str \| None = None` on the task MCP tools at build time | file does NOT exist at HEAD (`find yadgar -path "*server/tools/task.py"` → empty); `_validate_project_id` cited at `core/server/tools/task.py:68` in the master plan §16 is NOT present via grep. Car D builds it; Car M's `project=` is added during D's build, not retrofitted |
| `yadgar/core/server/tools/__init__.py` | RE-EXPORT any new helper (e.g. `_resolve_effective_project`) if one is extracted; `__all__` unchanged (tool names stay the same) | registration confirmed: `:11-12` import side-effects, `:31-32` re-export `memorize`/`recall`, `:66,69` `wiki_add`/`wiki_read`, `:124` `adr_add`/`adr_get`/`adr_list`, `:134-135,158-161,204-206` `__all__` entries |
| `yadgar/core/hooks/session-start-context.py` | NOT touched by Car M — Car E extends this to expose the derived project_id to the session. Car M CONSUMES the session-context value as the default for `project=` | `session-start-context.py:37 main()` confirmed present |

## 3. Functions / symbols

### New helper — effective-project resolution (core)

A small shared helper so the five tool modules do not each re-implement the default-vs-override+validate logic. Proposed home: `yadgar/core/server/tools/_project_param.py` (NEW) — [VERIFY: exact filename at build time; a private helper module matching the existing `_runtime_config.py` sibling convention].

```python
def resolve_effective_project(
    *,
    project: str | None,
    directory: str | None,
    session_project: str | None,   # from SessionStart context (Car E)
) -> str:
    """Resolve the effective project_id for a tool call and validate it.

    §16.6 / §16.11 Car M:
      1. If `project` is supplied → use it (override). Validate against the
         project registry (Car A0). Unknown → raise structured error (FAIL LOUD,
         not auto-create).
      2. Else if `session_project` is set (SessionStart context, Car E) → use it.
         Still validate — the session value is derived, but a derived key for a
         project whose registry row was since removed is still invalid.
      3. Else derive from `directory` via `derive_project_id()` (Car A0).
      4. None of the above → error (do NOT fall back to a sentinel).

    Returns the validated project_id string.

    Registry validation is a backend op (§15: core never touches the DB), so this
    helper calls the backend over HTTP, OR the registry is cached core-side via
    the PTC. [VERIFY: the validation transport — core must not hit MariaDB
    directly; route through the backend PTC (§15.1) once it exists, or accept
    that Car M lands a round-trip per call and the PTC caches it later]
    """
```

### Modified tool signatures (core)

Each tool gains `project: str | None = None` as a trailing keyword param (keyword-only to avoid breaking positional callers):

```python
# recall.py:319 — add after max_chars
def recall(
    query: str,
    max_results: int = 5,
    min_heat: float = 0.0,
    profile: str | None = None,
    directory: str | None = None,
    type: str = "all",
    mode: str | None = None,
    tags: list[str] | None = None,
    max_chars: int | None = None,
    *,
    project: str | None = None,
) -> list[dict]:
    ...

# memorize.py:31 — add after wait
def memorize(
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
    provenance_agent: str | None = None,
    tier: str | None = None,
    valid_until: str | None = None,
    ttl_days: int | None = None,
    reason: str = "",
    wait: bool = False,
    *,
    project: str | None = None,
) -> dict:
    ...

# wiki.py:296 — add after upsert
def wiki_add(
    title: str,
    content: str,
    category: str = "reference",
    tags: list[str] | None = None,
    source_memory_ids: list[int] | None = None,
    confidence: str = "medium",
    append: bool = False,
    force: bool = False,
    replace_slug: str | None = None,
    wait: bool = False,
    directory: str | None = None,
    page_type: str | None = None,
    slug: str | None = None,
    upsert: bool = True,
    *,
    project: str | None = None,
) -> dict:
    ...

# wiki.py:521 — add after directory
def wiki_query(
    query: str,
    tags: list[str] | None = None,
    category: str | None = None,
    max_results: int = 5,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> list[dict]:
    ...

# wiki.py:626 — add after directory
def wiki_read(
    slug: str,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    ...

# adr.py:143 — add after supersedes (Car F may reshape; coordinate)
def adr_add(
    directory: str,
    title: str,
    status: str,
    date: str,
    context: str,
    decision: str,
    rationale: str,
    alternatives: str,
    consequences: str,
    revisit_trigger: str,
    supersedes: str,
    *,
    project: str | None = None,
) -> dict:
    ...

# adr.py:295 / :319 — add project to adr_get / adr_list similarly
```

[VERIFY: `directory` and `project` interaction when BOTH are supplied — propose: `project` wins, `directory` is ignored with a warning logged. `directory` remains the only scope key when `project` is None. Confirm the precedence rule with the user at build time; it is a behavior choice, not a mechanical fact.]

### task tools (Car D, built with `project=` already)

`task.py` does not exist at HEAD. Car D builds the task MCP tools; Car M's `project=` is added during D's build, not retrofitted onto a shipped file. [VERIFY: coordinate with Car D author that the `project=` param + `resolve_effective_project` helper are wired at build time]

### Non-session writers (NOT Car M scope, noted for boundary)

- **Drainer** — derives `project_id` per-item from the enqueue-time `directory_context` at `yadgar/backend/queue_drainer/apply.py:126` (the `wiki_add` branch already fills `directory_context` per-item). Folded into Cars A0/L. Car M does NOT touch the drainer.
- **CLI** — derives from cwd via `derive_project_id()` (Car A0). CLI lives at `yadgar/core/cli/` (NOT `yadgar/cli/main.py` as the folded 0095 plan claimed — that path was wrong). [VERIFY: whether CLI tools gain a `--project` flag is a separate scope decision; not required for Car M's MCP-surface goal]

## 4. Build steps (TDD)

1. **RED** — `tests/core/test_project_param.py`: `resolve_effective_project(project="unknown/proj", directory=None, session_project=None)` raises a structured "unknown project" error when the registry has no row. Assert the error is NOT a silent auto-create (no `INSERT` observed). Mock the registry-lookup transport.
2. **GREEN** — implement `resolve_effective_project` in `yadgar/core/server/tools/_project_param.py` with the override → session → directory → error precedence. Route registry validation through the backend (HTTP to `admin_exec`, NOT direct MariaDB — §15).
3. **RED** — assert `resolve_effective_project(project=None, directory=<this repo>, session_project=None)` returns `m-agahi/yadgar` (host excluded) by calling the real `derive_project_id` from Car A0 (integration test, gated on A0 merged).
4. **RED** — `tests/core/test_recall_project_param.py`: `recall(query="x", directory=None, project="m-agahi/yadgar")` scopes results to that project_id's memories + global wiki. Assert a memory stamped with a different project_id does NOT appear. (Requires Car L's backfill to have stamped `project_id` on memories — this test is meaningful only post-L; mark it `@pytest.mark.skip` until L merges, but write it now.)
5. **GREEN** — thread `project` through `recall()`: when supplied, replace the `directory`-derived scope with the `project`-derived scope (project_id equality, NOT path equality). Fall back to the existing `directory` path when `project` is None (backward-compat).
6. **RED** — `tests/core/test_memorize_project_param.py`: `memorize(content=..., context=<dir>, tags=[], project="quinyx/aws2slack")` stamps `project_id="quinyx/aws2slack"` on the stored memory, NOT the directory-derived project. Assert the registry validation was called (mock).
7. **GREEN** — thread `project` through `memorize()`: when supplied, override the `context`-as-directory derivation and stamp the validated `project_id`.
8. **RED** — parallel tests for `wiki_add`, `wiki_read`, `wiki_query`: each honors `project=` for scoping/stamping.
9. **GREEN** — thread `project` through the three wiki tools.
10. **RED** — tests for `adr_add`/`adr_list`/`adr_get` honoring `project=`. These run against the POST-Car-F ledger-backed shape (coordinate with F's test fixtures).
11. **GREEN** — thread `project` through the adr tools.
12. **REFACTOR** — extract the common "validate project against registry" call so all five tool modules share `resolve_effective_project`; ensure no core module imports `_shared.storage` directly (§15 — core never touches the DB).

## 5. Acceptance gates

- [ ] `project=` parameter present on `recall`, `memorize`, `wiki_add`, `wiki_read`, `wiki_query`, `adr_add`, `adr_list`, `adr_get`, and the task tools (Car D) — all keyword-only, default `None`
- [ ] Unknown `project=` value is REJECTED with a structured error (FAIL LOUD); test proves no auto-INSERT (registry row count unchanged on a rejected call)
- [ ] `project=None` preserves the existing `directory`-scoped behavior byte-for-byte (backward-compat test on a pre-existing call)
- [ ] `recall(query, project="m-agahi/yadgar")` returns only memories stamped with that project_id + global wiki (integration test, post-L)
- [ ] `memorize(..., project="quinyx/aws2slack")` stamps `project_id="quinyx/aws2slack"` on the stored memory (not the cwd-derived project)
- [ ] core version bumped per WORKFLOW RULE (new core code: MCP tool signatures in `core/server/tools/`) — [VERIFY: exact core bump mechanism; `pyproject.toml:7` = `5.181.0` and `server.json:10` `version` = `5.181.0` today; `scripts/check_versions.py` enforces pyproject ↔ server.json ↔ docker-compose.yml consistency, NOT whether a bump is warranted — the WORKFLOW RULE governs "when to bump"]
- [ ] backend version NOT bumped (`scripts/check_backend_bump.py:44` `BACKEND_BUILD_DIRS=("backend",)`; Car M touches only `yadgar/core/server/tools/`)
- [ ] pre-commit green (ruff, import-linter — core must not import `_shared.storage` or `backend.*`)
- [ ] tests pass

## 6. Sequencing

- **Must merge AFTER A0** — `resolve_effective_project` validates against the `project` registry (table `003_project_registry`, built by A0) and calls `derive_project_id` (A0's `yadgar/core/identity.py`). Without A0, there is no registry to validate against and no derivation to fall back to.
- **Must merge AFTER D** — the task tools (`task.py`) are built by Car D; Car M's `project=` is wired into D's build. M's helper (`resolve_effective_project`) must exist when D's task tools are written, so the two cars are entangled: either M lands first (helper + the five non-task tools) and D wires the task tools against it, or they land together. [VERIFY: the train ordering — §7 lists "Depends on: A0, D, F" for M, implying D precedes M, but the helper must be available to D. Coordinate: ship M's helper as a standalone commit D can depend on, OR add `project=` to task tools in the same PR as D]
- **Must merge AFTER F** — Car F re-points `adr_add`/`adr_list`/`adr_get` from wiki-page reads to ledger-backed queries. Car M adds `project=` to the post-F shape; adding it to the pre-F wiki-page shape would be immediately thrown away.
- **Does NOT depend on L for the tool-surface work** — the `project=` param, default resolution, and registry validation all land against A0's registry. BUT: cross-project `recall(query, project=...)` is only MEANINGFUL once Car L has backfilled `project_id` onto existing memories; before L, recall-by-project_id returns only memories written AFTER M. The tool-surface can ship before L; the query usefulness waits on L. Flag in the PR if shipped before L.
- **Does NOT depend on E for the override path** — when a caller explicitly supplies `project=`, no SessionStart context is needed. The DEFAULT (no `project=`) consumes the session value from Car E's extension of `session-start-context.py:37`; until E lands, the default falls through to `derive_project_id(directory)`.

## 7. ADRs / decisions

- **§16.6** — cross-project work solved by the key, NOT a stateful mode. A per-call `project=` parameter is noisier and safer than `yadgar use <project>` (a forgotten mode writes to the wrong project silently — the exact failure §16.5 exists to prevent).
- **§16.5 decision (2026-08-08, amends ADR-0202)** — the `project` registry is the typo guard; an unknown `project_id` is REJECTED fail-loud. NOT `INSERT OR IGNORE` — auto-creating manufactures phantom namespaces. Car M's `resolve_effective_project` enforces this on the read/write tool surface.
- **D13/D14 (§16.1/§16.2)** — `project_id` = `owner/repo` from git remote (host excluded), `local/<basename>` fallback. Car M's default path derives via A0's `derive_project_id` when no `project=` is supplied.
- **§15 (ADR-0078, anchor #33)** — core NEVER touches the DB. Car M's registry validation MUST route through the backend (HTTP to `admin_exec`), NOT a direct MariaDB read. `resolve_effective_project` lives in core but delegates the lookup.
- **§16.11 Car M "backend-bump: NO"** — MCP tools are in `core/server/tools/`, not `backend/`; the drainer per-item derivation (backend) is a separate YES folded into A0/L.

## 8. Out of scope

- **Car A0** — `derive_project_id()`, the `project` registry table + FK, `_ensure_project_exists` backend guard. M consumes these.
- **Car D** — task tools (`task.py`). M's `project=` is wired into D's build.
- **Car F** — re-pointing `adr_*` from wiki-page reads to ledger-backed queries. M adds `project=` to the post-F shape.
- **Car E** — SessionStart hook extension (`session-start-context.py:37`) exposing the derived project_id to the session. M consumes the session value as the default.
- **Car L** — memory + wiki `directory_context`→`project_id` backfill + 194-page ADR re-slug. M's cross-project recall is only meaningful post-L, but M's tool-surface ships independently.
- **Drainer per-item `project_id` derivation** — `yadgar/backend/queue_drainer/apply.py:126` already fills `directory_context` per-item; deriving `project_id` from it is folded into A0/L (backend), NOT M.
- **CLI `--project` flag** — `yadgar/core/cli/` derives from cwd; adding a flag is a separate scope decision, not required for M's MCP-surface goal.
- **The transport for registry validation** (core→backend HTTP vs. PTC cache) — M ships a working path; the PTC caching of registry lookups is §15's backend-PTC scope (not yet built). [VERIFY: M may land a synchronous round-trip per validation; the PTC caches it later]

## 9. Risks / open questions

- [VERIFY: `directory` vs `project` precedence when BOTH are supplied — proposed: `project` wins, `directory` ignored with a warning. This is a behavior choice, not a mechanical fact; confirm with the user at build time.]
- [VERIFY: Car F's final `adr_add`/`adr_list`/`adr_get` signatures — F may remove or repurpose the `directory` param when re-pointing to the ledger. Car M's `project=` must be added to F's final shape, not the current wiki-page shape at `adr.py:143/295/319`. Coordinate within the train.]
- [VERIFY: Car D / Car M ordering — §7 says M depends on D, but M's `resolve_effective_project` helper must be available when D writes the task tools. Resolve by shipping M's helper as a standalone prerequisite commit (helper + the five non-task tools), then D wires task tools against it, then M's remaining tools land — OR add `project=` to task tools inside D's PR. The train ordering needs a concrete decision before D starts.]
- [VERIFY: registry-validation transport — core must not hit MariaDB directly (§15). The backend PTC (§15.1) does NOT exist yet. Car M either (a) ships a synchronous HTTP round-trip to `admin_exec` per `project=` validation, or (b) waits on the backend PTC. Option (a) is the safe path; the PTC caches it later. Confirm the `admin_exec` endpoint shape for a registry lookup at build time — `_ensure_project_exists` (A0) is the backend guard, but its calling convention from core over HTTP needs defining.]
- [VERIFY: core version bump — `pyproject.toml:7` and `server.json:10` are both `5.181.0` today; `scripts/check_versions.py` enforces three-way consistency (pyproject ↔ server.json ↔ docker-compose.yml `CORE_VERSION`), NOT bump policy. The WORKFLOW RULE governs when to bump. Confirm the rule's trigger for "new keyword param on existing MCP tools" — it is a core change, backend-bump is NO per §16.11.]
- [VERIFY: whether `project=` on `recall` should also scope the GLOBAL wiki blend (recall.py:353-368 wiki-blend branch fetches with NO directory arg today). A `project=` override should scope memories to that project_id but keep the global wiki blend global (wiki pages are not per-project in the current model — they carry `directory_context`, which Car L backfills to `project_id`). Decide: does `project=` on `recall` filter wiki results by project_id too, or keep them global? This is a behavior choice; confirm at build time.]
- **Cross-project recall usefulness waits on L** — M can ship the `project=` surface before L backfills `project_id` onto the ~2,919 existing memories, but `recall(query, project="m-agahi/yadgar")` returns only post-M new memories until L runs. Flag this in the PR if M ships before L.
