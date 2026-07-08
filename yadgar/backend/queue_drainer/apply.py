"""Apply mixin for QueueDrainer — replay queued writes against storage."""

from __future__ import annotations

import contextlib
import logging

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


def _apply_span():
    """Child span per drained record, nested under drainer.cycle (v5.100).

    STAGE granularity: one span per queued write replayed (op set as an
    attribute inside the body), NOT per inner item. Returns an UN-entered
    context manager (mirrors _drainer_span) — the caller enters it via `with`.
    No-ops to nullcontext when OTel is absent.
    """
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        return _ot.get_tracer("yadgar.file_queue").start_as_current_span("drainer.apply")
    except Exception:
        return contextlib.nullcontext()


def _set_apply_op(op: str) -> None:
    """Set the `op` attribute on the active drainer.apply span. No-op if absent."""
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        sp = _ot.get_current_span()
        if sp is not None and sp.is_recording():
            sp.set_attribute("op", op)
    except Exception:
        pass


class _ApplyMixin:
    """Queue apply/replay operations for QueueDrainer."""

    def _apply(self, record: dict) -> None:
        """Replay a queued write by re-invoking the tool function.

        Sets _drain_local.active = True so write tools skip re-enqueueing
        during this call, preventing exponential queue growth on replay.

        v5.10.2: SecretLeakBlocked from Layer 1 storage gate propagates up
        to the drainer's _drain_once() exception handler, which classifies it
        as "permanent" and DLQs the payload so operator can inspect.
        """
        from yadgar.backend.queue_drainer._locals import _drain_local

        _drain_local.active = True
        try:
            with _apply_span():
                _set_apply_op(str(record.get("op", "unknown")))
                self._apply_inner(record)
        finally:
            _drain_local.active = False

    @observe(tier="stage", metric="drainer.apply_inner")
    def _apply_inner(self, record: dict) -> None:
        op = record["op"]
        p = record["payload"]

        if op == "memorize":
            from yadgar.backend.write_exec import run_memorize_replay

            run_memorize_replay(
                content=p["content"],
                context=p["context"],
                tags=p.get("tags", []),
                is_protected=p.get("is_protected", False),
                provenance_agent=p.get("provenance_agent"),
                tier=p.get("tier"),
                valid_until=p.get("valid_until"),
                branch=p.get("branch"),
                reason=p.get("reason", ""),  # R3: semantic_immortal tier requires reason
                # ttl_days not needed: valid_until already computed before enqueue
            )
        elif op == "anchor":
            from yadgar.backend.write_exec import run_anchor_replay

            # Branch in anchor payload (p.get("branch")) is captured at enqueue
            # time and is authoritative for the replay write.
            run_anchor_replay(
                content=p["content"],
                context=p["context"],
                reason=p.get("reason", ""),
                tier=p.get("tier"),
                valid_until=p.get("valid_until"),
                branch=p.get("branch"),
                # ttl_days not needed: valid_until already computed before enqueue
            )
        elif op == "checkpoint":
            from yadgar.backend.write_exec import run_checkpoint_replay

            run_checkpoint_replay(
                directory=p["directory"],
                current_task=p.get("current_task", ""),
                files_being_edited=p.get("files_being_edited"),
                key_decisions=p.get("key_decisions"),
                open_questions=p.get("open_questions"),
                next_steps=p.get("next_steps"),
                active_errors=p.get("active_errors"),
                custom_context=p.get("custom_context", ""),
            )
        elif op == "wiki_add":
            from yadgar.backend.write_exec import run_wiki_add_replay

            # §26 Option Z — fill fields the skill cannot know before calling wiki_add
            p = self._fill_wiki_add_defaults(dict(p))
            # directory_context falls back to the enqueue-time directory
            p["directory_context"] = p.get("directory_context") or p.get("directory")

            run_wiki_add_replay(p)
        else:
            logger.debug("Unknown queue op %r — skipping", op)
