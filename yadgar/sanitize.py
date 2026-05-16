"""Sanitization utilities for auto-capture log fields.

Strips control characters, ANSI escape sequences, and caps field length
to prevent prompt-injection via the action log surfaced into Claude context.
"""

from __future__ import annotations

import re

# ANSI escape sequence pattern — covers CSI, OSC, and single-char sequences
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;]*[A-Za-z]"  # CSI sequences: ESC [ ... <letter>
    r"|"
    r"\][^\x1b\x07]*(?:\x07|\x1b\\)"  # OSC sequences: ESC ] ... BEL or ESC \
    r"|"
    r"[^[]"  # single-char: ESC <anything except [>
    r")"
)

# Control characters to strip: everything in C0/C1 except TAB (\x09),
# LF (\x0a), and CR (\x0d) — those are allowed structural whitespace.
_CTRL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # C0 minus \t\n\r
    r"|[\x80-\x9f]"  # C1 control range
)

# Unicode bidirectional override characters — U+200B–U+200F (zero-width
# directional marks), U+202A–U+202E (embedding/override marks), U+2066–U+2069
# (isolate marks), U+FEFF (BOM / zero-width no-break space).  These pass
# silently through terminals and can reorder displayed text to hide injected
# content when surfaced into Claude context.
_BIDI_RE = re.compile("[​-‏‪-‮⁦-⁩﻿]")

# Default maximum field length — caps prompt-injection surface area.
_DEFAULT_MAX_LEN = 1_000


def sanitize_log_field(value: str, max_len: int = _DEFAULT_MAX_LEN) -> str:
    """Strip ANSI escapes, bidi overrides, control characters, truncate.

    Args:
        value:   Raw string from an untrusted source (hook body, env, etc.)
        max_len: Maximum allowed length; excess is truncated.

    Returns:
        Sanitized string safe for storage in the action log.
    """
    # Strip ANSI escape sequences first (may contain control chars inside)
    cleaned = _ANSI_RE.sub("", value)
    # Strip Unicode bidi override / directional-isolate characters
    cleaned = _BIDI_RE.sub("", cleaned)
    # Strip remaining control characters
    cleaned = _CTRL_RE.sub("", cleaned)
    # Truncate
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned
