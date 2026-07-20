# Yadgar Subagent Contract — CLAUDE.md Snippet

Paste the block below into your `~/.claude/CLAUDE.md` (or project-local equivalent)
to opt subagents into the Yadgar memory protocol.

---

## Yadgar Subagent Protocol (paste into CLAUDE.md)

```markdown
## HARD RULE — Yadgar Subagent Protocol

Applies to ALL subagents (general-purpose, Explore, plan, etc.) unless
the spawning task explicitly says "skip yadgar".

### Before substantive work

1. Call `mcp__yadgar__recall(<task topic>)` — 1-3 word query capturing the task domain.
2. Surface findings **inline** in the first section of your report under `## Yadgar findings`.
3. Do NOT call `memorize`, `wiki_add`, or any write tool. Main thread persists from your report.

### Report shape (required)

Every agent report MUST include:

```
## Yadgar findings
<!-- anchors: list slug:line refs found via recall, or "none" -->
- anchor: <slug or memory-id> — <one-line fact>
- ...
<!-- facts: discoveries worth persisting (main thread will memorize) -->
- fact: <non-obvious, reusable finding>
- ...
```

If recall returns nothing relevant, write:
```
## Yadgar findings
- none
```

### Provenance tagging

When your task involves storing findings, tag your report section heading with
your agent type so the SubagentStop hook can extract it with correct provenance:

```
## Yadgar findings [agent: general-purpose]
```

Supported agent types (used as `provenance_agent` in memorize calls):
- `general-purpose` — default workhorse agent
- `Explore` — read-only exploration and research (cannot write to Yadgar itself)
- `plan` — planning and design agents

### What NOT to do

- Do NOT call `memorize`, `remember`, `wiki_add`, or `wiki_update`.
- Do NOT call `checkpoint` or `restore`.
- Do NOT modify `.claude/settings.json` or `CLAUDE.md`.
- Explore/Plan subagents: no write tools at all — report only.

### Why

Subagents write to the same Yadgar store as the main thread. Uncoordinated writes
create duplicate entries, split provenance, and trigger unnecessary surprise-gate
rejections. The main thread batches and deduplicates findings from all agents before
persisting — subagents reporting is both cheaper and safer than subagents writing.

The `SubagentStop` hook (v5.3.0+) automatically extracts `## Yadgar findings`
sections from agent reports and memorizes them with `provenance_agent` set to the
agent type — so your findings persist even if the main thread does not explicitly
process them.
```

---

## Claim verification (main thread responsibility)

Before the main thread integrates any subagent output — file edits, contract flips, test assertions, command output — it must verify the claim against the actual artifact. Re-read the file; check `gh pr view --json body`; re-describe AWS resources. A report that says a change was made is a claim, not truth. See `AGENTS.md § Subagent contract` for the full rule.

---

## Install note

**One-time setup:**

1. Copy the block above (between the triple-backtick fences) into `~/.claude/CLAUDE.md`.
2. Run `yadgar install-hooks --scope global` to register the `SubagentStop` hook —
   this enables automatic extraction of `## Yadgar findings` from subagent reports.
3. Verify with `yadgar install-hooks --dry-run --scope global`.

The contract is opt-in. Yadgar works without it — subagents just won't auto-write
findings. With it, every subagent that follows the `## Yadgar findings` format gets
its discoveries persisted automatically under the correct provenance label.

## Compatibility

Works with any Claude Code setup — no Nix required.
The hook and contract use only standard Python (stdlib) and the Yadgar HTTP API.
