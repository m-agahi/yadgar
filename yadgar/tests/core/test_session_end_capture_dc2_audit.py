"""DC2 audit test (task #35): session-exit hook curated-vs-raw decision.

The hook ships a curated 4KB snippet (last 5 human turns, 500B per turn).
DC2 audited two paths:

  A. KEEP curated + ANNOTATE the decision in-script (this test's target)
  B. SWITCH to RAW save with secret-scrub

This test pins path A so a future reader can detect drift. The decision lives
IN the hook script (single source of truth next to the code that implements
it) and the test checks the block's presence and content.

Why path A and not B (rationale mirrored in the comment block):

- Hook runs at exit, observational-only — cannot block on secret detection
- Transcripts contain real secrets (Bearer tokens, API keys, customer PII);
  regex scrub is a known losing game
- The transcript path is already in the sentinel; the next session's LLM can
  read the transcript directly when it still exists
- The snippet is rotation-resilience context for the synthesising LLM, not
  the primary signal
- Sentinel gets stored in memory via memorize() at SessionStart; curators
  and other readers will see the snippet contents

If a future maintainer picks path B, they MUST delete this test, replace the
decision block, and write a new test asserting the new contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).parent.parent.parent / "core" / "hooks" / "session-end-capture.py"


@pytest.fixture(scope="module")
def hook_source() -> str:
    return HOOK_SCRIPT.read_text(encoding="utf-8")


def test_dc2_decision_block_present(hook_source):
    """The decision-anchored justification block must exist in the hook source.

    Block is identified by the marker `DC2-DECISION: KEEP-CURATED` so a
    grep can find it without reading the whole file.
    """
    assert "DC2-DECISION: KEEP-CURATED" in hook_source, (
        "DC2 audit decision marker missing — the script must carry an in-source "
        "justification block pinning the curated-vs-raw decision. Re-add the "
        "block per the DC2 spec (task #35)."
    )


def test_dc2_block_documents_rejected_alternative(hook_source):
    """The block must name the rejected alternative (raw save with secret scrub).

    Drift to silence on the rejected path is a known failure mode of in-source
    justifications — a future maintainer adds RAW and quietly removes the
    reference to what was rejected. This test forces the record to stay open.
    """
    block_match = re.search(
        r"#\s*DC2-DECISION: KEEP-CURATED.*?(?=\n#\s*DC2-|\Z)",
        hook_source,
        re.DOTALL,
    )
    assert block_match, "DC2 block not found or not terminated by next DC2 marker"
    block = block_match.group(0)
    assert "rejected" in block.lower() or "alternative" in block.lower(), (
        "DC2 block must explicitly name the rejected alternative (RAW save "
        "with secret scrub) so a future maintainer can see the trade-off."
    )
    # The rejected path must be the raw path, not a vague "other".
    assert re.search(r"\braw\b", block, re.IGNORECASE), (
        "DC2 block must mention RAW as the rejected alternative specifically."
    )


def test_dc2_block_documents_revisit_trigger(hook_source):
    """A revisit trigger is required — when would this decision be reopened?

    A justification without a revisit trigger is a one-way ratchet. The hook
    must name a concrete condition that would force the audit again.
    """
    block_match = re.search(
        r"#\s*DC2-DECISION: KEEP-CURATED.*?(?=\n#\s*DC2-|\Z)",
        hook_source,
        re.DOTALL,
    )
    assert block_match
    block = block_match.group(0).lower()
    assert "revisit" in block or "trigger" in block, (
        "DC2 block must include a revisit trigger (the condition that would "
        "force re-auditing this decision)."
    )


def test_dc2_no_runtime_mode_switch(hook_source):
    """ONE decision per host — no env-var runtime switch between curated and raw.

    The DC2 spec is explicit: do not add a runtime switch keyed on env vars.
    If a maintainer later wants to toggle, they must edit this block, not
    add a new env knob. This test guards against silent env-var dual modes.
    """
    # No env knob that toggles "raw" vs "curated" mode at hook runtime.
    forbidden = re.findall(
        r"os\.environ\.get\(\s*[\"']([^\"']*RAW[^\"']*|[^\"']*SCRUB[^\"']*)[\"']",
        hook_source,
        re.IGNORECASE,
    )
    assert not forbidden, (
        f"DC2 forbids runtime mode-switch env knobs. Found: {forbidden}. "
        "If a switch is genuinely needed, edit the DC2 decision block and "
        "update the test in tandem — do not add a parallel mode."
    )


def test_dc2_caps_pinned(hook_source):
    """The 4KB / 500B / 5-turn caps from the DC2 audit are the implemented caps.

    Drift on the numeric constants is a silent spec change. The block must
    restate them so a future reader can verify the cap they implement against
    the cap the audit decided on.
    """
    # 4096 (4KB total) and 500 (per turn) must be present as the chosen caps.
    assert "4096" in hook_source, "Expected 4096-byte total cap somewhere in source"
    assert "500" in hook_source, "Expected 500-byte per-turn cap somewhere in source"
    # Both should appear in the DC2 block (not just in unrelated constants).
    block_match = re.search(
        r"#\s*DC2-DECISION: KEEP-CURATED.*?(?=\n#\s*DC2-|\Z)",
        hook_source,
        re.DOTALL,
    )
    assert block_match
    block = block_match.group(0)
    assert "4096" in block and "500" in block, (
        "DC2 block must restate the 4KB total + 500B per-turn caps so a "
        "maintainer can audit the constants against the decision."
    )
