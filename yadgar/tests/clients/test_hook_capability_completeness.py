"""Car 0 — hook_capability matrix completeness (plan §2, ADR-0143 gate).

Every client that carries a ``hooks_kind`` must carry a ``HookCapability`` row
(the structural encoding of which of the 5 core hooks it supports + the Stop
mechanism). The one client with ``hooks_kind is None`` (Gemini, advisory-only)
must have ``hook_capability is None`` — no faked hook surface.

The rows mirror the plan §2 matrix; this test pins the load-bearing facts
(SessionStart/PreCompact presence, Stop mechanism) so a later car's re-verify
edit is a visible diff against a pinned baseline (ADR-0143: fast-moving tools).
"""

from __future__ import annotations

import pytest

from yadgar.core.install.clients import registry as reg
from yadgar.core.install.clients.descriptor import HookCapability, StopMechanism

# Every client with a hook surface (hooks_kind set).
_HOOKED = (
    "claude-code",
    "codex",
    "cursor",
    "cline",
    "windsurf",
    "kiro",
    "amp",
    "opencode",
)


def test_gemini_has_no_hook_capability():
    """Gemini is advisory-only (hooks_kind None) → hook_capability None (not faked)."""
    g = reg.CLIENT_REGISTRY["gemini"]
    assert g.hooks_kind is None
    assert g.hook_capability is None


@pytest.mark.parametrize("name", _HOOKED)
def test_hooked_client_has_capability_row(name):
    desc = reg.CLIENT_REGISTRY[name]
    assert desc.hooks_kind is not None
    assert isinstance(desc.hook_capability, HookCapability)
    assert isinstance(desc.hook_capability.stop, StopMechanism)
    # verified_date is stamped (ADR-0143 snapshot discipline).
    assert desc.hook_capability.verified_date


def test_capability_iff_hooks_kind():
    """hook_capability present exactly when hooks_kind present — no orphans."""
    for desc in reg.CLIENT_REGISTRY.values():
        assert (desc.hook_capability is not None) == (desc.hooks_kind is not None)


# ── Load-bearing matrix facts (plan §2 / ADR-0145 corrections) ────────────────


def test_claude_code_full_five_with_block_stop():
    cap = reg.CLIENT_REGISTRY["claude-code"].hook_capability
    assert cap.session_start and cap.user_prompt_submit
    assert cap.post_tool_use and cap.pre_compact
    assert cap.stop is StopMechanism.BLOCK


def test_codex_stop_does_not_block():
    # #59: Stop does NOT block → degrade to opportunistic checkpoint (R5).
    assert reg.CLIENT_REGISTRY["codex"].hook_capability.stop is StopMechanism.NONE


def test_windsurf_no_session_start_transcript_stop():
    cap = reg.CLIENT_REGISTRY["windsurf"].hook_capability
    assert cap.session_start is False  # inject rides pre_user_prompt first-fire
    assert cap.pre_compact is False
    assert cap.stop is StopMechanism.TRANSCRIPT


def test_kiro_has_session_start():
    # ADR-0145 corrects the survey's "no SessionStart".
    assert reg.CLIENT_REGISTRY["kiro"].hook_capability.session_start is True


def test_amp_no_user_prompt_no_precompact():
    cap = reg.CLIENT_REGISTRY["amp"].hook_capability
    assert cap.user_prompt_submit is False
    assert cap.pre_compact is False
    assert cap.stop is StopMechanism.TRANSCRIPT


def test_pre_compact_absent_for_the_four_documented_clients():
    # PreCompact is the most-commonly-absent hook (plan §2 "reading the matrix").
    for name in ("cline", "windsurf", "kiro", "amp"):
        assert reg.CLIENT_REGISTRY[name].hook_capability.pre_compact is False
