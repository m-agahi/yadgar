"""v5.88 — seed materials consolidation.

Static seed CONTENT now lives in yadgar/seed/materials/ (data files), separated
from loader logic. This test pins:

  1. STARTER_PROMPTS loads from materials/agent_prompts.yaml (not Python tuples);
     the first 4 starters equal the expected list byte-for-byte, and the 5th
     (plan-executing-build, v5.122.0) is pinned by pattern + content markers.
  2. The implement-tdd starter carries the YAGNI least-code ladder (new content).
  3. The materials dir holds both seed data files (anchors.yaml + agent_prompts.yaml).
  4. _load_anchors_yaml still loads the relocated anchors.yaml.

Behaviour-preservation guard: the 3 unchanged starters (code-review,
debug-investigate, explore-codebase) match master's content exactly; only the
SOURCE moved + implement-tdd content updated.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

# Expected starters. The 3 unchanged ones are copied verbatim from the pre-refactor
# Python tuples; implement-tdd is the new YAGNI-ladder version.
_EXPECTED: list[tuple[str, str, str]] = [
    (
        "code-review",
        "Review a diff or PR for correctness and risk.",
        (
            "Review the given diff or PR. One finding per line, severity-tagged"
            " (critical/high/medium/low).\n"
            "Cite file:line for every finding.\n"
            "Flag only what changes correctness, security, or observable behavior.\n"
            "No praise, no scope creep, no unrelated cleanups.\n"
            "If nothing is wrong, report 'no issues found'."
        ),
    ),
    (
        "debug-investigate",
        "Root-cause a bug and ship a minimal fix with a regression test.",
        (
            "Reproduce the bug first — confirm failure before touching code.\n"
            "Isolate via bisection, logging, or binary-search; identify the true"
            " root cause, not the symptom.\n"
            "Apply the minimal fix — surgical edit, no opportunistic cleanups.\n"
            "Add a regression test that fails before the fix and passes after.\n"
            "Run the full suite; loop until green."
        ),
    ),
    (
        "explore-codebase",
        "Map where code lives / how a subsystem works (READ-ONLY).",
        (
            "READ-ONLY investigation — make zero edits.\n"
            "Locate where X lives or how Y works; start broad (grep/glob) then narrow.\n"
            "Return a file:line table with one row per relevant symbol or entry-point.\n"
            "Quote function signatures exactly as they appear in source.\n"
            "Do NOT propose or apply fixes; report the map, not opinions."
        ),
    ),
    (
        "implement-tdd",
        (
            "Implement a feature test-first (red→green→refactor), writing the least"
            " code that satisfies it — a YAGNI least-code ladder applied before coding."
        ),
        (
            "Write a failing test that pins the desired behavior (red) before any"
            " implementation code.\n"
            "\n"
            "Before writing implementation, climb the least-code ladder — STOP at the"
            " first rung that works:\n"
            "  1. Does it need to exist at all? Drop speculative / just-in-case scope.\n"
            "  2. Already in the codebase → reuse it.\n"
            "  3. Standard library / language built-in.\n"
            "  4. Native feature of a framework/platform already in use.\n"
            "  5. An already-installed dependency. (Do NOT add a new dep without asking.)\n"
            "  6. A one-liner.\n"
            "  7. Else: the minimal implementation — no abstraction a second caller"
            " doesn't yet need.\n"
            "\n"
            "Implement the minimal code to pass (green). Refactor with tests green —"
            " no behaviour change.\n"
            "Run the full check surface (tests/lint/types) and loop until clean.\n"
            "Done = tests pass, checks green, and no code exists that a higher rung"
            " could have avoided."
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
    """Interface preserved: list of (pattern, purpose, content) 3-tuples, length 5."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    assert isinstance(STARTER_PROMPTS, list)
    assert len(STARTER_PROMPTS) == 5
    for entry in STARTER_PROMPTS:
        assert isinstance(entry, tuple) and len(entry) == 3


def test_plan_executing_build_starter_pinned():
    """5th starter (v5.122.0): plan-executing-build — verbatim copy of the live
    wiki page so the packaged prelude contract's rule-4 pointer resolves on
    fresh installs. Pinned by pattern + load-bearing content markers."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    pattern, purpose, content = STARTER_PROMPTS[4]
    assert pattern == "plan-executing-build"
    assert "ADR-0081/0082" in purpose
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


def test_implement_tdd_has_yagni_ladder():
    """implement-tdd carries the new YAGNI least-code ladder (purpose + 7 rungs)."""
    from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

    by_pattern = {p: (purpose, content) for p, purpose, content in STARTER_PROMPTS}
    purpose, content = by_pattern["implement-tdd"]
    assert "YAGNI least-code ladder" in purpose
    for rung in range(1, 8):
        assert f"  {rung}. " in content, f"rung {rung} missing from implement-tdd prompt"
    assert "no code exists that a higher rung" in content


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
