# Telemetry Trio — Design REVIEW (task #40)

**Status: DRAFT — awaiting audit.**
**Date:** 2026-07-13
**Author:** design-review pass (opus), plan-only — no code, no version files, read-only except this file.
**Scope:** REVIEW of the existing design-of-record, not a fresh design.

---

## BLUF

**A complete, verified-accurate design already exists:**
`docs/plans/telemetry-update-prompt-sync-2026-07-10.md` (3 days old, Status: REVIEW).
It covers exactly task #40's three sub-features (there labelled F1/F2/F3). This doc is
the **audit layer** on top of it — it does not re-derive the mechanisms. Read the
2026-07-10 plan for full design; read this for verdicts.

Headline findings from the audit:

1. **The 2026-07-10 plan is sound and its shipped-state section is materially correct** —
   with **one stale file:line** (the `update_check_on_start` knob is NOT at
   `config_yaml.py:1035`; real location `config.py:924` + `config_registry.py:376-378`,
   verified below). Fix that reference before either plan is treated as authoritative.
2. **One-train-vs-split verdict: SPLIT into three independently-shippable tracks.**
   The 2026-07-10 plan already splits implicitly (F1+F2 share one endpoint; F3 Half-A
   separate; F3 Half-B fenced off). This audit makes the split explicit and adjudicates
   scope: **F1-free ships now; F2 is a small opt-in track; F3 is a separate product —
   Half-B should be cut, not merely deferred.**
3. **Biggest risk is F3, not F1/F2.** F1/F2 as scoped (opt-in, no-ID, buckets-only,
   public dashboard) have a modest, well-precedented privacy surface. F3 converts yadgar
   from "a local tool that sends nothing" into "an operator of an executable-instruction
   distribution channel" — the injection/moderation liability is the real hazard, and it
   is unbounded for a solo dev.
4. **Redundancy callout (brutal honesty):** producing a *second* plan file for the same
   three features risks plan-rot (two docs drift). This audit's findings would arguably be
   better folded as a review section INTO the 2026-07-10 plan. Written as a separate file
   because task #40 explicitly demanded this path + structure; flagged for the user in §
   Open Questions.

---

## Existing update-CLI state (verified file:line)

All lines below were verified by direct `grep` over the source tree on 2026-07-13 (quoted
`--include='*.py'`, both cases, tests excluded). Where they disagree with the 2026-07-10
plan, the disagreement is called out.

| Fact | Verified location | Notes |
|---|---|---|
| `yadgar update` CLI dispatcher | `yadgar/core/cli/update.py:101` (`cmd_update`) | routes to check/install/finalize/rollback; default = `--check` |
| Subcommand registration | `yadgar/core/cli/update.py:38` (`register(subparsers)`), args `:50–96` | `--check --install --finalize --rollback --snapshot --version` |
| Subcommands shipped | `--check` (v5.48), `--install`/`--finalize`/`--rollback` (v5.49.0 Phase 10) | docstring `update.py:1–18` |
| PyPI probe | `yadgar/core/update/check.py:37` (`probe_latest_version`, `@observe(tier="boundary")`) | `GET https://pypi.org/pypi/yadgar/json`, UA `yadgar/<version>`, `Accept: application/json`; extracts only `info.version`; URL overridable via `YADGAR_UPDATE_PYPI_URL` |
| Auto-check on daemon start | `yadgar/core/daemon/daemons.py:215` (`_maybe_auto_check_for_update`); invoked `yadgar/core/server/_startup.py:118` | background thread, timeout-gated, gated on `UPDATE_CHECK_ON_START` |
| Knob `update_check_on_start` | Settings default **`yadgar/_shared/config/config.py:924`** (`UPDATE_CHECK_ON_START: bool = False`); FIELD_META **`config_yaml.py:1027`**; registry `config_registry.py:376` | **CORRECTION (partial):** 2026-07-10 plan cites `config_yaml.py:1035` — the FIELD_META entry IS in that file but at **:1027**, not :1035 (off by 8). And the canonical default is `config.py:924`, not config_yaml.py. Tighten both refs. |
| Install-method detection | `yadgar/core/update/install_methods.py:27` (`detect_install_method`) | returns `pipx / brew / nix-flake / container / source` — already computed locally |
| Version source | `yadgar/__init__.py:1–21` | `importlib.metadata.version("yadgar")` → pyproject.toml fallback → `"unknown"`. `BACKEND_VERSION` separate at `:21` |
| HTTP control endpoint | `POST /api/control/update` | auth + `UPDATE_DEBUG_APIS_ENABLED` gated |
| Privacy policy | `docs/PRIVACY.md` (v5.48.0) | wire format matches source verbatim; corporate-firewall note present (respects `HTTPS_PROXY`; air-gapped → keep OFF) |
| MIGRATION_NOTES | `MIGRATION_NOTES.md:453` (v5.48.0 section; repo root, not `docs/`) | states anonymous GET, no telemetry, no IP collection |
| Telemetry / stats / opt-in infra | **absent** | grep for `telemetry`/`analytics`/`opt_in`/`phone.home`/`usage_stats` finds only operational observability (exception/db-size/ml-unload). **F2 is greenfield.** |
| `get_memory_stats()` (F2 local source) | `yadgar/_shared/storage/ops.py:139` | 2026-07-10 plan cites `ops.py:139` — CORRECT |
| agent_prompt save (MCP) | `yadgar/core/server/tools/agent_prompts.py:82` (`agent_prompt_save`, `@_tool`) | slug `agent-prompt-<pattern>`, tags `["agent-prompt","task:<pattern>"]`, wiki-versioned |
| seed starter prompts | `yadgar/core/server/tools/agent_prompts.py:355` (`seed_agent_prompts`, `@_tool(power=True)`) | 15 starters + contract + disciplines, global scope, idempotent |
| agent_prompt backend write | `yadgar/backend/admin_exec/wiki.py:454` (`agent_prompt_save`) | DB write + TOC upsert (`agent-prompt-toc`) + library anchor + wiki-epoch bump |
| Genesis corpus (F3 tier-3 composes above) | `yadgar/core/seed/materials/agent_prompts.yaml` | ADR-0091 two-tier: file=law → wiki=practice → backflow; v5.123.0 promoted 10 live patterns |

**Net:** F1 (check-for-update) is **fully shipped and must NOT be re-designed.** F2
(opt-in stats) is **greenfield.** F3 (prompt-sync registry) rides existing
draft/approve + wiki-versioning machinery but the registry/accounts layer is greenfield.

---

## Three sub-feature designs (audit of the 2026-07-10 design-of-record)

For each: the design-of-record's mechanism (summarised, not re-derived), this audit's
**verdict**, and privacy/consent where relevant.

### F1 — check-for-update → usage count

**Design-of-record mechanism (§3 of 2026-07-10 plan):**
- The version *check* already ships (above). The open part is turning it into a *count*.
- Crux finding: **install/version/install-tool counts are already free** via
  `pypistats.org` (PyPI BigQuery `file_downloads`, carries installer type + py-version +
  platform) and Docker Hub pull counts — **zero user telemetry, zero infra.**
- Custom `POST /v1/ping` endpoint only buys *active-vs-cumulative* + *which versions are
  live now* + *install-method of running daemons*. Gated behind OQ-F1-1.
- If built: daemon-start + daily-jittered cadence; fields `version` (exact) / `platform`
  (os-arch) / `install_method`; **Option A = count pings, no ID** (best trust); notice via
  first-run banner + `project_brief` rail + `update --check` footer, never in MCP results.

**Verdict: ACCEPT, and strengthen.** F1-free (pypistats + Docker Hub reader) is the single
strongest idea in the whole trio — it delivers the dev's actual goal ("how many installs /
which versions") *immediately, collecting nothing*. Ship `yadgar stats downloads` (Phase 1).
The custom `/v1/ping` endpoint's marginal increment (active-vs-downloaded) is **not worth
the standing infra + retention-policy + abuse surface for a solo dev** — recommend
**resolving OQ-F1-1 as NO** and dropping Phase 5 unless active-install churn becomes a
concrete question later. This is a sharper position than the plan's "optional, gated."

**Files (new):** `yadgar/core/cli/stats.py` (or extend update CLI); read-only HTTP client
reusing the existing httpx patterns from `check.py`. No config knob needed for F1-free
(it queries public APIs, sends nothing).

**Privacy:** F1-free sends nothing from users at all. If `/v1/ping` is ever built, Option-A
(no ID) satisfies P5/P3 by construction.

### F2 — opt-in anonymous stats (default-OFF, no PII)

**Design-of-record mechanism (§4):**
- Greenfield. Content-free, **bucketed** payload from local `get_memory_stats()`
  (`ops.py:139`): `version` (exact) + `memory_count`/`wiki_pages`/`agent_prompt_count`/
  `tool_calls_30d`/`uptime` (buckets) + `install_method`/`platform` (enum). No per-tool
  breakdown, no titles, no content, no paths, no sub-day timestamps.
- Consent UX: `yadgar stats preview` (prints EXACT bytes, exists before send path) →
  `stats share --enable/--disable` (one-time preview + y/n) → config knob
  `stats_share_enabled: false` (default OFF, env-overridable, honors env-lock 409).
- Public dashboard (Codeberg Pages) is part of definition-of-done (reciprocity, P6).
- Cadence daily-jittered; suppressed in CI/non-tty/air-gapped (P7).

**Verdict: ACCEPT as scoped.** This is the correct shape: opt-in default-off + preview +
public dashboard is the Syncthing/popcon trust model, and the plan's P1–P8 rules are
genuinely testable (diff payload struct vs `docs/PRIVACY.md` table — reuses the I25/I32
drift-audit pattern already in the repo). Two must-hold constraints:
- **`preview` MUST land before any send path** (Phase 2 before Phase 4). Non-negotiable —
  it is the trust primitive.
- **No persistent install-ID by default.** Option A (count submissions, no ID). A
  popcon-UUID only under explicit double-opt-in, and honestly: retention curves are not
  worth the identifiability for this project — recommend never shipping the UUID.

**Files (new):** `yadgar/core/cli/stats.py` (`preview`/`share`); config knob via the
three-way registration per I25 — Settings default in `yadgar/_shared/config/config.py`,
FIELD_META in `config_yaml.py`, env registry in `config_registry.py` (same pattern the
`update_check_on_start` knob uses); the shared endpoint (§ below). Local source:
`ops.py:139` `get_memory_stats()`.

**Privacy/consent:** the crux surface. Buckets chosen so no bucket isolates one install;
exact-`version` is the only exact field; everything content-free. Consent is a deliberate
act (no inline first-run prompt → avoids dark-pattern / consent-fatigue). **This is the
one place to get exactly right** — see Risks.

### F3 — agent-prompt sync registry

**Design-of-record mechanism (§5):**
- **Half-A (download-only, curated-core):** upstream registry read API →
  `yadgar prompts pull user/pattern@ver` → lands as `wiki_draft` requiring explicit
  `wiki_approve` (P8; the draft/approve gate **already exists** — strongest control).
  Diff-on-update (`wiki_diff` vs approved local), provenance display, signing-verify
  (Sigstore keyless), curated-core reviewed by maintainer. Composes as a *third tier above*
  the ADR-0091 genesis/wiki split; upstream versions immutable; no auto-merge ever.
- **Half-B (deferred):** OAuth accounts + publish + private-team-sync + payments +
  moderation/report pipeline.

**Verdict: SPLIT HARD. Half-A: build only if there is demonstrated demand — otherwise
skip. Half-B: CUT, not merely defer.** Going further than the design-of-record:
- Half-A's *value* is thin for a solo-dev-scale project: the genesis corpus
  (`agent_prompts.yaml`, 15+ patterns, backflow-maintained) already gives every install the
  full discipline set offline. "Pull a curated core from upstream" duplicates what the seed
  already ships. The added value is *community-contributed* patterns — which is exactly the
  part that carries injection risk.
- Half-B is not a telemetry feature at all — it is a **paid executable-instruction
  marketplace**. For a solo dev the moderation + abuse + injection-liability + tax/KYC +
  support burden is unbounded and dwarfs any plausible revenue. The plan says "defer,
  possibly indefinitely"; this audit says **cut it from the roadmap and re-open only behind
  a written moderation plan and evidence of paying demand** — carrying it as "deferred"
  invites speculative build.

**Files:** Half-A reuses `wiki_add`(draft) / `wiki_approve` / `wiki_diff` / wiki-versioning
+ `agent_prompts.py`. A registry read client + signature verify are the only new surface.
Half-B: a **separate authenticated service**, never fused with F1/F2 (P5).

**Privacy/security:** F3 is a *security* problem more than a privacy one — a poisoned
prompt is RCE-by-social-engineering inside a subscriber's agent. The approve-gate is
load-bearing AND human-dependent (fatigue erodes it). Signing proves *who*, not *safe*.

---

## One-train-vs-split recommendation

**SPLIT into three independent tracks. Do NOT ship as one train.** Rationale:

| Track | Ship independently? | Recommended disposition |
|---|---|---|
| **F1-free** (pypistats + Docker Hub reader) | Yes — zero deps, zero telemetry | **Ship first.** ~1–2d. Delivers the actual goal immediately. |
| **F2** (opt-in stats + preview + shared endpoint + dashboard) | Yes — depends only on its own endpoint | **Ship second, small.** `preview` before send. Default OFF. |
| **F3 Half-A** (download-only registry) | Yes — separate from F1/F2 entirely | **Only on demand.** Thin value over genesis corpus; carries the injection surface. |
| **F1 custom `/v1/ping`** | Optional increment on F2's endpoint | **Drop** (OQ-F1-1 → no) unless active-churn becomes a real question. |
| **F3 Half-B** (accounts/publish/payments) | Separate product | **Cut.** Re-open only behind a moderation plan + demand evidence. |

The three features share almost nothing except a hosting decision (F1-count and F2 share
one endpoint IF F1-count is ever built). Coupling them into one train would gate the free,
immediately-valuable F1-free behind F3's unresolved product/liability questions. **The
correct sequencing is value-first: F1-free → F2 → (maybe) F3 Half-A, each its own minor.**

---

## Acceptance criteria

### F1-free
- [unit] `stats downloads` parses a canned pypistats JSON fixture → correct per-version /
  per-installer / per-platform counts; handles the deprecated `-1` download field.
- [unit] Docker Hub pull-count parse from canned fixture.
- [e2e] `yadgar stats downloads` against live pypistats + Docker Hub returns non-error
  aggregate (network-gated test, skip in CI-air-gap).
- [e2e] Command sends **no** outbound request carrying any local yadgar data (assert only
  the two public read URLs are hit).

### F2
- [unit] `stats preview` output is byte-identical to what the send path would transmit,
  from a fixed local-stats fixture (the P4 invariant, mechanically enforced).
- [unit] Every payload field maps to a `docs/PRIVACY.md` justification row (P1 drift-audit,
  reuse I25/I32 pattern) — a field with no row FAILS the test.
- [unit] Bucketing: exact `memory_count=3271` → bucket `1k–5k`; no exact count leaks.
- [unit] `stats_share_enabled` default is `false`; env-lock returns 409 when env-set.
- [unit] P7 suppression: send is a no-op under `CI=1` / no-tty / air-gapped.
- [e2e] `--enable` triggers a one-time preview + confirmation before first send.
- [e2e] Opted-out install (default) emits zero `/v1/stats` requests over a daemon lifecycle.

### F3 Half-A (only if built)
- [unit] Pulled prompt lands as `wiki_draft`, never in the composable prelude pre-approve.
- [unit] Signature-verify: unsigned/tampered pull → loud warning / reject.
- [e2e] `prompts pull` → draft → `wiki_diff` vs approved local shows exact change →
  `wiki_approve` promotes; a later upstream version offers a diff, never auto-merges.

---

## Test plan

- **Payload-contract tests (F2, load-bearing):** golden-file the exact wire struct; a
  CI lint diffs it against the `docs/PRIVACY.md` table (extends the existing
  `check_capability_coverage` / three-way-sync discipline). Any new field without a doc row
  breaks CI. This makes P1/P5 *enforced*, not aspirational.
- **Network-boundary assertion (F1-free + F2):** mock the httpx transport; assert the ONLY
  hosts contacted are the documented ones and F1-free carries no local data.
- **Consent-gate tests (F2):** state machine — default OFF → preview → enable → send;
  disable → zero sends. Assert no send without a recorded consent event.
- **Suppression matrix (P7):** parametrise `CI` / tty / proxy env → assert suppressed.
- **F3 draft-gate test:** assert no code path writes an upstream pull into
  `agent_dispatch_prelude` composition without `wiki_approve`.
- Reuse: existing `test_update_daemon.py`, `test_core_config_integrity.py` (knob default),
  wiki-versioning + draft/approve test suites for F3.

---

## Risks

- **PRIVACY (highest).** F2 is the surface that can burn trust. Mitigations already in the
  design: opt-in default-OFF, buckets-only, no-ID (Option A), `preview` before send,
  public dashboard, P1 drift-audit test. Residual: (a) exact-`version` + a rare
  `install_method`/`platform` combo could narrow identifiability for a tiny population —
  acceptable but note it; (b) consent-fatigue if the first-run disclosure nags — the plan
  correctly avoids an inline prompt. **Do not flip F2 to opt-out** (VS Code precedent =
  backlash + fork); opt-out is raised only as OQ-F1-2 and the default must stay opt-in.
- **NETWORK EGRESS.** Any new outbound path is an egress the user must be able to see and
  kill. `preview` + explicit knob + P7 suppression cover this. The version probe already
  respects `HTTPS_PROXY`; F1-free/F2 must too.
- **FIREWALL / air-gap.** Corporate-firewall handling already established for the update
  probe (`HTTPS_PROXY` honored, air-gapped → OFF; `PRIVACY.md:92–101`). F1-free/F2 inherit
  the same rule. F1-free additionally fails soft (public read APIs may be blocked → the
  command degrades to "unavailable", never blocks the daemon).
- **F3 INJECTION (unbounded).** A poisoned community prompt is RCE inside a subscriber's
  agent. The approve-gate reduces but cannot eliminate; review does not scale to a solo
  dev. This is the reason to keep F3 download-only-curated at most and cut Half-B.
- **PLAN-ROT (process risk).** Two plan files for the same features will drift. See Open
  Questions — fold-vs-separate is a real decision.
- **STALE REFERENCE.** The design-of-record's `config_yaml.py:1035` knob citation is
  imprecise: the FIELD_META entry is in that file but at `:1027`, and the canonical Settings
  default lives at `config.py:924`. Minor, but tighten it so the implementer isn't sent to
  the wrong line/file for the default.

---

## Scope

**IN:**
- Audit + verdicts on the three sub-features (this doc).
- Verified shipped-state file:line table.
- Split-vs-train recommendation; per-track disposition.
- Acceptance criteria + test plan for the tracks recommended to proceed (F1-free, F2,
  optionally F3 Half-A).

**OUT:**
- Any code, config, or version-file change (this is plan-only).
- Re-designing the shipped `yadgar update` CLI / check-for-update probe (frozen — F1 check
  is DONE).
- Building the F1 custom `/v1/ping` endpoint (recommend drop).
- F3 Half-B (accounts / publish / payments / moderation) — recommend cut.
- Editing the 2026-07-10 design-of-record or `docs/DECISIONS.md` / ADRs (constraint).
- Choosing the hosting vendor / payment rail (user decisions — OQ-INFRA-1 / OQ-F3-2).

---

## Open questions (user decisions)

Inherited from the design-of-record (still open) + this audit's additions:

- **OQ-AUDIT-1 (process, new):** This doc duplicates the 2026-07-10 plan's subject. Fold
  these verdicts INTO `telemetry-update-prompt-sync-2026-07-10.md` as a review section and
  delete this file, or keep two docs? **Recommendation: fold** — one design-of-record avoids
  drift.
- **OQ-AUDIT-2 (new):** Tighten the design-of-record's knob reference regardless of the fold
  decision: FIELD_META is `config_yaml.py:1027` (not :1035); Settings default is
  `config.py:924`.
- **OQ-F1-1 (telemetry destination + endpoint):** Is the active-install count worth a custom
  `/v1/ping` endpoint given pypistats + Docker Hub give downloads/versions/install-tool for
  free? **Audit recommendation: NO — ship F1-free only.**
- **OQ-F1-2 (consent):** Opt-in or opt-out for any count/stats? Shipped posture + `PRIVACY.md`
  document opt-in/OFF. **Audit recommendation: hold opt-in.**
- **OQ-F2-1 (bucket boundaries):** Are §4.1 buckets coarse enough? Any exact field beyond
  `version` wanted, or is exact-version + all-else-bucketed the line?
- **OQ-F2-2 (dashboard):** Commit to a public aggregate dashboard (trust move + standing
  maintenance)? **Audit recommendation: yes if F2 ships at all — it is the reciprocity that
  makes collection non-extractive.**
- **OQ-INFRA-1 (telemetry destination host):** Cloudflare Worker/DO ($0, edge-drops IP) vs
  fly.io/VPS (you run a data-collecting box)? **Audit recommendation: Worker/DO.**
- **OQ-F3-1 (build F3 at all?):** Half-A only, or skip F3 entirely and keep disciplines a
  local + genesis concern? **Audit recommendation: skip until demonstrated demand — genesis
  corpus already ships the core set.**
- **OQ-F3-2 (money):** If Half-B ever ships — subscription vs donations-only? **Audit
  recommendation: cut Half-B; if resurrected, donations only (LiberaPay/Sponsors).**
- **OQ-F3-3 (signing bar):** Sigstore keyless vs lighter publisher-attested + report-driven
  for v1?

---

## Version impact

- **F1-free:** one new feature minor (per versioning convention, one minor per feature).
  No migration. No breaking change.
- **F2:** one new feature minor. New config knob (safe default OFF → "no action required for
  existing installs" per PRIVACY.md convention). `docs/PRIVACY.md` extension (Phase 0).
  No DB migration (reads existing `get_memory_stats`).
- **F3 Half-A (if built):** one feature minor; reuses wiki-versioning, no new migration.
- **No major bump** — none of these are breaking-architectural (v6.0.0 territory is
  two-tier model routing, per versioning convention).
- Per the skip-1 odd-minor convention, each track takes the next odd minor at implementation
  time; slots not pre-reserved here (plan-only doc).

---

## Cross-references

- `docs/plans/telemetry-update-prompt-sync-2026-07-10.md` — **design-of-record** (F1/F2/F3
  full mechanism, P1–P8 rules, precedent table, sources). This doc audits it.
- `yadgar/core/cli/update.py:101` — shipped update CLI (frozen).
- `yadgar/core/update/check.py:37` — `probe_latest_version` (shipped probe).
- `yadgar/core/daemon/daemons.py:215` — `_maybe_auto_check_for_update` (auto-check).
- `yadgar/_shared/config/config.py:924` (Settings default) + `config_yaml.py:1027`
  (FIELD_META) + `config_registry.py:376` (env) — `update_check_on_start` knob (real
  locations; tightens the design-of-record's `config_yaml.py:1035`).
- `yadgar/_shared/storage/ops.py:139` — `get_memory_stats()` (F2 local source).
- `yadgar/core/server/tools/agent_prompts.py:82,355` — agent_prompt save + seed (F3 base).
- `yadgar/core/seed/materials/agent_prompts.yaml` — genesis corpus (ADR-0091; F3 tier-3).
- `docs/PRIVACY.md` — shipped v5.48 update-check privacy policy (F1 baseline).
- `docs/DECISIONS.md` PD-37 — distribution/update train (v5.45–v5.47).
