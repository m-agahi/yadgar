# Telemetry, Update-Count & Prompt-Sync — Design Plan

**Status: F1 DROPPED (pypistats/DockerHub give install+version counts free); F2 DEFERRED→v6; F3 DEFERRED→v8; F3-registry CUT per audit**
**Date:** 2026-07-10
**Branch:** `docs/telemetry-prompt-sync-plan`
**Author:** design pass (opus), plan-only — no code, no version files.

---

## 0. Framing — what already shipped, what this plan is actually about

The task brief describes three features (update-check + counting, opt-in stats,
prompt-sync). **The update-*check* already shipped** and must NOT be re-designed:

| Shipped | Where | Posture |
|---|---|---|
| `yadgar update --check` / `--install` / `--rollback` / `--finalize` | `yadgar/core/cli/update.py` (v5.48 → v5.49) | opt-in CLI |
| Version probe on daemon start | `update_check_on_start` config knob (`config.py:924`; env registry `config_registry.py:376-378`), **default `false`** | opt-in, OFF |
| Probe wire format | `GET https://pypi.org/pypi/yadgar/json`, UA `yadgar/<version>`, no body/cookies/params | documented in `docs/reference/privacy.md` (v5.48) |
| Install-method detection | `detect_install_method()` → `pipx / brew / nix-flake / container / source` | already computed locally |

So this plan's real subject is **the part the shipped probe deliberately does NOT
do**: (F1) turning the version probe into a usage *count*, (F2) an opt-in
aggregate-stats channel, (F3) an upstream prompt-sync registry with accounts and
maybe money. The single sharpest finding below is that **F1's headline goal —
"how many installs / which versions are live" — is already answerable for free
with zero new infrastructure** (pypistats + Docker Hub). That reframes F1 from
"build a counting endpoint" to "decide whether the marginal increment a custom
endpoint buys is worth its burden." Read §3 first.

This plan composes with, and does not reverse, PD-37 / `docs/reference/privacy.md` /
ADR-0091. Where it proposes anything the shipped posture forbids (e.g. opt-out),
that is surfaced as an **open question for the user**, never silently flipped.

---

## 1. Principles (privacy stance codified as testable rules)

These are written as *rules a reviewer or a test can check a payload against*,
not aspirations. They bind F1/F2/F3. Numbered so the user can accept/reject each.

- **P1 — No payload field without a user-visible justification.** Every field in
  any outbound request maps to a row in a public `docs/reference/privacy.md` table stating
  *what* and *why*. A field with no justification row is a bug. (Testable: diff
  the wire struct against the doc table — the existing drift-audit pattern,
  I25/I32.)
- **P2 — Opt-in for anything beyond the version ping.** The version-only check is
  the *maximum* default-considered behaviour, and even it ships OFF. Anything
  carrying a counter, a bucket, or an identifier is strictly opt-in with consent
  captured before first send. (No exceptions granted in this plan; opt-out for
  counting is raised only as an explicit OQ.)
- **P3 — Raw data is never retained.** Any server this plan introduces increments
  aggregate counters and drops the request. No raw-row store, no IP column, no
  per-request log beyond a short-lived rotating access log the host can't mine.
  (Testable: schema review — the only persistent tables are counters.)
- **P4 — Show-before-send.** For any opt-in channel, a `preview` command prints
  the *exact* bytes that would be transmitted, before consent and on demand
  thereafter. (Syncthing model — the trust-earning move.)
- **P5 — No identity, ever.** No user ID, hostname, username, account, project
  path, memory content, or conversation data leaves the machine on the
  telemetry/update channels. Accounts (F3) are a *separate* authenticated channel
  the user logs into deliberately, never fused with F1/F2.
- **P6 — Publish the aggregates back.** If counts are collected, the aggregate is
  public (a static dashboard). Collecting-without-publishing is extractive; the
  precedent that earned trust (Debian popcon, Syncthing) published.
- **P7 — Off in CI / non-tty / air-gapped by default.** Auto-detect and suppress:
  `CI` env, no controlling tty, container-without-tty, `HTTPS_PROXY` unset in an
  air-gapped context. Respect `HTTPS_PROXY` when set.
- **P8 — Downloaded instructions never auto-execute.** (F3.) Anything pulled from
  upstream lands as a `wiki_draft` requiring explicit `wiki_approve`. No path
  writes an upstream prompt straight into the composable prelude.

**Meta-rule:** if a feature can't satisfy P1–P8 without contortion, prefer not
building it. The "what NOT to build" section (§8) applies these ruthlessly.

---

## 2. Precedent table (what earned trust vs burned it)

Distilled from primary sources; full URLs in §10.

| Project | Model | Default | ID scheme | Trust outcome |
|---|---|---|---|---|
| **Debian popcon** | pkg list weekly | **opt-in** | random 128-bit UUID, host-local, stripped from public data | trusted 15+ yr; Ubuntu still dropped it from default for stricter privacy |
| **Syncthing** | feature counts, 24h | **opt-in** | none persistent; aggregate-only public dashboard | high trust — *preview-exact-payload-before-enable* is the cited reason |
| **Homebrew** | install/command events | **opt-out** (loud first-run disclosure) | aggregated, no UUID; moved GA→self-hosted InfluxDB & deleted old data | recovered trust *after* the GA→InfluxDB migration; early opt-in demands existed |
| **VS Code** | editor/usage telemetry | **opt-out**, on by default | — | burned trust; spawned VSCodium; repeated "leaks after disabling" issues |
| **Tailscale** | operational metadata inc. IP | not opt-out | — | concerns re: Five-Eyes jurisdiction + DNS logging |

**Reading:** opt-in + public dashboard + preview = trust (popcon, Syncthing).
Opt-out-on-by-default = backlash (VS Code). Homebrew shows opt-out *can* be
survived but only with loud disclosure and a visible privacy migration — a bar a
solo privacy-averse dev shouldn't want to clear. **Default to the Syncthing
model.**

---

## 3. F1 — Update-check as a usage count

### 3.1 The crux: install/version counts are already free

The shipped probe hits `pypi.org/pypi/yadgar/json`, so **PSF owns those request
logs and the dev sees zero counts.** But the count the dev wants already exists
elsewhere, at zero privacy cost and zero infra:

- **PyPI download counts** — the JSON API's download field is deprecated and
  returns `-1`; the real data is the public BigQuery dataset
  `bigquery-public-data.pypi.file_downloads` (Linehaul-fed, ~180-day window),
  surfaced free by **pypistats.org** and its API. Rows carry
  **installer type** (`pip` / `pipx` / `bandersnatch` / …) and **Python version /
  platform** — so the pipx install path is directly countable, broken down by
  version and platform, *with no yadgar-side telemetry at all.*
- **Docker Hub pull counts** — the `docker.io/openfantasy/yadgar` (+ backend)
  repos expose pull totals via the Docker Hub API; covers the container install
  path.
- **nix / brew** — long-tail; nix has no central count (acceptable blind spot),
  brew analytics would count formula installs if a tap is published (opt-out
  upstream — out of scope).

**Conclusion:** cumulative installs + version-adoption curve + install-method
split are answerable today by a scheduled read of pypistats + Docker Hub. This is
the privacy-first answer and it is the *lead recommendation*. A `yadgar stats
downloads` CLI (or a maintainer cron writing a static JSON) that pulls these is
~a day of work and collects nothing from users.

### 3.2 What a custom endpoint buys — and its cost

pypistats/Docker Hub give **downloads**, not **active running installs**. The only
increment a custom endpoint adds:

- *active vs cumulative* — how many installs are actually *running* (a download is
  not a live daemon; churn is invisible to pypistats).
- *which versions are live right now* (vs ever-downloaded).
- *install-method of running daemons* (pypistats sees the download tool, not
  whether that install is still alive months later).

That increment is real but modest. Against it: hosting a service, a retention
policy, an abuse surface, and the trust cost of *any* phone-home beyond the PyPI
read. Per the meta-rule, **the custom count endpoint is proposed as OPTIONAL and
gated behind an explicit user decision (OQ-F1-1),** not as the default build.

### 3.3 If the custom count endpoint IS built — minimal design

Only if OQ-F1-1 resolves "yes, active-install count is worth it":

- **Cadence:** on daemon start + daily with jitter (± up to 12h) to avoid a
  thundering-herd timestamp signature. Not per-recall, not per-tool-call.
- **What's sent — justify every field (P1):**

  | Field | Value | Justification | Keep? |
  |---|---|---|---|
  | `version` | `5.123.0` | the whole point: which versions run | **yes** |
  | `platform` | `linux-x86_64` (os-arch only) | portability signal; coarse | yes |
  | `install_method` | `pipx`/`container`/… (already local) | which path to support | yes |
  | `py_version` | `3.14` minor only | drop-EOL-python decisions | maybe |
  | anything else | — | no justification | **no** |

  No hostname, no locale, no timezone (timezone leaks geography), no uptime here
  (that belongs to opt-in F2, not the version ping).

- **Counting WITHOUT tracking — three options, analysed:**

  | Option | Mechanism | Counts | Trust trade |
  |---|---|---|---|
  | **A. No ID at all** | server `+1` per ping, drops request | pings, not users; daily-active ≈ pings/day (each install pings ≤ ~2×/day) | **best trust, coarsest data.** Can't dedupe; a busy restarter over-counts. Recommended default *if* an endpoint exists. |
  | **B. Ephemeral daily-rotating hash** | `sha256(salt_of_the_day)` computed *server-side* from coarse buckets, or client sends nothing and server buckets by day | approximate daily-unique | needs care: any client-supplied stable seed re-introduces tracking. Prefer server-side day-bucketing over client hash. |
  | **C. Random install-UUID (popcon)** | 128-bit UUID stored `~/.local/state/yadgar/`, sent each ping | true unique installs + retention curves | **most data, most identifiable.** popcon survives it via strict opt-in + UUID stripped from public data. For a version *check* this is heavier than justified. |

  **Recommendation: Option A** (count pings, no ID). It satisfies P5 outright and
  P3 trivially. Option C only if the user later wants retention curves *and*
  accepts popcon-grade opt-in discipline.

- **Opt-out vs opt-in (the honest tension):** the shipped posture is **opt-in /
  OFF** (`update_check_on_start: false`). A version *check* is conventionally
  opt-out with loud disclosure (Homebrew). But the count is a *different*
  transmission target than the shipped PyPI check (PyPI can't count for the dev;
  a count needs the dev's own endpoint). Flipping the count to opt-out would (a)
  reverse the documented PRIVACY.md posture and (b) sit poorly with the dev's
  stated ethos. **This plan keeps opt-in as baseline and raises opt-out only as
  OQ-F1-2** — it is a genuine decision, not a default.

- **Where the notice surfaces:** first-run banner (once, on first `yadgar`
  invocation with a tty) + a `project_brief` `recommended_actions` signal (the
  existing rail) + a one-line footer on `yadgar update --check`. NOT in MCP tool
  results (pollutes agent context) and NOT nagging.

- **Server — dumbest thing that works:**
  - *No counts wanted:* a **static `latest.json` on Codeberg Pages** gives
    update-availability with zero server, zero counts. (This is strictly weaker
    than the shipped PyPI probe, which already gives latest-version — so only
    worth it to decouple from PyPI.)
  - *Counts wanted:* one tiny endpoint, `POST /v1/ping` → increments an aggregate
    counter keyed by `(version, platform, install_method, date)`, returns
    `{latest: "x.y.z"}` (so the ping *is* the check — one round trip). Stack:
    a single Cloudflare Worker + Durable Object counter, or a 1-file FastAPI on
    the cheapest VPS/fly.io free tier. **Log retention = aggregates only; the
    web-server access log rotates hourly and is never mined; raw IP dropped at
    the edge (P3).**
  - Cost: static Pages = $0. Worker/DO = $0–5/mo at yadgar's scale. VPS = ~$5/mo.

### 3.4 F1 recommendation

**Ship the free path first (pypistats + Docker Hub reader, no user telemetry).
Treat the custom active-install endpoint as an optional later increment gated on
OQ-F1-1.** This gets the dev "some idea of usage" *immediately* without asking a
single user to opt into anything.

---

## 4. F2 — Optional anonymous stat sharing (STRICTLY OPT-IN)

Greenfield: no `stats.share` knob exists today. F2 is the channel that answers
"how is yadgar *used*", which pypistats can never show (downloads ≠ usage).

### 4.1 Payload schema — coarse buckets, content-free

Everything content-free and **bucketed** where an exact number would narrow
identifiability. Draws from `get_memory_stats()` (`ops.py:139`) locally; sends
only buckets.

| Field | Type | Example | Why bucketed |
|---|---|---|---|
| `version` | exact | `5.123.0` | usage-by-version |
| `memory_count` | bucket | `1k–5k` | exact count is near-unique per install |
| `wiki_pages` | bucket | `1k–2k` | same |
| `agent_prompt_count` | bucket | `10–50` | feature adoption |
| `tool_calls_30d` | bucket | `10k–50k` | volume of use, not which tools' content |
| `uptime` | bucket | `7–30d` | liveness, coarse |
| `install_method` | enum | `pipx` | support prioritisation |
| `platform` | os-arch | `linux-x86_64` | support prioritisation |

Bucket boundaries chosen so no bucket is likely to isolate one install. **No
per-tool breakdown, no wiki titles, no memory content, no directory paths, no
timestamps finer than a day.** Same no-ID analysis as §3.3 — **Option A (count
submissions, no ID)** is the default; a popcon-UUID is available *only* if the dev
later wants retention curves and the user opts into it explicitly (double opt-in).

### 4.2 Consent UX (the trust surface)

- **`yadgar stats preview`** — prints the EXACT JSON that would be sent, from the
  live local numbers, before any consent (P4). This command is the centerpiece;
  it must exist before the send path.
- **`yadgar stats share --enable` / `--disable`** — explicit toggle; enabling
  triggers a one-time `preview` + a y/n confirmation.
- **Config knob** `stats_share_enabled: false` (default OFF) in the `FIELD_META`
  registry (`config_yaml.py`), env-overridable `YADGAR_STATS_SHARE=…`, honoring
  the existing env-lock (409) semantics.
- **First-run:** the disclosure *mentions* F2 exists but does NOT prompt-to-enable
  inline (avoid consent-fatigue / dark-pattern). Enabling is a deliberate act.
- **Cadence:** daily with jitter, same suppression rules as P7.

### 4.3 Public dashboard (P6)

If F2 collects, the aggregate is published: a static page (Codeberg Pages) with
"N installs sharing stats, memory-count distribution, version split, feature
adoption." Mirrors Syncthing's `data.syncthing.net`. This is the *reciprocity*
that converts collection from extractive to communal — and it doubles as
marketing ("yadgar users curate a median of X memories").

### 4.4 F2 recommendation

Build `preview` + `--enable` + config knob + the coarse bucket schema. Reuse F1's
endpoint (§6) — do not stand up a second service. Default OFF, no inline prompt.
The dashboard is part of the F2 definition-of-done, not a follow-up.

---

## 5. F3 — Agent-prompt sync with upstream (accounts, maybe subscription)

The heavy one. Designed honestly, including the recommendation to possibly **not
build it as a paid product**.

### 5.1 Security first — a poisoned prompt is an injection vector

Shared agent-prompts are **instructions executed by other people's agents inside
their codebases.** A malicious prompt is remote code execution by social
engineering the subscriber's agent (`rm -rf`, exfiltrate secrets, open a PR with a
backdoor). The 2024–2026 registry incident record is unambiguous: npm (287-pkg &
500-pkg typosquat campaigns), PyPI (180+ pkg Dec-2025 campaign, Shai-Hulud
copycat), VS Code Marketplace (Nx Console verified-publisher compromise; 1.5M
installs of malicious AI-assistant extensions). **Any registry yadgar publishes is
a target from day one.**

**Mitigation stack (defense in depth):**

1. **No-auto-apply — the machinery already exists.** Downloads land as
   `wiki_drafts`; promotion to a live composable prompt requires explicit
   `wiki_approve`. Nothing pulled from upstream ever reaches the
   `agent_dispatch_prelude` composition path without a human `approve`. This is
   P8 and it is *already implemented* — the single strongest control. (VS Code's
   catastrophe was silent auto-update; yadgar structurally cannot do that.)
2. **Diff-on-update.** An updated upstream prompt shows a `wiki_diff` against the
   locally-approved version before re-approval. The subscriber sees exactly what
   changed. Reuses `wiki_diff` / versioning.
3. **Provenance display.** Every draft shows `user/pattern@version`, upload date,
   signature status, and download/report counts at approve time. The human
   approves with provenance in view.
4. **Signing + transparency.** Publishers sign submissions; the registry records
   them in an append-only transparency log (Sigstore/Rekor model, keyless OIDC —
   no key management burden on a solo dev). Verify on download; unsigned =
   loud warning. SLSA-style provenance is aspirational, not v1.
5. **Curation gate for the "core" set.** The *curated* set (see 5.3) is
   maintainer-reviewed before it's downloadable — the free tier ships only
   reviewed content. Community uploads are quarantined/unlisted until reviewed or
   until they clear a reputation threshold.
6. **Community reporting.** A "report" action + a 48h review SLA (PyPI's model),
   and an ingestion delay before a new upload is broadly listed (dependency-
   confusion window mitigation).

**Brutal residual risk:** review does not scale to a solo dev; a determined
attacker crafts a *subtly* malicious prompt that reads benign; signing proves
*who* uploaded, not that the content is *safe*; the approve-gate depends on the
human actually reading the diff (fatigue erodes it); a compromised publisher
account bypasses reputation. **The approve-gate is load-bearing and human-
dependent** — it reduces risk, doesn't eliminate it. This is the honest reason F3
is the least ethos-aligned feature: it converts yadgar from a local tool that
sends nothing into an operator of an executable-instruction distribution channel,
with all the moderation liability that implies.

### 5.2 Auth — least infrastructure

- **Codeberg / GitHub OAuth** (yadgar already lives on Codeberg; the dev's
  audience is developers with such accounts) — no password store, no email-
  verification flow, no credential-reset burden. **Recommended.**
- Fallback: email magic-link (no passwords) if OAuth-only excludes too many.
- **What the account stores:** a stable pseudonymous publisher handle, OAuth
  subject id, uploaded pattern list, and (if paid) a Stripe/LiberaPay customer
  ref. **Nothing is fused with F1/F2** (P5) — the telemetry channels stay
  anonymous; accounts are a separate, deliberately-authenticated surface.

### 5.3 Sync semantics — composing with the genesis/wiki split

Yadgar already has a two-tier local model (ADR-0091):
**file = law** (`yadgar/core/seed/materials/agent_prompts.yaml`, the seeded
genesis corpus) → **wiki = practice** (live pages, `agent_dispatch_prelude`
composes from these) → **backflow** (manual/audited promotion of battle-tested
wiki patterns back into genesis, before releases that touch seed materials).

F3 adds a **third tier above genesis**: an upstream registry. It composes cleanly:

```
upstream registry (user/pattern@version)
        │  download (explicit)
        ▼
   wiki_drafts  ──approve──▶  local wiki (practice)  ──backflow──▶  genesis (law)
        │                          │
        └ diff-on-update           └ existing composition into prelude
```

- **Namespacing:** `user/pattern@version` (npm-style). Local patterns keep their
  bare slug; upstream ones carry the `user/` prefix until approved-and-localised
  (at which point they become ordinary local wiki pages, decoupled from upstream).
- **Version / conflict model:** upstream versions are immutable
  (`user/pattern@1.2.0` never mutates; a change is `@1.3.0`). On download of a
  newer version of an already-approved pattern → `wiki_draft` + `wiki_diff` vs the
  approved local copy → re-approve or ignore. Local edits to an approved pattern
  *fork* it (it's now yours); a later upstream pull doesn't clobber — it offers a
  diff. **No auto-merge, ever.**
- **Seed corpus interaction:** the seeded genesis set stays the offline default
  and is never overwritten by sync. Upstream sync is *additive discovery*, not a
  replacement for the local law. A user with no account gets the full seeded
  experience.

### 5.4 Business — free vs paid, and whether money is worth it

- **Suggested split:** free = *download* of the curated core set + community-
  reviewed patterns (this is the growth engine; friction here kills adoption).
  Paid = *publish* under a stable handle + *private team sync* (a team shares a
  namespace not visible publicly). Rationale: charge the value-capture side
  (teams standardising internal disciplines), keep the value-*spread* side free.
- **Payment rails for a solo dev:** LiberaPay (0% platform, ~2–3% processor, EU,
  KYC only at payout) or GitHub Sponsors (0% fee) for *donations*; Stripe
  (~2.9%+$0.30) only if a genuine *subscription* with private-team-sync is built —
  Stripe is the realistic rail for recurring SaaS billing but adds KYC, tax, and
  dunning burden. Ko-fi/Patreon fees (5–15%) are worse for this shape.
- **Does money change the trust story?** **Yes, and mostly for the worse.** F1/F2
  are "solo dev wants to know his work is used" — sympathetic, easy to trust.
  F3-with-a-subscription is "solo dev runs a paid executable-instruction
  marketplace" — that carries moderation duty, abuse handling, refund/support
  load, tax/KYC, and a fiduciary-ish expectation. It also invites the cynical read
  that telemetry exists to feed a funnel. **Keep money entirely out of F1/F2**
  (already the plan) and treat F3-paid as a *separate product decision*, not a
  telemetry feature.
- **Honest flag:** for a solo dev, **F3's revenue is likely NOT worth its
  moderation + abuse + support + injection-liability burden.** The defensible
  first move is **free, community-driven, download-only, curated-core-only** —
  ship the *download* half (which reuses the existing draft/approve machinery and
  adds little surface), publish a curated set the dev personally vetts, and defer
  *publish/accounts/payments* until there's demonstrated demand and a moderation
  plan. If publish never ships, F3 is just "a curated upstream you can pull the
  core disciplines from," which is low-risk and genuinely useful.

### 5.5 F3 recommendation

**Phase F3 in two clearly-separated halves.** Half A (download-only, curated-core,
no accounts, drafts+approve+diff+signing-verify) is defensible and rides existing
machinery. Half B (accounts + publish + private-team-sync + payments) is a
distinct product with real liability — **default recommendation: defer, possibly
indefinitely; do not tie it to the telemetry story.**

---

## 6. Shared minimal-infra section (one tiny API for F1 + F2)

If any server is built (i.e. OQ-F1-1 or F2 ships), it is **one** service, not
three:

- **Surface:**
  - `POST /v1/ping` → `{latest: "x.y.z"}` — F1 count + version check in one trip.
  - `POST /v1/stats` → `204` — F2 opt-in bucket submission (only ever hit by
    opted-in installs).
  - `GET /v1/dashboard.json` → static-ish aggregate for the public page (P6).
  - (F3 Half-B, if ever) a *separate* authenticated service — NOT this one.
- **Data model:** counter tables only — `(endpoint, version, platform,
  install_method, date) → count`. **No raw-row table. No IP column.** (P3.)
- **Retention:** aggregates indefinitely (they're already anonymous); web-server
  access log rotates hourly, never analysed, IP dropped at the edge.
- **Hosting / cost:**
  - Cheapest: **Cloudflare Worker + Durable Object** (or KV) — $0 at yadgar scale,
    edge-drops IPs, no VPS to patch.
  - Alt: 1-file FastAPI on fly.io free tier / a $5 VPS.
  - Static dashboard + `latest.json`: **Codeberg Pages, $0.**
- **Ops burden:** near-zero for the Worker path (no server to maintain). This is
  the recommended host precisely because a solo privacy-averse dev should not run
  a data-collecting VPS if an edge counter suffices.

---

## 7. Phased build order + effort estimates

| Phase | Scope | Effort | Depends on |
|---|---|---|---|
| **0** | `docs/reference/privacy.md` extension for F1-count + F2 (before any code) | 0.5d | — |
| **1** | **F1-free:** `yadgar stats downloads` reads pypistats + Docker Hub API; optional maintainer cron → static `usage.json`. Zero user telemetry. | 1–2d | — |
| **2** | **F2 preview:** `yadgar stats preview` prints exact bucketed JSON from local `get_memory_stats()`. No send path yet. | 1d | bucket schema |
| **3** | **Shared endpoint** (Worker/DO): `/v1/ping` + `/v1/stats` + counters + `dashboard.json`. Static dashboard page. | 2–3d | Phase 0 |
| **4** | **F2 send:** `stats_share_enabled` knob + `--enable/--disable` + jitter cadence + P7 suppression + first-run disclosure copy. | 2d | 2, 3 |
| **5** | **F1 custom count** (OPTIONAL, gated OQ-F1-1): wire daemon-start `/v1/ping`, Option-A no-ID. | 1d | 3 |
| **6** | **F3 Half-A:** upstream download-only — registry read API, `yadgar prompts pull user/pattern@ver` → `wiki_draft`, diff-on-update, signature-verify-on-download, curated-core seed. | 5–8d | draft/approve (exists) |
| **7** | **F3 Half-B** (DEFERRED / product decision): OAuth accounts, publish, private-team-sync, payments, moderation/report pipeline. | 3–6wk+ ongoing | 6 + moderation plan |

**Sequence rationale:** Phases 1–2 deliver value *and* the trust surface (preview)
before a single byte is collected. Phase 3 is the only infra. Phase 5 is
opt-*in* to build at all. Phase 7 is fenced off as a product, not a feature.

---

## 8. What NOT to build

- **A custom install-count endpoint as the *first* move.** pypistats + Docker Hub
  already answer "how many installs / which versions / which install tool" for
  free. Build the reader (Phase 1) first; only add the endpoint if the
  active-vs-downloaded increment is judged worth it (OQ-F1-1). *Lead entry — the
  privacy-first and ethos-aligned answer.*
- **Opt-out anything.** Do not flip the version check to opt-out; do not make F2
  opt-out. (Raised only as OQ-F1-2 for the user; default is opt-in.)
- **Any persistent user/install identifier by default.** No popcon-UUID unless the
  user later explicitly wants retention curves *and* accepts opt-in discipline.
- **Per-tool / content-level stats.** Which tools, wiki titles, memory content,
  directory paths — never. Buckets only.
- **A second service.** F1 and F2 share one endpoint. F3 is separate only because
  it's authenticated.
- **Fusing accounts (F3) with telemetry (F1/F2).** Keep the anonymous channels
  anonymous; never key a ping to an account.
- **F3 publish/payments before demand.** Do not build the paid marketplace,
  accounts, or Stripe integration speculatively. The moderation + injection
  liability is real and unbounded for a solo dev. Ship download-only-curated
  first; let demand pull Half-B, gated on a written moderation plan.
- **Auto-apply of upstream prompts.** Structurally forbidden (P8); the draft/
  approve gate is non-negotiable.
- **A telemetry SDK / OTLP-to-a-collector phone-home.** The existing `/metrics`
  is local-scrape and stays local; do not repurpose it as egress.

---

## 9. Open questions FOR THE USER (decision-shaped)

- **OQ-F1-1 — Is the active-install count worth a custom endpoint?** pypistats +
  Docker Hub give downloads/versions/install-tool for free. A custom `/v1/ping`
  additionally gives *active-vs-cumulative* and *which versions are live now*. Is
  that increment worth hosting an endpoint + owning a retention policy? **(If no →
  Phase 5 is dropped; F1 is Phase 1 only, zero telemetry.)**
- **OQ-F1-2 — Opt-in or opt-out for the count/stats?** Shipped posture is opt-in/
  OFF and `docs/reference/privacy.md` documents it. Opt-out (Homebrew-style, loud
  disclosure) yields far better numbers but reverses the documented stance and
  sits against your stated ethos. Hold opt-in, or accept opt-out with loud
  first-run disclosure? **(Plan default: opt-in.)**
- **OQ-F2-1 — Bucket boundaries.** Are the proposed buckets (§4.1) coarse enough?
  Do you want *any* exact field beyond `version`, or is exact-`version` +
  everything-else-bucketed the right line?
- **OQ-F2-2 — Publish the dashboard?** Committing to a public aggregate dashboard
  (P6) is the trust-earning move but a standing (small) maintenance item. In, or
  collect-privately-and-summarise-occasionally?
- **OQ-INFRA-1 — Host: Cloudflare Worker/DO vs fly.io/VPS?** Worker = $0, no
  server to run, edge-drops IPs (recommended). VPS = full control but you run a
  data-collecting box. Preference?
- **OQ-F3-1 — Build F3 at all, and if so, only Half-A?** Half-A (download-only
  curated-core, rides existing draft/approve) is low-risk and useful. Half-B
  (accounts/publish/payments) is a separate product with real moderation +
  injection + support liability for a solo dev. Ship A and defer B? Or skip F3
  entirely and keep disciplines a local + genesis concern?
- **OQ-F3-2 — If Half-B ever: money or donations?** A subscription (Stripe,
  private-team-sync) changes the trust story from "solo dev curious about usage"
  to "solo dev runs a paid instruction marketplace." Would you rather never charge
  (LiberaPay/Sponsors donations only) even if publish ships?
- **OQ-F3-3 — Signing bar for v1.** Sigstore keyless (OIDC, no key management) vs
  a lighter "publisher-attested + report-driven" gate for the first cut? Signing
  proves *who*, not *safe* — is it worth the v1 complexity, or does the
  approve-gate + provenance display suffice initially?

---

## 10. Sources (precedent)

- Homebrew Analytics — https://docs.brew.sh/Analytics ; GA→InfluxDB migration
  discussion https://news.ycombinator.com/item?id=36628013
- Debian popcon — https://popcon.debian.org/ ; FAQ
  https://salsa.debian.org/popularity-contest-team/popularity-contest/raw/master/FAQ
- Syncthing usage reporting — https://docs.syncthing.net/users/security.html ;
  https://forum.syncthing.net/t/infrastructure-report-2-usage-reporting/22846 ;
  dashboard https://data.syncthing.net
- VS Code telemetry issues — https://github.com/microsoft/vscode/issues/123037 ,
  https://github.com/microsoft/vscode/issues/176269
- Tailscale privacy — https://tailscale.com/privacy-policy
- PyPI download stats — https://packaging.python.org/guides/analyzing-pypi-package-downloads/ ;
  BigQuery https://docs.pypi.org/api/bigquery/ ; https://pypistats.org/about ;
  Stats API https://docs.pypi.org/api/stats/
- Payment rails — https://en.liberapay.com/ ; Codeberg×LiberaPay
  https://docs.codeberg.org/integrations/liberapay/
- Supply-chain / signing — https://sigstore.dev ; https://slsa.dev ;
  npm trusted publishers https://docs.npmjs.com/trusted-publishers/ ; incident
  reporting e.g. https://about.gitlab.com/blog/shai-hulud-copycat-campaign-targets-python-developers/

---

## Audit (folded from telemetry-trio 2026-07-13)

Three verdicts from the follow-up audit (telemetry-trio-2026-07-13.md, now archived):

1. **SPLIT into 3 independent tracks.** F1, F2, F3 have no shared code or deployment dependency — deferred/dropped independently.
2. **F3 Half-B (registry with accounts/payments) — CUT, not deferred.** Unbounded injection/moderation liability for solo dev. Decision is permanent; do not re-scope into any future feature.
3. **Stale file:line reference fixed.** `config_yaml.py:1035` was wrong; canonical locations are `config.py:924` (default), `config_registry.py:376-378` (env registry), `config_yaml.py:1027` (FIELD_META).

## Cross-references (in-repo)

- `docs/reference/privacy.md` — shipped v5.48 update-check privacy policy (F1 baseline)
- `yadgar/core/cli/update.py` — shipped update CLI (do not re-design)
- `yadgar/_shared/config.py:924` — `update_check_on_start` default; `config_registry.py:376-378` — env registry; `config_yaml.py:1027` — FIELD_META
- `yadgar/_shared/storage/ops.py:139` — `get_memory_stats()` (F2 local source)
- `yadgar/core/seed/materials/agent_prompts.yaml` — genesis corpus (ADR-0091, F3 tier-3 composes above this)
- `docs/reference/decisions.md` PD-37 — distribution/update train (v5.45–v5.47)
