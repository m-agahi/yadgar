# PLAN v5.53 — Knowledge-Base Usability (umbrella) — make Claude read-first

Status: PLANNED 2026-06-12. UMBRELLA across sub-versions v5.53.0 → v5.53.3. Diagnostic source: wiki `[[yadgar-knowledge-base-usability-rca-why-claude-doesn-t-read-firs]]` (RCA 2026-06-12, 3 parallel investigations). User-reported: "Claude doesn't read yadgar first, re-greps info yadgar already has."

## Root cause (from RCA)

Yadgar optimized for WRITING, never for READING or CURATION. 646 wiki pages ingested, but no discoverable INDEX, no consistent SCHEMA, no live CURATION loop → write-only landfill. Four compounding faces:
- **#2 bootstrap** surfaces only 3 bare slugs (`server/tools/project.py:301`); MCP `instructions` is one vague sentence (`server/_app.py:54`); `wiki_list`/`wiki_coverage` never advertised. → Claude blind to the corpus.
- **#1 rules** mis-scoped + downstream of #2 (`wiki_query` ~0.34; reliable read-first needs a slug needs the catalog).
- **#3 format** drift: `fn-*` = 41% of corpus in ≥3 shapes (generation-batch drift, NOT directory); duplicates; zero schema enforcement.
- **#4 stale/update** dead/advisory: `stale_wiki_count` HARDCODED 0 (`project.py:1314`); `wiki_refresh_stale` writes a queue nothing reads; 0.80 dedup fragments; no write-back forcing function; no post-write verification.

## Strategy

Break the vicious cycle at leverage points, cheapest-first. Each phase ships independently and delivers value alone. Daemon has NO LLM → all regeneration/write-back is Claude-in-loop via hooks (same pattern as Idea 6 Tier-2), never autonomous.

| Sub-version | Phase | Scope | Effort | Why this order |
|---|---|---|---|---|
| **v5.53.0** | A + D | Bootstrap catalog + honest read-first contract | S–M | Keystone — unblocks #1, makes corpus visible. Cheapest, highest leverage. |
| **v5.53.1** | C | Live curation loop (revive stale signal, dedup-as-update, write-back hook, diff) | M | Restores trust/freshness so reading is worth it. |
| **v5.53.2** | B-schema | Page types + templates + format lint | M | Stops NEW drift immediately; makes the catalog parseable. |
| **v5.53.3** | B-migration | Normalize/dedup the 646-page corpus to types | L | The heavy low-glamour tail. Schema-forward (5.53.2) already delivers without it. |

---

## v5.53.0 — Phase A + D: Bootstrap catalog + honest rule (keystone)

**Problem:** Claude can't read-first because it doesn't know what exists.

**Changes:**
1. **Catalog in `project_brief` (catalog/restore modes).** Replace the 3-bare-slug "Wiki Keys" render (`server/tools/project.py:297-305`) with a compact INDEX: wiki pages grouped by `category`/`page_type` with **titles** (not bare slugs), per-group counts, total page count, and a one-line "yadgar knows N pages on this repo." Source from `wiki_list` (enumerate slug+title+category+updated) — cheap, not the noisy `wiki_query`. Cap length sensibly (top-N per group + "…M more, call `wiki_list(category=…)`").
2. **Server-advertised read-first contract.** Rewrite the FastMCP `instructions` string (`server/_app.py:54`) from one vague sentence to: what yadgar holds (memories + curated wiki), and "Before searching a repo for structure/conventions/decisions/where-code-lives, consult the wiki index (`wiki_list` / the session-start catalog) and `wiki_read` the relevant page; reserve `wiki_query` for fuzzy topic search; grep for exact current code lines." This reaches the model even when CLAUDE.md doesn't.
3. **Honest read-first rule (D) — TWO delivery forms, both required:**
   - **D-general (ships IN yadgar, for every user):** the new read-first guidance, written once as canonical text yadgar provides to all users — a `docs/RECOMMENDED_CLAUDE_RULES.md` (or a section in the install README) that any yadgar user copies into their `~/.claude/CLAUDE.md`. The rule: *"Yadgar wiki = the map (conventions, module purpose, past decisions, where subsystems live); grep = the territory (exact current code lines). At session start you get a wiki catalog — read the relevant page first, then grep there. Use `wiki_list`→slug→`wiki_read` for named pages; reserve `wiki_query` for fuzzy topic search (it scores ~0.34, not for coordinates)."* This is the "normal user Claude rules change."
   - **D-personal (Max's nix TODO — NOT general):** Max's `~/.claude/CLAUDE.md` is nix-managed; the source is `~/git/nix/dotfiles/common/claude.md` (the yadgar block, lines ~6-33: "Read-first triggers" + "Tool selection"). Max must hand-apply the same rule there + `home-manager switch`. See the **User TODO** section below. (We do NOT edit Max's nix dotfiles or global CLAUDE.md ourselves — prepared text only.)
4. Surface `wiki_coverage` in the catalog when cheap (which modules ARE documented) so gaps are visible.

**TDD:** project_brief catalog output includes titles+counts grouped by category (not bare slugs); index covers all categories present; length-capped; MCP instructions string asserts the read-first contract keywords. **Acceptance:** a fresh session shows Claude a real table-of-contents of repo knowledge. **Effort:** S–M. Core release.

---

## v5.53.1 — Phase C: Live curation loop

**Problem:** stale/missed info has no remediation path.

**Changes:**
1. **Revive `stale_wiki_count`.** Un-hardcode `project.py:1314` (and 1407). Compute real count from the existing hash-drift logic in `wiki_refresh_stale` (`project.py:~1744-1772`) — wire its `stale` list length into the signal. This resurrects the dead stop-hook path ("stale_wiki_count>0 → dispatch repo-wiki update").
2. **Make `wiki_refresh_stale` close the loop.** Either the stop-hook reads the queue file it writes, or it returns the stale slugs prominently so Claude dispatches regeneration. (Regeneration = Claude-in-loop; daemon has no LLM.)
3. **Dedup-as-update, not reject.** Change the similarity gate (`wiki.py:534`, `file_queue/dlq.py:307`, threshold 0.80) so a near-duplicate returns "this matches page X — append/update it?" with the candidate slug, instead of silent soft-allow (fragment) or hard-reject (orphan). Embed more than `content[:2000]`. Goal: writes CONSOLIDATE onto existing pages.
4. **Write-back forcing function.** A hook (mirror the memory stop-checkpoint) that, after significant work, prompts Claude: "did you learn something durable about this repo's structure/conventions/decisions? update the EXISTING type-templated wiki page (use the catalog to find it)." Primary-session write-back (today only subagents get `_subagent_writeback`).
5. **Post-write diff.** Surface the version diff (wiki versioning exists) so an update is verifiable, not fire-and-forget.

**TDD:** stale_wiki_count reflects real hash-drift; gate returns a candidate-to-update on near-dup; write-back hook fires + injects the prompt. **Effort:** M. Core release.

---

## v5.53.2 — Phase B-schema: Page types + templates + format lint

**Problem:** same info-kind has ≥3 formats; nothing enforces shape.

**Changes:**
1. **Add `wiki_schema_version` + `page_type`** to wiki frontmatter/schema (module / function / service / decision / architecture / host / runbook / analysis — start with the 4-5 covering 90%).
2. **Per-type templates** (required sections + frontmatter fields). `wiki_add` and the repo-wiki skill emit per template.
3. **Format lint in `wiki_lint`** — a page of type X must have sections Y; flag violations. Forcing function for consistency (today wiki_lint checks health only).
4. Catalog (5.53.0) groups by `page_type` once it exists.

**TDD:** wiki_add of a typed page enforces template sections; wiki_lint flags a type-X page missing a required section; schema_version present. **Effort:** M. Core release. (NOTE: stops NEW drift on day one — independent of the migration.)

---

## v5.53.3 — Phase B-migration: Normalize the 646-page corpus

**Problem:** 646 legacy pages, 41% drifted, duplicates (`shared-models` vs `mod-shared-models`).

**Changes:**
1. **Classification pass** — assign `page_type` to existing pages (heuristic by slug prefix `fn-`/`mod-`/`services-` + content shape).
2. **Reconcile duplicates** — merge same-subject pages (the dedup-as-update from 5.53.1 + Layer-1 wiki edit tools from v5.64 help here; consider sequencing 5.64 before this).
3. **Reformat to template** per type — Claude-in-loop batch (daemon has no LLM), gated + diffed, in batches, branch-isolated.
4. Backfill `wiki_schema_version`.

**TDD/guards:** migration is idempotent; no content loss (diff before/after); dry-run mode. **Effort:** L (the heavy tail). Can defer / run in slices. **Dependency:** wants v5.64 (wiki surgical-edit primitives) + 5.53.1 (dedup-as-update) landed first.

---

## Cross-cutting caveats (brutal)
1. Multi-release; not a patch. A+D (5.53.0) is the cheap keystone — ship it first for immediate value.
2. Daemon has NO LLM → stale-regeneration, write-back, and migration are **Claude-in-loop via hooks**, not autonomous.
3. The 646-page migration (5.53.3) is the expensive low-glamour part — A/D/C + B-schema deliver most of the value WITHOUT it.
4. Don't over-build the type system — 4-5 types cover 90%; resist a taxonomy explosion.
5. **Dependency:** v5.53.3 benefits from v5.64 (wiki edit primitives) — sequence 5.64 before the migration.

## User TODO — Max's personal nix-managed Claude config (NOT general; applied by Max, not shipped)

These are Max-only actions on the nix-managed Claude setup. They are NOT yadgar code and NOT for other users. Yadgar prepares the text; Max applies via `home-manager switch`. We do NOT edit these files.

- [ ] **(with v5.53.0 / Phase D-personal) Rewrite the read-first rule** in `~/git/nix/dotfiles/common/claude.md` — the yadgar block (currently lines ~6-33: "Read-first triggers" + "Tool selection"). Replace with the D-general rule text above: wiki = map (concepts/conventions/decisions/location), grep = territory (exact lines); read the session-start catalog first then grep there; `wiki_list`→slug→`wiki_read` for named pages, `wiki_query` only for fuzzy topic search (~0.34, not coordinates). Keep the existing `restore(directory=...)` resume guidance.
- [ ] **(with v5.53.1) Add a write-back rule** to the same file: after significant work, consolidate findings onto the EXISTING type-templated wiki page (find it via the catalog) — update, don't create a near-duplicate.
- [ ] **(with v5.53.2) Add a page-type rule:** when writing wiki, set `page_type` + follow the template for that type.
- [ ] **Apply:** edit `dotfiles/common/claude.md` → `home-manager switch` (regenerates `~/.claude/CLAUDE.md`). `modules/home/claude-code.nix` wires the dotfile.
- [ ] **Verify after switch:** `~/.claude/CLAUDE.md` reflects the new rule; the global file is the only allowed CLAUDE.md (no per-project copies).

> Each phase's MIGRATION_NOTES will also restate that phase's Max-nix TODO when the phase is built, so it isn't missed.

## Success signal
Hard to measure directly. Proxies: (a) Claude opens sessions by reading catalog pages instead of immediately grepping for known structure; (b) `wiki_coverage` rises without new format variance; (c) duplicate-page count trends down; (d) `stale_wiki_count` becomes a live, acted-on number. Add a lightweight read-first / wiki-hit telemetry if cheap.

## Ship discipline
Each sub-version = its own core release (branch → master → tag → dockerhub → pypi → nix). Backend unchanged. Update this umbrella's table as phases land.
