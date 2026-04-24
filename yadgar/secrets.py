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
