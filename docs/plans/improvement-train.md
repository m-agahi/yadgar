# Improvement Train (#29) — umbrella

**Status:** umbrella #29 — #4 + #9 shipped v5.85; B1-B5 + C4 still open.

Created 2026-06-25. Umbrella for issue **#29**: a batch of independent improvements
grouped into **three coherent sub-PRs** (A perf, B ADR-capture, C bugs/cleanup) — NOT
one mega-PR. Each car has its own plan doc (or section) below. All cites verified
against current code 2026-06-25 (read-only); the per-car plans carry the line refs.

> **Scope note.** The en2a/COMET ablation + comet-retire-dormant work
> (`en2a-comet-fpa-v5.82.md`, `comet-retire-dormant.md`) is a SEPARATE train and is
> effectively resolved (ADR-0004 RETIRE, in CHANGELOG `[Unreleased]`). It is NOT part
> of #29 and is excluded here. Two #29 cars (B-lead, #25) turned out already shipped —
> marked DONE below, not re-planned.

## PR strategy — three PRs, not one

| PR | Group | Theme | Coherence |
|----|-------|-------|-----------|
| **PR-A** | A | embedding-scan perf + int8 CE + scrape tune | all "make the daemon/backend cheaper"; touch storage/consolidation/backend + one nix hand-off |
| **PR-B** | B | ADR-capture follow-through | all extend the ADR system (P0 shipped); touch hooks + adr tools + project_brief + models |
| **PR-C** | C | bug fixes + a test unquarantine + a flag | small independent correctness fixes; touch project.py + a test + nix/dotfiles hand-offs |

Keep them separate: A is perf-risky (benchmark gates), B is feature work, C is
low-risk fixes that should not wait on A/B review.

---

## Group A — PERF (branch `feat/opt-embedding-scans` + nix)

| Car | Task | Plan | Scope (one line) | Status |
|-----|------|------|------------------|--------|
| A1 | #30–33 | [cpu-burst…embedding-scan-fix](cpu-burst-rootcause-and-embedding-scan-fix.md) **Part 2** | column projection + server-side heat decay + dream sample-then-fetch + incremental-by-time linking (kills `SELECT *` full scans) | AUDITED current (31/33 cites exact; 2 line + 1 path drift noted in-doc) |
| A2 | #4 | [ce-perf-options](ce-perf-options.md) §"Option B — concrete ship plan" | opt-in int8/ONNX cross-encoder in `ml_client._try_st_cross_encoder`, gated, fp32 default | AUDITED + made concrete (B chosen); **toolchain sub-choice = user/impl decision** |
| A3 | #34 | [process-exporter-scrape-interval](process-exporter-scrape-interval.md) | high-res Prometheus scrape 2s→5s (observer-effect) | NEW; **nix-only, hand to user** (no in-repo change) |

**Sequencing inside A:** A1 and A2 are independent (storage vs backend) — parallelizable.
A3 is a one-line nix hand-off, no code dep, can ride along or go straight to
MIGRATION_NOTES. A1 is the highest-value (removes the latent 100k-scale scan cliff);
A2 gated by an offline recall@k <2% A/B; A3 is diagnostic.

**Group A risks:** A1 server-side decay must preserve BC-C2/BC-CSW1 (single heat
writer) — characterization test first. A2 must not regress LongMemEval recall. A3
changes nothing functional. **Part 1 of the cpu-burst doc (host-side fan burst) stays
OPEN and out of scope — hand to user.**

---

## Group B — ADR-CAPTURE

| Car | Task | Plan | Scope (one line) | Status |
|-----|------|------|------------------|--------|
| B0 | (P0) | [adr-capture-system](adr-capture-system.md) §P0 | stop-hook capture-first + ADR schema + branch_hint write-side | **DONE — shipped PR #121 (`eeaec40`)** |
| B1 | #19 | [adr-capture-system](adr-capture-system.md) §P0.5 | stop-hook ADR prompt READ-side branch_hint (wiki_read/wiki_history miss default-branch ADR page on a feature branch) | NEW section; verified gap, one-template edit |
| B2 | #12 | [adr-capture-system](adr-capture-system.md) §P1 | `adr_add`/`decide` MCP tool (11-field typed/validated) + `adr_due` nudge in project_brief/checkpoint | PLANNED (P1, unbuilt) |
| B3 | #13 | [adr-capture-system](adr-capture-system.md) §P2 | project_brief surfaces the ADR page first-class (read-first) | PLANNED (P2, unbuilt) |
| B4 | #14 | [adr-capture-system](adr-capture-system.md) §P3 | consolidate record shapes → `models.py` (ADR shape first) | PLANNED (P3; `models.py` already 234 lines/16 classes — add the ADR type) |
| B5 | #20 | [anchor-signal-gap](anchor-signal-gap.md) | project_brief over-signals `audit_anchors` (count>15 gate ignores actionable items) + phantom action names; the stop-hook step-4 anchor-hygiene flow consumes this signal | NEW; root-caused. **Correction: audit_anchors DOES handle expired — fix is over-signal + name indirection, NOT "add expired handling"** |

**Sequencing inside B:** B1 first (tiny, closes the P0 dedup gap). Then B2 (the durable
fix — moves capture off the prompt); B3 depends on B2 (surface what B2 structures); B4
(models.py) underpins B2's typed schema — do B4's ADR-shape slice with B2. B5 is
independent (signals/anchors, touches project_brief + the stop-hook step-4 prose) — can
land anytime in B or as its own small PR. So: **B1 → (B4-adr-shape + B2) → B3**, B5 ‖.

**Group B risks:** prompt bloat (B1 is one line, fine); B2 is the real engineering
(server-side schema validation); B3/B4 small. No benchmark gates.

---

## Group C — BUGS / CLEANUP

| Car | Task | Plan | Scope (one line) | Status |
|-----|------|------|------------------|--------|
| C1 | #9 | [adr-capture-system](adr-capture-system.md) §Bug fixes (Bug A) | `stale_wiki_count` dead gate — `project.py:1862` reads `source_files`/`sources`, disk has `source_file` → always 0 | VERIFIED still broken; fix = add `source_file` key |
| C2 | #10 | [adr-capture-system](adr-capture-system.md) §Bug fixes (Bug B) | wiki_add/approve convention text | **in-repo already correct** (`sync_instructions` misc.py:517); real fix is out-of-repo nix dotfiles — **hand to user** |
| C3 | #25 | [comet-dormant-startup-warning](comet-dormant-startup-warning.md) | COMET-dormant startup warning reachability | **LIKELY CLOSED** — warning fires on streamable-http path; reduces to user decision (server-log vs client-visible) |
| C4 | #21 | [recall-content-integrity-flake](recall-content-integrity-flake.md) | unquarantine `test_specific_detail_preserved` (recall ranking miss, not content drop) | NEW; needs investigate→diagnose→fix; **don't overfit recall to one fixture (user judgment)** |

**Sequencing inside C:** all independent. C1 is a clean 1-line fix (ship immediately).
C2 + C3 are mostly hand-offs/decisions (MIGRATION_NOTES + a yes/no to the user). C4 is
the only real investigation — can trail the others.

**Group C risks:** C4 risks overfitting recall fusion to a synthetic abbreviation case
— gated by the LongMemEval recall@k regression check and a user decision on test
realism. C1/C2/C3 low-risk.

---

## Cross-group dependencies & order

- **No hard cross-group deps.** A, B, C are independent PRs and can land in any order /
  in parallel.
- **Suggested order:** **C first** (cheap correctness wins + clears the C3/C2 decisions
  and the C1 one-liner), then **B** (feature follow-through on the already-shipped P0),
  then **A** (perf, the most review-heavy + benchmark-gated). This front-loads low-risk
  value and defers the heaviest review.
- Branch discipline (global rules): branch off latest `master` per PR; A uses
  `feat/opt-embedding-scans` (+ a nix hand-off for A3); B/C get their own feature
  branches. Train-branch exception applies only to multi-stage trains — these three are
  standalone PRs, so normal post-merge cleanup applies.

## Items flagged for USER DECISION (do not invent)
1. **A2 toolchain** — int8 CE via ST-onnx vs optimum.onnxruntime vs torch-eager:
   pick at impl by image-install cleanliness. Plus confirm `settings` reaches
   `_try_st_cross_encoder` so the knob can be an I25 field (preferred) not a backend
   `os.getenv` orphan.
2. **C3 (#25)** — is the server-log WARNING sufficient (close #25), or surface
   COMET-dormant client-visibly? Recommend close.
3. **C4 (#21)** — after diagnosing the score gap, decide: deterministic tie-break
   (safe) vs recall re-weighting (risks benchmark) vs make the assertion realistic.
   Needs the per-signal score data first.
4. **C2 (#10) / A3 (#34)** — both require edits in `~/git/nix` (out of this repo);
   confirm the exact nix file/line before the user applies.
