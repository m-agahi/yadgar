"""Built-in secret detection for write-path protection.

These patterns fire BEFORE user rules and cannot be disabled.
Content matching any pattern is rejected with a reason and the
(truncated) matched substring — the full secret is never logged.
"""

from __future__ import annotations

import re

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
    (
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}"),
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
    (
        re.compile(r"sk-ant-[A-Za-z0-9\-_]{32,}"),
        "Anthropic API key",
    ),
    # §5 T-0019: OpenAI API key — covers both legacy sk-... and sk-proj-... format
    (
        re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{30,}"),
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
