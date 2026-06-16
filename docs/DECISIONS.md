# Decisions Log

**Purpose:** persistent record of ALL decisions — from audits, plan design, session-level open questions, and architectural code-level deferrals. Future agents (human or LLM) MUST consult this file before recommending changes, to avoid re-litigating already-decided questions and to surface revisit triggers.

**Format:** append-only chronological log. Each session or audit produces one section. Never edit prior entries; if a decision is reversed, add a NEW entry in the next applicable section that supersedes the old one (with `**Supersedes:**` link).

**Storage:** this file is canonical. Mirrored to wiki page `yadgar-audit-decisions-log` for searchable cross-session access. All "we said we'd do this later" notes live HERE — not in individual plan files (those rot).

---

---

## 2026-06-16 Directory scoping subsystem (v5.60–v5.66)

### PD-46 — Drop 'system' from directory eligible set (DONE — v5.65)

**Decision:** Remove `'system'` from the eligible set checked by `is_directory_eligible`. The eligible set is now `{caller_dir, 'global', '', None}` only.

**Rationale:** `'system'` was the primary mis-stamp sink — rows that failed directory derivation landed there with no real project affiliation, then surfaced in every caller's results. Removing it from eligibility cuts cross-project leakage at the retrieval layer immediately, before the corpus re-stamp migration completes. Rows still stamped `'system'` will not be returned by retrieval; they become invisible (not deleted) until the re-stamp sweep reclaims them.

**Note:** `dominant_directory._SENTINELS` still contains `'system'`, but for the opposite purpose: excluding sentinel values from the directory vote when deriving a stamp. These roles are unrelated.

**Affected code:** `storage/directory.py::_ALWAYS_ELIGIBLE`; `hooks/prompt-recall.py` supplement WHERE (`directory_context != $dir` → `directory_context IN ('', 'global')`).

---

### PD-47 — Hard-require `directory` on recall() / wiki_query() (DONE — v5.62)

**Decision:** `recall()` and `wiki_query()` raise `ValueError` if `directory` is absent. No `os.getcwd()` fallback.

**Rationale:** The daemon runs containerized. `os.getcwd()` inside the container returns the container's filesystem path, not the caller's project directory. Using it as a directory filter silently mis-scopes every read — worse than failing loudly. Callers already have the project path (Claude Code passes `cwd` to hook scripts; MCP clients supply it explicitly). Raising `ValueError` forces callers to fix the omission rather than accepting silently wrong results.

**Affected code:** `retrieval/core.py`, `server/tools/recall.py`, `server/tools/wiki.py` entry points.

---

### PD-48 — Write-time directory stamp derivation via dominant_directory() (DONE — v5.64)

**Decision:** Auto-generated memories (strengthened curations, CLS promotions, dream summaries) derive their `directory_context` stamp from `dominant_directory(source_candidates)` instead of hardcoding `'system'` or `'global'`.

**Rationale:** Hardcoded `'system'` was the write-side partner of PD-46's read-side problem. Newly minted derived memories were guaranteed to be invisible (after PD-46 drops `'system'`) and unrecoverable without a re-stamp. `dominant_directory()` inspects the source memories' `directory_context` values, excludes sentinel values (`None`, `''`, `'global'`, `'system'`) from the vote, and returns the single unambiguous real project path. When sources span multiple projects or are all sentinels, it returns `'global'` — appropriate for cross-project derived knowledge (dreams).

**Affected modules:** `curation/strengthen.py`, `cls_store/promotion.py`, `sleep_compute/dream.py`.

---

### PD-49 — Purge by access recency for derived/auto-abstracted memories (IN PROGRESS — v5.66)

**Decision:** Auto-abstracted and derived memories (tagged `_auto`, `_derived`) are pruned when they are old AND have not been accessed recently, even if their `access_count` is non-zero.

**Rationale:** Prior rule spared any memory with `access_count != 0`, regardless of when it was last accessed. This accumulated stale derived memories that were accessed once (e.g., during the consolidation run that created them) but never retrieved by a real caller. The new rule adds a recency gate: `last_accessed < now() - PURGE_RECENCY_CUTOFF_DAYS`. Memories actively serving recall remain protected; memories that have gone cold despite a non-zero access count are reclaimed.

**Affected code:** `curation/prune_passes.py`. Controlled by `PURGE_RECENCY_CUTOFF_DAYS` env knob.

---

## 2026-06-06 Internal dev workflow vs production CI separation

## PD-45 — Internal dev workflow vs production CI separation (2026-06-06)

**Decision:** All Forgejo Actions workflow jobs gated to `workflow_dispatch`-only triggers during internal development phase. Push events (master pushes + tag pushes) DO NOT fire jobs. Manual UI trigger required to run any workflow.

**Rationale:** User direction 2026-06-06: "no our internal development workflow is different from ci workflow. internal was put in place to prevent burning minutes while we fast developing. ... when pushing tags, tests start to run. that is wrong. tests should ONLY run in pr."

Forgejo workflows currently consume 500 prepaid Docker Build Cloud minutes per multi-arch image build (linux/amd64+linux/arm64 in single buildx run). v5.46.7 tag push hit the limit. Internal dev workflow per anchors 490140 + 491179 already established local-only amd64 build + skip dockerhub push. PD-45 codifies that the Forgejo CI surface is PRODUCTION-only and stays disabled until production-ready.

**Affected workflows:**
- `.forgejo/workflows/ci.yaml` — `on.push.tags` removed (tests no longer fire on tag push); `build` job gated to `workflow_dispatch`
- `.forgejo/workflows/release.yaml` — all 4 jobs (build-wheel, build-sbom, attach-to-release, publish-pypi) gated to `workflow_dispatch`
- Header comment blocks added to both workflows documenting the gate

**Internal dev workflow (current):**
1. Code change → merge to master → push code only (no tag)
2. amd64 local build via build agent (`podman build --arch amd64 -t docker.io/openfantasy/yadgar:VER`)
3. Manual nix repo bump (`modules/home/yadgar.nix`)
4. User applies via `home-manager switch`
5. Manual PyPI publish via `twine upload` (when desired) using `op://Private/PyPI/yadgar-api-token`

**Production transition (future):**
1. Remove `if: github.event_name == 'workflow_dispatch'` gates from job definitions in both workflows
2. Remove the gate header comment block (or update to "production-active")
3. Re-add `tags: ["v*"]` to ci.yaml `on.push` block if test runs on tag are desired
4. Verify SBOM cyclonedx-bom install (deferred to production transition)
5. Verify Build Cloud quota refresh / budget

**Reversibility:** Per-workflow per-job — remove `if:` gates to restore push/tag triggers. Per-event — re-add `tags: ["v*"]` to ci.yaml.

**Deferred:** SBOM cyclonedx-bom install issue in release.yaml build-sbom job. Production-transition concern, not internal-dev. v5.46.8 does NOT fix this; documented in CHANGELOG + here.

---

## 2026-06-05 v7 "real-time synthesis" reframe + usefulness audit

## PD-44 — v7 reframe: "Asynchronous Progressive Synthesis"; usefulness audit raises retire-vs-keep question (2026-06-05 evening)

**Decision:** v7 framing "Real-time synthesis" is dead after PD-43 (LLM inference is pluggable; sub-100ms only realistic for local LLM hardware or team backend). For personal mode the best-case latency with pre-built `yadgar-curator` image + warm container pool + Claude pass-through OAuth is ~3-5s per inference. NOT real-time. Reframe to **"Asynchronous Progressive Synthesis"**.

**Rationale + usefulness audit:**

User direction 2026-06-05 evening — "im getting more causius about the usability of v7. i mean its usefullness". Honest audit follows.

**Why "real-time" framing was attractive originally:** assumed local LLM (sub-100ms inference) was realistic. PD-43 surfaced that local LLM gates out individual users (22+ GB RAM minimum). Without sub-100ms, "real-time" becomes "async with ~3-5s budget" — fundamentally different UX.

**Concrete use cases v7 was supposed to enable + honest assessment:**

| Original v7 use case | Does it actually need <5s synthesis? | v6 nightly curator covers? |
|---|---|---|
| Surface emerging patterns immediately | NO — patterns aren't actionable within 5s window | YES (batch finds same patterns) |
| Cross-pollinate memories as they arrive | NO — user isn't watching memory engine in real-time | YES (nightly cross-correlation) |
| Real-time relevance scoring | NO — recall already does this on demand | N/A (already in v5.x recall) |
| Active debugging suggestions | MAYBE — but rare + user can manually trigger recall | Partial (debugging is interactive) |
| Contradiction detection | YES — but THIS already exists at write-time in v5.39+ | N/A (v5.x boundary validation, not v7 synthesis) |
| Wiki page auto-update suggestions | NO — nightly cadence acceptable | YES (curator generates suggestions) |

**Conclusion: v7's distinct value over v6 nightly curator + v5.x write-time gates is thin.** The "real-time" promise was the differentiator; without it, v7 risks being speculative-infrastructure pattern (same anti-pattern class as branch-on-wiki original rationale archaeology — built because the concept sounded compelling, not because users demand it).

**Three options going forward:**

**Option A — Keep v7 as low-priority slot, rename + downscale:** ship v7 as "Asynchronous Progressive Synthesis" with queue+scheduler+container-pool for ~3-5s latency. Personal mode benefits when user explicitly wants live feedback. Risk: 2-3 months engineering for marginal UX gain over v6 batch.

**Option B — Retire v7 entirely:** v6 LLM curator at batch cadence covers 90%+ of synthesis value. Write-time contradiction detection (v5.39+) covers the only "must be sub-second" use case. Move any remaining v7 concepts into v6.x sub-releases as opt-in enhancements. Free up v7.0.0 slot for something with proven user demand. Recommend.

**Option C — Scope v7 as team-only:** v7 ships only in team mode (v8). Centralized warm-pool + shared inference makes ~1-2s realistic. Personal users never get v7; v6 nightly is canonical for them. Compromise: keeps v7 alive but limits scope to where the infra justifies it.

**LEAN: Option B (retire v7).** Brutal honesty: v7 was speculative. v6 nightly + v5.x write-time gates + v8 team backend cover the use cases. Engineering effort better spent elsewhere (v8 team rollout, v6 curator quality, v5 ongoing).

**RESOLVED 2026-06-05 night — Option B chosen.** User direction: "im thinkig b as well then v8 becomes v7." Consequences applied:
- Original v7 ("real-time synthesis") RETIRED. Slot vacated. Concepts absorbed by v6 batch curator (covers ~5 of 6 original use cases) + v5.x write-time gates (already cover contradiction detection).
- v8 ("team usability") RENUMBERED to v7. Plan file renamed: `docs/PLAN_V8_TEAM_USABILITY_SKELETON.md` → `docs/PLAN_V7_TEAM_USABILITY_SKELETON.md`. Sub-slots renamed: v8.0 → v7.0, v8.1 → v7.1, v8.2 → v7.2, v8.2.5 → v7.2.5, v8.3 → v7.3, v8.4 → v7.4.
- Roadmap pipeline `docs/yadgar-roadmap-future-improvements.md`: "v7.0.0 Real-time synthesis" row deleted; "v8.0.0 Team usability" row renamed → "v7.0.0 Team usability"; cross-references updated.
- PD-43 still applies — "v6 + v7" (team) + "v8" (no longer exists in this nomenclature). Where PD-43 references v8, treat as "v7" post-renumber. Historical PD body left intact per DECISIONS.md convention (append-only log; renumber is the resolution recorded HERE in PD-44).

**Effort freed:** ~2-3 months engineering that would have gone to v7 real-time synthesis. Redirect to v7 (formerly v8) team rollout + v6 curator quality + ongoing v5.x.

**Implications for v8 plan:**

- v8.2.5 (team-backend inference) absorbs the "fast inference" value even without v7 — team users get sub-second curator + synthesis via centralized inference regardless of v7 status.
- If Option B chosen, v8 plan does NOT need to coordinate with a separate v7 plan.
- If Option C chosen, v7 plan slots BETWEEN v8.2.5 and v8.3.

**Cross-references:**
- `docs/PLAN_V8_TEAM_USABILITY_SKELETON.md` — references PD-43; will be updated based on Option choice
- Future `docs/PLAN_V7_*.md` — write only if Option A or C selected
- Roadmap pipeline (`docs/yadgar-roadmap-future-improvements.md`) — v7.0.0 row needs update based on Option

---

## 2026-06-05 v6/v7/v8 LLM inference strategy (cross-cutting architecture)

## PD-43 — Pluggable LLM inference; default OFF for personal mode; 4 backend paths (2026-06-05)

**Decision:** v6 (LLM curator scaffolding) + v7 (real-time synthesis) + v8 (team mode) all depend on LLM inference for curator + synthesis tasks. Hardware barrier for local LLM (DeepSeek 22+ GB RAM minimum, GPU usually needed for usable latency) makes "local LLM required" a gatekeeping anti-feature for individual users. Instead: **inference is pluggable; default OFF for personal mode; 4 backend paths, user picks**.

**Rationale:** User direction 2026-06-05 evening — "the llm curator and realtime synthesis is major problem in my mind. it needs massive hardware on users side. so they should be optional to enable. otherwise its useless but in team mode if the backend is in a strong server, we can pull it off unless we can somehow use the claude or whatever agent the user uses to overcome the challenge. running even a deepseek needs like 22 gb ram minimum"

**Five inference paths (curator config: `inference.backend = ...`):**

| Path | Backend ID | User cost | Latency | Privacy | Nightly background OK? |
|---|---|---|---|---|---|
| Local LLM | `local:<model>` | hardware (22+ GB RAM, GPU recommended) | sub-100ms | full local | yes |
| Remote API | `api:<provider>` (claude, groq, together, fireworks) | $0.001-0.01/inference | 200-2000ms | API provider sees data | yes |
| Claude pass-through (interactive) | `claude-passthrough:interactive` | $0 (existing sub) | conversational pace | Anthropic sees data | NO (requires open session) |
| Claude pass-through (background, OAuth) | `claude-passthrough:headless` | $0 (existing sub) | seconds | Anthropic sees data | YES (via `claude -p` + shared OAuth) |
| Claude Code via Ollama | `claude-via-ollama:<model>` | $0 (local) OR free (cloud variants) | seconds | full local OR Ollama Cloud sees data | YES |
| Team backend (v7 only) | `team-backend` | team pays infra | sub-100ms | within team | yes |

**Ollama-via-Claude-Code path added 2026-06-06 (per user pointer to https://docs.ollama.com/integrations/claude-code).** Mechanism: Ollama exposes Anthropic-compatible API locally on `http://localhost:11434`; Claude Code talks to it via three env vars (`ANTHROPIC_AUTH_TOKEN=ollama` + `ANTHROPIC_API_KEY=""` + `ANTHROPIC_BASE_URL=http://localhost:11434`) OR via `ollama launch claude --model <name> --yes -- -p "<prompt>"` simplified launcher. Headless `claude -p` mode supported — viable for nightly curator. Recommended models per Ollama docs: kimi-k2.5:cloud, glm-5:cloud, minimax-m2.7:cloud, qwen3.5:cloud, glm-4.7-flash, qwen3.5. "Cloud" variants relay through Ollama's cloud (free remote inference, no local hardware) — strong fit for hardware-constrained personal users who want curator quality without API costs. Context window: Claude Code recommends 64k+ tokens (some small local models may not meet without truncation; cloud variants fine).

**Why this matters for v6/v7:** lowers hardware barrier dramatically. Local hardware path (22+ GB RAM) → free Ollama-cloud variant (no hardware) gives users a third "free + decent quality" lane between paid API and Claude pass-through OAuth. Quality lower than Claude proper but acceptable for bounded curator tasks (summarize, extract entities, find contradictions, propose anchors).

**Claude pass-through clarification (per user 2026-06-05):** interactive pass-through (dispatching subagents from open Claude session) doesn't work for nightly curation since user's session isn't always open. Resolution: yadgar daemon needs access to user's OAuth credentials so `claude -p` (programmatic non-interactive Claude Code) can run in the background with user's auth. Claude Code stores credentials at `~/.claude/.credentials.json` (verified 2026-06-05 on host); yadgar daemon reads them to authenticate `claude -p` invocations. Trust model: yadgar daemon already holds other secrets (DB passwords, MCP auth token); OAuth share is a continuation of existing trust boundary.

**VALIDATED MECHANISM (proof-of-concept 2026-06-05 evening):**

End-to-end probe succeeded — `claude -p` invocation returned expected `HEADLESS-PROBE-OK` response via OAuth-mounted credentials in ephemeral container:

```bash
podman run --rm \
  -v ~/.claude/.credentials.json:/root/.claude/.credentials.json:ro \
  -v ~/.claude.json:/root/.claude.json:ro \
  docker.io/library/node:22 \
  bash -c 'npm install -g @anthropic-ai/claude-code && claude -p "<task>"'
```

Properties validated:
- Container ephemeral (`--rm`) — no persistent state from probe runs
- Credentials read-only mount (`:ro`) — host file untouched
- Credentials never leave host filesystem (bind-mount, not copied into image layer)
- No API key needed — OAuth via mounted creds works
- Zero per-inference cost — uses user's Claude subscription
- Authenticated as user identity — same account as interactive Claude
- Background-capable — no open Claude session required

Production implementation pattern for v6/v7/v8 curator + synthesis backends:
1. Yadgar daemon spawns curator container per nightly task (or per inference batch)
2. Bind-mount `~/.claude/.credentials.json:/root/.claude/.credentials.json:ro` + `~/.claude.json:/root/.claude.json:ro`
3. Pass curator prompt via stdin OR `-p "<prompt>"` flag
4. Capture response on stdout
5. Container exits + is removed (`--rm`); host credentials untouched

Performance optimization: cold container with `npm install -g @anthropic-ai/claude-code` adds ~20-30s per invocation. Pre-built image `docker.io/openfantasy/yadgar-curator:VER` with claude-code baked in drops startup to ~1-2s. Pattern mirrors `docker.io/openfantasy/yadgar-ci` image (PD-42 carve-out). v6 plan MUST ship `Dockerfile.curator` + workflow to build + push image alongside yadgar core+backend.

Security observations:
- Read-only mount enforced at container runtime; container processes cannot write to host credential file
- Per-invocation container teardown prevents lateral leakage between curator tasks
- Standard podman/docker process isolation (cgroup + namespace) applies — no shared FS state with other containers
- Audit log captures each invocation (timestamp, task type, prompt hash, response token count)

**OAuth share security considerations:**
- Token stored on user's machine only (yadgar daemon runs locally for personal mode)
- Yadgar daemon scope-restricts use: ONLY for curator/synthesis inference; never for arbitrary `claude -p` runs
- User can revoke at any time by signing out of Claude Code (invalidates the OAuth token at provider)
- Per-team mode: team admin decides whether team server uses team's shared Claude account OR each user's OAuth (with explicit per-user consent flow)
- Audit log: every `claude -p` invocation logged with timestamp + task type

**Default state:** v6 curator + v7 synthesis features are OFF in personal mode by default. User explicitly enables via config + picks an inference path. Team mode (v8) defaults ON with team-backend (centralized inference).

**Implications for v6/v7/v8 plans:**

- **v6 (LLM curator scaffolding):** MUST ship pluggable backend abstraction + at minimum 2 reference backends (Claude API + Claude pass-through headless). Local LLM backend ships as optional drop-in (community-maintained or v6.x sub-release). Curator default OFF; explicit `yadgar config set curator.enabled true` + `curator.backend = ...` required.
- **v7 (real-time synthesis):** rename concept — "real-time" assumed sub-100ms which only works for local LLM or team backend. Better framing: "asynchronous synthesis with progressive results." User-facing UX: synthesis runs in background; results surface when ready. Latency budget governed by chosen backend.
- **v8 (team usability):** team-backend path becomes the natural home for the heavier features. Personal mode supports them with effort; team mode supports them by default. Strengthens v8 value prop ("team gets curator/synthesis without per-user hardware cost").

**Reversibility:** Plan-only decision at this stage. Affects future v6/v7/v8 plan drafts. Can be revisited when v6 plan is written if better architecture emerges.

**Cross-references:**
- `docs/PLAN_V8_TEAM_USABILITY_SKELETON.md` — updated to reference PD-43
- Future `docs/PLAN_V6_*.md` — MUST cite PD-43 + ship 4-backend abstraction
- Future `docs/PLAN_V7_*.md` — MUST cite PD-43 + drop "real-time" framing
- v5.44.0 X2 SubagentStop machinery — extends naturally to interactive pass-through dispatch

---

## 2026-06-05 v5.46.3 → v5.46.6 CI-green cycle (chained release plan)

## PD-42 — Chained CI-failure remediation v5.46.3 → v5.46.6 + custom yadgar-ci image (2026-06-05)

**Decision:** v5.46.2 CI run surfaced 27 issue classes (24 BLOCKING + 3 WARNING) per `docs/CI_ISSUES_2026_06_05.md` @706e61d. Fix in a chained release cycle v5.46.3 → v5.46.6 before v5.47.0 dispatch. Umbrella plan at `docs/PLAN_V5_46_CI_GREEN_CYCLE.md`.

**Rationale:** User direction 2026-06-05 evening — "i need proper planning to fix all the tests (i mean all i need clean 100 percent results) before we move to 5.47. so plan then implement. only build and push when you get to the last one. do it automatically." 12 directional questions answered via AskUserQuestion (release shape, pass def, CI image strategy, tag policy, fixture authority, B2 mechanism, deploy semantics, budget overrun, SBOM fold, self-test coverage, PyPI failure handling, plan doc shape).

**Cycle strategy:**
- v5.46.3 — CI infrastructure (yadgar-ci image + YADGAR_CI_BRANCH env + make availability + pytest-asyncio + SBOM wheel install)
- v5.46.4 — Test fixture refactor (directory_context + vector-dim + harness hardening + migration assertion + DLQ fixtures + token budget)
- v5.46.5 — Missing functions, endpoints, hook files (hook_db_lockdown_check, session-start-context.py, /hooks/session-context, /viz/config, sleep_cycle, dbsize mock)
- v5.46.6 — Behavior fixes + final cleanup + SHIP (circuit breaker probe state, NLI default-OFF test alignment, health endpoint, B18-B21 cascade verification, optional W1+W2 fold, amd64 build + nix bump + tag + PyPI publish + post-ship verification)

**Workflow-rule exception (documented):** `docker.io/openfantasy/yadgar-ci` image PUSHES to dockerhub. Normal yadgar/yadgar-backend images do not push (per workflow rule 2026-05-19). CI consumer images need registry presence so CI runners can pull. PD-42 records the carve-out; rule does NOT change for daily build images.

**Deploy gating:** Only the final release (v5.46.6) gets amd64 build + nix repo bump + tag push + user-applied home-manager switch + post-ship probes. Intermediate v5.46.3/.4/.5 merge to master + push code only. PyPI publish fires only on v5.46.6 tag push.

**Removed/added artifacts:**
- ADD: `Dockerfile.ci` at repo root (custom yadgar-ci image spec)
- ADD: `docker.io/openfantasy/yadgar-ci:5.46.3+` image on dockerhub
- ADD: `YADGAR_CI_BRANCH=master` env var in workflows
- ADD: ~30 LOC light self-tests across the chain
- MODIFY: `.forgejo/workflows/{ci.yaml,release.yaml}` image refs + SBOM install step
- MODIFY: ~50 test files (fixture refactor per B1/B8/B13/B9/B10/B11)
- MODIFY: `yadgar/scripts/hook_runner.py` (B3), `yadgar/hooks/*.py` (B4), `yadgar/server/http.py` (B5+B16), `yadgar/ml_client.py` (B14)

**Reversibility:** Per-release git revert restores prior state. Pre-commit auto-syncs flake.nix on revert. PyPI only gets v5.46.6 so no cleanup needed if cycle aborted before final ship.

**Estimated effort:** ~3 calendar days total. Continue + report at completion if overrun (per Q9 answer).



## PD-41 — Reuse v5.46.2 slot for runtime detection UX hotfix (2026-06-05)

**Decision:** v5.46.2 slot reused for `detect_runtime.sh` + `yadgar-setup.sh` + `Makefile` runtime-detection UX hotfix. Original retired v5.46.2 (cross-repo PR auto-open per PD-40) plan archived to `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md`.

**Rationale:** User testing in fresh VM 2026-06-05 found `yadgar-setup.sh` fails abruptly with stale "Run: yadgar install" message (DP-3 override post-make-canonical) and no install guidance. Per strict-version-order rule, hotfix slots between v5.46.1 and v5.47.0; v5.46.2 was empty (retired by PD-40 with no shipped artifacts). Slot reuse is clean — RETIRED plan archaeology preserved via rename.

**Scope:** `detect_runtime.sh` fixes stale message + adds OS-aware install hints; new `install_runtime.sh` shared helper for interactive podman install prompt (Debian/Ubuntu/Fedora/Arch/Alpine/SUSE/macOS); `yadgar-setup.sh` + `Makefile` both call shared helper (DRY); `--install-runtime` / `--no-install-runtime` flags added to `yadgar-setup.sh`; `INSTALL_NONINTERACTIVE=1` non-interactive gate (existing Makefile var).

**Test seams added:** `YADGAR_TEST_OS_RELEASE`, `YADGAR_TEST_INSTALL_DRYRUN`, `YADGAR_TEST_TTY` — allow pytest to exercise all distro branches without root or TTY.

**macOS scope:** `brew install podman` only; `podman machine init && podman machine start` printed as follow-up guidance, not executed. Full macOS podman-machine automation deferred per PD-38 precedent.

---

## 2026-06-05 v5.46.x scope reduction — drop cross-repo nix PR lane

## PD-40 — Drop cross-repo nix PR auto-open lane (2026-06-05)

**Decision:** v5.46.2 cross-repo nix PR auto-open RETIRED. Replaced with pre-commit hook approach.

**Rationale:** User assessment 2026-06-05 — "leave the nix repo out of scope, instead add precommit to check the versions and also update nix flake version before we commit. much easier." Cross-repo PAT (NIX_BUMP_TOKEN) + workflow job + auto-PR machinery was speculative-infrastructure for a problem better solved at commit-time: when pyproject.toml version bumps, `scripts/sync_version.py` auto-updates flake.nix line 41 + `scripts/check_versions.py` verifies consistency on commit. No cross-repo write needed; yadgar repo's flake.nix IS the public nix distribution surface (nix users install via `nix profile install git+https://codeberg.org/maxagahi/yadgar`).

**User's personal `~/git/nix` (home-manager config):** remains a separate concern outside yadgar v5.46.x cycle. Manual bump + home-manager switch per existing workflow (anchor 490140 + 491179).

**Removed artifacts:**
- v5.46.1 USER_CHECKLIST steps 1+2 (NIX_BUMP_TOKEN PAT + Forgejo secret).
- v5.46.2 plan RETIRED (preserved as archaeological artifact); deliverables nil after PD-39 brew drop + PD-40 nix drop.
- `.forgejo/workflows/release.yaml` open-nix-pr stub: deleted.

**Added artifacts (committed @53de97a):**
- `scripts/sync_version.py` extended to auto-update flake.nix line 41 on pyproject.toml change.
- `scripts/check_versions.py` extended to verify flake.nix consistency.
- `.pre-commit-config.yaml` updated: sync-version + check-versions hooks now include flake.nix in files pattern.

**Reversibility:** If real demand surfaces for cross-repo nix PR (e.g., third-party nix overlay maintenance), can re-add ~1d. Pre-commit hook approach handles >95% of realistic version-sync needs.

---

## 2026-06-05 v5.46.x scope reduction — drop Homebrew lane

## PD-39 — Drop Homebrew lane (2026-06-05)

**Decision:** Homebrew tap distribution lane retired from v5.46.x cycle. Continue distributing via PyPI (mandatory, covers macOS+Linux+Windows via pipx) + nix flake (already in v5.46.0 flake.nix, zero ongoing maintenance).

**Rationale:** User assessment 2026-06-05 — "if pip works then why do i need homebrew? i think pip works in arch as well so why complicate it i just need nix and pip." Brew added zero functional capability beyond PyPI for a Python CLI/daemon tool; speculative-infrastructure pattern (same anti-pattern class as branch-on-wiki original rationale archaeology 2026-06-03). Brew formula maintenance per release (SHA256 + version updates) cost not justified by macOS UX polish gain.

**Removed artifacts:** Formula/yadgar.rb.in (deleted), `.forgejo/workflows/release.yaml` open-brew-pr stub (deleted), v5.46.1 user actions 1+2 (tap repo + BREW_BUMP_TOKEN PAT) + secret step (BREW_BUMP_TOKEN forgejo secret), v5.46.2 open_brew_pr.sh deliverable.

**Preserved:** docs/PLAN_V5_46_0_DISTRIBUTION.md historical brew sections (archaeological reference); PD-39 cross-referenced from all touched plans.

**Reversibility:** Brew lane can be re-added later if real macOS user demand surfaces. Cost: ~1d to rebuild tap repo + Formula + PAT + workflow job.

---

## 2026-06-04 v5.45.1 ship decisions

**PD-38. v5.45.1 ships paper-only macOS launchd. Host verification deferred.**

- **Source:** `docs/PLAN_V5_45_1_MACOS_LAUNCHD.md` DP-A — verifying host availability
- **Decision:** DEFER — ship paper implementation now; fix-ups via post-ship hotfix when macOS host is available.
- **Background:** User directive: "paper-only implementation. User explicitly accepts NO verifying macOS host; fix-ups happen post-ship when host available." DP-A (verifying host) left open. DPs B/C/D resolved via lean recommendations: bootstrap for macOS 11+/load fallback, YADGAR_TEST_PODMAN_MACHINE_SOCKET sentinel for DP-C, RunAtLoad+KeepAlive for DP-D. All cross-platform render tests pass on Linux. 5 darwin-skipif tests document the deferred live probes.
- **Reason:** No macOS machine accessible. Blocking on DP-A would delay macOS support indefinitely; code is correct-by-construction and the deferred items are verifiable the moment a host is available.
- **Revisit triggers:** First macOS host access. Run the 5 verification probes from `MIGRATION_NOTES.md` v5.45.1. If all pass, close PD-38. If any fail, open a hotfix.
- **Artifacts:** `scripts/install/launchd/*.plist.in`, `scripts/install/generate_launchd.sh`, `scripts/install/uninstall.sh` macOS path, `Makefile` enable-units-macos + _enable-units-auto. Test coverage: `yadgar/tests/test_v5_45_1_*.py` (54 pass cross-platform, 5 skipif darwin).

---

## Protocol (how to use this file)

### When running an audit (you = audit agent or human)

1. **Read this file BEFORE recommending changes.** If a recommendation appears here with `Decision: KEEP-AS-IS` / `REJECT` / `DEFER`, do NOT re-recommend it unless its `Revisit triggers` have fired. Instead, write a one-line "previously decided, no new evidence" note in your audit output.
2. If a previously-rejected recommendation NOW has new evidence that triggers revisit, frame it as "RECONSIDER" not "NEW RECOMMENDATION". Link to the prior entry.
3. If your audit produces new recommendations (not previously seen), they're fair game — propose freely.

### When writing a plan, merging a feature, or deferring any "we'll do this later" item

1. Do NOT leave deferrals in plan files only — those rot and become invisible. For every significant deferral, extract it here under the appropriate dated section.
2. Add a one-line pointer in the plan file back to this file. For example: "See `docs/DECISIONS.md` — 2026-05-30 Plan-derived deferrals."
3. Required fields per DEFER / OPEN-QUESTION entry:
   - **Item** (short label)
   - **Source** (file:line or plan name)
   - **Decision** (from categories below)
   - **Reason** (why not now)
   - **Revisit triggers** (when to re-evaluate — REQUIRED for DEFER)
   - **Supersedes** (link if reversing prior decision — optional)

### When acting on an audit (you = main thread synthesizing)

1. For every recommendation in the audit, add an entry here. Even if the decision is "do nothing" — that IS a decision and needs the trail.
2. Commit to master per workflow rule (docs-only direct, set 2026-05-30).

---

## Categories

- **ADOPT:** will implement; assigned version slot. Plan file should exist in `docs/PLAN_V*.md`.
- **DEFER:** valid; not now. Revisit triggers REQUIRED.
- **REJECT:** disagree (strongest evidence required).
- **KEEP-AS-IS:** code already does this OR change rejected due to current evidence.
- **DONE-ALREADY:** audit missed prior implementation (link to commits/plans).
- **PLANNED:** in roadmap, no audit involvement — tracked here for consolidation.
- **OPEN-QUESTION:** raised but no decision yet. Must have an owner or expected resolution path.

---

## 2026-06-01 — v5.35.1 Memory Blocks Follow-ups

**Source:** `docs/PLAN_V5_35_1_BLOCKS_FOLLOWUPS.md`
**Branch:** `fix/v5.35.1-blocks-followups`

### Item 6: `_active_work` canonicalization — DEFER (Option C)

**Item:** Should `_active_work` episodic memory be replaced by a named memory block `active_work`?

**Options evaluated:**
- (A) Keep both — parallel infrastructure, bloat.
- (B) Canonicalize `_active_work` as a memory block — deprecate `update_active_work` MCP tool.
- (C) Defer — design call, no obvious right answer yet.

**Decision: DEFER — Option C**

**Reason:**
- Block hook integration (Items 2-4) shipped in this version and is still new; UX is unproven.
- Canonicalizing `_active_work` as a block risks data loss during migration if the path is buggy.
  Plan risk note: "keep parallel path 1 release before deprecating."
- No user-reported pain with `update_active_work` tool. No compelling functional reason to merge now.
- `update_active_work` vs. `block_update("active_work")` is a surface-area question, not a
  correctness question. Both paths produce the same restore() injection; blocks just add
  always-injected semantics that `_active_work` doesn't have.

**Revisit triggers:**
- v5.50+ after block UX is proven (3+ releases with block hooks stable).
- User or agent explicitly requests the merge / reports confusion between the two surfaces.
- Restore() performance or context-window pressure makes the dual-path a measurable problem.

**If implemented (Option B) — guard rails:**
- Keep `update_active_work` MCP tool alive for ≥1 release, delegating to `block_update`.
- Restore() reads from block if present, falls back to episodic memory tag.
- Migration script to copy existing `_active_work` episodic memory → block for each project.
- No `DELETE` of episodic memory until 2 releases post-migration.

---

## 2026-05-31 — v5.26.0 Adopt-1 Benchmark Ship + D2/D3 RECONSIDER

**Source:** v5.26.0 ship — Phase 1 (retrieval) + Phase 2 (QA) LongMemEval pilot.
**Commit:** v5.26.0 release commit (see CHANGELOG).
**Plan:** `docs/PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md`

### Adopt-1: LongMemEval benchmark — SHIPPED

Adopt item 1 ("Formal benchmarking (LongMemEval / LoCoMo)") is now SHIPPED as of v5.26.0.

- **Phase 1 gate:** PASS. Sonnet full run: MRR=0.928, Recall@10=0.906 (500q natural distribution).
- **Phase 2 headline QA accuracy: 69.4% (347/500).** Sonnet 4.6 reader + judge, 470 min wall-clock.
  Per-type: single-session-assistant 96.4%, single-session-user 92.9%, knowledge-update 75.6%,
  abstention 80.0%, temporal-reasoning 63.9%, multi-session 55.6%, single-session-preference 33.3%.
- **Dataset:** LongMemEval `s` variant, 500 questions (natural distribution).
- **Model:** `claude-sonnet-4-6` (reader + judge).
- **Result files:** `benchmarks/results/longmemeval_v5.26.0_s_full.json` (Sonnet 500q final),
  `benchmarks/results/longmemeval_v5.26.0_s_full_hypotheses.jsonl` (500 lines),
  `benchmarks/results/longmemeval_v5.26.0_s_retrieval.json` (96q stratified pilot Phase 1, historical).
- **Supersedes:** Haiku 96q pilot (61.46%, 59/96) in the same Adopt-1 slot.

### D2 — NLI diversity stage: DEFER (post-Sonnet baseline)

D2 revisit trigger "Adopt-1 benchmarks produce baseline numbers" has fired. Post-Sonnet analysis:

- **Current status:** DEFER (was RECONSIDER — reverting to DEFER because no A/B exists)
- **Baseline with NLI ON:** 69.4% (347/500) overall QA accuracy, Sonnet 4.6 reader.
- **NLI settings in benchmark:** `NLI_RERANKING_ENABLED=True` in `make_benchmark_settings()`.
- **Why DEFER not FLIP/STAY:** the v5.26.0 Sonnet run is a single-arm measurement (NLI ON only).
  The D2 decision rule requires a NLI-OFF arm to compute delta. Without that arm, any decision
  would be guesswork. DEFER until NLI-OFF ablation run is complete per `docs/PLAN_V5_25_X_D2_NLI_AB.md`.
- **Next action:** Run D2 A/B (NLI OFF) per `docs/PLAN_V5_25_X_D2_NLI_AB.md`.
  Decision rule: delta < 5pp → flip default OFF; >= 5pp → keep ON.
- **Note:** Refactor-2 (v5.31.0 plugin arch) NOT yet shipped — A/B doable via `NLI_RERANKING_ENABLED=False` env var.

### D3 — PC algorithm causal discovery: DEFER (graph signals off in benchmark)

D3 revisit trigger "Adopt-1 benchmarks produce causal-on vs causal-off accuracy numbers" has
technically fired, but the v5.26.0 benchmark does NOT test causal discovery impact on retrieval.

**Why:** `make_benchmark_settings()` sets `WRRF_PPR_WEIGHT=0.0` and `WRRF_SPREADING_WEIGHT=0.0`
(graph signals disabled). The PC algorithm builds a causal DAG used only by graph signals.
The v5.26.0 baseline is implicitly "causal-off" for retrieval purposes.

- **Current status:** DEFER (unchanged — revisit trigger fired but is inconclusive for causal signal)
- **Next action:** Follow `docs/PLAN_V5_25_X_D3_PC_AB.md`.
  Primary question: does PC algorithm phase take > 30s in nightly cycle? Check production logs.
  Secondary: run LongMemEval with `WRRF_PPR_WEIGHT > 0` to get true causal-on vs causal-off QA data.
- **CPU burst watch:** D3 revisit trigger "CPU bursts traced to PC algorithm" has NOT fired.
  As of v5.25.3, no PC-algorithm-related CPU burst events in production journal.

---

## 2026-05-30 — Competitor Audit (mem0 / chroma / pinecone / zep / letta / postgres / DW)

**Audit doc:** `docs/competitor-audit-2026-05-30.md` (commit `635781e`)
**Scan doc:** `docs/competitor-audit-scan-2026-05-30.md`

### Adopt items (decisions pending — being planned by parallel agents)

| Item | Status |
|---|---|
| 1. Formal benchmarking (LongMemEval / LoCoMo) | SHIPPED v5.26.0 (69.4%, 347/500, Sonnet 4.6 full run). Adopt-1 CLOSED. |
| 2. Write-time conflict resolution | SHIPPED v5.17.0 |
| 3. Bi-temporal edges on all relationships | Planned → v5.29.0 |
| 4. In-context memory blocks (Letta) | Planned → v5.33.0 |
| 5. JavaScript / TypeScript SDK | Planned → v5.35.0 |
| 6. DuckDB analytics export | IMPLEMENTED v5.27.0 (2026-06-01, branch feat/v5.27.0-duckdb-export) |

### Refactor items

#### R1. Decouple consolidation from sleep cycle
- **Recommendation:** Separate consolidation cycle (deterministic, fast) and sleep cycle (LLM/CPU-heavy, slow) into distinct orchestrators with separate triggers.
- **Decision:** PARTIAL-ADOPT (limited scope only)
- **What was adopted:** `consolidate_now(mode='light'|'full')` param + 6h gate respect (SHIPPED v5.10.4). Stops at param-level switch; no full structural separation.
- **What was NOT adopted:** full split into separate orchestrator classes (`ConsolidationOrchestrator` + `SleepCycleOrchestrator`). Audit recommended this; user decided current scope is enough.
- **Reason:** v5.10.4 mode param solves the immediate bug (13-min surprise + design inversion). Full structural separation is bigger blast radius without clear additional value. Preserve as audit-recorded future option.
- **Evidence:** `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md`; shipped 2026-05-30.
- **Revisit triggers:** sleep cycle grows enough phases that mode param becomes unwieldy; or new use cases require running sleep-cycle phases independently of consolidation; or LLM curator tier (v6) needs different scheduling model.

#### R2. Modularize 8-stage retrieval pipeline for pluggability
- **Recommendation:** Make each stage of recall() pipeline a registered plugin to enable A/B testing.
- **Decision:** ADOPT
- **Scope:** full plugin architecture. Each stage = `RetrievalStage` interface with `name`, `apply(state)`, `enabled` flag. Pipeline = list of stages. Per-call profiles (fast/full/debug) + per-stage metrics.
- **Reason:** A/B testing of individual stages currently impossible without code surgery. Pays off once Adopt item 1 (benchmarks) lands — enables data-driven pipeline tuning.
- **Evidence:** `docs/competitor-audit-2026-05-30.md` Refactor section R2. Current pipeline coupled in single `recall()` function.
- **Revisit triggers:** none expected — forward commitment. If implementation hits unexpected friction, reassess.
- **Version slot:** v5.31.0 (after benchmarks land in adopt #1 plan — `docs/PLAN_V5_25_0_BENCHMARK_PUBLICATION.md`).

#### R3. Replace file-based write queue with DB-native pub/sub
- **Recommendation:** Replace `file_queue/` with SurrealDB `LIVE SELECT` or Postgres LISTEN/NOTIFY.
- **Decision:** REJECT (accept eventual consistency everywhere instead)
- **What was rejected:** the migration itself. File queue stays. No `flush_only()` MCP primitive added either.
- **Reason:** SurrealDB LIVE SELECT is experimental; pgvector migration is multi-version refactor. File queue works. Callers must design around eventual consistency.
- **Evidence:** `docs/PLAN_V5_99_0_ROADMAP_FRESHNESS.md` documents the constraint; user explicitly chose this option.
- **Revisit triggers:** SurrealDB LIVE SELECT exits experimental; or yadgar suffers multiple production incidents traced to file-queue state; or migration to Postgres+pgvector becomes a separate priority.

### Ditch items

#### D1. MTREE corruption auto-repair
- **Recommendation:** Demote auto-rebuild to probe-only-LOUD-log; stop masking upstream SurrealDB bug.
- **Decision:** KEEP-AS-IS
- **Reason:** zero corruption events in production journal over 30 days. Production uses HNSW (since migration_001), not MTREE. Probe is one fast KNN query per nightly cycle — negligible cost. Auto-rebuild path never fires in current production.
- **Evidence:** `journalctl ... grep "MTREE index corruption detected" → 0 events`; `yadgar/storage/migrations.py:31` `_migration_001_hnsw_indexes`.
- **Revisit triggers:** any HNSW corruption event logged; SurrealDB upstream issue tracker opens HNSW corruption bug; switch to different vector backend (e.g. pgvector); probe becomes hot in profiles.

#### D2. NLI diversity stage as always-on
- **Recommendation:** Make NLI diversity (HEAVY_RERANK_ENABLED) opt-in rather than default-on.
- **Decision:** DEFER
- **Reason:** no benchmark data on NLI vs no-NLI recall accuracy. Tied to two prerequisites: Adopt-1 (benchmarks) and Refactor-2 (recall plugin arch — makes stages independently togglable).
- **Evidence:** `HEAVY_RERANK_ENABLED` env knob exists; cross-encoder model `cross-encoder/nli-deberta-v3-small` loaded eagerly when enabled.
- **Revisit triggers:** Adopt-1 benchmarks (v5.25.0) produce baseline numbers; Refactor-2 plugin arch (v5.31.0) ships; A/B run shows NLI contributes less than 5 percentage points accuracy gain (then flip default) OR more than 5pp gain (then keep default and close revisit).

#### D3. PC algorithm causal discovery
- **Recommendation:** Validate that causal discovery improves recall accuracy. If not, retire or gate.
- **Decision:** DEFER
- **Reason:** same posture as D2 — need benchmark data first. Unique-moat feature; removing without measurement also removes architectural distinction.
- **Evidence:** `yadgar/causal_discovery/` (5 files: pc.py, meek.py, independence.py, dag_io.py, __init__.py). No recall A/B data exists.
- **Revisit triggers:** Adopt-1 benchmarks (v5.25.0) produce causal-on vs causal-off accuracy numbers; Refactor-2 plugin arch (v5.31.0) ships; CPU bursts traced to PC algorithm phase; or PC algorithm completion duration more than 30s on typical state.

### Hold items (audit identified as unique moats — recorded for future agents)

- **H1** Branch-aware retrieval — no competitor has this. Keep and deepen.
- **H2** Wiki and memory pairing — Yadgar's structured knowledge base distinct from pure memory.
- **H3** Nightly multi-phase consolidation pipeline — most sophisticated batch in audit.
- **H4** Surprise-gated writes — prevents duplicates pre-write, unique to Yadgar.
- **H5** 32 MCP tool surface — far ahead of competitors (mem0 ~4, Zep 0).

---

## 2026-05-30 — Plan-derived deferrals (consolidated)

Items extracted from "What does NOT ship" / "Non-goals" / "Out of scope" sections in active plan files. These represent real design decisions that would otherwise rot in individual plan files.

### From `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md`

**PD-1. Full structural separation of ConsolidationOrchestrator and SleepCycleOrchestrator**
- **Decision:** DEFER (tracked under R1 above)
- **Revisit triggers:** same as R1 — mode param becomes unwieldy OR v6 LLM curator tier needs different scheduling.

**PD-2. Nightly cron PR-1 wiring of `_maybe_sleep_cycle()`**
- **Source:** `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions
- **Decision:** OPEN-QUESTION
- **Background:** commit `bac9540` said `_maybe_sleep_cycle` is "preserved for PR-1 to wire". Current nightly cycle calls `force_consolidate()` only — no sleep cycle. Post-v5.10.4, sleep cycle no longer runs via `consolidate_now` default path either. Sleep cycle currently never runs unless `consolidate_now(mode="full")` is called.
- **Resolution path:** Decide in v5.10.9 plan or at next nightly cycle review. If sleep cycle is supposed to run nightly, add explicit wiring to `yadgar/scripts/nightly_cycle.py`. If not, delete `_maybe_sleep_cycle()` as dead code.

### From `docs/PLAN_V5_10_5_NIGHTLY_CYCLE_REMAINING.md`

**PD-3. Vacuum shared HTTP client refactor**
- **Decision:** DEFER
- **Reason:** surgical literal-replace (Bug 1 fix) is sufficient. Shared client is scope creep for the hotfix.
- **Revisit triggers:** vacuum gains a third call site; or multiple http-client-related bugs surface.

**PD-4. Strict exit code semantics (vacuum-fail from warn-only to fatal)**
- **Source:** `docs/PLAN_V5_10_5_NIGHTLY_CYCLE_REMAINING.md` "What does NOT ship"
- **Decision:** DEFER
- **Reason:** discussed in v5.7.0 PR-2 design; out of scope for hotfix. Currently nightly cycle exits 30 on vacuum fail (treated as non-fatal).
- **Revisit triggers:** operational pain from ambiguous exit codes; or next nightly cycle redesign.

### From `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md`

**PD-5. Cross-session "what did I work on this week" rollup**
- **Source:** `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md` section Out of scope, line 503
- **Decision:** PLANNED — v5.12 candidate
- **Reason:** requires multi-session data accumulated from session-end sentinels (v5.10.6 ships the data source first).
- **Revisit triggers:** v5.10.6 ships and sentinel data has 2 or more weeks of history.

**PD-6. CLI command `yadgar session-extract <transcript>` for manual extraction**
- **Source:** `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md` section Out of scope, line 504
- **Decision:** PLANNED — v5.12 candidate
- **Revisit triggers:** user requests manual extraction for historical sessions.

**PD-7. LLM synthesis in SessionEnd hook itself**
- **Source:** `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md` section Out of scope, line 501
- **Decision:** DEFER (indefinite)
- **Reason:** no model access from hook context; hook runs at exit-time when daemon may be down. Filesystem-first design was chosen instead (Q2 advisor recommendation).
- **Revisit triggers:** Claude Code SDK gains hook-context model access OR daemon-down-at-exit is resolved structurally.

### From `docs/PLAN_V5_10_7_VIZ_FIXES.md`

**PD-8. Viz performance for 5K+ node graphs**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 158
- **Decision:** DEFER
- **Reason:** current 2K nodes renders smoothly. Not an observed bottleneck.
- **Revisit triggers:** graph grows to more than 3K nodes AND frame rate drops noticeably.

**PD-9. Viz dark mode toggle**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 159
- **Decision:** DEFER
- **Reason:** cosmetic; no user request.
- **Revisit triggers:** user explicitly requests; or new viz major version (v6+).

**PD-10. Live anchor highlighting (red border for `_anchor`-tagged nodes)**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 160
- **Decision:** DEFER
- **Reason:** UX enhancement; blocked on confirming ThreeJS per-node styling API.
- **Revisit triggers:** anchor cross-project feature (v5.21.0) ships — anchors become more prominent in the data model.

**PD-11. Viz "replay last session" mode via action_log**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 161
- **Decision:** DEFER
- **Reason:** requires session-end capture (v5.10.6) as data source plus significant frontend work.
- **Revisit triggers:** session-end capture ships and there is clear user demand.

### From `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md`

**PD-12. ML-based secret detection**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Non-goals, line 31
- **Decision:** REJECT (scoped to v5.10.8; may revisit for v6+)
- **Reason:** regex-based gate works. ML adds model dependency, false positive complexity, and latency. v5.10.2 tightened thresholds are sufficient.
- **Revisit triggers:** regex false-positive rate becomes operationally painful AND a well-tested pre-trained model is available with less than 10ms inference.

**PD-13. Allowlist YAML schema versioning strategy**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Open questions, line 59
- **Decision:** OPEN-QUESTION
- **Resolution path:** decide before v5.10.8 agent dispatch. Options: (a) version field in YAML root; (b) no versioning, break on malformed only; (c) semver major in filename.

**PD-14. Allowlist audit log rotation policy**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Open questions, line 60
- **Decision:** OPEN-QUESTION
- **Resolution path:** decide before v5.10.8 agent dispatch. Lean: date-based (one file per day), consistent with existing `~/.yadgar/*.jsonl` patterns.

**PD-15. Allowlist pattern overrides (threshold raise vs full-bypass)**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Open questions, line 61
- **Decision:** OPEN-QUESTION
- **Resolution path:** start with full-bypass only in v5.10.8; add threshold-raise as v5.10.9+ enhancement if operational need surfaces.

### From `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`

**PD-16. F5 — embed_service saturation root fix (lazy-load rerankers OR cap batch OR cgroup bump)**
- **Source:** `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, lines 29 and 90
- **Decision:** OPEN-QUESTION (status unknown — needs verification)
- **Background:** Incident 501148 identified embed_service saturation at 32h+ uptime as root cause of CB-1 CPU burst. F5 was deferred at that time. As of v5.10.3 investigation, F5 ship status is unconfirmed.
- **Resolution path:** v5.10.9 acceptance criterion D5 — check CHANGELOG for v5.4.2+ for any embed_service lazy-load or cgroup changes. Open explicit issue if not found.

**PD-17. `DREAM_REPLAY_PAIRS` production default**
- **Source:** `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, line 127 and section Open Questions
- **Decision:** OPEN-QUESTION
- **Background:** if `DREAM_REPLAY_PAIRS` is high (more than 500), dream_replay is a significant CPU contributor in sleep cycle. Current production default unknown.
- **Resolution path:** check `~/.yadgar/config.yaml` config defaults before v5.10.9 agent dispatch.

**PD-18. Sleep cycle health metric ("last ran" timestamp)**
- **Source:** `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md` section Open Questions, line 186
- **Decision:** PLANNED
- **Background:** post-v5.10.4, sleep cycle no longer runs via default `consolidate_now`. If it also doesn't run via cron (PD-2 unresolved), there should be a metric to detect "sleep cycle has not run for more than 48h".
- **Version slot:** v5.10.9 or alongside PD-2 resolution.
- **Revisit triggers:** PD-2 (nightly cron wiring question) resolved.

### From `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md`

**PD-19. Call-count telemetry: `anchor()` vs `memorize(is_protected=True)` usage ratio**
- **Source:** `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section v5.X+ follow-up, line 160
- **Decision:** PLANNED
- **Background:** track `yadgar_memorize_is_protected_invocations_total` and `yadgar_anchor_invocations_total`. If `anchor()` drops to near-zero over months, candidate for implementation removal.
- **Version slot:** v5.X+ (after parity fix ships).
- **Revisit triggers:** parity fix ships for 3 or more months; then check ratio before deciding removal.

**PD-20. One-shot migration script for legacy `memorize(is_protected=True)` rows without `_anchor` tag**
- **Source:** `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section v5.X+ follow-up, line 161
- **Decision:** PLANNED — `scripts/migrate_legacy_protected_to_anchor.py`
- **Reason:** old rows lack `_anchor` tag injection; audit currently won't surface them.
- **Revisit triggers:** call-count telemetry (PD-19) reveals significant gap volume; or user manually discovers invisible anchors.

### From `docs/PLAN_V5_10_TEST_HARNESS_HARDENING.md`

**PD-21. Performance regression test suite**
- **Source:** `docs/PLAN_V5_10_TEST_HARNESS_HARDENING.md` "What does NOT ship", line 156
- **Decision:** DEFER
- **Reason:** scope creep for test hardening sprint; no current regression signal.
- **Revisit triggers:** a performance regression ships to master and is caught only by manual observation; or Adopt-1 benchmark plan (v5.25.0) creates infra reusable for this.

### From `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md`

**PD-22. Tier auto-upgrade (`conditional` to `semantic_immortal` after N clean audits)**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` "What does NOT ship"
- **Decision:** DEFER — v5.21+
- **Reason:** needs more real-world audit history before designing auto-upgrade thresholds.
- **Revisit triggers:** cross-project audit (v5.21.0) ships and runs for 30+ days; tier distribution data available.

**PD-23. `migration_grace=true` graceful expiry design hole**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md`
- **Decision:** PLANNED — v5.21.x candidates documented
- **Background:** v5.8 backfill set `migration_grace=true` on ALL pre-v5.8 `_anchor` rows. After 90d TTL, rows become invisible but persist as dead weight indefinitely, counting toward `anchor_count_project` signal threshold. This is a silent data leak. CRITICAL: first affected rows expire 2026-08-26 (anchored 2026-05-27 + 90d).
- **Candidates:** (a) `verify_grace_expired_anchor` recommendation type in `audit_anchors` — surfaces grace-protected rows past `valid_until` for user-gated review, auto-clears after N skipped audits; (b) auto-upgrade to `semantic_immortal` if heat above threshold at grace-expiry, else re-enter normal expiry. Lean (a).
- **Revisit triggers:** must ship v5.21.x grace handler before 2026-08-26.

**PD-24. Multi-language ticket tag patterns (Linear, GitHub Issues)**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` "What does NOT ship"
- **Decision:** PLANNED — start with Jira; expand on demand
- **Revisit triggers:** user actively uses Linear or GitHub Issues for task tracking alongside yadgar.

**PD-25. Anchor reorganization UI / web frontend (`yadgar-tui`)**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` "What does NOT ship"
- **Decision:** DEFER (permanent for v5.x)
- **Reason:** out of yadgar core scope. CLI and MCP surface is the primary interface.
- **Revisit triggers:** yadgar-tui becomes a real project with scope.

### From `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md`

**PD-26. Multi-user bookmarks**
- **Source:** `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md` section Non-goals
- **Decision:** DEFER — v6+ concern
- **Reason:** yadgar is single-user. Per-user bookmarks require auth model that does not exist.
- **Revisit triggers:** yadgar gains multi-user concept.

**PD-27. Playwright automated browser tests for viz/bookmark UI**
- **Source:** `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md`
- **Decision:** DEFER
- **Reason:** big infra add for a cosmetic/UX feature. Manual smoke test acceptable per v5.10.7 viz plan precedent.
- **Revisit triggers:** recurring browser-regression bugs caught only by manual testing; or test suite standardizes on headless browser.

### From `docs/PLAN_V5_99_0_ROADMAP_FRESHNESS.md`

**PD-28. v5.99.0 roadmap freshness mechanism**
- **Decision:** DEFER — to v5.99.0
- **Reason:** fundamental design issue with yadgar's async wiki write queue: read-after-write race means splice operations corrupt wiki content. Requires `flush_only()` primitive or blocking write path first.
- **Revisit triggers:** yadgar gains `flush_only()` MCP primitive; OR SurrealDB blocking write path available; OR roadmap drift incident severe enough to justify accepting data loss risk.

### From `docs/PLAN_NIGHTLY_BACKUP_NIX_FIX.md`

**PD-29. Nightly backup Tier 2 permanent fix — pure-Python consolidation (Candidate 1)**
- **Source:** `docs/PLAN_NIGHTLY_BACKUP_NIX_FIX.md` line 167
- **Decision:** PLANNED — v5.12.0
- **Reason:** Tier 1 (nix-repo edit) applied as emergency fix. Permanent solution requires eliminating numpy dependency from nightly-cycle execution context.
- **Revisit triggers:** Tier 1 (nix fix) breaks OR numpy version conflict recurs in NixOS upgrade.

**PD-30. Nightly backup Tier 2 permanent fix — container-based nightly execution (Candidate 2)**
- **Source:** `docs/PLAN_NIGHTLY_BACKUP_NIX_FIX.md` line 168
- **Decision:** PLANNED — v5.12.1 (alternative to PD-29; only one will ship)
- **Revisit triggers:** same as PD-29.

### From `docs/PLAN_BACKEND_V5_4_CACHING.md`

**PD-31. N+1 `get_memory` hydration batching**
- **Source:** `docs/PLAN_BACKEND_V5_4_CACHING.md` section v5.4.1 follow-up, line 188
- **Decision:** PLANNED — backend v5.4.1
- **Background:** 51 sequential reads per recall replaces with single `WHERE id IN $ids` query. Approximately 1s win per recall.
- **Revisit triggers:** v5.4.0 cache hit-rate baseline established (target 30% or more CE, 50% or more embed). Ship after baseline confirmed.

**PD-32. BM25 / HNSW result caches**
- **Source:** `docs/PLAN_BACKEND_V5_4_CACHING.md` "What does NOT ship", line 100
- **Decision:** DEFER (indefinite)
- **Reason:** BM25 and HNSW stages already under 50ms. Write-invalidation cost exceeds cache benefit.
- **Revisit triggers:** BM25 or HNSW becomes observed bottleneck post-CE-cache baseline.

**PD-33. Full recall-pipeline cache**
- **Source:** `docs/PLAN_BACKEND_V5_4_CACHING.md` "What does NOT ship", line 101
- **Decision:** REJECT (for current architecture)
- **Reason:** recall results are freshness-sensitive. User memorizes and expects immediate visibility in next recall. TTL trade-off is unacceptable.
- **Revisit triggers:** use cases where stale recall is acceptable emerge; or pipeline gains a staleness-tolerance flag per query.

### From `docs/PLAN_V5_8_ANCHOR_HYGIENE.md`

**PD-34. `semantic_immortal` tier write gate (require `reason` argument)**
- **Source:** `docs/PLAN_V5_8_ANCHOR_HYGIENE.md` section Open / parked questions, line 185
- **Decision:** OPEN-QUESTION (from v5.8 design)
- **Background:** should `anchor(tier="semantic_immortal")` require an additional `reason` argument? Forces deliberate thought. Lean: yes.
- **Resolution path:** confirm whether this was implemented in v5.8. Check `yadgar/server/tools/memorize.py` or `anchor.py` for required `reason` field on `tier="semantic_immortal"`.

### From `docs/PLAN_V6.md`

**PD-35. LLM-in-the-loop curator (v6 LLM nightly curator via Ollama)**
- **Source:** `docs/PLAN_V6.md` lines 9, 52, 69
- **Decision:** DEFER — after v5.x train complete
- **Reason:** substrate (provenance_agent, bi-temporal edges, citation tracing, recall-modulated decay, agent prompts, hooks) ships in v5. v6 adds the LLM that uses it. Skeleton only pending v5.4 soak data.
- **Revisit triggers:** v5.x train stabilizes plus soak data lands; DeepSeek-R1 8B benchmark meets v6 task bar.

**PD-36. Depth saturation chunking strategy for v6 curator**
- **Source:** `docs/PLAN_V6.md` lines 46, 52
- **Decision:** OPEN-QUESTION (must resolve BEFORE v6 first nightly run)
- **Background:** SleepGate paper: 16.5% accuracy at depth-15 contradictions. Chunking plus per-cluster scope limit MUST land before first nightly run. Cluster by topic via community detection; curate cluster-by-cluster; never whole-store batch.
- **Resolution path:** design chunking strategy as part of v6 plan refinement post-soak. Do NOT implement v6 before v5.4 soak data arrives.

---

## 2026-05-31 — Setup mechanism decision (v5.45 plan-derived)

**PD-37. Setup mechanism for non-NixOS installs**
- **Source:** `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` (v5.45.0 plan), `docs/PLAN_V5_46_0_DISTRIBUTION.md` (v5.46.0 plan), `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` (v5.47.0 plan)
- **Decision:** ADOPT — Compose-canonical + systemd opt-in + interactive installer + auto-detect runtime + auto-detect OS
- **Scope:** Three-ship train:
  - **v5.45.0** ships the foundation: portable Makefile (`make setup` / `make uninstall` / `make uninstall-purge`), interactive `yadgar install` CLI, container-runtime auto-detect (podman/docker), OS auto-detect (Linux/macOS/others), systemd opt-in path with new `yadgar.target`, macOS launchd plist path. Data preserved by default on uninstall; `--purge` for full wipe. Hooks delegated to existing MCP `install_hooks` tool. NixOS hosts refused with suggestion to use v5.46 nix flake.
  - **v5.46.0** ships the distribution: PyPI metadata polish, new Homebrew tap (separate `homebrew-yadgar` Codeberg repo), Nix flake at yadgar repo root (packages/apps/nixosModules/homeManagerModules outputs), Codeberg release automation via Forgejo Actions (sdist + container manifest + checksums + CycloneDX JSON SBOM + brew/nix bump PRs). Container source-of-truth stays at `docker.io/openfantasy/yadgar`; release manifest mirrors. Single-source-of-truth version bumper (`scripts/bump_version.py`).
  - **v5.47.0** ships the update mechanism: `yadgar update [--check | --install]` CLI subcommand (detects install method: pipx / brew / nix-flake / container / source), opt-in anonymous version-only auto-check on daemon start (`update.check_on_start: false` default OFF; no IP, no user-ID, no telemetry — strictly version probe), `/api/control/update` HTTP route (gated by `YADGAR_DEBUG_APIS_ENABLED=on` + bearer middleware). v5.50 viz Control-tab Update button wires to this API.
- **Reason:**
  - Compose is portable across Linux/macOS/Windows/WSL2 — single deployment model.
  - systemd opt-in path supports power users + matches NixOS-managed pattern without forcing it.
  - Interactive installer (no curl-pipe-sh) eliminates supply-chain attack surface.
  - Auto-detect runtime/OS removes per-distro tribal knowledge from documentation.
  - macOS launchd path bundles the same UX as Linux systemd — first-class macOS support.
  - Homebrew + Nix flake first-class install paths cover the long-tail user base.
  - Codeberg release automation removes manual asset-attachment toil; SBOM (CycloneDX JSON) satisfies enterprise security scanners.
  - Anonymous version-only update check respects privacy (opt-in, no telemetry) while letting users discover updates.
- **Alternatives considered + rejected:**
  - **Per-service systemd units only** — rejected; excludes macOS users entirely.
  - **Detect-OS hybrid without compose path** — rejected; loses portability across Linux distros (Alpine, RHEL, Ubuntu, NixOS each differ).
  - **Compose-only without systemd opt-in** — rejected; loses daemon supervision on power-user Linux installs; doesn't match NixOS pattern.
  - **curl-pipe-sh installer** — rejected; supply-chain attack surface unacceptable for a memory engine that handles user data.
  - **Phone-home telemetry with usage data** — rejected; privacy violation. Version-only opt-in probe is the maximum acceptable.
  - **SPDX SBOM** — deferred to v5.47+ variant; CycloneDX is v5.46 default (broader enterprise scanner support).
  - **Signed release artifacts (sigstore/cosign)** — deferred to v5.48+ candidate.
- **Lower-priority opens resolved:**
  - Data preservation: `make uninstall` preserves `~/.yadgar/` (DB + queue) by default; `make uninstall-purge` for full wipe.
  - Hooks delegation: Makefile delegates to existing `mcp__yadgar__install_hooks` MCP tool.
  - Versioning sync: bump script keeps pyproject + server.json + nix module + brew formula + Codeberg tag in sync; single source of truth.
  - Container source-of-truth: image stays at `docker.io/openfantasy/yadgar`; release manifest in Codeberg points to it (mirror reference, not duplicate hosting).
  - Phone-home: explicit anonymous version-only check (no IP / user-id telemetry); opt-in via `update.check_on_start: false` in config.yaml.
  - NixOS user migration: installer detects existing nix-managed install via `/etc/NIXOS` or `command -v nixos-version`; if detected, refuses to overwrite + suggests using nix flake derivation instead.
- **Open questions retained in plan docs (not blockers for PD-37):**
  - macOS launchd plist exact content + management commands (resolved during v5.45 Step 4 implementation).
  - Python 3.14 availability on macOS Homebrew core (resolved during v5.46 Step 0 implementation; fallback to 3.13 if needed).
  - Anonymous version-check payload exact wire shape — corporate firewalls + privacy auditors (resolved during v5.47 Step 0 implementation; documented in `docs/PRIVACY.md`).
- **Revisit triggers:** macOS launchd path proves unreliable in field; OR compose v3 spec deprecates a depended-on feature; OR user demand for FreeBSD / Windows-native paths; OR Codeberg releases API rate-limits the update-check probe; OR privacy posture must extend (e.g. SBOM transparency on update check); OR multi-host / multi-user yadgar deployment becomes a real use case (current scope: single-user).

**v5.45.0 implementation amendments (2026-06-04):**
- DP3 override: `yadgar install` CLI subcommand DEFERRED — `make setup` is the ONLY canonical install entrypoint for v5.45.0. No `cmd_install` / `yadgar install` command shipped. Plan's Step 5 "interactive installer" reduced to Makefile targets only.
- DP6 fold-back: seed anchors + CLAUDE.md fragment folded INTO v5.45.0 (originally split to v5.45.1). Ships as `install_assets/seeds/anchors.yaml` + `install_assets/CLAUDE.md.fragment` + `make seed-anchors` + `make install-rules`.
- macOS launchd: deferred to v5.45.1 / v5.46.0 per scope cut (no macOS host for verification).
- `yadgar install` as CLI entry-point deferred to v5.46.0 when interactive install prompt is designed.

---

## 2026-05-30 — Open architectural questions

Questions raised during design reviews, plan drafting, or session investigations that do not yet have a decision. Each needs an owner or an expected resolution path.

| ID | Question | Raised in | Resolution path |
|---|---|---|---|
| OQ-1 | Should nightly cron PR-1 wire `_maybe_sleep_cycle()`? Post-v5.10.4, sleep cycle currently never runs. | `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions | Decide in v5.10.9 plan or next nightly cycle review. |
| OQ-2 | What is `DREAM_REPLAY_PAIRS` set to in production? If more than 500, dream_replay is significant CPU contributor. | `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md` section Open Questions | Check `~/.yadgar/config.yaml` before v5.10.9 dispatch. |
| OQ-3 | Is F5 (embed_service lazy-load rerankers OR cap batch OR cgroup bump) shipped? | `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, line 29 | Check CHANGELOG for v5.4.2+ embed_service changes. |
| OQ-4 | `consolidate_now mode='full'` — should respect 6h gate or run unconditionally? | `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions | Resolved in v5.10.4 — gate respected. Mark DONE. |
| OQ-5 | v5.23.0 wiki bookmarks page — refresh-on-focus, `[[slug]]` clickable, pre-seed defaults? | `yadgar-roadmap-future-improvements` section Open Questions | Decide before v5.23.0 agent dispatch. |
| OQ-6 | `flush_only()` MCP primitive — design now or wait for clear use case? | `docs/PLAN_V5_99_0_ROADMAP_FRESHNESS.md` (deferred for it) | Wait for v5.99.0 to become active; design as prerequisite. |
| OQ-7 | `reason` kwarg on `memorize()` — keyword-only? | `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section Open / parked questions | Lean: keyword-only. Confirm in v5.10.x implementation. |
| OQ-8 | Auto-prepend `_anchor` to tags vs reject without it in `memorize(is_protected=True)` | Same plan | Auto-prepend (advisor lean). Confirm in implementation. |
| OQ-9 | Allowlist YAML schema versioning strategy (v5.10.8) | `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` | Decide before dispatch. Lean: version field in YAML root. |
| OQ-10 | Allowlist audit log rotation: size-based vs date-based? | Same plan | Lean: date-based. Confirm before dispatch. |
| OQ-11 | Should `anchor(tier="semantic_immortal")` require a `reason` argument? | `docs/PLAN_V5_8_ANCHOR_HYGIENE.md` | Verify if shipped in v5.8. |
| OQ-12 | `migration_grace=true` row expiry — handler must ship before 2026-08-26 (earliest affected rows expire). | `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` | PD-23. Ship v5.21.x grace handler before that date. |

---

## 2026-05-30 — Code-level architectural TODOs

Code comments representing architectural decisions — not trivial cleanups. Approximately 35 TODO/FIXME occurrences examined and excluded (test fixture strings, CI timeout tuning comments, version/history references in plan headers).

| ID | File:Line | Comment (quoted) | Category | Suggested action |
|---|---|---|---|---|
| CT-1 | `yadgar/storage/client.py:411` | "roll-your-own JSON escaping via LET $k = json.dumps bypasses SurrealDB's native bind facility. Migrate all _q callers to POST {"sql": stmt, "vars": params}" | DEFER | Architectural debt. Migrate when SurrealDB bind facility confirmed stable and no escaping edge cases. Affects all `_q()` callers. |
| CT-2 | `yadgar/tests/test_sleep_compute.py:509` | "tighten back to 5s once CI runner is faster; 15s is the hard ceiling" | OPEN-QUESTION | Test infra concern. Monitor CI runner speed. Tighten when 5s consistently passes without flake. |

---

## 2026-05-30 — Yadgar memory + wiki scan (consolidation addendum)

**Scan scope:** yadgar wiki pages (all yadgar-* slugs) + episodic memory recall. Prior pass (same date) scanned PLAN_*.md files and repo source. This pass explicitly targets yadgar's own memory store for deferrals not yet captured in DECISIONS.md.

**Noise filtered:** 40+ items excluded — adopt/PD/OQ items already present in this file, action-stream episodic memories, roadmap pipeline entries already in `yadgar-roadmap-future-improvements`, one-off planning notes without revisit triggers.

### Wiki-sourced deferrals

**YM-W-1: Anchor unconditional surfacing — implementation not shipped**
- **Source:** [wiki: yadgar-anchor-memory-design-scopes-and-surfacing] (2026-05-18)
- **Decision:** DEFERRED (design decided, code not shipped)
- **Background:** `restore()` and `session-start-context.py` rank-filter ALL anchors by relevance, dropping cross-project anchors (e.g. PR workflow anchor not surfaced during bug-fix task). Design specifies two scope buckets (global + project) surfaced unconditionally before ranked content. Implementation surface: `yadgar/restore.py` anchor query split + `dotfiles/common/yadgar-hooks/session-start-context.py` + one-time SQL migration of legacy `directory_context IN ("", "system")` rows to `"global"`. S6 from frozen v5.2 plan.
- **Revisit triggers:** next session-start context failure ("I forgot anchor X exists") OR v5.11 cross-project anchor work ships (natural time to wire unconditional surfacing alongside new anchor scope).

**YM-W-2: MCP + Supervisor container proxy (Idea 1) — deferred pending prerequisites**
- **Source:** [wiki: yadgar-deferred-architecture-ideas-half-baked-exploration] (2026-05-23)
- **Decision:** DEFER (prerequisites unmet)
- **Background:** split MCP transport into thin `yadgar-mcp` container that stays alive across daemon restarts, eliminating manual `/mcp` reconnect after vacuum / upgrades. Rejected for now: P8 idempotency markers (currently deferred v5.5) required to prevent double-writes on replay; MCP spec has no pause/resume notification; Claude Code has no auto-reconnect.
- **Revisit triggers:** P8 idempotency markers ship; OR SurrealKV gains online compaction (enables Alt A: move vacuum to separate service); OR Claude Code MCP plugin gains auto-reconnect; OR multi-host deployment.

**YM-W-3: Loki log ingestion blocked — Alloy DynamicUser home-dir permission**
- **Source:** [wiki: yadgar-obs-2026-05-23-investigation] Bug 2 (2026-05-23)
- **Decision:** OPEN-QUESTION (unresolved; three candidate fixes documented)
- **Background:** Alloy (DynamicUser) cannot traverse `~/.yadgar/logs/` because home dir is mode 700. Log shipper silently never ingests — Loki is empty. Dashboard Row 11 (logs) renders blank. Options: A (move log dir to `/var/log/yadgar/` + FHS bind-mount); B (switch to journald + `loki.source.journal` — cleanest for NixOS); C (chmod g+rx home — discouraged). Requires knowing whether yadgar already logs to stdout and whether log files are test-pinned.
- **Resolution path:** decide Option A vs B. Check `yadgar/log_config.py` stdout support + `tests/test_logs_*` coupling. Then implement in yadgar repo + nix repo in same cycle.

**YM-W-4: Tempo OTLP tracing not wired — spans produced but immediately dropped**
- **Source:** [wiki: yadgar-obs-2026-05-23-investigation] Bug 3 (2026-05-23)
- **Decision:** DEFER (needs yadgar version bump + TDD + nix-side Tempo OTLP receiver verification)
- **Background:** `yadgar/tracing.py` has `_OTEL_AVAILABLE` and `get_current_trace_id()` / `get_current_span_id()`. No `OTLPSpanExporter` or `BatchSpanProcessor` wired. No `OTEL_EXPORTER_OTLP_ENDPOINT` set on containers. Tempo OTLP receiver in `modules/observability/tempo.nix` unverified. Full wiring requires: `init_tracing()` in `yadgar/server/__main__.py` + `yadgar/embed_service.py`, env in `docker-compose.yml`, pyproject deps verify, nix receiver confirm.
- **Revisit triggers:** tracing becomes a debugging priority; OR F5-A semaphore CPU burst recurs and trace data would help root-cause; OR next observability session explicitly targets Tempo.

**YM-W-5: cAdvisor + rootless podman label mismatch — Row 9 dashboard queries may be wrong**
- **Source:** [wiki: yadgar-obs-2026-05-23-investigation] Bug 4 (2026-05-23)
- **Decision:** OPEN-QUESTION (predicted issue, not yet observed)
- **Background:** cAdvisor was enabled (v5.6.6 session). Dashboard Row 9 queries `container_cpu_usage_seconds_total{name=~"yadgar.*"}`. Rootless podman puts containers under user cgroup slice with auto-generated IDs; cAdvisor `name` label may be empty or different. Needs first scrape to inspect actual labels.
- **Resolution path:** after next nix apply, curl cAdvisor metrics endpoint, identify correct label for yadgar containers, update Row 9 queries + `$container` variable in `dotfiles/observability/dashboards/yadgar.json`.

**YM-W-6: Security findings S1–S3 — ALL SHIPPED in v5.2.0 (corrected 2026-05-30)**
- **Source:** [wiki: yadgar-v5-stabilize-strategy-tldr-gap-analysis] Security findings section (frozen 2026-05-20, predates v5.2.0)
- **Decision:** **DONE-ALREADY** (corrected from DEFER after security-planner agent verified code state on 2026-05-30)
- **Background:** Three H-level security findings from gap audit: (S1) `storage/ops.py:110,138` + `storage/client.py:375` — raw `json.dumps` in INSERT and raw `extra_where` interpolation (SQL injection). (S2) `rules_engine.py:445` — caller-supplied regex → ReDoS. (S3) `config_yaml.py:840` — config file written without `chmod 600`.
- **Verified shipped:**
  - **S1 SQL injection** — `yadgar/storage/ops.py:25-28,159-163` has `_EXTRA_WHERE_PATTERN` allowlist + `$data` bind param with `S1a`/`S1b` comments. Commit `bea40e2` (v5.2.0).
  - **S3 ReDoS** — `yadgar/rules_engine.py:7-19` imports third-party `regex` lib with `_REGEX_TIMEOUT_S = 1.0`; `:460-484` calls `_regex_lib.sub(..., timeout=_REGEX_TIMEOUT_S)` with `TimeoutError` handler. Commit `e7d231b` (v5.2.0).
  - **S2 chmod 600** — `yadgar/config_yaml.py:977-978` has `os.chmod(path, 0o600)` with `S2 (H-9)` comment. Commit `be1a653` (v5.2.0).
- **Root cause of false premise in original YM-W-6 entry:** the entry was synthesized from a frozen wiki page (`yadgar-v5-stabilize-strategy-tldr-gap-analysis`, frozen 2026-05-20) whose security section predated v5.2.0 ship. Observed state (code + git log) beats stale wiki snapshot.
- **Revisit triggers:** none — closed. If a follow-up regression hardening plan is desired (AST lint confirming no `_EXTRA_WHERE_PATTERN` bypass callers, `regex` lib pin enforcement, audit of any other config files lacking chmod), that's a separate plan and can claim the freed v5.10.11 slot.
- **Lesson recorded:** when consolidating memory-store / wiki content into DECISIONS.md, verify ALL claims against current code state, not just the wiki snapshot. Frozen wiki entries can be stale by multiple ship cycles.

**YM-W-7: repo-wiki DLQ escalation trigger — Option Y threshold**
- **Source:** [wiki: yadgar-repo-wiki-queue-drainer-validation-option-z-v5] (2026-05-15)
- **Decision:** PLANNED (trigger condition documented, not tracked in DECISIONS.md)
- **Background:** Option Z (queue boundary validation) ships as drainer gatekeeper for repo-wiki format drift. Escalation condition: if DLQ accumulates > 5 entries/week from repo-wiki path in v5 production → escalate to Option Y (in-daemon regen, coupling yadgar to repo-indexer CLI). No DLQ monitoring or alert exists yet for this threshold.
- **Revisit triggers:** DLQ monitoring added and first 7-day window with > 5 degenerate/missing-field/schema-old repo-wiki entries observed.

**YM-W-8: v6 depth saturation chunking — must design BEFORE first nightly LLM curator run**
- **Source:** [wiki: yadgar-v5-stabilize-strategy-tldr-gap-analysis] Open design forks #3 (frozen 2026-05-20); partially overlaps PD-36
- **Decision:** OPEN-QUESTION (PD-36 exists but resolution path vague; this entry sharpens it)
- **Background:** SleepGate paper: 16.5% accuracy at interference depth 15. Cluster-by-topic chunking (community detection; curate cluster-by-cluster; never whole-store batch) must be designed as part of v6 plan refinement, not improvised at first-run time. PD-36 says "design as part of v6 plan refinement post-soak" — accepted. This entry surfaces the design artifact needed: a separate `docs/PLAN_V6_CHUNKING_STRATEGY.md` before any v6 LLM curator dispatch.
- **Resolution path:** Draft `docs/PLAN_V6_CHUNKING_STRATEGY.md` as prerequisite gate blocking first `_dream_replay` LLM curator dispatch. **Supersedes:** PD-36 (adds artifact gate — not contradictory).

### Memory-sourced deferrals

**YM-M-1: I13 ruff pre-existing gap — `heuristic_rerank` C901=17 + `sample_system_metrics` PLR0913 noqa fix pending**
- **Source:** [memory id 495179] v5.4 P12 complexity audit anchor (recorded 2026-05-20)
- **Decision:** PLANNED — v5.4.3 (per anchor text "v5.4.3 noqa fix pending")
- **Background:** I13 enforcement shipped v5.4.2 with baseline-ratchet. Two pre-existing ruff violations survive as known gap: `heuristic_rerank` cyclomatic=17 (cap 15) and `sample_system_metrics` PLR0913 (too many args). Ratchet blocks NEW violations; these pre-existing ones need `# noqa: C901` / `# noqa: PLR0913` inline annotations to silence without worsening.
- **Revisit triggers:** v5.4.3 cycle or next complexity-touching PR. Low priority — ratchet prevents regression.

---

## Convention for future use

- **This file** lives at `docs/DECISIONS.md` on master (renamed from `docs/AUDIT_DECISIONS.md` on 2026-05-30).
- **Mirrored** to wiki page `yadgar-audit-decisions-log` (search-discoverable).
- **When dispatching an audit agent**, include in the prompt: "Read `docs/DECISIONS.md` first. Do not re-recommend items marked KEEP-AS-IS, REJECT, or DEFER unless their revisit triggers have fired."
- **When drafting any plan**, any "does not ship / out of scope / later" item of architectural significance must be extracted here before merge. Add a pointer in the plan file: "See `docs/DECISIONS.md` — [dated section]."
- **New audit/plan entries appended** at top under a new dated section.
- **Plan-only commits** go direct to master per yadgar workflow rule (set 2026-05-30).
