"""Tests for ledger task #342 — ``_drain_local.active`` is a defensive flag.

Background (task #342):
    ``yadgar/backend/queue_drainer/_locals.py`` previously claimed the
    ``_drain_local.active`` thread-local was consulted by write tools to
    skip re-enqueueing during crash-recovery replay. The claim was stale:
    no production write path inside the replay pipeline re-enqueues today,
    so the flag is defensive infrastructure with zero current readers.

This file pins the actual contract:

  - ``is_draining()`` returns False by default (no reader thread).
  - Setting ``_drain_local.active = True`` makes ``is_draining()`` return True
    in the same thread (the only reader contract that exists today).
  - The flag is per-thread (``threading.local()``); setting it on one thread
    does NOT leak to another.
  - The module docstring accurately describes the defensive status, not the
    stale "write tools check this to skip re-enqueueing" claim.

The fourth assertion is the regression guard: if someone reverts the
docstring to the pre-#342 wording, this test fails.
"""

from __future__ import annotations

import threading

from yadgar.backend.queue_drainer import _locals as _locals_mod
from yadgar.backend.queue_drainer import is_draining


def test_flag_default_false() -> None:
    """Calling ``is_draining()`` without any set returns False."""
    # Ensure a clean thread-local state for this test thread.
    _locals_mod._drain_local.active = False
    assert is_draining() is False


def test_flag_true_during_apply() -> None:
    """Mimic ``_apply()``'s try/finally: True inside, False after."""
    _locals_mod._drain_local.active = False
    try:
        _locals_mod._drain_local.active = True
        assert is_draining() is True
    finally:
        _locals_mod._drain_local.active = False
    assert is_draining() is False


def test_flag_does_not_persist_across_threads() -> None:
    """``threading.local()`` contract: set in one thread, unread in another."""
    _locals_mod._drain_local.active = False
    other_thread_saw_value: list[bool] = []

    def setter_thread() -> None:
        _locals_mod._drain_local.active = True
        # Confirm the setter thread itself sees True.
        other_thread_saw_value.append(is_draining())

    t = threading.Thread(target=setter_thread)
    t.start()
    t.join(timeout=5.0)
    assert t.is_alive() is False, "setter thread hung"
    assert other_thread_saw_value == [True], "setter thread did not see its own True"

    # Crucial assertion: the *current* (main) thread still sees False,
    # because threading.local() isolates per-thread.
    assert is_draining() is False, (
        "threading.local() leaked across threads — the drainer contract "
        "would be broken because two drainers in two threads would race"
    )


def test_module_docstring_mentions_current_status() -> None:
    """Regression guard for task #342 docstring honesty.

    Pre-#342 wording claimed ``is_draining`` is consulted by write tools to
    skip re-enqueueing. Post-#342 wording must call out the DEFENSIVE status
    (no current production reader) so future readers understand the flag
    exists for correctness of any *future* re-enqueue path, not for any
    existing one.
    """
    doc = _locals_mod.__doc__ or ""
    assert "DEFENSIVE" in doc, (
        "module docstring must label the flag DEFENSIVE so future readers "
        "understand no production caller reads it today"
    )
    assert "no production" in doc.lower(), (
        "module docstring must explicitly state no production write tool re-enqueues today"
    )
    # The stale primary claim must no longer appear unqualified — historical
    # mention in a "Why this exists" paragraph is fine, but the assertion
    # below guards against the original unqualified claim being the headline.
    # Slice off the "Why this exists" paragraph (the historical section) so
    # the remaining text is the post-#342 authoritative description.
    lowered = doc.lower()
    why_marker = "# why this exists"
    idx = lowered.find(why_marker)
    if idx != -1:
        # Skip past the next blank line that ends the Why paragraph.
        para_end = lowered.find("\n\n", idx)
        if para_end == -1:
            para_end = len(lowered)
        post_why = lowered[para_end:]
    else:
        post_why = lowered
    assert "skip re-enqueueing" not in post_why, (
        "the 'skip re-enqueueing' phrase must NOT appear as the unqualified "
        "primary description of the flag's job post-#342"
    )
