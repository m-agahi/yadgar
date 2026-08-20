"""DLQ mixin for QueueDrainer — move-to-DLQ, validation, defaults."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import _NON_IDENTIFYING_PROJECT_IDS

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JSON serializer for objects not serializable by default json."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


class _DLQMixin:
    """Dead-letter queue operations for QueueDrainer."""

    @observe(tier="stage", metric="drainer.dlq.move_to_dlq")
    def _move_to_dlq(
        self,
        path: Path,
        attempt,
        op_type: str,
        failure_reason: str = "permanent_error",
        failure_metadata: dict | None = None,
    ) -> None:
        """Atomically move a queue file to DLQ, write a .error.json sidecar, append events log.

        v5.42.0: failure_reason taxonomy (permanent_error | duplicate_detected | policy_rejected).
        failure_metadata carries structured context for rejection entries.
        """
        now_ts = datetime.now(UTC).isoformat()
        first_failed = (
            datetime.fromtimestamp(attempt.first_failed_at, UTC).isoformat()
            if attempt.first_failed_at
            else now_ts
        )
        meta = {
            "op_type": op_type,
            "first_failed_at": first_failed,
            "last_failed_at": now_ts,
            "attempts": attempt.count,
            "classification": attempt.classification,
            "last_error": attempt.last_error,
            "moved_to_dlq_at": now_ts,
            "failure_reason": failure_reason,
        }
        if failure_metadata:
            meta["failure_metadata"] = failure_metadata

        dlq_path = self._queue.dlq_dir / path.name
        try:
            path.rename(dlq_path)
        except OSError as exc:
            logger.error("Failed to move %s to DLQ: %s", path.name, exc)
            return

        # Write error sidecar atomically
        sidecar = self._queue.dlq_dir / (path.name + ".error.json")
        tmp = self._queue.dlq_dir / (path.name + ".error.json.tmp")
        try:
            tmp.write_text(
                json.dumps(meta, ensure_ascii=False, default=_json_default), encoding="utf-8"
            )
            tmp.rename(sidecar)
        except OSError as exc:
            logger.warning("Failed to write DLQ sidecar for %s: %s", path.name, exc)

        # Append to audit events log (never pruned by cleanup_dlq)
        events_log = self._queue.dlq_dir / ".events.log"
        event = {"event": "dlq_move", "ts": now_ts, "file": path.name, **meta}
        try:
            with open(events_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=_json_default) + "\n")
        except OSError as exc:
            logger.warning("Failed to append DLQ event log: %s", exc)

        logger.error(
            "MOVED TO DLQ: %s (%d attempts, %s) — %s",
            path.name,
            attempt.count,
            attempt.classification,
            attempt.last_error[:200],
        )

    # ── §26 Option Z ─────────────────────────────────────────────────────────

    _MIN_WIKI_SCHEMA_VERSION: int = 2
    _WIKI_REQUIRED_FIELDS: tuple[str, ...] = ("slug", "title", "content", "category")

    @observe(tier="stage", metric="drainer.dlq.validate_wiki_add")
    def _validate_wiki_add(self, record: dict) -> str | None:
        """Validate a wiki_add queue record (§26 Option Z).

        Returns a rejection reason string if the record should go to DLQ,
        or None if it passes all checks.

        v5.42.5: checks directory_context — payloads missing it (and not
        _internal=True) are rejected with failure_reason=missing_directory.
        Defense-in-depth for the MCP boundary validator; also catches direct
        file_queue writes that bypass MCP.
        """
        p = record.get("payload", {})

        # 1. Schema-version gate
        schema_ver = p.get("wiki_schema_version")
        if schema_ver is None or int(schema_ver) < self._MIN_WIKI_SCHEMA_VERSION:
            return (
                f"schema_version_too_old: got {schema_ver!r}, "
                f"require >= {self._MIN_WIKI_SCHEMA_VERSION}"
            )

        # 2. Required fields
        for field in self._WIKI_REQUIRED_FIELDS:
            if not p.get(field):
                return f"missing_required_field: {field}"

        # 3. Degenerate content filter (v4.9 guard)
        try:
            from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

            if _is_degenerate_auto_abstracted(p.get("content", "")):
                return "degenerate_content"
        except Exception as _e:
            logger.debug("_validate_wiki_add: degenerate check failed: %s", _e)

        # 4. v5.42.5: directory_context required for all writes.
        # _internal=True carve-out applies here too.
        # C5 (0047 PR#40 §5): the ``YADGAR_DIRECTORY_ENFORCEMENT`` escape hatch is
        # DELETED — "relaxed enforcement" is the mode in which unscoped rows
        # entered the corpus, and ADR-0225's end condition for the knob (the
        # registry check being wired) is met by C6 in this same PR.
        if not p.get("_internal"):
            dc = p.get("directory_context") or p.get("directory")
            if not dc or not str(dc).strip():
                return "missing_directory: wiki_add payload lacks directory_context."

        # 5. C4 (0047 PR#40 §5): the enqueue-time project_id stamp is required.
        return self._validate_project_id(p, "wiki_add")

    #: project_id values treated as ABSENT rather than as an identity.
    #:
    #: ``"unresolved"`` had exactly one producer — the classifier-failure arm of
    #: ``_resolve_project_id_for_write`` — so it can only ever have meant "a
    #: derivation was attempted and failed". C5 deleted the producer; the
    #: sentinel stays listed because the DLQ also sees jobs enqueued before this
    #: car by an older client.
    #:
    #: **``"global"`` joins the set here (C4b handoff #3).** C4 deliberately left
    #: it accepted: at that point it was still a LIVE scope value that
    #: ``resolve_effective_project`` produced for every unresolvable tree, so
    #: rejecting it one car early would have DLQ'd every legitimate global-scoped
    #: write. C5 deletes the tier that produced it (§1.4: ``"global"`` is never a
    #: project_id — cross-project reach is a separate TAG), so the same edit that
    #: removes ``GLOBAL_FALLBACK`` and ``_project_id_writer``'s ``return
    #: "global"`` branch adds it to the sentinel set. Doing one without the other
    #: in either order is a live breakage.
    #: Car 5 (2026-08-20 train): this WAS a private literal
    #: ``frozenset({"", "global", "unresolved"})`` — a copy that had already
    #: drifted, omitting ``"system"`` (the pre-v5.64 mis-stamp sink) which
    #: ``_NON_IDENTIFYING_PROJECT_IDS`` carries. The comment above claiming the
    #: sentinel set "cannot drift" was describing an intent, not a mechanism,
    #: while a second copy sat two lines below it. Bound to the one frozenset,
    #: which is what makes the claim true: the create gate
    #: (``_project_param._reject_sentinel``), the restamp gate
    #: (``project_id_value_error``) and this un-bypassable drainer gate now read
    #: the same object.
    _SENTINEL_PROJECT_IDS: frozenset[str] = _NON_IDENTIFYING_PROJECT_IDS

    @observe(tier="stage", metric="drainer.dlq.validate_project_id")
    def _validate_project_id(self, payload: dict, op_type: str = "wiki_add") -> str | None:
        """Return a rejection reason when the enqueue-time project_id is missing.

        C4 (0047 PR#40 §5). The drainer runs inside the backend container,
        which has no git binary and no host project mounts, so it cannot mint
        an identity and must not invent one. C3 made the core tool stamp
        ``payload["project_id"]`` at enqueue time — in the one process that
        can see the session — so by the time a job reaches here the value is
        either present or the job is unattributable.

        The declared failure path is the **DLQ**, reusing the v5.42.0
        taxonomy (``failure_reason="missing_project_id"``) rather than
        inventing a path or falling back to a default. DLQ, not skip-and-count:
        unlike a nightly-cycle row, a queued write is the user's own content
        and is recoverable — it sits in the DLQ with an actionable hint and can
        be requeued once the caller passes ``project=``.

        The ``_internal=True`` carve-out does NOT apply. ``_internal`` is a
        server-only token set by ``_wiki_write_canonical``, whose two callers
        (``adr_add``, ``wiki_write_task_list``) run in the process that HAS a
        session — they have no more excuse for an unnamed project than any
        other tool. Exempting them would leave the canonical page types as the
        one hole through which sentinels keep entering the corpus.

        C5 (0047 PR#40 §5): ``op_type`` is a parameter rather than the hardcoded
        ``"wiki_add"`` it used to be (C4b handoff #2). The gate now runs for every
        queued op, so a message naming ``wiki_add`` would misreport a ``memorize``
        or ``anchor`` rejection to the one reader who has to act on it.
        """
        raw = payload.get("project_id")
        if isinstance(raw, str) and raw.strip() not in self._SENTINEL_PROJECT_IDS:
            return None
        return f"missing_project_id: {op_type} payload lacks a usable project_id (got {raw!r})."

    def _build_missing_project_id_metadata(self, record: dict, op_type: str) -> dict:
        """Build failure_metadata for missing_project_id DLQ entries (C4).

        C5: the hint is built from ``op_type`` rather than hardcoding
        ``wiki_add`` (C4b handoff #2) — it goes stale the moment the gate widens,
        and C5 is the car that widens it.
        """
        return {
            "field": "project_id",
            "payload_op_type": op_type,
            "hint": (
                f'Re-issue the {op_type} call with project="owner/repo" so the '
                "enqueue stamps an identity, then requeue. The drainer cannot "
                "resolve one: it runs in a container with no git and no project "
                "mounts (ADR-0227)."
            ),
        }

    @observe(tier="stage", metric="drainer.dlq.validate_directory_context")
    def _validate_directory_context(self, record: dict) -> str | None:
        """Return failure_reason string if directory_context is missing/empty, else None.

        v5.42.5: defense-in-depth for the MCP boundary validator. Called for
        wiki_add ops that bypass the MCP layer (direct file_queue writes).
        _internal=True is the carve-out for system/migration paths.
        """
        p = record.get("payload", {})
        if p.get("_internal"):
            return None
        dc = p.get("directory_context") or p.get("directory")
        if not dc or not str(dc).strip():
            return "missing_directory"
        return None

    def _build_missing_directory_metadata(self, record: dict, op_type: str) -> dict:
        """Build failure_metadata dict for missing_directory DLQ entries (v5.42.5)."""
        return {
            "field": "directory_context",
            "payload_op_type": op_type,
            "hint": (
                "Add directory_context key (absolute project path or 'global') "
                "and requeue with force=True."
            ),
        }

    @observe(tier="hot", metric="drainer.dlq.fill_wiki_add_defaults")
    def _fill_wiki_add_defaults(self, payload: dict) -> dict:
        """Fill fields that the export-yadgar skill cannot know (§26 Option Z).

        - confidence: set to 'medium' if absent.
        - _internal: strip before storage write (never persisted to DB).

        v5.42.3: _internal flag stripped here so it is never passed to wiki_add().

        ADR-0215: the branch arm (leave-as-None-if-absent) was dropped. Migration
        029 drops the branch column from wiki_page/memory entirely, so no reader
        of payload["branch"] remains — run_wiki_add_replay never looks at it.
        A payload that still carries a stale "branch" key deserialises unchanged
        (plain JSON read via .get() downstream); one missing it is unaffected too.
        """
        if not payload.get("confidence"):
            payload["confidence"] = "medium"
        payload.pop("_internal", None)  # strip before DB write — system-only runtime flag
        return payload

    # ── end §26 ──────────────────────────────────────────────────────────────

    # ── v5.41.5: similarity gate in drainer (I9 fix) ─────────────────────────

    @observe(tier="stage", metric="drainer.dlq.sim_gate_for_drainer")
    def _sim_gate_for_drainer(self, payload: dict) -> dict | None:
        """Run the v5.39 similarity gate in the drainer pre-apply stage (I9 fix).

        Called for wiki_add jobs BEFORE _apply() so the embed+KNN cost runs on
        the drainer thread, not the MCP request thread (I1 thin-request-path).

        Returns a rejection dict {stored: False, reason: "duplicate_detected", ...}
        if the gate fires in hard mode. Returns None if gate passes or is bypassed.

        Bypass conditions (I6 no-double-pay):
        - force=True in payload
        - replace_slug set in payload
        - append=True in payload
        - WIKI_SIM_GATE_ENABLED=False config
        - WIKI_SIM_MODE=soft (allows write, logs warning)
        """
        # Bypass: force, replace_slug, append
        if payload.get("force"):
            return None
        if payload.get("replace_slug") is not None:
            return None
        if payload.get("append"):
            return None

        # Car C3 (0047 §7 D21): policy dispatch — identity vs similarity. The
        # identity gate is a pass-through for deterministic-slug page types
        # (adr, task_list, agent_prompt library) where content similarity is
        # structurally near-identical by design. The slug IS the identity; a
        # re-write of the same slug is an update, not a duplicate. The
        # upsert=False slug-collision check at WikiStore.add handles real
        # collisions — the identity gate does not duplicate it.
        from yadgar._shared.wiki.policy import get_policy  # noqa: PLC0415

        if get_policy(payload.get("page_type")).gate_mode == "identity":
            return self._identity_gate_for_drainer(payload)
        return self._similarity_gate_for_drainer(payload)

    @observe(tier="stage", metric="drainer.dlq.identity_gate_for_drainer")
    def _identity_gate_for_drainer(self, payload: dict) -> dict | None:
        """D21 identity gate: slug-based identity, no content-similarity check.

        For page_types with deterministic slugs (``adr``, ``task_list``,
        ``agent_pattern``/``agent_discipline``/legacy ``agent_prompt``), a
        page's identity IS its slug — content similarity is structurally
        near-identical by design (canonical writers all generate the same
        shape). The gate is a pass-through: a re-write of the same slug is
        an UPDATE, not a duplicate. The upsert=False slug-collision case is
        already enforced at ``WikiStore.add`` → ``slug_exists``
        (``__init__.py:365-374,419``).

        Car C3 (0047 §7 D21): replaces the canonical ``force=True`` /
        ``replace_slug`` bypasses used pre-C3 by ``_canonical_adr_payload``
        and ``wiki_write_task_list``. Those bypasses are no longer required
        — the gate path is now policy-driven, and identity-gated types pass
        without a bypass flag.
        """
        return None

    @observe(tier="stage", metric="drainer.dlq.similarity_gate_for_drainer")
    def _similarity_gate_for_drainer(self, payload: dict) -> dict | None:
        """Content-similarity half of the drainer gate (Car B, #83 extraction).

        Split out of ``_sim_gate_for_drainer`` (which now owns only the bypass +
        policy dispatch) to keep cyclomatic complexity under the I13 hard cap.
        Behaviour is unchanged: config read → find_similar (dir-scoped) →
        soft/hard disposition.
        """
        try:
            from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415

            cfg = _get_settings()
            if not getattr(cfg, "WIKI_SIM_GATE_ENABLED", True):
                return None

            sim_mode = getattr(cfg, "WIKI_SIM_MODE", "hard")
            sim_threshold = getattr(cfg, "WIKI_SIM_CONTENT_THRESHOLD", 0.80)
            sim_top_k = getattr(cfg, "WIKI_SIM_TOP_K", 5)
        except Exception as exc:
            logger.debug("_similarity_gate_for_drainer: config error (non-fatal): %s", exc)
            return None

        try:
            import yadgar._shared.runtime.state as _st  # noqa: PLC0415

            if _st._wiki is None:
                return None

            title = payload.get("title", "")
            candidates = _st._wiki.find_similar_wiki_pages(
                title=title,
                content=payload.get("content", ""),
                threshold=sim_threshold,
                top_k=sim_top_k,
                exclude_slug=payload.get("slug", ""),
                directory_context=payload.get("directory_context"),
            )
            if not candidates:
                return None

            if sim_mode == "soft":
                logger.warning(
                    "_similarity_gate_for_drainer (soft): near-duplicate for '%s', "
                    "candidates=%s — allowing",
                    title,
                    [c["slug"] for c in candidates],
                )
                return None

            # hard mode: reject
            logger.info(
                "_similarity_gate_for_drainer: rejecting '%s' as duplicate (candidates=%s)",
                title,
                [c["slug"] for c in candidates],
            )
            try:
                from yadgar._shared.observability.metrics import (
                    yadgar_wiki_add_rejected_total,  # noqa: PLC0415
                )

                yadgar_wiki_add_rejected_total.labels(reason="duplicate_detected").inc()
            except Exception:
                pass
            # v5.53.1: surface the best-match slug as a consolidation suggestion.
            # Candidates are sorted desc by similarity; index 0 is the closest match.
            best_slug = candidates[0]["slug"] if candidates else None
            return {
                "stored": False,
                "reason": "duplicate_detected",
                "suggested_update_slug": best_slug,
                "candidates": candidates,
                "hint": (
                    "Near-duplicate detected. "
                    "Update the existing page instead: "
                    f"wiki_add(title=..., content=..., replace_slug={best_slug!r}). "
                    "Use force=True to bypass this gate and create a new page anyway."
                ),
            }
        except Exception as exc:
            logger.debug("_similarity_gate_for_drainer: gate error (non-fatal): %s", exc)
            return None
