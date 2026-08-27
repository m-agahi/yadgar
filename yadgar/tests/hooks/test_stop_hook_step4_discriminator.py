"""DC1 (task #337) — stop_checkpoint_prompt.md step 4 has a category list but
no machine-checkable discriminator: each of the five judgement arms names a
specific tool but the rule that maps a bullet to one arm is prose-only. ADR-0434
was mis-filed as a memory because the model could not, on inspection, tell which
arm matched. Pinning the discriminator that DOES exist in the prompt — the
per-arm tool routing — turns the failure mode into a structural regression a
template edit can break loudly.

Scope: this test asserts the load-bearing strings present in the on-disk
template at yadgar/core/hooks/templates/stop_checkpoint_prompt.md, inside the
named step 4 ("SUBAGENT FINDINGS CURATION"). It pins:

  - the step is named + reachable between step 3 and step 5
  - every judgement arm lists the exact tool the bullet routes to
  - memorize's rewrite rule (REWRITTEN in your words, never verbatim) is present
  - the discard arm explicitly names what gets dropped (noise/status/one-off/dup)
  - the step mentions the yadgar pending-findings CLI it routes through

NOTE: this is a structural guard only. It pins the discriminator the prompt
already exposes — it does NOT introduce a new classifier. A future prompt-text
fix that adds a real machine-checkable rule would be a separate car; this car
makes sure the rule, when added, lands inside step 4 with the routing intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_TEMPLATE_PATH = _REPO / "core" / "hooks" / "templates" / "stop_checkpoint_prompt.md"


@pytest.fixture(scope="module")
def template_text() -> str:
    assert _TEMPLATE_PATH.exists(), f"Template not found at {_TEMPLATE_PATH}"
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _step4_slice(text: str) -> str:
    """Slice the template to the body of step 4 (SUBAGENT FINDINGS CURATION)."""
    start = text.index("4. SUBAGENT FINDINGS CURATION")
    # step 5 begins at the next "5. " line that starts a numbered step.
    end = text.index("5. TASK-LIST MIRROR", start)
    return text[start:end]


# ---------------------------------------------------------------------------
# The step exists and is reachable between steps 3 and 5
# ---------------------------------------------------------------------------


def test_step4_is_named_and_reachable(template_text: str) -> None:
    """Step 4 carries its name and lives between step 3 (AGENT-PROMPT) and step 5 (TASK-LIST)."""
    step3 = template_text.index("3. AGENT-PROMPT CAPTURE")
    step4 = template_text.index("4. SUBAGENT FINDINGS CURATION")
    step5 = template_text.index("5. TASK-LIST MIRROR")
    assert step3 < step4 < step5, (
        f"step 4 out of order: step3={step3}, step4={step4}, step5={step5}"
    )


# ---------------------------------------------------------------------------
# Discriminator: each of the five judgement arms names the right routing tool
# ---------------------------------------------------------------------------


# Each entry pairs a judgement-arm label (the discriminator) with the tool the
# bullet must route to. The expected destinations are exactly the five arms the
# prompt lists under "JUDGE each bullet ... Exactly one of:".
DISCRIMINATOR_ARMS = [
    ("durable decision", "adr_add"),
    ("repo structure/convention", "wiki_add"),
    ("reusable dispatch prompt", "agent_prompt_save"),
    ("useful working fact", "memorize"),
    ("noise/status/one-off/dup", "DISCARD"),
]


@pytest.mark.parametrize("arm_label,expected_tool", DISCRIMINATOR_ARMS)
def test_step4_judgement_arm_names_routing_tool(
    template_text: str, arm_label: str, expected_tool: str
) -> None:
    """Each judgement arm names the tool (or DISCARD) the bullet routes to.

    This is the discriminator the prompt already exposes: a bullet classified
    under <arm_label> MUST land at <expected_tool>. If a future edit renames
    an arm without rewiring its tool, or replaces a tool with prose, this test
    breaks loudly — which is the failure mode ADR-0434 lived through.
    """
    step4 = _step4_slice(template_text)
    normalized = " ".join(step4.split())

    # The arm label appears verbatim in the step (it's the discriminator the
    # model is meant to apply).
    assert arm_label in normalized, (
        f"step 4 is missing the arm label {arm_label!r}; the discriminator "
        f"the prompt exposes lists these five arms exactly."
    )
    # The arm routes to a specific tool. For the four write-arms we require the
    # tool name to appear inside the same step body — that's the only place a
    # reader can locate the routing. For DISCARD the discriminator is "do
    # nothing", so we instead require the DISCARD marker itself to appear.
    if expected_tool == "DISCARD":
        assert "DISCARD" in normalized, (
            "step 4's discard arm must surface the DISCARD marker so the "
            "model can spot it; prose-only 'do nothing' phrasing regresses."
        )
    else:
        # Pin on the bare tool name. The prompt's arm rows read e.g.
        # `durable decision → adr_add (step 1 rules)` or
        # `repo structure/convention → wiki_add / update owning page (step 2)`,
        # so a call-shape pin (`wiki_add(`) would over-specify whitespace
        # around the trailing parenthesis and trip on legitimate prose
        # re-flows. The discriminator is "this arm routes to <tool>"; the
        # routing shows up as the tool's name on the same line.
        assert expected_tool in normalized, (
            f"arm {arm_label!r} should route to {expected_tool} but the "
            f"step 4 body does not name that tool"
        )


# ---------------------------------------------------------------------------
# Rewrite rule for the memorize arm: bullets must NEVER be stored verbatim
# ---------------------------------------------------------------------------


def test_memorize_arm_requires_rewriting_not_verbatim(template_text: str) -> None:
    """The memorize arm carries an explicit rewrite rule.

    ADR-0434 was filed by storing a subagent bullet verbatim — that is the
    failure mode the prompt explicitly forbids. Pin the rule that forbids it.
    """
    step4 = _step4_slice(template_text)
    normalized = " ".join(step4.split())

    # The memorize call itself is present (the prompt carries the full call
    # shape `memorize(content=...)` so we pin on that exact phrase).
    assert "memorize(content=" in normalized
    # The rewrite instruction is present.
    assert "REWRITTEN in your words" in normalized
    # The verbatim-storage prohibition is present (negated form is fine).
    assert "never store the raw bullet verbatim" in normalized


# ---------------------------------------------------------------------------
# Step 4's read-surface CLI is named (so the discriminator is reachable at all)
# ---------------------------------------------------------------------------


def test_step4_names_its_read_surface(template_text: str) -> None:
    """The step 4 CLI surface (`yadgar pending-findings`) is named in step 4."""
    step4 = _step4_slice(template_text)
    normalized = " ".join(step4.split())
    assert "yadgar pending-findings" in normalized, (
        "step 4 must name the CLI it routes through; the discriminator is "
        "unreachable if the read-surface call shape is missing"
    )


# ---------------------------------------------------------------------------
# Pin: the prompt does NOT carry a machine-checkable classifier today
# ---------------------------------------------------------------------------


def test_step4_has_no_executable_classifier(template_text: str) -> None:
    """Pin the defect: step 4 lists the five arms but exposes no executable rule
    that maps a bullet to an arm. The only machine-checkable artefact is the
    per-arm tool routing pinned by the parametrize above.

    If a future car introduces a classifier (a Python-style decision function
    or a regex), this test breaks loudly and signals that the discriminator
    has been added. For now, the absence IS the bug being pinned.
    """
    step4 = _step4_slice(template_text)
    normalized = " ".join(step4.split())
    # No Python-style classifier function definition in the prompt body.
    assert "def classify" not in normalized
    assert "def categorise" not in normalized and "def categorize" not in normalized
    # The five arms are listed as a bullet enumeration under "Exactly one of:".
    assert "Exactly one of:" in normalized
