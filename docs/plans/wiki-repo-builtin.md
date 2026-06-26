# PLAN — Wiki-repo BUILT-IN + shared stale-hash (absorbs #36 residual)

**Status:** PLANNED (NEW, v5.85 train car #36). Scoped 2026-06-26, code-grounded
against `master`. Riskiest car — read the "car vs train" call below.
**Theme:** wiki / repo-docs / staleness / external-skill removal
**Effort:** M (module-only, recommended scope) · XL (full fn/index parity — its OWN
train, do NOT attempt in one car) · **Risk:** M→XL by scope

---

## The actual problem (re-grounded — the original framing was incomplete)

The #36 residual is "the in-repo stale-wiki checker over-reports because it can't
reproduce the external repo-wiki skill's hashes." True, but verification found a
**deeper, store-level gap that the hash framing hides:**

- **The in-repo checker reads DISK, not the DB.** `_scan_stale_wiki_slugs`
  (`yadgar/server/tools/project.py` ~2027) globs `<dir>/.local-review/wiki/*.md` and
  reads YAML frontmatter via `_is_wiki_page_stale` (`project.py:2000`): it pulls
  `fm.get("hash")` (`:2015`) + `source_file`/`source_files`/`sources` (`:2018`), then
  `_compute_source_hash(source_files)` (`:2023`).
- **`repo_wiki_generate` writes the DB, not `.local-review/`.** The tool
  (`yadgar/server/tools/repo_wiki.py:31`) **returns page dicts** (`{slug, title,
  content, tags, category:"code", page_type:"code", directory_context}`) for the
  caller to submit via `wiki_add` → SurrealDB / file-queue. Its output dict carries
  **no `hash` and no `source_file`** (docstring `:42-58`; the `TODO (post-T6): auto-
  submit via wiki_add` at `:7-18,59` confirms it does NOT touch `.local-review/`).
- **`.local-review/wiki/*.md` is written by the EXTERNAL repo-wiki skill**
  (`~/.claude/plugins/.../repo-wiki/skills/repo-wiki/SKILL.md`), whose hash spec is:
  module/file = `SHA256(full file content)`; **function = `SHA256(sig+body)`**;
  **index = `SHA256(serialised IR section)`** (SKILL.md "Hashing Strategy"). The
  external skill is what the in-repo checker was implicitly built to read.

**Consequence — the original #36 design is INSUFFICIENT as stated.** "Make generation
built-in so the checker shares the hash algo" does NOT kill the over-report by itself:
built-in generation writes a store (DB) the checker never reads (disk). You must ALSO
**bridge the stores**. That bridge — not the hash algorithm — is what decides
car-vs-train.

## Where the hashes actually diverge (verified, per page type)

| Page type | External skill hash | In-repo checker hash | Match? |
|---|---|---|---|
| **module / file** | `SHA256(file content)` | `SHA256(file bytes)` (`_compute_source_hash`, file branch) | **YES** (UTF-8 files) |
| **function** | `SHA256(sig.strip()+body.strip())` per fn | whole-file or dir-manifest hash — **no fn concept** | **NO → always stale** |
| **index** (overview/architecture) | `SHA256(serialised IR section)` (LLM-synth driven) | dir-manifest hash | **NO → always stale** |

So module pages already agree on the algorithm; fn + index pages are structurally
unreproducible by the in-repo checker (it has no IR pipeline, no fn-granularity).

## Recommended scope for car #36 — MODULE-ONLY + store bridge (Effort M)

Make `repo_wiki_generate` the built-in source of truth for **module/code pages**,
stamp each with a hash the checker can read, and point the checker at the same store.
Drop fn/index parity (defer — see "car vs train").

1. **Generator stamps a hash.** In `yadgar/repo_wiki/generator.py` (the module-page
   builder), compute `SHA256(file bytes)` of the module source and include
   `"hash": <hex>` + `"source_file": <abs path>` in each page dict. This is the SAME
   algorithm the checker's file branch already uses (`_compute_source_hash`) — so a
   module page generated built-in is, by construction, checkable.
2. **`wiki_add` persists the hash.** Pass `hash` + `source_file` through to storage.
   The `wiki_page` table is SCHEMALESS (`storage/wiki.py` insert/update) — add the
   two fields (no migration needed; just write + read them).
3. **Checker reads the DB, not disk** (the bridge). Add a DB-backed path to
   `_scan_stale_wiki_slugs` / `_is_wiki_page_stale`: for built-in code pages, read
   the stored `hash` + `source_file` from the wiki store and compare to the live
   `_compute_source_hash(source_file)`. Keep the disk `.local-review/` scan as a
   fallback for externally-authored pages (don't break existing behavior).
4. **Stop over-reporting external fn/index pages:** the checker should only flag a
   page stale if it KNOWS how to hash its sources. For external fn/index pages whose
   hash it cannot reproduce, mark them **"external-sourced, not tracked"** rather than
   stale (kills the over-report without pretending to reproduce the skill's hash).

Net: built-in module pages are accurately staleness-tracked via a shared algorithm;
external fn/index pages stop being false-positives; the external-skill dependency is
dropped *for module/code pages* (the bulk of repo-wiki value).

## Car vs train — the honest call

- **Module-only (above): ONE CAR, Effort M.** Hash algorithm already aligns; the work
  is the store bridge (generator stamp → wiki_add passthrough → checker DB path) +
  the "external = not tracked" classification. ~3 focused changes + tests.
- **Function pages: ITS OWN TRAIN, Effort XL.** Requires a fn-granularity IR pipeline
  in Python (or shelling to the external Rust `repo-indexer`) to compute
  `SHA256(sig+body)` per function, plus per-fn storage. Do NOT fold into car #36.
- **Index pages: ITS OWN TRAIN, Effort L-XL.** Index hashes derive from LLM-synthesis
  / serialised IR — non-deterministic across model/prompt. Needs either deterministic
  IR-input hashing or synthesis-version metadata. Defer.

**Recommendation: ship module-only as car #36. Flag fn/index as a follow-up train
("repo-wiki IR parity") only if losing fn/index built-in generation is unacceptable —
the external skill can keep producing them in the meantime.**

## TDD outline (write failing first)

Patterns: `tests/test_repo_wiki.py` (`fixture_repo` tmp Python modules, generator
asserts; note `:251-256` already asserts `directory_context` ≠ 'global') +
`tests/test_wiki_refresh_stale.py` (`:155-174` hash-match; `:381-431` dir source;
`:453-478` pycache-churn-ignored).

1. `test_generator_stamps_hash` — `generate_wiki_pages(repo)` output dicts each carry
   `hash` = `SHA256(module file bytes)` + `source_file`. *(red first.)*
2. `test_wiki_add_persists_hash` — `wiki_add(page_with_hash)` → `wiki_read(slug)` /
   store query returns the stored `hash` + `source_file`.
3. `test_checker_db_path_not_stale_when_match` — built-in page whose stored `hash`
   matches the live file hash → NOT flagged stale (DB path, no `.local-review` file).
4. `test_checker_db_path_stale_on_drift` — mutate the source file → page flagged stale.
5. `test_external_fn_index_not_overreported` — an external `.local-review` page whose
   hash the checker can't reproduce is classified "external-sourced, not tracked",
   NOT stale (codifies the #36 residual fix).
6. `test_module_hash_matches_checker_algo` — assert generator's `SHA256(file bytes)`
   == checker's `_compute_source_hash(source_file)` for the same file (the alignment
   that makes shared-hash work).

## Contracts / config touched

- **Wiki store schema (additive):** new `hash` + `source_file` fields on `wiki_page`
  (schemaless — no `DEFINE FIELD` migration strictly needed, but add for clarity).
- **No I25 knob required** for module-only scope.
- **Staleness signal path** (`_compute_stale_wiki_count`, the I8/I9 signals hot path,
  TTL-cached) — gains a DB read; keep it cheap / cached.
- **No BEHAVIOR_CONTRACT row change** (staleness is an internal signal, not an
  outcome BC). The recall/wiki BCs must stay green.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Store bridge breaks existing disk-scan behavior | keep disk `.local-review` scan as fallback; DB path is additive, gated to built-in `page_type:"code"` pages |
| Non-UTF-8 source files → module hash divergence | Python modules are UTF-8 in practice; read bytes (not decoded text) on both sides so they agree exactly |
| Scope creep into fn/index IR pipeline | explicit "car vs train" wall above; fn/index = separate train |
| Signals hot-path latency from DB reads | TTL cache already exists (`STALE_COUNT_CACHE_TTL_S`); reuse it |
| "external = not tracked" silently hides genuinely stale external pages | acceptable for #36 (kills the over-report); revisit if external pages must be tracked → that's the IR-parity train |

## How this goes wrong like C1/C2

The C1-trap here is taking the original framing at face value — "just share the hash
algorithm" — implementing the generator stamp, and declaring victory while the
checker still globs `.local-review/` and never sees the DB pages. Over-report
unchanged → a committed-but-ineffective car (C2-class). **The store bridge (step 3)
is the load-bearing change, not the hash.** The C2-trap is folding fn/index parity in
and shipping a half-built IR pipeline. The car-vs-train wall is the guard.

## Related
- `repo_wiki_generate` (`server/tools/repo_wiki.py`), `generate_wiki_pages`
  (`repo_wiki/generator.py`), `_compute_source_hash` / `_scan_stale_wiki_slugs`
  (`project.py:2000-2070`), external skill `repo-wiki/SKILL.md`,
  adr-capture-system.md Bug A (the `source_file`-singular frontmatter key).
