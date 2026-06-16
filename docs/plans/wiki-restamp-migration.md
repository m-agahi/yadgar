# PLAN — Wiki re-stamp migration (616 global → correct buckets)

Status: **PLANNED 2026-06-16.** Runs AFTER v5.65 merges + container rebuilds
(recall now filters wikis by directory — `[[recall-scoping-restamp]]` §A/B + the
v5.65 wiki-path + hard-require fixes). This is the **data** half (plan §C);
v5.62/64/65 were the code half.

theme: data-integrity / retrieval
priority: high (616 of ~620 wiki pages mis-stamped `global` → cross-project leak)

## Problem

`wiki_list(directory="/nonexistent")` returns the global-only set = **616 pages**.
Nearly the entire wiki corpus is stamped `directory_context="global"` (the
always-eligible sink). Post-v5.65, `global` still surfaces in every project's
recall by design (it's the intentional cross-cutting bucket). So mis-stamped
project pages keep leaking until re-stamped to their real project bucket.

Clean buckets already exist: `/home/max/aws-work` (the AWS inventory, ~1404 pages
correctly stamped), `/home/max/git/nix`. This migration moves the 616 `global`
pages that actually belong to a project into that project's bucket, leaving only
the genuinely cross-cutting pages `global`.

## Tooling (what's possible)

- `wiki_set_metadata(slug, field="directory_context", value=<abs path | "global">)`
  — the ONLY MCP write path for a page's directory. **Idempotent** (no-op when
  value already matches), **versioned** (each real change creates a
  `wiki_page_version` row → `wiki_history` audit + rollback), **bypasses the
  similarity gate** (metadata revision, not new page).
- `wiki_list(directory=...)` — read. `directory="/nonexistent"` → global-only set
  (sizing + verification). No `directory_context` field in list output; derive
  bucket from **slug prefix** (the corpus uses disciplined `prefix-...` slugs).
- **Memories are OUT OF SCOPE** — `memory_update` cannot set `directory_context`.
  Existing `system`/mis-stamped memories are handled by v5.65 drop-`system`
  (they stop surfacing; no re-stamp tool exists). This migration is wikis only.

## Bucket ruleset (refined from live slug clustering 2026-06-16)

First-token histogram of the 616 (corrects `[[recall-scoping-restamp]]` §C — which
mis-routed `quinyx-*` to meridian and `tests-*`/`services-aws` wrongly):

### → `/home/max/git/yadgar` (~400; yadgar's own code/test/subsystem docs)
`fn-*` (268), `mod-*` (77), `tests-*` (40), `yadgar-*` (11), `wiki-*` (4),
`ccpm-*` (2), `api-*` (2), `surrealdb-*` (1), `code-review-plugin-*`.

### → `/home/max/aws-work` (~80; AWS infra/inventory/devops)
`aws-*` (14), `quinyx-*` (6 — **AWS ORG INVENTORY**, e.g.
`quinyx-aws-org-migration-roadmap`, `quinyx-iam-users-inventory`; NOT meridian),
`github-team-*`/`github-*` (23), `sg-*` (9, security groups), `ri-*` (9, reserved
instances/savings plans), `jit-*` (8, JIT access), `cloudbeaver-*` (4),
`digger-*` (2), `lambda-*` (2), `emr-*` (2), `vpc-*` (1), `services-aws-*` (23).

### → `/home/max/quinyx/meridian` (~50; NEW bucket — meridian platform)
`meridian-*` (5), `ui-*` (3, meridian frontend: `ui-vite-config` etc), `ir-*` (6),
`services-{scheduler,mcp,code,openlineage,metrics,log,jaeger,om}-*` (~35, meridian
platform services — everything under `services-` EXCEPT `services-aws-`).

### → `/home/max/git/nix` (~8)
`nixos-*` (4), `flux-*` (4).

### LEAVE `global` / review during dry-run (~20; genuinely cross-cutting OR ambiguous)
`architecture-*` (2), `index-*` (4), `v4-*`/`v43-*` (6), `shared-*` (4 — yadgar
shared OR meridian? inspect), `data-*` (2), `om-*` (2), `buildkit-*` (2, nix vs
aws? inspect), `tf-*`/`tfp-*`/`state-*` (5, aws-infra vs nix? inspect), `zed-*` (1),
`scan-*` (1), `slack-*`, and ANY slug not matching a confident rule above.

**Conservative default: no-match → leave `global`.** Mis-stamping a genuinely-
global page into a project bucket would HIDE it from other projects — worse than
leaving it global. Better under- than over-migrate.

## Execution (single yadgar-connected agent; user authorized "build and run")

1. **Enumerate** — `wiki_list(directory="/nonexistent", limit=1000)` → all global
   slugs (current count 616). Capture exact count.
2. **Classify (dry-run, NO writes)** — apply the ruleset by slug prefix. Build a
   table: `slug → proposed_bucket | LEAVE-global`. Inspect (read 1-2 pages) for
   the ambiguous clusters before deciding their bucket; if still unclear → LEAVE.
3. **Self-review** — emit the proposed histogram (per-bucket counts) + the FULL
   ambiguous/leave list. Sanity gates:
   - yadgar bucket ≈ 400, aws ≈ 80, meridian ≈ 50, nix ≈ 8, leave ≈ 20.
   - NO page routed to a bucket whose slug clearly contradicts it (e.g. an `aws`
     token landing in meridian). If a gate trips → STOP, report, don't apply.
4. **Manifest (rollback)** — write the full pre-state to a manifest file
   (`docs/migrations/wiki-restamp-2026-06-16-manifest.json`): `[{slug, old: "global",
   new: <bucket>}]` for every page to be changed. All olds are `global`, so
   rollback = `wiki_set_metadata(slug, "directory_context", "global")` for each
   changed slug. Commit the manifest (docs-direct-to-master).
5. **Apply** — for each classified (non-LEAVE) slug:
   `wiki_set_metadata(slug, field="directory_context", value=<bucket>)`. Idempotent
   + versioned. Log each result (`changed: true/false`). ~540 calls.
6. **Verify** — re-run `wiki_list(directory="/nonexistent")` → global count must
   drop from 616 to ≈ leave-count (~20). `wiki_list(directory="/home/max/git/yadgar")`
   etc. → per-bucket pages now present. Spot-check 3 slugs via `wiki_history` →
   confirm the metadata version row recorded.
7. **Effectiveness check** (needs v5.65 live) — `recall("aws rds digger", directory="/home/max/git/yadgar")`
   → AWS wikis NO LONGER returned (were leaking pre-migration). Re-measure
   recall-from-yadgar noise vs the 37.5% baseline; target < 10%.

## Safety / constraints

- **Apply/Import rule:** user explicitly authorized running this in an agent
  (2026-06-16). Still: dry-run + self-review gate + manifest BEFORE apply.
- **Rollback:** all 616 are currently `global` → trivial revert from manifest.
- **Idempotent + re-runnable:** safe to re-run; matched pages no-op.
- **Order:** run only AFTER v5.65 deployed (container rebuilt) — the noise-cut
  benefit needs the recall wiki-path + drop-system fixes live. The re-stamp
  itself works on any version (wiki_set_metadata shipped v5.61).
- **Branch:** wikis are branch-scoped too; `wiki_set_metadata` preserves branch.
  Re-stamp does not touch branch.

## Acceptance

- Global wiki count: 616 → ≈20 (verified via `wiki_list(directory="/nonexistent")`).
- Per-bucket: yadgar/aws-work/meridian/nix pages appear under their dir scope.
- Manifest committed (rollback ready).
- Post-v5.65 recall-from-yadgar: no AWS/meridian wiki leak; noise < 10%.

## Related
- `[[recall-scoping-restamp]]` — §C is this migration (rules corrected here).
- `[[unified-scoped-recall]]` — the rebuild this unblocks.
- Code: `wiki_set_metadata` (v5.61), `is_directory_eligible` (storage/directory.py),
  recall wiki-path + hard-require (v5.65).
