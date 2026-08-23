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
    def test_step_1_lists_scanned(self):
        assert "scanned" in _CONTENT

    def test_step_1_lists_action_id_field(self):
        # Field name must match the audit_anchors return shape: actions[i].id
        assert '"id"' in _CONTENT

    def test_step_1_lists_action_type_field(self):
        assert '"type"' in _CONTENT

    def test_step_1_lists_action_tags_field(self):
        assert '"tags"' in _CONTENT

    def test_step_1_lists_action_rationale_field(self):
        assert '"rationale"' in _CONTENT


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
