"""Built-in secret detection for write-path protection.

These patterns fire BEFORE user rules and cannot be disabled.
Content matching any pattern is rejected with a reason and the
(truncated) matched substring — the full secret is never logged.

v5.10.2 changes:
  - SecretLeakBlocked exception class (storage-level gate)
  - gate_or_reject() helper for multi-field API-boundary checks
  - GitHub token threshold lowered {36,} → {20,}
  - Anthropic key threshold lowered {32,} → {20,}
  - OpenAI key threshold lowered {30,} → {20,}

v5.13.0 changes:
  - gate_or_reject() gains tags= and source= kwargs for context-awareness
  - Allowlist integration: YADGAR_SECRET_GATE_ALLOWLIST_PATH YAML bypass
    with per-tag + per-pattern entries; every hit audited to JSONL
  - Source call-site detection via inspect.stack() (only when allowlist loaded)
"""

from __future__ import annotations

import logging
import re

from yadgar.observability.observe import observe

_log = logging.getLogger(__name__)

# (compiled_pattern, human_readable_name)
# Order matters: specific patterns must appear before the generic catch-all.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "Private key",
    ),
    (
        re.compile(r"eyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+"),
        "JWT token",
    ),
    # v5.10.2: lowered {36,} → {20,} — short test/fake tokens slipped through
    (
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}"),
        "GitHub token",
    ),
    (
        re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
        "GitLab token",
    ),
    (
        re.compile(
            r"(?:mysql|postgres|mongodb|redis)://\w+:[^@\s]+@",
            re.IGNORECASE,
        ),
        "Database connection string",
    ),
    # §5 T-0019: GCP service-account JSON (contains "type": "service_account")
    (
        re.compile(r'"type"\s*:\s*"service_account"'),
        "GCP service account credential",
    ),
    # §5 T-0019: Stripe live secret key
    (
        re.compile(r"sk_live_[A-Za-z0-9]{24,}"),
        "Stripe secret key",
    ),
    # §5 T-0019: Slack tokens (xoxb-, xoxa-, xoxp-, xoxs-)
    (
        re.compile(r"xox[bpas]-[0-9A-Za-z\-]{10,}"),
        "Slack token",
    ),
    # §5 T-0019: Anthropic API key — must appear before OpenAI (more specific prefix)
    # v5.10.2: lowered {32,} → {20,}
    (
        re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
        "Anthropic API key",
    ),
    # §5 T-0019: OpenAI API key — covers both legacy sk-... and sk-proj-... format
    # v5.10.2: lowered {30,} → {20,}
    (
        re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        "OpenAI API key",
    ),
    # §5 T-0019: AWS secret access key (40 chars base64-like; broad, comes after specifics)
    (
        re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
        "AWS secret key",
    ),
    # Generic catch-all — must be last so specific patterns fire first.
    (
        re.compile(
            r"(?:api[_-]?key|token|secret|password|passwd|credentials?)"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9+/=_-]{20,}",
            re.IGNORECASE,
        ),
        "Credential pattern",
    ),
]

_MATCH_PREVIEW_LEN = 20


class SecretLeakBlocked(Exception):
    """Raised by the storage-level gate when content contains a detected secret.

    This is the Layer 1 (storage-level) exception.  It should never reach
    the caller under normal operation because the Layer 2 (API-boundary)
    gate_or_reject() fires first and returns a rejection dict without
    raising.  If it does reach the caller it means someone bypassed the
    API-boundary gate — the storage layer is the last line of defence.

    Args:
        reason: human-readable pattern name (e.g. "AWS access key")
        pattern_preview: first 20 chars of the matched text
    """

    def __init__(self, reason: str, pattern_preview: str = "") -> None:
        super().__init__(f"SecretLeakBlocked: {reason} — preview: {pattern_preview!r}")
        self.secret_reason = reason
        self.pattern_preview = pattern_preview


@observe(tier="stage")
def check_secrets(content: str) -> tuple[bool, str, str]:
    """Scan content for known secret patterns.

    Args:
        content: The text to scan.

    Returns:
        (blocked, reason, pattern_matched) where:
        - blocked: True if a secret was detected
        - reason: "secret_detected: <name>" if blocked, else ""
        - pattern_matched: first 20 chars of matched text + "..." if blocked, else ""
    """
    for pattern, name in _SECRET_PATTERNS:
        m = pattern.search(content)
        if m:
            matched = m.group(0)
            preview = matched[:_MATCH_PREVIEW_LEN]
            if len(matched) > _MATCH_PREVIEW_LEN:
                preview += "..."
            return True, f"secret_detected: {name}", preview
    return False, "", ""


@observe(tier="stage")
def gate_or_reject(
    *content_fields: str | None,
    tags: list[str] | None = None,
    source: str | None = None,
) -> dict | None:
    """Scan all provided fields for secrets.  Return rejection dict or None.

    Layer 2 (API-boundary) helper.  Each write tool calls this before any
    state mutation and returns the dict directly if non-None.

    v5.13.0: accepts optional tags= and source= kwargs for allowlist context-
    awareness.  When tags= are provided and an allowlist entry matches, the
    field is allowed through and an audit entry is written.  When no allowlist
    file exists, behavior is identical to v5.10.x (default-deny).

    Increments the yadgar_writegate_outcome{outcome="rejected_secret"} metric
    on rejection.

    Args:
        *content_fields: Text fields to scan (None/"" are skipped).
        tags:            Optional list of tags at the call site.  Used for
                         allowlist matching.  When None, allowlist is skipped.
        source:          Optional call-site name.  When None, auto-detected
                         via inspect.stack() if allowlist has entries.

    Returns:
        None if all fields are clean (or allowlisted).
        {"stored": False, "reason": "secret_detected: ...", "pattern_preview": "..."}
        on first match that is not allowlisted.
    """
    from yadgar.security.allowlist import (  # noqa: PLC0415
        _detect_source,
        _write_audit,
        is_allowlisted,
    )

    # Resolve source lazily — only pay inspect.stack() cost when allowlist may apply
    _resolved_source: str | None = source

    for field in content_fields:
        if not field or not isinstance(field, str) or not field.strip():
            continue

        # --- Allowlist check (before pattern scan) ---
        allowed, entry = is_allowlisted(field, tags, _resolved_source or "")
        if allowed and entry is not None:
            if _resolved_source is None:
                _resolved_source = _detect_source()
            _write_audit(
                matched_pattern=next(
                    (p for p in entry.patterns if p.rstrip("*") in field),
                    entry.patterns[0] if entry.patterns else "",
                ),
                tags=list(tags or []),
                reason=entry.reason,
                source=_resolved_source,
                content_preview=field,
            )
            try:
                from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

                yadgar_writegate_outcome.labels(outcome="allowlisted").inc()
            except Exception:
                pass
            # Field is allowlisted — skip pattern scan for this field
            continue

        # --- Pattern scan ---
        blocked, reason, preview = check_secrets(field)
        if blocked:
            try:
                from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

                yadgar_writegate_outcome.labels(outcome="rejected_secret").inc()
            except Exception:
                pass
            return {"stored": False, "reason": reason, "pattern_preview": preview}
    return None
