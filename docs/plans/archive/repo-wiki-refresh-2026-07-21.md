# Repo-Wiki Refresh Loop — host-side gen, crossref edges, validated ingest, cadence refresh (#83)

- status: **DRAFT — design agreed with user 2026-07-21; verify-then-build**
- branch: `feat/repo-wiki-refresh` off master @ v5.159.0
- version: **core-only** (repo_wiki + hooks + CLI + tool removal all under `yadgar/core/`), no backend bump expected
- task: #83. Delete of 345 stale fn-/mod- pages already DONE.
- principle (user): NO hacks/workarounds — fix the cause/design. If a surface doesn't work, remove or fix it, don't route around it.

## Root causes to fix (not work around)
1. **Container-blind MCP tools.** `repo_wiki_generate` + `wiki_coverage` (and `wiki_refresh_stale`, `server/tools/project.py`) run daemon-side (in the container) and cannot see the host repo → return `Not a directory` / `0 modules` / can't hash host source. A tool that structurally can't do its job is a trap. **Remove them.** Host source scanning is CLI-only (`yadgar repo-wiki` already works host-side).
2. **Generator emits content-less pages.** ~13/446 are empty `__init__.py` stubs (`# x **File:** __init__.py`, no signatures/docstring). Fix at the generator: **skip modules with no extractable content**.
3. **Generator emits NO crossref edges.** VERIFIED: no `[[...]]` anywhere; imports render as plain backticks (`generator.py:150`). So generated pages build ZERO graph edges. **Implement:** render imports that resolve to an in-repo module as `[[mod-<slug>]]` links → `wiki_add`'s crossref-sync builds the import graph as wiki edges automatically. Kills the old skill's separate edge/entity-file artifacts entirely (those raw dumps were extra schema surface).
4. **Ingestion via raw queue-dump → schema fragility.** The old skill dumped raw JSON (pages + edge files) into `~/.yadgar/queue/`; a schema/drainer upgrade before drain mismatches the backlog. **Fix:** ingest through the validated `wiki_add` path with **`wait=True`** → each page commits before the next → ≤1 item in flight → an upgrade can't catch a backlog. No raw queue writes, ever.
5. **Auto-refresh never wired.** `wiki_refresh_stale` exists but nothing calls it (and it's container-blind anyway). **Wire** a stop-hook `_MAINTENANCE_ITEMS` cadence item that drives `yadgar repo-wiki --stale-only` (user's Q2 design).

## Design (single coherent path)
**One surface = the host CLI `yadgar repo-wiki`.** It sees host source; the daemon owns the DB; the bridge is `wiki_add` (MCP, validated), called from the host client/agent.

- **Generation (host):** `yadgar repo-wiki --dry-run --json .` emits page dicts (each with `hash = SHA256(file bytes)`, `source_file`, `[[mod-]]` crossref links after Car A).
- **Ingestion (validated, no backlog):** for each page, `wiki_add(slug, content, title, tags, category="code", page_type="code", directory_context=<repo>, wait=True)`. Edges built by crossref-sync. NEVER write queue files directly.
- **Non-blocking:** the bulk (~430 pages) runs in a **background agent** (has Bash for the CLI + MCP for `wiki_add`) so the main instance never blocks. Steady-state refresh (few drifted pages) is cheap enough to run **inline** at the cadence.
- **Stale detection (host-side):** `--stale-only` computes host source hashes, fetches stored page hashes from the daemon (bulk slug→hash read — see open Q), diffs, emits only drifted/new pages. Replaces the container-blind `wiki_refresh_stale`.
- **Cadence (user Q2):** new `_MAINTENANCE_ITEMS` entry `repo-wiki-refresh` (slow cadence, e.g. every N checkpoints or when drift detected) → injects a prompt → main instance runs `yadgar repo-wiki --stale-only` and `wiki_add(wait=True)` the drifted pages. Same dumb-pipe pattern as anchor-audit + findings-curation.

## Cars (core-only)
- **Car A — generator fixes (source):** (1) skip content-less modules (no functions/classes AND empty/near-empty docstring → no page); (2) emit `[[mod-<slug>]]` links for imports resolving to an in-repo module (map import path → module slug; non-repo imports stay plain backtick). TDD: fixture repo → assert empty module skipped + in-repo import rendered as `[[mod-...]]` + external import stays backtick.
- **Car B — `--stale-only` + host-side stale detection:** CLI flag; compute host hashes; fetch stored hashes (bulk); emit only drifted/new. TDD: unchanged source → 0 pages; edited file → only its page.
- **Car C — remove container-blind MCP tools:** delete `repo_wiki_generate`, `wiki_coverage`, `wiki_refresh_stale` MCP registrations + their daemon-side impls (verify no live caller; the CLI + `--stale-only` replace them). Update I32/I25 gates + tests. Confirm the `/hooks/wiki-generate` ingest endpoint (`server/tools/repo_wiki.py`) writes VALIDATED (through the wiki write path), not raw-queue — if it raw-queues, either fix it to validated+wait or bypass it entirely in favor of the agent calling `wiki_add` directly.
- **Car D — stop-hook `repo-wiki-refresh` cadence item + prompt template:** add to `_MAINTENANCE_ITEMS`; new `repo_wiki_refresh_prompt.md` (LIST drift via `yadgar repo-wiki --stale-only` → `wiki_add(wait=True)` each → done). Renumber/step-count like the ADR-0156 curation add.
- **Post-merge (live, one-time):** background agent runs the full regen (~430 pages via `wiki_add(wait=True)`), schema-safe + non-blocking.

## Schema-safety rationale (the crux)
`wait=True` per page → synchronous commit → **at most one item queued at any instant** → a schema/drainer upgrade physically cannot catch a large stale backlog. This is the whole reason to abandon the raw-queue-dump. Slower per page, but off the critical path (background agent), and correct.

## Open questions (resolve in the relevant car)
1. **Bulk slug→hash read** for `--stale-only`: does `wiki_list` expose the stored `hash` field? If not, add a tiny read (one daemon call returning `{slug: hash}` for `page_type=code`) so the CLI can diff host-side without 430 `wiki_read`s.
2. **`/hooks/wiki-generate` ingest behavior** (`server/tools/repo_wiki.py`): validated write vs raw queue? Decides whether the CLI keeps POSTing there (if validated + wait) or the agent calls `wiki_add` directly (preferred if the endpoint raw-queues).
3. **`[[link]] resolution:** map an import dotted-path to a `mod-` slug reliably (only for in-repo modules); how to handle `from x import y` (link the module, not the symbol).
4. **Cadence interval** for `repo-wiki-refresh` (slower than the 25-msg checkpoint; drift is rare unless active refactor).
5. **Category/page_type + similarity gate:** 430 code pages through `wiki_add` — confirm the similarity gate doesn't false-reject near-identical thin modules (use `page_type="code"` + possibly `force` for regen of an existing slug via `replace_slug`).

## Not in scope
No separate edge/entity-file artifacts (edges come from `[[links]]`). No backend change. No re-introduction of the queue-dump path.

---

## REVISION (2026-07-21, post fresh-eyes opus review) — SUPERSEDES §Design/§Cars/§Open-Qs above where they conflict

**Headline: the native CLI ingest path NEVER worked.** `/hooks/wiki-generate` was never registered (`git log -S wiki-generate` = only the v5.78.0 CLI commit; absent from all 30 `custom_route`s). Every `_submit_page` POST 404s → counts as `failed`. The ~430 pages that existed came from the OLD external skill, NOT this code. There is no endpoint to fix/bypass — only dead code to delete.

**Schema-safety verdict (corrected):** there is NO ingest handler (not raw-queue, not validated — nonexistent; `server/tools/repo_wiki.py` is the MCP tool, returns dicts, never writes). `wiki_add(wait=True)` IS validated + synchronous (`wiki.py:496` → `_wiki_add_wait_path` polls the drainer to completion; secret/size/surrogate gates on the request thread; similarity gate pre-apply in the drainer). It is the correct bridge; the ≤1-in-flight rationale holds.

**New bugs (must fix):**
- **Slug collision → silent overwrite.** `_slugify` (`generator.py:42`) maps BOTH `.` and `_` → `-`, so `file-changed.py`/`file_changed.py`, `instructions-loaded.py`/`instructions_loaded.py`, `subagent-start.py`/`subagent_start.py` collide → same slug + `append=False` = data loss. Root cause: the hyphenated ones are non-importable hook SCRIPTS. Fix: only page IMPORTABLE (identifier-named) files → drops all 3 at source.
- **`hash`/`source_file` silently DROPPED by `wiki_add`.** Storage column exists (`_shared/storage/wiki.py:198`) but `WikiAddOptions` (`backend/write_exec/wiki_add_impl.py:37`) never carries them → never persisted. BLOCKS `--stale-only` (no stored hash to diff). → new prerequisite Car B0.
- **`category="code"` off-enum** (`generator.py:135,186`) → use `reference`. `page_type="code"` off-enum (advisory) → use `module`.

**Similarity gate:** default `WIKI_SIM_CONTENT_THRESHOLD=0.80`, `WIKI_SIM_MODE=hard`, on by default; `page_type` does NOT bypass. 430 thin near-identical code pages will hard-reject each other. Regen MUST use `replace_slug=<slug>` (existing) / `force=True` (new).

**Policy answers (corrected):**
- **Empty-page:** skip only if no functions AND no classes AND no docstring AND **no `__all__`** (scanner must ADD `__all__` extraction to `ModuleRecord` — else re-exporting `__init__.py` wrongly dropped). Enforce in the SCANNER via a `has_content`/skip flag; generator honors it.
- **Ignore — two layers in `scanner.scan_repo`:** (a) gitignore-aware (`git check-ignore --stdin` batched, or `pathspec`) + configurable extra-ignore, layered on `_should_skip_dir` (add `migrations/`, `*_pb2.py`, `*.pyi`, `alembic/versions`); (b) **first-party-only**: build the scanned module-name SET at scan time; page only importable (identifier-named) files.
- **Crossref resolution:** map import dotted-path → module via the scanned module-name SET (longest-prefix-match), NOT a slug round-trip (round-trip re-triggers the `_`/`.` collision). Link the MODULE for `from x.y import z`, never stdlib/third-party. (`pkg/__init__.py`→`pkg` is collision-safe; the real risk is `_`-vs-`-`, fixed by not paging non-identifier files.)
- **Multi-lang seam:** add an extension→extractor REGISTRY in `scan_repo` (`{".py": scan_python_module}`); `ModuleRecord` is already language-neutral; content-policy stays on `ModuleRecord`. Add the indirection ONLY — do NOT build a 2nd extractor now.

**Revised cars:**
- **Car A (bigger):** category→`reference` + page_type→`module`; `__all__` extraction + corrected empty-page rule; only-page-importable-files (kills the 3 collisions); gitignore + first-party ignore layers; set-based import→`[[link]]` resolver. TDD each.
- **NEW Car B0 (prereq to B):** forward `hash`/`source_file` through `wiki_add` → `WikiAddOptions` → drainer replay so they persist; add a bulk `{slug: hash}` read (`wiki_list` returns no hash, `wiki.py:860`). Without this, Car B has nothing to diff.
- **Car B:** `--stale-only` host-side hash-diff (depends on B0); regen writes via `replace_slug`/`force` to survive the 0.80 hard gate.
- **Car C (reframed):** DELETE the dead CLI submit code (`_submit_page`/`_submit_all_pages`/`_daemon_health_ok`/`_read_auth_token`, `cli/repo_wiki.py:34-116,159-181`) + remove the 3 container-blind MCP tools (`repo_wiki_generate`, `wiki_coverage`, `wiki_refresh_stale`). No "fix endpoint" work.
- **Car D:** unchanged (stop-hook `repo-wiki-refresh` cadence item + prompt).

Order: A → B0 → B → C → D. Post-merge: one-time bulk regen via background agent (`replace_slug`/`force`).

## ADDITIONS (2026-07-21, user) — discoverability + opt-in/no-nag cadence

**1. Root TOC/index page (Car A).** The generator emits a single navigable entry point `yadgar-repo-wiki-index` — the package/module tree with `[[mod-<slug>]]` links. One `wiki_read` → the whole code map → drill via crossrefs. This is the "know what you have access to" artifact (counts in the SessionStart catalog are not a usable map).

**2. ONE pointer-anchor (post-regen).** After the bulk regen, create a SINGLE project-anchored memory pointing at the TOC + usage: *"code-structure reference = mod-* wiki pages (AST signatures+docstrings, auto-refreshed); start at [[yadgar-repo-wiki-index]]; consult for 'where does X live' before grepping."* NOT a per-page or bare-"they exist" anchor — that duplicates the SessionStart wiki catalog. Honest caveat: this anchor's value is CONTINGENT on the refresh loop keeping pages trustworthy (stale pages are exactly why grep wins today); it is not a standalone win. The TOC page is the unconditional win.

**3. Car D cadence prompt — existence-branch + opt-out (no surprise bulk, no nag).**
The stop-hook `repo-wiki-refresh` injected prompt branches on per-project state:
- Check a per-project repo-wiki state marker (enabled / disabled / unset) for `{directory}`.
- **DISABLED** (user opted out) → no-op, skip silently.
- **ENABLED / pages exist** → auto-run `yadgar repo-wiki --stale-only`; `wiki_add(replace_slug=…)` the drifted pages. Usually nothing drifted → silent no-op. NEVER asks.
- **UNSET / no repo-wiki pages exist** → ASK the user: *"No code-structure wiki for {project} — want an initial full run (~N modules, in a background agent)? yes → run + mark enabled; no → mark disabled."* **On NO, record the opt-out marker** so the cadence does NOT re-ask every fire (else it nags). On YES, dispatch the background bulk-regen agent (`wiki_add(wait=True)`, `replace_slug`/`force`) + create the pointer-anchor (#2) + emit the TOC (#1).
- Marker mechanism (config knob vs per-directory memory/anchor/sentinel) decided in Car D build. Result: **opt-in per project, self-maintaining once in, never nags.**

---

## Car B — SHIPPED (2026-07-21) — `--stale-only` host-side hash diff (Car D references this)

**Interface** (`yadgar/core/cli/repo_wiki.py`):
- `yadgar repo-wiki [REPO] --stale-only --stored-hashes <path|-> --json`
- The CLI generates every page host-side (hashes included) and emits, as `--json`, ONLY the module pages whose SHA256 differs from the stored baseline (drifted) or that have no stored entry (new). It **NEVER contacts the daemon** (returns before the `_daemon_health_ok()`/`_submit_all_pages()` path — container boundary; keeps the CLI pure).
- `--stored-hashes` is the caller-supplied baseline `{slug: hash}` (JSON), read from a file or, with `-`, from stdin. Omitting it → empty baseline → every module page is new (first-run behaviour). Blank/empty input → `{}` (no crash).
- **Output JSON:** `{"stale_only": true, "pages": [<drifted/new module pages>], "deleted": [<slugs>], "toc_stale": bool, "total": N, "directory_context": <repo>}`.
  - `pages` = hash-bearing module pages only (drifted or new). The hashless `<project>-repo-wiki-index` TOC is **never** in `pages` (nothing to diff).
  - `deleted` = stored slugs with no matching generated module (source file removed).
  - `toc_stale` = True when the module SET changed (new or deleted modules); a content-only drift leaves the tree identical, so the TOC need not be regenerated.

**Flow (Car D prompt uses this):**
1. Caller builds `{slug: hash}` via MCP `wiki_list(directory)` (B0 returns `hash` per row) or the `list_wiki_hashes(directory)` helper.
2. Pipe it to `yadgar repo-wiki --stale-only --stored-hashes - --json`.
3. CLI emits only drifted/new pages + `deleted` + `toc_stale`.

**Regen write policy (the CALLER writes back; the host CLI stays read/generate-only):**
- For each page in `pages`: forward the stamped `hash` + `source_file` so `--stale-only` can diff again next cycle. To survive the 0.80 HARD similarity gate (near-identical thin code pages hard-reject each other):
  - EXISTING slug → `wiki_add(replace_slug=<slug>, hash=…, source_file=…, wait=True)`
  - NEW slug → `wiki_add(force=True, hash=…, source_file=…, wait=True)`
- For each slug in `deleted` → `wiki_delete(<slug>)`.
- When `toc_stale` is True, re-write the `<project>-repo-wiki-index` page from the full (non-stale) `--json` output.
- Same policy documented in the CLI module docstring + `--stale-only`/`--stored-hashes` argparse help.
