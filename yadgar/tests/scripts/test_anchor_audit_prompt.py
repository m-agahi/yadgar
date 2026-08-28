"""Bug-bag-2 train 2026-08-23, C5 task 206 — anchor-audit prompt tests.

Pins the prompt's protocol against the real ``audit_anchors`` MCP tool:
the recall(_audit_anchors sentinel anchor hygiene, ...) Step 1 call that
served as a stand-in for a tool that did not yet exist is gone; the prompt
now references ``audit_anchors(directory, dry_run=True, *, project=...)``
and the new return-shape fields (``scanned``, ``actions[].id``,
``actions[].type``, ``actions[].tags``, ``actions[].rationale``); Step 5
collapses the per-id de_anchor loop into a single apply call.

Pure string-content tests on the prompt file; no DB / fixture required.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROMPT = (
    Path(__file__).resolve().parents[3]
    / "yadgar"
    / "core"
    / "hooks"
    / "templates"
    / "anchor_audit_prompt.md"
)
_CONTENT = _PROMPT.read_text(encoding="utf-8")


def _step_block(n: int) -> str:
    """Return the text of numbered step ``n`` only, up to step ``n + 1``.

    Whole-file ``in _CONTENT`` assertions cannot fail for any token the prompt
    mentions anywhere — "scanned" appears in Step 1's return-shape listing, so
    a Step-2 gate that never reads it still satisfies a file-wide check. Every
    positional claim in this module is asserted against the block that is
    supposed to carry it.
    """
    start = re.search(rf"^{n}\. ", _CONTENT, flags=re.MULTILINE)
    assert start is not None, f"step {n} heading not found in prompt"
    end = re.search(rf"^{n + 1}\. ", _CONTENT, flags=re.MULTILINE)
    return _CONTENT[start.start() : (end.start() if end else len(_CONTENT))]


class TestAnchorAuditPromptNoLongerCallsRecall:
    def test_does_not_mention_recall_with_audit_anchors_sentinel(self):
        # The old Step 1 was a hack: it called recall() with a literal sentinel
        # phrase so the keyword search would surface audit_anchors memory rows.
        # The prompt must not regress to that shape.
        assert "recall(_audit_anchors sentinel anchor hygiene" not in _CONTENT
        assert 'recall("_audit_anchors sentinel anchor hygiene' not in _CONTENT

    def test_does_not_mention_max_results_25_with_sentinel(self):
        # The old recall call had max_results=25 typed in by hand. The new
        # protocol calls audit_anchors which has no such knob.
        assert "max_results=25" not in _CONTENT


class TestAnchorAuditPromptCallsAuditAnchors:
    def test_step_1_references_audit_anchors_dry_run_true(self):
        assert 'audit_anchors("{directory}", dry_run=True' in _CONTENT

    def test_step_1_references_project_keyword(self):
        # The new tool signature requires project= as a keyword (C5 / C7); the
        # prompt's Step 1 must carry the same.
        assert 'project="{project}"' in _CONTENT

    def test_step_5_uses_dry_run_false_apply_call(self):
        assert 'audit_anchors("{directory}", dry_run=False' in _CONTENT


class TestAnchorAuditPromptReferencesNewFields:
    """Return-shape fields are pinned INSIDE Step 1, where the call is made.

    A file-wide ``in _CONTENT`` cannot fail once the token appears anywhere,
    so these assert against ``_step_block(1)``.
    """

    def test_step_1_lists_scanned(self):
        assert "scanned" in _step_block(1)

    def test_step_1_lists_action_id_field(self):
        # Field name must match the audit_anchors return shape: actions[i].id
        assert '"id"' in _step_block(1)

    def test_step_1_lists_action_type_field(self):
        assert '"type"' in _step_block(1)

    def test_step_1_lists_action_tags_field(self):
        assert '"tags"' in _step_block(1)

    def test_step_1_lists_action_rationale_field(self):
        assert '"rationale"' in _step_block(1)


class TestAnchorAuditPromptStepTwoGateReadsScanned:
    """Task 206's second half: the empty gate must not conflate two states.

    ``scanned == 0`` (this project has NO anchors at all) and ``scanned > 0``
    with an empty action list (every anchor is healthy) are different facts and
    read back to the user differently. Before this test the Step-2 gate never
    mentioned ``scanned``, so both collapsed into a silent STOP and the user
    could not tell "nothing to audit" from "audit ran, all clean".
    """

    def test_step_2_gate_reads_scanned(self):
        assert "scanned" in _step_block(2)

    def test_step_2_branches_on_zero_scanned(self):
        block = _step_block(2)
        assert "scanned == 0" in block

    def test_step_2_branches_on_scanned_with_no_actions(self):
        block = _step_block(2)
        assert "scanned > 0" in block

    def test_step_2_keeps_prose_only_conjunct(self):
        # The prose-only-archive risk census is part of the gate: an empty
        # action list with a non-zero prose-only count is NOT the healthy
        # state and must not silence the pass.
        assert "anchored_by_prose_only" in _step_block(2)


class TestAnchorAuditPromptCallSyntaxIsValid:
    def test_no_definition_site_star_marker_in_a_call(self):
        # ``audit_anchors("{directory}", dry_run=True, *, project="{project}")``
        # is not valid Python: the bare ``*`` is a DEFINITION-site keyword-only
        # marker, illegal in a call. An LLM reads this prompt literally and
        # will reproduce whatever it is shown.
        assert ", *, project=" not in _CONTENT

    def test_audit_anchors_calls_still_pass_project_keyword(self):
        assert 'project="{project}"' in _CONTENT


class TestAnchorAuditPromptTruncationAndRerunNote:
    def test_prompt_mentions_truncated_flag(self):
        # audit_anchors returns _truncated=True when the cap is hit; the prompt
        # must instruct the agent to re-run after apply.
        assert "_truncated" in _CONTENT

    def test_prompt_mentions_re_run_pattern(self):
        # The truncation flag is informational — the agent needs a re-run
        # instruction, otherwise stale candidates stay unapplied forever.
        lowered = _CONTENT.lower()
        assert "re-run" in lowered or "rerun" in lowered or "re run" in lowered


class TestAnchorAuditPromptDeAnchorStillReferenced:
    def test_prompt_references_de_anchor_as_lower_level_primitive(self):
        # Scope: keep de_anchor() mentioned as the lower-level primitive the
        # dry-run-then-apply flow stands on; an audit reader must not conclude
        # the primitive was deleted.
        assert "de_anchor" in _CONTENT


class TestAnchorAuditPromptSurfacesCoverage:
    """Task 391 — the human-facing path must not restate the under-count.

    ``audit_anchors`` now reports ``coverage``; if the protocol renders only
    ``scanned`` and ``actions``, the 95-vs-102 shortfall stays silent on the
    automated path and the fix never reaches a reader.
    """

    def test_step_1_return_shape_lists_coverage(self):
        assert '"coverage"' in _step_block(1)

    def test_step_2_block_instructs_reading_unscanned(self):
        block = _step_block(2)
        assert "coverage.unscanned" in block
        assert "protected_total" in block

    def test_step_2_block_names_the_causes(self):
        block = _step_block(2)
        assert "no_anchor_tag" in block
        assert "directory_context_mismatch" in block

    def test_step_2_block_handles_a_failed_coverage_query(self):
        # An `error` key must not be read as "no unscanned rows".
        assert "error" in _step_block(2)

    def test_unscanned_rows_are_not_audit_candidates(self):
        # Coverage is a REPORT; the protocol must not invite action on rows
        # the audit proposed nothing for.
        assert "Do NOT act on unscanned rows" in _step_block(2)

    def test_wrap_up_repeats_the_coverage_number(self):
        assert "coverage.unscanned" in _step_block(7)


def _identity_recovery_header(content: str) -> str:
    """Return the header comment block only (everything before the closing --> )."""
    marker = "-->"
    assert marker in content, "template lost its header comment"
    return content.split(marker, 1)[0]


def test_header_names_both_identity_failure_modes():
    """Task 423: the header used to dead-end ("say so and stop / skip").

    Two DIFFERENT failures reach the same symptom-space and need opposite
    fixes: the mint failing (no key exists) versus the registry refusing a key
    that does exist. Prose that conflates them sends the user to the wrong
    remedy, so the header must name both.
    """
    header = _identity_recovery_header(_CONTENT)
    assert "MODE 1" in header and "MODE 2" in header, (
        "the header must separate the mint failure from the registry refusal"
    )
    assert "mint failed" in header, "MODE 1 must name the mint failure"
    assert "unknown project_id" in header, "MODE 2 must name the error string that identifies it"


def test_header_instructs_asking_the_user():
    """The recovery is the USER's to make — the instance must surface it."""
    header = _identity_recovery_header(_CONTENT)
    assert "ASK" in header.upper(), "the header must instruct asking the user"
    # The ADR-0227 prohibition stays intact alongside the new instruction.
    assert "invent" in header, "the never-invent-a-key prohibition must survive"
    assert "ADR-0227" in header


def test_header_carries_the_verified_recovery_commands():
    """Commands are pinned against the real parser (yadgar/core/cli/project.py).

    ``project`` registers exactly two subcommands — ``seed`` (with ``--map``)
    and ``list``. A header naming anything else would be inventing a flag.
    """
    header = _identity_recovery_header(_CONTENT)
    assert ".yadgar/project-id" in header, "MODE 1 needs the override-file remedy"
    assert "yadgar project list" in header, "MODE 2 needs the diagnostic"
    assert "yadgar project seed" in header, "MODE 2 needs the registration path"
    # ``DEFAULT_MAP_PATH`` in yadgar/core/cli/project.py is ``Path.cwd()`` bound
    # at IMPORT time, so a relative ``--map`` resolves against the shell's cwd,
    # not the project root the append instruction targeted. The header must
    # anchor both halves on {directory} or the two lines disagree.
    assert "{directory}/.yadgar/project-id-map.tsv" in header, (
        "the map path must be anchored on {directory}, not left relative"
    )
    assert "--map {directory}/.yadgar/project-id-map.tsv" in header, (
        "the seed command must carry the same anchored path as the append step"
    )


def test_header_says_mode_2_surfaces_mid_protocol():
    """MODE 2 is invisible when the header is read.

    The key and the ``current_project`` block are both present, so the
    discriminator (``unknown project_id``) can only appear once a scoped call
    has been made — i.e. after the instance has scrolled past this header. Left
    unsaid, the refusal reads as a transient tool error and the instance
    retries or gives up instead of recognising the mode.
    """
    header = _identity_recovery_header(_CONTENT)
    low = header.lower()
    assert "surfaces on the first scoped call" in low, (
        "the header must say WHEN the MODE 2 signal appears"
    )
    assert "transient" in low, (
        "the header must say the refusal is not a transient error to retry past"
    )


def test_header_guards_against_an_unreachable_backend():
    """``yadgar project list`` exits 1 with an ``ERROR:`` line when the backend
    is down (cmd_project_list). Reading that as "not registered" would send the
    user to seed a registry that cannot be reached, so the header must call the
    case undetermined rather than let it collapse into MODE 2."""
    header = _identity_recovery_header(_CONTENT)
    assert "ERROR:" in header, "the header must name the failure marker to look for"
    assert "UNDETERMINED" in header.upper(), (
        "an unreachable backend must be reported as undetermined, not as MODE 2"
    )
