"""Temporal expression parsing for query analysis."""

import re

from yadgar.observability.observe import observe

# Compiled patterns for temporal extraction (module-level for performance)
_MONTH_NAMES = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
_MONTH_PATTERN = "|".join(_MONTH_NAMES)
_DATE_MONTH_YEAR_RE = re.compile(
    rf"\b(\d{{1,2}}\s+(?:{_MONTH_PATTERN})\s+\d{{4}})\b", re.IGNORECASE
)
_MONTH_YEAR_RE = re.compile(rf"\b((?:{_MONTH_PATTERN})\s+\d{{4}})\b", re.IGNORECASE)
_MONTH_ONLY_RE = re.compile(rf"\b({_MONTH_PATTERN})\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_RELATIVE_RE = re.compile(
    r"\b(yesterday|today|last\s+week|last\s+month|"
    r"\d+\s+days?\s+ago|\d+\s+weeks?\s+ago|"
    r"recently|earlier|previously|before|"
    r"the\s+week\s+before|the\s+day\s+before|prior\s+to)\b",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(
    r"\b(session[\s_]\d+|(?:first|second|third|last)\s+session|conversation\s+\d+)\b",
    re.IGNORECASE,
)
_ORDINAL_TEMPORAL_RE = re.compile(
    rf"\b((?:before|after|during|in)\s+(?:{_MONTH_PATTERN}))\b",
    re.IGNORECASE,
)


@observe(tier="hot", name="retrieval.temporal.parse_expression")
def parse_temporal_expression(query: str) -> dict:
    """Extract date/time information from natural language queries.

    Returns a dict with:
      has_temporal, expressions, date_hints, month_hints,
      relative_hints, session_hints
    """
    result: dict = {
        "has_temporal": False,
        "expressions": [],
        "date_hints": [],
        "month_hints": [],
        "relative_hints": [],
        "session_hints": [],
    }

    if not query or not isinstance(query, str):
        return result

    seen_expressions: set[str] = set()

    def _add_expr(value: str) -> None:
        if value not in seen_expressions:
            seen_expressions.add(value)
            result["expressions"].append(value)

    # 1. "Day Month Year" patterns (e.g. "25 May 2023")
    for m in _DATE_MONTH_YEAR_RE.finditer(query):
        hint = m.group(1)
        result["date_hints"].append(hint)
        _add_expr(hint)

    # 2. "Month Year" patterns (e.g. "May 2023")
    for m in _MONTH_YEAR_RE.finditer(query):
        hint = m.group(1)
        # Avoid duplicating if already captured as part of Day Month Year
        if hint not in result["date_hints"]:
            result["date_hints"].append(hint)
        _add_expr(hint)

    # 3. ISO dates (e.g. "2023-05-25")
    for m in _ISO_DATE_RE.finditer(query):
        hint = m.group(1)
        result["date_hints"].append(hint)
        _add_expr(hint)

    # 4. Month names
    for m in _MONTH_ONLY_RE.finditer(query):
        month = m.group(1).lower()
        if month not in result["month_hints"]:
            result["month_hints"].append(month)

    # 5. Relative expressions
    for m in _RELATIVE_RE.finditer(query):
        hint = m.group(1).lower()
        if hint not in result["relative_hints"]:
            result["relative_hints"].append(hint)
        _add_expr(m.group(1))

    # 6. Session references
    for m in _SESSION_RE.finditer(query):
        hint = m.group(1)
        if hint not in result["session_hints"]:
            result["session_hints"].append(hint)
        _add_expr(hint)

    # 7. Ordinal temporal ("before May", "after June", "during July", "in May")
    for m in _ORDINAL_TEMPORAL_RE.finditer(query):
        _add_expr(m.group(1))

    result["has_temporal"] = bool(result["expressions"])
    return result
