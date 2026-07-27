"""v5.88 — seed materials consolidation.

Static seed CONTENT now lives in yadgar/seed/materials/ (data files), separated
from loader logic. This test pins:

  1. STARTER_PROMPTS loads from materials/agent_prompts.yaml (not Python tuples);
     the first 4 starters equal the expected list byte-for-byte, and the 5th
     (plan-executing-build, v5.122.0) is pinned by pattern + content markers.
  2. The implement-tdd starter carries the YAGNI least-code ladder (new content).
  3. The materials dir holds both seed data files (anchors.yaml + agent_prompts.yaml).
  4. _load_anchors_yaml still loads the relocated anchors.yaml.
  5. v5.124.0 consolidation: entries 5..14 are the generic subset of the
     consolidated live library (3 merged canonicals with ## Modes + retained
     generics); each pinned by pattern + load-bearing content markers. Replaces
     the v5.123.0 backflow set (crash-rca / plan-corpus-status-sweep /
     perf-anomaly-metrics dropped as merged or reclassified).
  6. Wave 2 model-tier: all 15 starters carry a DISPATCH: first line (task #48).

Behaviour-preservation guard: first 4 starters pinned byte-for-byte including
DISPATCH lines (Wave 2 addition). implement-tdd carries the YAGNI ladder.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

# Expected starters. The 3 unchanged ones are copied verbatim from the pre-refactor
# Python tuples; implement-tdd is the new YAGNI-ladder version.
_EXPECTED: list[tuple[str, str, str]] = [
    (
        "pr-review",
        "Review any PR against its base by its real effect — universal core checks + conditional stack lenses (code/infra/migration/API), evidence-gated, read-only.",
        "DISPATCH: model=opus (fallback=sonnet for small/mechanical PRs) — review depth scales with blast radius — canonical: model-tier-dispatch\n\n## Purpose\nReview ANY pull request against its base branch by its REAL effect, not just the visible\ndiff. Adapt the depth and the lenses to what the PR actually touches. READ-ONLY.\n\n## Safety (hard, non-negotiable)\nA review MUTATES NOTHING. Allowed: `git diff/show/log -p`, read files, run the repo's\nexisting tests/lint/type/build, render/dry-run (`kubectl kustomize`, `helm template`,\n`--dry-run=client`). FORBIDDEN: any apply/deploy/push/merge, `kubectl apply/delete`,\n`helm install/upgrade`, `flux reconcile`, `argocd sync`, `terraform apply/import/state`\n(never run terraform at all unless the repo already sanctions `plan`), `digger ...`, DB\nwrites. If a check would mutate, don't run it.\n\n## Method\n1. ELIGIBILITY: closed/draft/superseded → say so, stop.\n2. MAP THE CHANGE SURFACE from `git diff <base>...<head>` (three-dot / merge-base):\n   app code · tests · public API/contract · DB migration · infra/IaC\n   (k8s/helm/kustomize/terraform) · config/secrets · CI/build · deps · docs. This selects\n   which LENSES apply. Scale effort to blast radius.\n3. CONTEXT: read the PR body + root and directory-scoped CLAUDE.md / conventions / OWNERS.\n   Observed code ALWAYS wins over the PR description.\n4. LOOK BEYOND THE VISIBLE DIFF where an edit has amplified effect: rendered manifests\n   (kustomize/helm), generated code, a config value that changes runtime behavior, a\n   migration that reshapes the schema, a shared helper now called differently.\n5. CORE CHECKS (every PR) + 6. TRIGGERED LENSES (only for touched surfaces) — below.\n7. RUN THE REPO'S OWN CHECKS (never author new infra): tests/lint/types/build/schema/\n   policy it already ships; report pass/fail. Surface pre-existing failures separately.\n8. VERIFY each finding against the actual code/rendered output BEFORE flagging.\n\n## Core checks (every PR)\nCorrectness (bugs, off-by-one, null, wrong operator, unhandled error) · Security\n(injection, secrets in the diff, authz gaps) · Tests (fails-before/passes-after, or\nuntested?) · Completeness (a caller/companion resource/flag/doc/enum-case not updated;\nwhat the diff implies but doesn't do) · PR-desc vs diff (undisclosed / scope-creep\nchanges) · Compat (breaking public API/schema/CLI/config key? versioned?) · Conventions\n(quote the CLAUDE.md/project rule violated).\n\n## Triggered lenses (apply only if that surface is touched)\n- INFRA/IaC: render BASE vs PR and DIFF the output (the real change surface); trace every\n  base/patch/HelmRelease/valuesFrom/secretRef. Ownership conflict: a resource managed by\n  TWO controllers (Flux vs Argo vs raw vs Terraform) = drift/flap. Missing\n  limits/probes/PDB/NetworkPolicy, `:latest` tags, privileged/hostPath, RBAC over-grant.\n  State the live-cluster impact.\n- DB MIGRATION: reversible? locking/blocking on a big table? backfill safe? data-loss?\n  forward/backward-compatible with the running app during rollout?\n- PUBLIC API/CONTRACT: breaking for consumers? versioned/deprecated cleanly?\n- CONCURRENCY/ASYNC: races, deadlocks, unclosed resources, event-loop-bound primitives.\n- DEPENDENCIES: new dep justified + not a lighter option? lockfile updated? supply-chain?\n- FRONTEND: a11y, XSS/injection, bundle-size cliff.\n\n## Severity + confidence\nTag every finding; score confidence 0-100; surface only >=80.\n  BLOCKER  — breaks prod / data-loss / security / ownership conflict / secret leak\n  CONCERN  — fragile: missing guard/test, drift risk, unpinned dep, perf cliff\n  NIT      — style/naming (only when a thorough review is requested)\n  QUESTION — need author intent before judging\n0-25 likely false-positive/pre-existing · 26-50 minor · 51-75 valid-low · 76-90 important\n· 91-100 critical. Drop <80.\n\n## Do NOT flag (false-positive guards)\nPre-existing issues not in this diff · lint/type/compiler-catchable (assume CI) · pedantic\nnits · findings on lines the PR didn't touch · intentional-and-explained changes ·\nanything you did NOT verify · the author's rationale on faith (verify it). If unsure, ask\n(QUESTION) rather than assert a BLOCKER.\n\n## Output\n1. Findings by severity: `<file>:<line>: <severity>: <problem>. <impact>. <fix>.` Cite the\n   rule when one applies; permalink (SHA + #Lstart-Lend) for a remote PR.\n2. `## PR-desc vs diff` — matches / diverges.\n3. `## Checks` — tests/lint/render/policy pass/fail (pre-existing failures noted separately).\n4. `## Verdict` — REQUEST-CHANGES (any blocker) · COMMENT (only concerns/questions) ·\n   APPROVE (clean) + a concise, ready-to-paste review comment.\nTerse by default; security, data-loss, ownership, and breaking-change findings get a full\nparagraph (cause + reference). No praise, no restating the code.",
    ),
    (
        "debug-investigate",
        "Root-cause a bug and ship a minimal fix with a regression test.",
        (
            "DISPATCH: model=opus (fallback=sonnet for shallow bugs)"
            " — canonical: model-tier-dispatch\n"
            "\n"
            "Reproduce the bug first — confirm failure before touching code.\n"
            "Isolate via bisection, logging, or binary-search; identify the true"
            " root cause, not the symptom.\n"
            "If a `code_graph` memory block exists for this repo, its"
            " hotspots/fan-in data can help localize which module the bug"
            " likely lives in before you bisect blind.\n"
            "Apply the minimal fix — surgical edit, no opportunistic cleanups.\n"
            "Add a regression test that fails before the fix and passes after.\n"
            "Run the full suite; loop until green.\n"
            "\n"
            "## Composes\n"
            "- [[agent-discipline-adr-consult]]"
        ),
    ),
    (
        "explore-codebase",
        "Map where code lives / how a subsystem works (READ-ONLY).",
        (
            "DISPATCH: model=sonnet (haiku for pure listing)"
            " — canonical: model-tier-dispatch\n"
            "\n"
            "READ-ONLY investigation — make zero edits.\n"
            "If the repo has code_graph enabled, check the per-dir `code_graph`"
            " memory block (auto-injected digest:"
            " layers/hotspots/entry-points/endpoints) or run"
            " `yadgar code-graph query <repo> <cypher>` FIRST — real indexed"
            " structure beats cold grep on a large/unfamiliar tree. Fall back to"
            " grep/glob when it doesn't have the answer, or the repo has no"
            " digest.\n"
            "Locate where X lives or how Y works; start broad (grep/glob) then narrow.\n"
            "Return a file:line table with one row per relevant symbol or entry-point.\n"
            "Quote function signatures exactly as they appear in source.\n"
            "Do NOT propose or apply fixes; report the map, not opinions."
        ),
    ),
    (
        "implement-tdd",
        (
            "Implement a feature test-first with a 5-phase hardening pipeline"
            " (RED-VERIFY → adversarial critic → green → mutation+fuzz → gates)"
            " — risk-tiered so trivial diffs skip the expensive phases."
        ),
        (
            "DISPATCH: model=sonnet (fallback=opus) — mechanical when the spec is"
            " crisp; opus for gnarly logic. Background; worktree if parallel writers."
            " — canonical: model-tier-dispatch\n"
            "\n"
            "Build application/library code test-first with a hardening pipeline."
            " Phases are RISK-TIERED: run the FULL pipeline (all 5) for load-bearing"
            " app/lib logic; for trivial/mechanical diffs (config, infra .nix/.yaml,"
            " one-liners, version bumps, docs, codemods) run phases 1+3+5 ONLY.\n"
            "\n"
            "Contract: recall-first; observed-state-wins; end your report with"
            " `## Yadgar findings`.\n"
            "\n"
            "PHASE 1 — TESTS FIRST + RED-VERIFY\n"
            "- Write tests before implementation, one per acceptance criterion in the spec.\n"
            "- Run them. Confirm they FAIL — and fail for the RIGHT reason: an assertion"
            " failure or NotImplementedError, NOT ImportError / SyntaxError / collection"
            " error. A test that ERRORS instead of failing has not been shown to test"
            " anything. Fix until the failure is a genuine assertion miss.\n"
            "\n"
            "PHASE 2 — ADVERSARIAL TEST-CRITIC (load-bearing code only; skip for trivial)\n"
            "- Review the tests adversarially (assume they are weak) against this rubric:\n"
            "  - Real assertions — not assertTrue(True), not tautological, not asserting"
            " a mock return you just set.\n"
            "  - Tests BEHAVIOR/contract, not implementation detail.\n"
            "  - Every acceptance criterion AND every non-trivial branch has a test.\n"
            "  - Edge + error cases present (empty, boundary, malformed, exception paths).\n"
            "  - No over-mocking that stubs out the unit under test.\n"
            "- Fix gaps. Loop MAX 2 rounds; still weak → STOP + report (do not spin).\n"
            "\n"
            "PHASE 3 — IMPLEMENT → GREEN\n"
            "- Minimal code to pass. Red → green.\n"
            "\n"
            "PHASE 4 — POST-GREEN HARDEN (load-bearing code only; skip for trivial)\n"
            "- Mutation testing (mutmut) on the CHANGED module(s) ONLY, time-boxed (~10 min)."
            " Exhaustive per module (NOT sampled). A surviving mutant = a bug your tests miss"
            " → add a test that kills it. Loop MAX 2. A genuinely-equivalent mutant may be"
            " allowlisted with a one-line justification comment.\n"
            "- Property/fuzz (hypothesis) for pure functions + parsers: invariant-driven"
            " (never-raises / idempotent / round-trip / conserves-invariant),"
            " max_examples >= 200 (>= 500 for critical paths). A fuzz failure → fix the"
            " code, PIN the discovered case with @example so it cannot re-flake, and keep"
            " it as a regression test.\n"
            "\n"
            "PHASE 5 — GATES + COMMIT\n"
            "- Run available checks (lint, types, complexity, observe-coverage, e2e). Fix"
            " root cause; surface pre-existing failures separately. Same fix fails 2×"
            " → stop + report.\n"
            "- No --no-verify / hook bypass. No Co-Authored-By. Then commit.\n"
            "\n"
            "CAPS: every fix-loop bounded (critic 2, mutation 2, fuzz 2) → escalate,"
            " never infinite-loop.\n"
            "TIER RULE: does the diff carry non-trivial branching/logic in app/lib code?"
            " yes → full 5 phases; no → phases 1+3+5."
        ),
    ),
]


# ── materials directory holds the data files ─────────────────────────────────


def test_materials_dir_has_both_seed_files():
    """yadgar/seed/materials/ holds anchors.yaml + agent_prompts.yaml."""
    materials = files("yadgar.core.seed").joinpath("materials")
    assert materials.joinpath("anchors.yaml").is_file(), "materials/anchors.yaml missing"
    assert materials.joinpath("agent_prompts.yaml").is_file(), (
        "materials/agent_prompts.yaml missing"
    )


# ── STARTER_PROMPTS loads from the yaml material ─────────────────────────────


def test_starter_prompts_loaded_from_materials():
    """The first 4 starters equal the expected list byte-for-byte (loaded from yaml)."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    assert STARTER_PROMPTS[:4] == _EXPECTED, (
        "first 4 STARTER_PROMPTS do not match expected materials content"
    )


def test_starter_prompts_is_list_of_3_tuples():
    """Interface preserved: list of (pattern, purpose, content) 3-tuples, length 15."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    assert isinstance(STARTER_PROMPTS, list)
    assert len(STARTER_PROMPTS) == 15
    for entry in STARTER_PROMPTS:
        assert isinstance(entry, tuple) and len(entry) == 3


def test_plan_executing_build_starter_pinned():
    """5th starter (v5.122.0): plan-executing-build — verbatim copy of the live
    wiki page so the packaged prelude contract's rule-4 pointer resolves on
    fresh installs. Pinned by pattern + load-bearing content markers.
    Wave 2 (task #48): DISPATCH line is now the first line of content."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    by_pattern = {p: (purpose, content) for p, purpose, content in STARTER_PROMPTS}
    assert "plan-executing-build" in by_pattern, "plan-executing-build starter missing"
    purpose, content = by_pattern["plan-executing-build"]
    assert "ADR-0081/0082" in purpose
    # Wave 2: DISPATCH line is first
    assert content.startswith("DISPATCH:"), (
        "plan-executing-build must open with a DISPATCH: line (Wave 2, task #48)"
    )
    assert "canonical: model-tier-dispatch" in content
    # Stage 2 (genesis synced to live page v3): the cross-cutting rule text was
    # EXTRACTED to composed discipline pages — the pattern genesis now carries
    # ## Composes references instead of the inline rules.
    assert "## Composes" in content
    assert "[[agent-discipline-plan-lifecycle]]" in content
    assert "## Yadgar findings" in content

    # The move-not-copy rule lives in the plan-lifecycle DISCIPLINE genesis.
    from yadgar.core.server.tools.agent_prompts import DISCIPLINES

    lifecycle = {name: body for name, _purpose, body in DISCIPLINES}["plan-lifecycle"]
    assert "git mv docs/plans/" in lifecycle
    assert "git ls-files docs/plans/" in lifecycle


def test_implement_tdd_has_hardening_pipeline():
    """implement-tdd carries the 5-phase hardening pipeline (purpose + load-bearing markers)."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    by_pattern = {p: (purpose, content) for p, purpose, content in STARTER_PROMPTS}
    purpose, content = by_pattern["implement-tdd"]
    assert "hardening pipeline" in purpose
    for marker in [
        "RED-VERIFY",
        "ADVERSARIAL TEST-CRITIC",
        "mutmut",
        "max_examples >= 200",
        "TIER RULE",
    ]:
        assert marker in content, f"marker {marker!r} missing from implement-tdd prompt"


# ── v5.123.0 seed backflow: live-corpus growth ───────────────────────────────
# 10 battle-tested, generally-reusable live wiki patterns synced into the genesis
# corpus (user directive 2026-07-10 "improving the seeds"). Each pinned by
# load-bearing content markers (verbatim substrings of the live page body).

# v5.124.0 consolidation: the post-preamble starters (entries 5..14) are the
# GENERIC subset of the consolidated live library — 3 merged canonicals carrying
# a ## Modes section (rca-diagnose, plan-audit, scope-and-plan, build-car) plus
# retained generics. crash-rca / plan-corpus-status-sweep / perf-anomaly-metrics
# were dropped (merged into rca-diagnose mode=prod-crash, drift-audit
# mode=plan-corpus, or reclassified yadgar-specific).
_BACKFLOW_MARKERS: dict[str, list[str]] = {
    "rca-diagnose": [
        "ROOT CAUSE ONLY",
        "mode=prod-crash",
        "PROVE or EXCLUDE",
    ],
    "plan-audit": [
        "INDEPENDENT skeptic",
        "VERIFIED / CRACKED / UNCERTAIN",
        "DO-NOT-BUILD",
        "writeback=true",
    ],
    "scope-and-plan": [
        "PLAN for <change>",
        "measure_first=true",
        "domain=perf-lever",
    ],
    "build-car": [
        "WORK LOCATION",
        "plan_spec=",
        "the final full pass is the authoritative gate",
    ],
    "drift-audit": [
        "PHANTOM",
        "MISSING",
        "STALE",
        "MALFORMED",
        "mode=plan-corpus",
    ],
    "feasibility-design": [
        "BUILDABLE",
        "FEASIBILITY first",
        "loop-safety",
    ],
    "feature-kill-closeout": [
        "zero residue",
        "docs/plans/archive/",
        "[[agent-discipline-branch-state]]",
    ],
    "dispatch-fix-test-migration": [
        "NO TEST BENDING",
        "agent-prompt-plan-executing-build",
        "hollow-pass",
    ],
    "mechanical-refactor-chunk-commit-early": [
        "ONE chunk",
        "COMMIT immediately",
        "sys.modules",
    ],
    "stacked-car-parallel-build": [
        "ADR-0088",
        "MUST NOT push to the train branch",
        "[[agent-discipline-plan-lifecycle]]",
    ],
}


def test_backflow_patterns_pinned():
    """v5.124.0: the post-preamble starters (entries 5..14, file order preserved)
    are the generic consolidated subset, each carrying its load-bearing markers."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    backflow = STARTER_PROMPTS[5:]
    patterns = [p for p, _, _ in backflow]
    assert patterns == list(_BACKFLOW_MARKERS), f"backflow patterns/order mismatch: {patterns}"
    for pattern, purpose, content in backflow:
        assert purpose, f"empty purpose for {pattern!r}"
        for marker in _BACKFLOW_MARKERS[pattern]:
            assert marker in content, f"marker {marker!r} missing from {pattern!r} content"


def test_backflow_bodies_survive_unwrap():
    """No backflow body may start with a bare '## Purpose … ## Prompt' wrapper that
    _unwrap_purpose_prompt would strip (inner '## Prompt block (...)' headings are
    safe; a bare leading wrapper would lose content on seed)."""
    from yadgar.core.server.tools.agent_prompts import (
        STARTER_PROMPTS,
        _unwrap_purpose_prompt,
    )

    for pattern, _purpose, content in STARTER_PROMPTS:
        assert _unwrap_purpose_prompt(content) == content, (
            f"starter {pattern!r} body would be mangled by _unwrap_purpose_prompt"
        )


def test_all_starters_have_dispatch_line():
    """Wave 2 (task #48): every starter's content opens with a DISPATCH: line
    that carries a canonical: model-tier-dispatch pointer."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    for pattern, _purpose, content in STARTER_PROMPTS:
        assert content.startswith("DISPATCH:"), (
            f"starter {pattern!r} content must open with 'DISPATCH:' (Wave 2, task #48);\n"
            f"got: {content[:80]!r}"
        )
        assert "canonical: model-tier-dispatch" in content, (
            f"starter {pattern!r} DISPATCH line must end with '— canonical: model-tier-dispatch'"
        )


def test_loader_reads_yaml_not_inline_tuples():
    """The loader source must read the yaml material, not embed the tuples inline.

    Guards against a regression where someone re-inlines the prompts in Python.
    """
    src = (
        Path(__file__).parent.parent.parent / "core" / "server" / "tools" / "agent_prompts.py"
    ).read_text()
    assert "agent_prompts.yaml" in src, "loader no longer references agent_prompts.yaml"


# ── anchors.yaml still loads from the relocated path ─────────────────────────


def test_anchors_yaml_loads_from_materials():
    """_load_anchors_yaml loads the relocated materials/anchors.yaml (>= 6 entries)."""
    from yadgar.core.cli.seed import _load_anchors_yaml

    anchors_path = str(files("yadgar.core.seed").joinpath("materials").joinpath("anchors.yaml"))
    entries = _load_anchors_yaml(anchors_path)
    assert len(entries) >= 6
    for e in entries:
        assert "content" in e and "tags" in e
