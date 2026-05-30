# PLAN — v5.12.0: Wiki Bookmarks page in viz (pinned wiki reader)

**Status:** drafted 2026-05-30. Plan-first per I27. Implementation deferred — requires backend (storage + endpoints) + frontend (new page) + markdown rendering pipeline. Net positive UX: replaces "ask Claude what's in roadmap" with one-click pinned page.

**Master at draft time:** core v5.10.3 shipped.

**Sequencing:** v5.12.0 slot. After v5.11.0 (anchor cross-project) ships. Independent of v5.10.x train. Independent of v5.20.0 (deferred roadmap freshness) — but if v5.20.0 ships first, the roadmap wiki page will be one of the most-pinned bookmarks (synergy).

---

## Why

User repeatedly asks Claude "what's in the roadmap" / "what's pinned" / "what's on the wiki page X". Every ask costs:
- 1 session round-trip (1-3 sec latency)
- LLM tokens (summary + render)
- Context budget (wiki page is multi-KB)
- Coordination overhead (Claude must be in the loop for every read)

A bookmarks page lets the user check pinned wiki content directly — no Claude in the loop. Read-only consumption pattern.

Secondary win: yadgar wiki has hundreds of pages, semantic discovery via `wiki_query` is fast but unstructured. Bookmarks turn frequently-accessed pages into 1-click access.

---

## Goals

1. **New viz page** `/bookmarks` (or similar route) accessible via button on existing graph view.
2. **Left column:** persistent list of bookmarked wiki pages (slug + display label).
3. **Right pane:** rendered markdown of selected bookmark with syntax highlighting + decent typography.
4. **Add button:** opens search box. Two modes:
   - Direct slug entry (autocomplete from `wiki_list`)
   - Semantic search (calls `wiki_query`, displays ranked results, user picks one)
5. **Persistence:** bookmarks stored in yadgar DB (SurrealDB), survive daemon restart.
6. **Remove:** per-bookmark delete (X button or right-click).
7. **Reorder:** drag-to-reorder (nice-to-have, V2 if scope creeps).
8. **Live freshness:** bookmark view fetches latest wiki content on each click (no stale cache).
9. **Mobile-tolerant:** usable on tablet; phone is nice-to-have.

---

## Non-goals

- **Wiki editing in viz.** Read-only. Edits stay via `wiki_add` MCP (Claude-driven).
- **Multi-user bookmarks.** Single-user yadgar — one global bookmark list. (If yadgar grows multi-user later, this becomes per-user, but that's a v6+ concern.)
- **Full-text search across all wikis.** `wiki_query` semantic search is the search primitive; no separate ElasticSearch.
- **Real-time updates.** No WebSocket/SSE for "wiki changed" push. User refreshes manually.
- **Bookmark folders / nesting.** Flat list. (V2 if user complains.)
- **Export / import bookmarks.** YAGNI until asked.

---

## Architecture

### Backend (yadgar)

**Storage — new SurrealDB table:**

```surql
DEFINE TABLE wiki_bookmark SCHEMAFULL;
DEFINE FIELD slug ON wiki_bookmark TYPE string ASSERT $value != NONE;
DEFINE FIELD label_override ON wiki_bookmark TYPE option<string>;  -- user-given display name, falls back to wiki title
DEFINE FIELD position ON wiki_bookmark TYPE int DEFAULT 0;  -- for ordering
DEFINE FIELD added_at ON wiki_bookmark TYPE datetime DEFAULT time::now();
DEFINE INDEX wiki_bookmark_slug ON wiki_bookmark FIELDS slug UNIQUE;
```

One bookmark = one row. Slug is the wiki page slug (existing yadgar wiki convention).

**MCP tools — 4 new:**

| Tool | Signature | Purpose |
|---|---|---|
| `bookmark_add` | `(slug: str, label_override: str = "")` | Adds bookmark. Idempotent (no error if exists; updates label). |
| `bookmark_remove` | `(slug: str)` | Removes bookmark. Idempotent. |
| `bookmark_list` | `()` | Returns ordered list of bookmarks with metadata. |
| `bookmark_reorder` | `(slug: str, new_position: int)` | Updates position; other bookmarks shift. |

All gated by existing auth bearer token. Located in `yadgar/server/tools/bookmarks.py` (new file).

**HTTP routes — exposed via existing viz proxy (`yadgar/viz_server.py`):**

The viz already reverse-proxies `/api/*` to the yadgar daemon. New API endpoints:

| Endpoint | Method | Backed by |
|---|---|---|
| `/api/bookmarks` | GET | `bookmark_list` |
| `/api/bookmarks` | POST | `bookmark_add` (body: `{slug, label_override?}`) |
| `/api/bookmarks/{slug}` | DELETE | `bookmark_remove` |
| `/api/bookmarks/{slug}/position` | PUT | `bookmark_reorder` (body: `{position}`) |
| `/api/wiki/search` | GET | passthrough to existing `wiki_query` (params: `q`, `tags?`, `limit?`) |
| `/api/wiki/list` | GET | passthrough to existing `wiki_list` (params: `slug_prefix?`) — used for slug autocomplete |
| `/api/wiki/read/{slug}` | GET | passthrough to existing `wiki_read` |

All via the proxy with bearer token injection — browser never sees credentials.

### Frontend

**Tech stack:** keep yadgar's existing approach (plain HTML/JS in `yadgar/static/`, no React/Vue/build step). New file:

- `yadgar/static/bookmarks.html` — bookmarks page (CSS + JS inlined or in `bookmarks.css` / `bookmarks.js` siblings).
- `yadgar/static/lib/marked.min.js` — markdown renderer (vendored; ~50KB).
- `yadgar/static/lib/highlight.min.js` + `yadgar/static/lib/github-dark.css` — syntax highlighting (~80KB JS + CSS theme).

Optional: `yadgar/static/lib/dompurify.min.js` (~30KB) to sanitize rendered HTML — defensive against XSS from any wiki page that embeds raw HTML. Yadgar's wiki content is operator-controlled but DOMPurify is cheap insurance.

**Markdown rendering choice — recommended `marked` + `highlight.js`:**

Why this combo over alternatives:
- **`marked`:** small (~50KB), no build step, CommonMark + GFM tables/strikethrough/task lists, fast.
- **`highlight.js`:** broad language support (100+), good defaults, GitHub-dark theme matches typical dev preference.
- **vs `markdown-it`:** also fine; `marked` simpler. Pick whichever has more recent maintenance at impl time.
- **vs `react-markdown`:** would require React build infra — wrong fit for yadgar viz's plain-HTML posture.

**Page layout:**

```
┌────────────────────────────────────────────────────────┐
│ [yadgar logo]  [← Graph]  [Bookmarks]      [search 🔍] │  ← Top nav
├──────────────┬─────────────────────────────────────────┤
│              │                                         │
│ 📑 Roadmap   │  # Yadgar Roadmap & Future Improvements │
│ ─ MCP gotcha │                                         │
│ ─ CPU bursts │  (rendered markdown of selected bookmark│
│ ─ v5.10.4 …  │   with syntax highlighting + tables +   │
│              │   headings, code blocks, etc.)          │
│ ──────────── │                                         │
│ [+ Add]      │                                         │
│              │                                         │
└──────────────┴─────────────────────────────────────────┘
```

Left column: ~280px wide, scrollable. Right pane: fills rest, scrollable.

**Add modal (clicked from `+ Add` button):**

```
┌─ Add bookmark ───────────────────────────────┐
│ ○ Search semantically  ● Type slug           │
│ ┌────────────────────────────────────────┐   │
│ │ yadgar-mcp-...                         │   │  ← autocomplete dropdown
│ └────────────────────────────────────────┘   │
│ Display label (optional): [..............]   │
│                                              │
│           [Cancel]  [Add]                    │
└──────────────────────────────────────────────┘
```

When "Search semantically" radio selected:
- Debounced 300ms input → `/api/wiki/search?q=<query>`
- Results displayed as ranked list with score + title + snippet
- Click on result → fills slug field → user can adjust label → Add

When "Type slug" selected:
- Plain text input with autocomplete from `/api/wiki/list?slug_prefix=...`
- Tab to accept top suggestion

**Entry point — button on existing graph page:**

Add a "📑 Bookmarks" link in the existing top nav of `yadgar/static/index.html`. Opens `bookmarks.html` in same tab. Back button returns to graph.

---

## Recommended approach (vs alternatives)

### Option 1 (RECOMMENDED): Plain HTML/JS, vendored libs, server-side proxy
- Stays consistent with existing yadgar viz posture.
- No build step, no framework lock-in.
- Vendored libs = reproducible deploys.
- All wiki access via existing MCP proxy — single auth point.

### Option 2: Lightweight SPA (e.g. Alpine.js or htmx)
- Marginal ergonomic win (declarative bindings).
- Adds a runtime dep + learning curve.
- Reject unless viz grows beyond bookmarks page (e.g. v5.13+ adds settings panel etc.).

### Option 3: Browser localStorage for bookmarks (no DB)
- Faster initial impl, no backend changes.
- BUT loses persistence across browsers / devices / clean profiles.
- Loses queryability via MCP from Claude or other clients.
- Reject.

### Option 4: CDN-loaded `marked` + `highlight.js` (vs vendored)
- Smaller repo footprint.
- Adds runtime external dep + privacy implications.
- yadgar runs offline-first (local daemon). Vendor wins.

**Recommendation: Option 1 + vendored libs + DB-backed bookmarks.**

---

## Tests (red-first per TDD)

### Backend
1. `test_bookmark_add_creates_row` — call `bookmark_add('foo')`, assert row exists.
2. `test_bookmark_add_idempotent` — call twice, no error, one row.
3. `test_bookmark_remove_idempotent` — remove nonexistent, no error.
4. `test_bookmark_list_returns_ordered` — add 3 with positions 0/1/2, `bookmark_list()` returns ordered.
5. `test_bookmark_reorder_shifts_others` — reorder middle to top, others shift down.
6. `test_bookmark_add_normalizes_slug` — trim whitespace, reject empty.
7. `test_bookmark_label_override_optional` — falls back to wiki title if `label_override` empty (or returned as-is for frontend to look up).
8. `test_bookmark_persists_across_daemon_restart` — integration test, write, restart daemon, read.

### HTTP routes (proxy + auth)
9. `test_api_bookmarks_get_returns_json` — GET `/api/bookmarks` returns 200 + JSON array.
10. `test_api_bookmarks_post_201` — POST with valid body creates row.
11. `test_api_bookmarks_post_missing_slug_400` — POST with empty body returns 400.
12. `test_api_bookmarks_delete_204` — DELETE removes row.
13. `test_api_unauthenticated_401_when_proxy_disabled` — direct access without bearer token rejected.
14. `test_api_wiki_search_passthrough` — GET `/api/wiki/search?q=test` returns ranked results.

### Frontend (manual smoke + optional Playwright)
15. **Smoke (manual):** load `bookmarks.html` → list renders, add modal opens, semantic search returns results, click result → bookmark added → click bookmark → markdown renders.
16. **Smoke (manual):** code block syntax highlighting visible (yellow/blue colors).
17. **Smoke (manual):** table renders with borders.
18. **(Optional Playwright):** automated browser test for the above. Likely DEFERRED — manual is acceptable per the v5.10.7 viz plan precedent.

---

## Acceptance

- All backend + HTTP tests green.
- Bookmarks survive `systemctl --user restart yadgar.service`.
- Markdown rendering shows code blocks with syntax highlighting matching `highlight.js` GitHub-dark.
- Tables render with visible borders + alternating row colors.
- Semantic search returns results in <500ms for a warm CE cache.
- Slug autocomplete returns results in <100ms (DB-backed).
- DOMPurify sanitization confirmed via fixture page with `<script>` tags — script blocks rendered as text, not executed.
- CHANGELOG + MIGRATION_NOTES updated for v5.12.0.

---

## Open questions (resolve before dispatch)

1. **Bookmarks per session vs persistent?** Plan assumes persistent. Confirm.
2. **Render markdown server-side or client-side?**
   - Client-side (recommended): less server CPU, browser parallelizes; live highlights any new wiki without backend changes.
   - Server-side: would let yadgar reuse caches if the same page is opened often. Probably premature optimization.
3. **Should bookmarks support tags / categories?** Plan says no (flat list). Reconsider if user accumulates 50+ bookmarks.
4. **Markdown extensions:** wiki content uses `[[slug]]` cross-references (per `wiki_add` doc). Should the renderer make these clickable to navigate to that bookmark / wiki page in-place? Recommend YES — small custom marked extension.
5. **Refresh-on-focus?** When user re-focuses the bookmarks tab, auto-refresh selected bookmark? Or only on explicit refresh? Lean explicit (manual). Add reload button per bookmark.
6. **Pin from Claude session?** Should Claude be able to `bookmark_add` via MCP? Yes — already implicit since MCP tool exists. Useful for: "Claude, pin this wiki for me" → done.
7. **Maximum bookmark count?** No hard cap. Soft warn at 100.
8. **What happens when wiki page is deleted?** Bookmark stays but renders "Page not found". User can manually remove. Lean: leave alone, don't auto-cleanup (deletions can be reverted in yadgar).
9. **Default bookmark set?** Optionally pre-seed with `yadgar-roadmap-future-improvements` on first run if user has zero bookmarks. Lean YES — proves the feature works immediately.
10. **Theming / dark mode?** Match existing viz styling. Likely dark (GitHub-dark code theme).

---

## Suggested ENHANCEMENTS (out of v5.12.0 scope; v5.12.x candidates)

- **Pinned-section headers:** group bookmarks under user-defined headings ("Roadmap & plans", "Reference docs", "Active investigations").
- **Search inside bookmark:** Ctrl-F over rendered markdown of currently-open bookmark.
- **Open multiple bookmarks in tabs:** intra-page tabs for parallel reading.
- **Last-modified indicator:** show wiki page's `updated_at` next to each bookmark + flag stale (>30 days).
- **Diff view:** highlight what changed since last view (requires storing per-bookmark `last_seen_updated_at`).
- **Export bookmark list to JSON / markdown:** for sharing or backup.
- **Keyboard shortcuts:** `j`/`k` to navigate list, `Enter` to open, `Del` to remove, `/` to focus search.
- **Recently-viewed wiki pages section** (auto-populated, separate from manual bookmarks).
- **Bookmark a wiki section** (anchor link into a specific heading), not just the whole page.
- **Live cycle indicator:** show roadmap pipeline as a horizontal timeline visualization on the roadmap bookmark.

---

## Dependencies

- yadgar wiki primitives (`wiki_add`, `wiki_read`, `wiki_list`, `wiki_query`) — already stable.
- viz proxy (`yadgar/viz_server.py`) — already routes `/api/*`.
- SurrealDB — schema migration to add `wiki_bookmark` table.
- New deps (vendored, not pyproject): `marked`, `highlight.js`, optional `dompurify`.
- No backend deps added.
- No new MCP infrastructure — just new tools using existing `@_tool()` pattern.

---

## Risk + rollback

| Risk | Mitigation |
|---|---|
| New bookmark API leaks wiki content to unauthenticated user | Proxy enforces bearer auth; tests assert 401 without token. |
| Markdown XSS via malicious wiki content | DOMPurify sanitizes rendered HTML; CSP header. |
| `wiki_query` is slow on cold cache | Async UI with spinner; debounce input; cache last 10 query results client-side. |
| SurrealDB schema migration breaks on upgrade | Migration script + rollback. Standard yadgar release procedure. |
| Vendored libs bloat repo | ~150KB total. Acceptable. Document in MIGRATION_NOTES. |
| Markdown renderer choice locks us in | All three candidates (`marked`/`markdown-it`/`react-markdown`) are interchangeable behind a thin rendering function. Swap cost = 1 file. |

**Rollback:** drop bookmarks page, keep DB rows (no destructive change), keep MCP tools (orphan but harmless). Revert HTML changes.

---

## Files to add / modify

### New
- `yadgar/server/tools/bookmarks.py` — 4 MCP tools.
- `yadgar/storage/bookmarks.py` — DB layer (CRUD).
- `yadgar/static/bookmarks.html` — page UI.
- `yadgar/static/bookmarks.css` — styles.
- `yadgar/static/bookmarks.js` — page logic.
- `yadgar/static/lib/marked.min.js` — vendored.
- `yadgar/static/lib/highlight.min.js` — vendored.
- `yadgar/static/lib/github-dark.css` — vendored (highlight.js theme).
- `yadgar/static/lib/dompurify.min.js` — vendored (optional XSS guard).
- `yadgar/tests/test_bookmarks.py` — unit + integration tests.
- `yadgar/tests/test_api_bookmarks.py` — HTTP route tests.
- `migrations/v5_12_0_bookmarks.surql` — SurrealDB schema.

### Modify
- `yadgar/viz_server.py` — add `/api/bookmarks*` + `/api/wiki/*` proxy routes.
- `yadgar/static/index.html` — add "📑 Bookmarks" nav link.
- `pyproject.toml` — version bump 5.11.x → 5.12.0.
- `server.json`, `docker-compose.yml`, `uv.lock` — version sync.
- `CHANGELOG.md` — v5.12.0 entry.
- `MIGRATION_NOTES.md` — v5.12.0 section (esp. DB migration step).

---

## ASCII mockup of full UX (single click flow)

```
1. User clicks 📑 Bookmarks in viz nav
2. Page loads, left column shows empty + "[+ Add]" button
3. User clicks +Add
4. Modal: types "roadmap" in semantic search
5. Results appear: yadgar-roadmap-future-improvements (score 0.94), ...
6. User clicks first result → slug fills → Add
7. Bookmark appears in left column
8. User clicks bookmark
9. Right pane renders the wiki content with code blocks, tables, headings
10. User reads roadmap without ever talking to Claude
```

---

## Sequencing relative to v5.11.0 and v5.20.0

- **v5.11.0** (anchor cross-project + Jira) is the next core ship. v5.12.0 lands AFTER v5.11.0 — gives time to bake schema patterns.
- **v5.20.0** (deferred roadmap freshness) is FAR future. v5.12.0 is INDEPENDENT — bookmarks work regardless of how roadmap is freshness-enforced. (Bonus: bookmarks page is a natural target for the roadmap autogen if/when v5.20.0 ships.)

---

## Implementation phasing suggestion

If v5.12.0 lands in pieces, suggest order:

1. **Phase 1 (backend, ~1 day):** schema migration + 4 MCP tools + tests. Ship dark — no UI yet.
2. **Phase 2 (HTTP routes, ~half day):** viz_server.py proxy additions + tests. Browser still has no UI but routes callable.
3. **Phase 3 (frontend skeleton, ~1 day):** `bookmarks.html` + `bookmarks.css` + vendored libs + nav link. Renders list, click to view, no add yet.
4. **Phase 4 (add modal, ~half day):** semantic search + autocomplete + add flow.
5. **Phase 5 (polish, ~half day):** DOMPurify, keyboard shortcuts (minimum: Esc to close modal), error states, mobile width.
6. **Phase 6 (release, ~half day):** CHANGELOG + MIGRATION_NOTES + version bump + nix bump + verify deploy.

Total: ~4 days dedicated work.
