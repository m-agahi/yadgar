"""Built-in secret detection for write-path protection.

These patterns fire BEFORE user rules and cannot be disabled.
Content matching any pattern is rejected with a reason and the
(truncated) matched substring — the full secret is never logged.

This is a SYNCHRONOUS, IN-PROCESS regex scan invoked from ``gate_or_reject``
(the I26 API-boundary chokepoint) and ``check_secrets`` (the storage-level
gate). No runtime network, no Go binary, no ``detect-secrets`` dependency.
It must stay within the ≤5ms p50 write-path budget (I9 / test_memorize_latency).

Ruleset provenance
------------------
Rule shapes are ported from **gitleaks** default rules
(https://github.com/gitleaks/gitleaks — ``config/gitleaks.toml``), which are
MIT-licensed. This is a hand-curated HIGH-VALUE SUBSET (OpenAI, Anthropic,
GitHub, GitLab, AWS, Google, Slack, Stripe, generic API-key) — not a
mechanical dump of the full ruleset.

Ported against gitleaks tag **v8.18.x** default rules.
Regen note: to refresh, re-read the upstream ``config/gitleaks.toml`` `[[rules]]`
blocks for each provider below and port the `regex` + `keywords` fields into
``_RULES``. Keep the pre-filter keywords lowercase; keep tight shapes ahead of
the broad catch-alls; keep Anthropic before OpenAI (more-specific prefix wins).

Per-rule keyword pre-filter
---------------------------
Each rule carries a tuple of lowercase ``keywords``. Before a rule's regex is
run, ``check_secrets`` short-circuits on a cheap ``str.lower()`` substring test:
if the rule declares keywords and NONE of them appear in the (lowercased)
content, the (expensive) regex never runs. Rules with an empty keyword tuple
are self-anchored (their prefix is already discriminating, e.g. ``AKIA``,
``ghp_``) and always run.

The keyword gate is also LOAD-BEARING for false-positive suppression: the
broad 40-char AWS-secret shape and the generic credential shape only run when
an ``aws`` / ``secret`` / ``key`` / ``token`` etc. keyword is present, so a bare
40-char hex SHA or a UUID never reaches those regexes.

Historical false-positive fixed here (v5.136 → this bump)
--------------------------------------------------------
The pre-port OpenAI rule ``sk-(?:proj-)?[A-Za-z0-9_-]{20,}`` had no word
boundary and allowed ``-``/``_`` in the body, so it fired mid-word — e.g. inside
``tasklist-mirror-2026-abcdefghij…`` the ``sk-list-mirror-2026-…`` run matched.
The ported OpenAI rule adds a leading ``\b`` and restricts the body to
alphanumerics, so hyphenated words can never reach the 20-char alnum run.
"""

from __future__ import annotations

import logging
import re

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)

# (keywords, compiled_pattern, human_readable_name)
#
# keywords: tuple of lowercase substrings. If non-empty, at least one must be
#   present in content.lower() before the regex runs (cheap pre-filter). An
#   empty tuple means the rule is self-anchored and always runs.
#
# Order matters: specific rules must appear before the generic catch-all, and
# Anthropic (sk-ant-) must precede OpenAI (sk-) — more-specific prefix wins.
_Rule = tuple[tuple[str, ...], "re.Pattern[str]", str]

_RULES: list[_Rule] = [
    # --- AWS access key (self-anchored: AKIA prefix is discriminating) ---
    ((), re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    # --- Private keys (self-anchored: PEM header is discriminating) ---
    (
        (),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
        "Private key",
    ),
    # --- JWT (self-anchored: eyJ...eyJ... triple-segment shape) ---
    (
        (),
        re.compile(r"\beyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+"),
        "JWT token",
    ),
    # --- GitHub tokens (self-anchored: ghp_/gho_/... prefixes) ---
    (
        (),
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
        "GitHub token",
    ),
    # --- GitLab PAT (self-anchored: glpat- prefix) ---
    (
        (),
        re.compile(r"\bglpat-[A-Za-z0-9_-]{20}\b"),
        "GitLab token",
    ),
    # --- Google API key (self-anchored: AIza prefix, exact 35-char body) ---
    (
        (),
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "Google API key",
    ),
    # --- Database connection string with inline credentials ---
    (
        ("://",),
        re.compile(
            r"(?:mysql|postgres|postgresql|mongodb|redis)://\w+:[^@\s]+@",
            re.IGNORECASE,
        ),
        "Database connection string",
    ),
    # --- GCP service-account JSON credential ---
    (
        ("service_account",),
        re.compile(r'"type"\s*:\s*"service_account"'),
        "GCP service account credential",
    ),
    # --- Stripe live secret key (self-anchored: sk_live_ prefix) ---
    (
        (),
        re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b"),
        "Stripe secret key",
    ),
    # --- Slack tokens (self-anchored: xox[bpas]- prefixes) ---
    (
        (),
        re.compile(r"\bxox[bpasr]-[0-9A-Za-z\-]{10,}"),
        "Slack token",
    ),
    # --- Anthropic API key — MUST precede OpenAI (more specific prefix) ---
    # Self-anchored: sk-ant- prefix. Body kept alnum + -/_ to match both the
    # real sk-ant-api03-... shape and synthetic sk-ant-<20> fixtures.
    (
        (),
        re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
        "Anthropic API key",
    ),
    # --- OpenAI API key — legacy sk-... and sk-proj-... ---
    # FP FIX: leading \b + alnum-only body (no -/_). Prevents mid-word matches
    # like the sk-list-mirror-2026-... run inside "tasklist-mirror-...".
    (
        (),
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b"),
        "OpenAI API key",
    ),
    # --- AWS secret access key (broad: 40-char base64-ish) ---
    # KEYWORD-GATED: only runs when an aws/secret/key/access token keyword is
    # present. A bare 40-char hex SHA or UUID never reaches this regex.
    (
        ("aws", "secret", "access", "key", "token", "credential"),
        re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
        "AWS secret key",
    ),
    # --- Generic credential catch-all — MUST be last ---
    # KEYWORD-GATED by construction: the regex itself requires a
    # key/token/secret/... anchor, so declaring keywords is a cheap short-circuit.
    (
        ("api", "key", "token", "secret", "password", "passwd", "credential"),
        re.compile(
            r"(?:api[_-]?key|token|secret|password|passwd|credentials?)"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9+/=_-]{20,}",
            re.IGNORECASE,
        ),
        "Credential pattern",
    ),
]

# Back-compat 2-tuple view: (compiled_pattern, name). Historical export used by
# the yadgar._shared.secrets shim and any external iterator. Derived from _RULES
# so the two never drift.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (pattern, name) for (_kw, pattern, name) in _RULES
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

    Runs each rule's cheap lowercase-keyword pre-filter before its regex: a
    rule with declared keywords is skipped entirely when none of its keywords
    appear in the content. Rules with no keywords are self-anchored and always
    run.

    Args:
        content: The text to scan.

    Returns:
        (blocked, reason, pattern_matched) where:
        - blocked: True if a secret was detected
        - reason: "secret_detected: <name>" if blocked, else ""
        - pattern_matched: first 20 chars of matched text + "..." if blocked, else ""
    """
    lowered = content.lower()
    for keywords, pattern, name in _RULES:
        if keywords and not any(kw in lowered for kw in keywords):
            continue
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
    from yadgar._shared.security.allowlist import (  # noqa: PLC0415
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
                from yadgar._shared.observability.metrics import (
                    yadgar_writegate_outcome,  # noqa: PLC0415
                )

                yadgar_writegate_outcome.labels(outcome="allowlisted").inc()
            except Exception:
                pass
            # Field is allowlisted — skip pattern scan for this field
            continue

        # --- Pattern scan ---
        blocked, reason, preview = check_secrets(field)
        if blocked:
            try:
                from yadgar._shared.observability.metrics import (
                    yadgar_writegate_outcome,  # noqa: PLC0415
                )

                yadgar_writegate_outcome.labels(outcome="rejected_secret").inc()
            except Exception:
                pass
            return {"stored": False, "reason": reason, "pattern_preview": preview}
    return None
