"""DLQ mixin for QueueDrainer — move-to-DLQ, validation, defaults."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.security.enforcement import _enforcement_on, _inc_relaxed

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

    # ── v5.42.3: op types that require branch context ─────────────────────────
    _MEMORY_OP_TYPES: frozenset[str] = frozenset({"memorize", "anchor", "checkpoint"})

    @observe(tier="stage", metric="drainer.dlq.validate_wiki_add")
    def _validate_wiki_add(self, record: dict) -> str | None:
        """Validate a wiki_add queue record (§26 Option Z).

        Returns a rejection reason string if the record should go to DLQ,
        or None if it passes all checks.

        v5.42.3: adds branch check — payloads missing branch (and not _internal=True)
        are rejected with failure_reason=missing_branch. Defense-in-depth for the
        MCP boundary validator; also catches direct file_queue writes that bypass MCP.
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

        # 4. v5.42.3: branch context required for external writes.
        # _internal=True is the carve-out for system/migration paths.  # _internal-only
        if not p.get("_internal"):
            branch_val = p.get("branch")
            if not branch_val:
                if _enforcement_on("YADGAR_BRANCH_ENFORCEMENT"):
                    return (
                        "missing_branch: wiki_add payload lacks branch context. "
                        "Supply branch or branch_hint. Use _internal=True for system writes."
                    )
                logger.warning(
                    "wiki_add: branch enforcement OFF — missing branch allowed "
                    "(YADGAR_BRANCH_ENFORCEMENT=false)"
                )
                _inc_relaxed("branch")

        # 5. v5.42.5: directory_context required for all writes.
        # _internal=True carve-out applies here too.
        if not p.get("_internal"):
            dc = p.get("directory_context") or p.get("directory")
            if not dc or not str(dc).strip():
                if _enforcement_on("YADGAR_DIRECTORY_ENFORCEMENT"):
                    return "missing_directory: wiki_add payload lacks directory_context."
                logger.warning(
                    "wiki_add: directory enforcement OFF — missing directory_context allowed "
                    "(YADGAR_DIRECTORY_ENFORCEMENT=false)"
                )
                _inc_relaxed("directory")

        return None

    @observe(tier="stage", metric="drainer.dlq.validate_branch_context")
    def _validate_branch_context(self, record: dict) -> str | None:
        """Validate that a memory-op queue record carries branch context (v5.42.3).

        Symmetric with _validate_wiki_add branch check. Called for memorize,
        anchor, and checkpoint ops. Returns rejection reason string or None.

        _internal=True in payload is the approved carve-out for system paths:
        sentinel, subagent_stop hook, plan_file hook, consolidation.
        These set _internal=True when enqueuing.  # _internal-only
        """
        p = record.get("payload", {})

        if not p.get("_internal"):  # _internal-only
            branch_val = p.get("branch")
            if not branch_val:
                if _enforcement_on("YADGAR_BRANCH_ENFORCEMENT"):
                    op = record.get("op", "memory-op")
                    return (
                        f"missing_branch: {op} payload lacks branch context. "
                        "Supply branch_hint=<branch> or ensure daemon can detect git branch. "
                        "Use _internal=True for system writes."
                    )
                logger.warning(
                    "memory-op: branch enforcement OFF — missing branch allowed "
                    "(YADGAR_BRANCH_ENFORCEMENT=false)"
                )
                _inc_relaxed("branch")
        return None

    def _build_missing_branch_metadata(self, record: dict, op_type: str) -> dict:
        """Build failure_metadata dict for missing_branch DLQ entries (v5.42.3)."""
        return {
            "field": "branch",
            "payload_op_type": op_type,
            "hint": (
                "Add 'branch' key to payload with the correct branch name, "
                "then call dlq_requeue(filename, force=True) to retry."
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

        - branch: leave as None if absent (canonical slot; matches wiki_add direct path).
        - confidence: set to 'medium' if absent.
        - _internal: strip before storage write (never persisted to DB).

        v5.42.2: changed from hardcoded "master" → None to match the wiki_add direct
        handler's canonical-slot behavior. Callers that need an explicit branch must pass
        it themselves; the drainer no longer injects a default branch value.

        v5.42.3: _internal flag stripped here so it is never passed to wiki_add().
        """
        if "branch" not in payload:
            payload["branch"] = None
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

        # Car C (#83): upsert=False slug-collision check.
        # Runs before policy dispatch so it applies to both identity and similarity
        # gate modes. When an explicit slug is present and upsert=False, a collision
        # must be rejected synchronously (wait=True) or routed to DLQ (wait=False)
        # rather than being silently swallowed inside _apply() → WikiStore.add().
        # Car B (#83): type-aware gate. Resolve the policy for this page_type.
        # identity mode (e.g. repo_wiki) → skip content-similarity entirely and
        # enforce schema-validity instead (slug-uniqueness + upsert handle identity).
        page_type = payload.get("page_type")
        try:
            from yadgar._shared.wiki.policy import get_policy  # noqa: PLC0415

            _policy = get_policy(page_type)
        except Exception as exc:
            logger.debug("_sim_gate_for_drainer: policy resolve error (non-fatal): %s", exc)
            _policy = None

        if _policy is not None and _policy.gate_mode == "identity":
            return self._identity_gate_for_drainer(payload)

        # Similarity mode (default) — content-similarity gate (now dir-scoped).
        return self._similarity_gate_for_drainer(payload)

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
                branch=payload.get("branch"),
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

    @observe(tier="stage", metric="drainer.dlq.identity_gate_for_drainer")
    def _identity_gate_for_drainer(self, payload: dict) -> dict | None:
        """Identity-mode gate for structural page types (Car B, #83).

        Used when ``get_policy(page_type).gate_mode == "identity"`` (e.g.
        ``repo_wiki``). Content-similarity is the WRONG guard for structural
        pages — two projects' thin ``logging.py`` share high cosine similarity
        yet are not duplicates. Instead we enforce SCHEMA validity here; the
        slug+upsert write path handles identity (create-or-overwrite at the
        caller-supplied slug — a revision, which bypasses the similarity gate the
        same way replace_slug/append do).

        Returns a rejection dict (reason ``repo_wiki_schema_invalid``) when the
        page fails schema validation, else None (allow the write).
        """
        try:
            from yadgar._shared.wiki.repo_wiki_schema import (  # noqa: PLC0415
                validate_repo_wiki_page,
            )

            errors = validate_repo_wiki_page(
                slug=payload.get("slug"),
                source_file=payload.get("source_file"),
                hash=payload.get("hash"),
            )
        except Exception as exc:
            logger.debug("_identity_gate_for_drainer: validation error (non-fatal): %s", exc)
            return None

        if not errors:
            return None  # valid → allow; slug-uniqueness + upsert handle identity

        logger.info(
            "_identity_gate_for_drainer: rejecting repo_wiki page '%s' — schema errors: %s",
            payload.get("slug", ""),
            errors,
        )
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_wiki_add_rejected_total,  # noqa: PLC0415
            )

            yadgar_wiki_add_rejected_total.labels(reason="repo_wiki_schema_invalid").inc()
        except Exception:
            pass
        return {
            "stored": False,
            "reason": "repo_wiki_schema_invalid",
            "errors": errors,
            "hint": (
                "repo_wiki page failed schema validation: "
                + "; ".join(errors)
                + ". Fix source_file (absolute path), hash (64 hex chars), and "
                "slug ('{project}-mod-...') then retry."
            ),
        }
