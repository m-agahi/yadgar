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
    except Exception:  # noqa: BLE001 — span construction per replayed write: a degraded or swapped OTel provider raises arbitrary types from get_tracer (the I3 case documented in tracing.py), and the caller must fall through to nullcontext
        return contextlib.nullcontext()


def _set_apply_op(op: str) -> None:
    """Set the `op` attribute on the active drainer.apply span. No-op if absent."""
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        sp = _ot.get_current_span()
        if sp is not None and sp.is_recording():
            sp.set_attribute("op", op)
    except Exception:  # noqa: BLE001 — same degraded-provider surface; setting the op attribute is decoration and must never fail the replay
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
        # Early-return dispatch (not an elif chain): the I13 nesting metric
        # counts each elif as a nested If, so a growing op set would trip the
        # HARD cap even though every branch is flat.
        op = record["op"]
        p = record["payload"]

        if op == "memorize":
            from yadgar.backend.write_exec import run_memorize_replay

            run_memorize_replay(
                content=p["content"],
                # C10 (f): ``context`` is optional now — a payload without one is
                # valid, not malformed, so this must not KeyError.
                context=p.get("context"),
                tags=p.get("tags", []),
                is_protected=p.get("is_protected", False),
                provenance_agent=p.get("provenance_agent"),
                tier=p.get("tier"),
                valid_until=p.get("valid_until"),
                reason=p.get("reason", ""),  # R3: semantic_immortal tier requires reason
                # C4b (0047 PR#40 §5): FORWARD the enqueue-time stamp; never
                # recompute. ``memorize`` is the highest-volume write path in
                # the system, and until this car it reached the write path
                # unstamped on every call that omitted ``project=`` — leaving
                # the storage chokepoint to derive an identity inside a
                # container that provably cannot (ADR-0227 §1.1). No default is
                # substituted for a payload that arrives unstamped: the key
                # stays None, the chokepoint sees ``caller_value=None``, and C5
                # turns that into a raise.
                project_id=p.get("project_id"),
                # ttl_days not needed: valid_until already computed before enqueue
            )
            return
        if op == "anchor":
            from yadgar.backend.write_exec import run_anchor_replay

            run_anchor_replay(
                content=p["content"],
                context=p["context"],
                reason=p.get("reason", ""),
                tier=p.get("tier"),
                valid_until=p.get("valid_until"),
                # C4b (0047 PR#40 §5): same forward-don't-derive contract as
                # ``memorize`` above — an anchor is a memory row like any other.
                project_id=p.get("project_id"),
                # ttl_days not needed: valid_until already computed before enqueue
            )
            return
        if op == "checkpoint":
            from yadgar.backend.write_exec import run_checkpoint_replay

            # C11: the whole payload, exactly like ``run_action_log_replay``
            # below. The old per-key kwarg list is what silently dropped
            # ``project_id`` — misc.py::checkpoint has always put it on the
            # payload and the drainer validated it, but this call did not
            # forward it and the table had no column for it anyway.
            run_checkpoint_replay(p)
            return
        if op == "action_log":
            from yadgar.backend.write_exec import run_action_log_replay

            # T2 Car E1: action-log rides the queue seam — core (auto-capture
            # flush, team-inbox ingest, capture CLI) enqueues; the write runs here.
            run_action_log_replay(p)
            return
        if op == "wiki_add":
            from yadgar.backend.write_exec import run_wiki_add_replay

            # §26 Option Z — fill fields the skill cannot know before calling wiki_add
            p = self._fill_wiki_add_defaults(dict(p))
            # directory_context falls back to the enqueue-time directory
            p["directory_context"] = p.get("directory_context") or p.get("directory")
            # C4 (0047 PR#40 §5): the enqueue-time stamp is FORWARDED, not
            # recomputed. Car L called the write chokepoint here with
            # ``caller_value=p.get("project_id")``, which meant an unstamped
            # payload fell through to the container-side classifier — the one
            # thing this container provably cannot do (no git binary, no host
            # project mounts; ADR-0227 §1.1). C3 made every core caller stamp
            # the value at enqueue time, and ``_validate_project_id`` now DLQs
            # any job that arrives without one, so reaching a derivation from
            # this line is no longer a fallback but a bug.
            #
            # No default is substituted for a payload that somehow arrives
            # unstamped: the key is left absent, the storage chokepoint sees
            # ``caller_value=None``, and C5 turns that into a raise.
            run_wiki_add_replay(p)
            return
        logger.debug("Unknown queue op %r — skipping", op)
