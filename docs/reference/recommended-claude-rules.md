# Recommended Claude Rules for Yadgar Users

Copy the rules below into your `~/.claude/CLAUDE.md` (or your nix-managed equivalent).
They teach Claude to read yadgar before grepping, which is the primary win the
knowledge-base is designed for.

---

## Yadgar Read-First Rule (Phase A+D — v5.53.0)

Add this block to the **Memory System — Yadgar** section of your `~/.claude/CLAUDE.md`,
replacing or extending the existing "Read-first triggers" and "Tool selection" guidance.

```
## Yadgar Read-First Rule

Yadgar wiki = the MAP (conventions, module purpose, past decisions, where
subsystems live). Grep / source-read = the TERRITORY (exact current code lines).

**Always consult the map before the territory.**

At session start you receive a wiki catalog in `project_brief` output. Read it.
The catalog groups pages by category with titles — scan it to find the relevant
page before grepping.

**Tool selection for reads:**

- Named page known (slug visible in catalog): `wiki_list()` → pick slug →
  `wiki_read(slug)`. Fast, precise.
- Topic search (slug unknown): `wiki_query(query, tags=[...])` — always add tags
  when domain is known. Note: wiki_query scores ~0.34 — reliable for discovery,
  NOT for precise coordinates. If the result isn't clearly relevant, fall back to
  `wiki_list()`.
- Grep / file reads: for exact current code lines AFTER you know where to look
  from the wiki. Not as a substitute for the wiki.

**Never start a structural question (where does X live? what convention governs Y?)
by grepping. Check the wiki catalog first — if the page exists, read it.
Grep is how you verify or update what the wiki says, not how you discover it.**

**Write-back:** after significant work on a repo, check the catalog for the
relevant page and update it (or create it) with what you learned. Prefer updating
an existing page over creating a near-duplicate.
```

---

## Why these rules

Without an explicit read-first rule, Claude defaults to grep because grep gives
immediate deterministic results. The wiki has higher signal-to-noise for structural
and convention questions but is only valuable if Claude checks it *before* grepping.

The session-start catalog (v5.53.0+) makes the index visible at the top of every
session — the rule above teaches Claude to act on it.

---

## Upcoming additions (later phases)

- **v5.53.1 write-back rule:** after significant work, consolidate findings onto
  the EXISTING type-templated wiki page (find it via the catalog); update, don't
  create a near-duplicate.
- **v5.53.2 page-type rule:** when writing wiki, set `page_type` and follow the
  template for that type.
